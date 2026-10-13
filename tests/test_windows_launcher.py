"""Native parser is executed through a real C compiler, not reimplemented in Python."""
from __future__ import annotations

import ctypes
import hashlib
import importlib.util
import shutil
import struct
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("build_windows_launcher", ROOT / "scripts/build_windows_launcher.py")
assert SPEC and SPEC.loader
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


@pytest.fixture(scope="module")
def native(tmp_path_factory):
    compiler = shutil.which("cc") or shutil.which("gcc")
    if not compiler:
        pytest.skip("Native C compiler unavailable")
    directory = tmp_path_factory.mktemp("native-launcher")
    source = directory / "verify.c"
    source.write_text('''
#include "payload.h"
#include "prerequisite_policy.h"
static size_t extracted_bytes;
static int accept(const char *name, const unsigned char *data, size_t size,
                  int directory, void *context) {
    (void)name; (void)data; (void)directory; (void)context;
    extracted_bytes += size;
    return 1;
}
int safe_path(const char *name) { return eh_valid_relative_path(name, strlen(name)); }
int read_payload(const unsigned char *data, size_t size) {
    char error[256]; extracted_bytes = 0;
    return eh_zip_walk(data, size, accept, NULL, error, sizeof(error));
}
size_t payload_bytes(void) { return extracted_bytes; }
size_t quote(const wchar_t *input, wchar_t *output, size_t capacity) {
    return eh_quote_windows_arg(input, output, capacity);
}
int webview_version(const wchar_t *version) { return eh_webview_version_suitable(version); }
int microsoft_publisher(const wchar_t *publisher) { return eh_is_microsoft_publisher(publisher); }
''', encoding="utf-8")
    library = directory / "verify.so"
    subprocess.run([compiler, "-std=c11", "-O2", "-shared", "-fPIC", "-Wall", "-Wextra", "-Werror",
                    "-I", str(ROOT / "desktop/native"), str(source), "-o", str(library)], check=True)
    result = ctypes.CDLL(str(library))
    result.safe_path.argtypes = [ctypes.c_char_p]
    result.read_payload.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    result.payload_bytes.restype = ctypes.c_size_t
    result.quote.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_size_t]
    result.quote.restype = ctypes.c_size_t
    result.webview_version.argtypes = [ctypes.c_wchar_p]
    result.microsoft_publisher.argtypes = [ctypes.c_wchar_p]
    return result


@pytest.mark.parametrize("path", [
    "../runtime/pythonw.exe", "/absolute.exe", "C:/evil.exe", "app/../../evil", "app\\evil",
    "app/file:stream", "app//file", "app/./file", "app/NUL.txt", "app/con", "app/COM1.dll",
    "app/LPT9", "app/trailing. ", "app/trailing.", "app/a?.dll", "app/CONIN$", "app/CONOUT$",
])
def test_native_rejects_unsafe_windows_names(native, path):
    assert native.safe_path(path.encode()) == 0


@pytest.mark.parametrize("path", ["app/expansion_hunter/desktop.py", "runtime/pythonw.exe",
                                        "packages/café.py", "app/config/", "packages/context.py"])
def test_native_accepts_safe_names(native, path):
    assert native.safe_path(path.encode()) == 1


def make_payload(path, compression=zipfile.ZIP_STORED, extra=None):
    with zipfile.ZipFile(path, "w", compression=compression) as archive:
        archive.writestr("runtime/pythonw.exe", b"MZ bundled runtime")
        archive.writestr("app/expansion_hunter/desktop.py", b"# app\n")
        archive.writestr("packages/café.py", b"# unicode\n")
        if extra:
            archive.writestr(extra, b"unsafe")
    return path.read_bytes()


def test_native_extracts_valid_unicode_bundle(native, tmp_path):
    payload = make_payload(tmp_path / "payload.zip")
    assert native.read_payload(payload, len(payload)) == 1
    assert native.payload_bytes() == 34


@pytest.mark.parametrize("mutation", ["crc", "truncate", "encrypted", "method", "directory_offset"])
def test_native_rejects_corrupted_or_unsupported_archive(native, tmp_path, mutation):
    payload = bytearray(make_payload(tmp_path / "payload.zip"))
    central = payload.index(b"PK\x01\x02")
    if mutation == "crc":
        payload[payload.index(b"MZ bundled runtime")] ^= 1
    elif mutation == "truncate":
        payload = payload[:-3]
    elif mutation == "encrypted":
        payload[central + 8] |= 1
    elif mutation == "method":
        payload[central + 10] = 8
    else:
        struct.pack_into("<I", payload, len(payload) - 6, 0xFFFFFFFA)
    raw = bytes(payload)
    assert native.read_payload(raw, len(raw)) == 0


def test_native_rejects_zip_slip_payload(native, tmp_path):
    payload = make_payload(tmp_path / "payload.zip", extra="../escape")
    assert native.read_payload(payload, len(payload)) == 0


@pytest.mark.parametrize(("argument", "expected"), [
    ("", '""'), ("plain", '"plain"'), ("C:\\Mijn map\\app", '"C:\\Mijn map\\app"'),
    ("C:\\Mijn map\\", '"C:\\Mijn map\\\\"'), ('with"quote', '"with\\"quote"'),
    ("café Ω", '"café Ω"'),
])
def test_real_native_commandline_quoting(native, argument, expected):
    buffer = ctypes.create_unicode_buffer(1024)
    size = native.quote(argument, buffer, len(buffer))
    assert buffer.value == expected
    assert size == len(expected) + 1


def test_builder_requires_stored_complete_bundle(tmp_path):
    payload = tmp_path / "payload.zip"
    make_payload(payload)
    assert builder.validate_payload(payload)["entries"] == 3
    make_payload(payload, zipfile.ZIP_DEFLATED)
    with pytest.raises(ValueError, match="ZIP_STORED"):
        builder.validate_payload(payload)
    make_payload(payload, extra="app/../../outside")
    with pytest.raises(ValueError, match="Unsafe"):
        builder.validate_payload(payload)


@pytest.mark.parametrize("filename", ["app/NUL.txt", "app/invalid?.dll", "app/trailing.",
                                     "app//double", "app/EXPANSION_HUNTER/DESKTOP.PY"])
def test_builder_rejects_windows_unsafe_or_case_duplicate_files(tmp_path, filename):
    payload = tmp_path / "payload.zip"
    make_payload(payload, extra=filename)
    with pytest.raises(ValueError, match="Unsafe"):
        builder.validate_payload(payload)


def test_builder_checks_actual_gui_pe_headers(tmp_path):
    artifact = tmp_path / "fake.exe"
    raw = bytearray(256)
    raw[:2] = b"MZ"
    struct.pack_into("<I", raw, 60, 64)
    raw[64:68] = b"PE\x00\x00"
    struct.pack_into("<HHIIIHH", raw, 68, 0x8664, 4, 0, 0, 0, 112, 0)
    struct.pack_into("<H", raw, 88, 0x20B)
    struct.pack_into("<H", raw, 156, 2)
    artifact.write_bytes(raw)
    assert builder.pe_metadata(artifact)["subsystem"] == "WINDOWS_GUI"
    struct.pack_into("<H", raw, 156, 3)
    artifact.write_bytes(raw)
    with pytest.raises(ValueError, match="GUI"):
        builder.pe_metadata(artifact)


def test_native_launcher_accepts_real_builder_manifest_and_licenses(native, tmp_path):
    payload_path = tmp_path / 'payload-with-metadata.zip'
    make_payload(payload_path)
    with zipfile.ZipFile(payload_path, 'a', compression=zipfile.ZIP_STORED) as archive:
        archive.writestr('licenses/CPython-LICENSE.txt', b'License retained')
        archive.writestr('payload-manifest.json', b'{"windows_execution_verified":false}')
    assert builder.validate_payload(payload_path)['entries'] == 5
    payload = payload_path.read_bytes()
    assert native.read_payload(payload, len(payload)) == 1


def test_payload_manifest_is_a_file_not_an_arbitrary_directory(tmp_path):
    payload_path = tmp_path / 'bad-metadata.zip'
    make_payload(payload_path, extra='payload-manifest.json/evil.exe')
    with pytest.raises(ValueError, match='Unsafe'):
        builder.validate_payload(payload_path)


@pytest.mark.parametrize("version", [None, "", "0.0.0.0", "85.0.616.0", "120", "120.0.0",
                                     "120.0.0.0 malformed", "120..0.0", "1234567.0.0.0"])
def test_native_webview_does_not_accept_missing_or_malformed_versions(native, version):
    assert native.webview_version(version) == 0


@pytest.mark.parametrize("version", ["86.0.616.0", "123.0.2420.97", "150.1.2.3"])
def test_native_webview_version_eligibility(native, version):
    assert native.webview_version(version) == 1


@pytest.mark.parametrize("publisher", [None, "", "Microsoft Corporation malware", "Other Vendor",
                                       " Microsoft Corporation", "Microsoft Corporation "])
def test_native_publisher_filter_is_exact(native, publisher):
    assert native.microsoft_publisher(publisher) == 0


def test_native_publisher_name_alone_does_not_replace_authenticode_verification(native):
    assert native.microsoft_publisher("Microsoft Corporation") == 1
    # This only tests the name predicate. No signature verification or Windows
    # certificate-store acceptance is claimed by the Linux-hosted fixture.


def test_manifest_prevents_implicit_installer_elevation():
    tree = ET.parse(ROOT / "desktop/native/launcher.manifest")
    namespace = {"asm": "urn:schemas-microsoft-com:asm.v3",
                 "windows": "http://schemas.microsoft.com/SMI/2016/WindowsSettings"}
    level = tree.find(".//asm:requestedExecutionLevel", namespace)
    assert level is not None and level.attrib == {"level": "asInvoker", "uiAccess": "false"}
    long_path = tree.find(".//windows:longPathAware", namespace)
    assert long_path is not None and long_path.text == "true"


def pe_resource_fixture(path: Path, payload: bytes, manifest: bytes) -> None:
    """Explicit synthetic PE container; never treated as a Windows executable."""
    resource_size = 192 + len(payload) + len(manifest)
    raw = bytearray(512 + resource_size)
    raw[:2] = b"MZ"
    struct.pack_into("<I", raw, 60, 64)
    raw[64:68] = b"PE\x00\x00"
    struct.pack_into("<HHIIIHH", raw, 68, 0x8664, 1, 0, 0, 0, 240, 0)
    struct.pack_into("<H", raw, 88, 0x20B)
    struct.pack_into("<H", raw, 156, 2)
    struct.pack_into("<II", raw, 216, 0x1000, resource_size)
    raw[328:333] = b".rsrc"
    struct.pack_into("<IIII", raw, 336, resource_size, 0x1000, resource_size, 512)
    struct.pack_into("<HH", raw, 512 + 12, 0, 2)
    struct.pack_into("<II", raw, 512 + 16, 10, 0x80000020)
    struct.pack_into("<II", raw, 512 + 24, 24, 0x80000050)
    for offset, name, target, directory in ((32, 101, 56, True), (56, 1033, 128, False),
                                           (80, 1, 104, True), (104, 1033, 144, False)):
        struct.pack_into("<HH", raw, 512 + offset + 12, 0, 1)
        struct.pack_into("<II", raw, 512 + offset + 16, name,
                         target | (0x80000000 if directory else 0))
    struct.pack_into("<II", raw, 512 + 128, 0x1000 + 192, len(payload))
    struct.pack_into("<II", raw, 512 + 144, 0x1000 + 192 + len(payload), len(manifest))
    raw[512 + 192:512 + 192 + len(payload)] = payload
    raw[512 + 192 + len(payload):] = manifest
    path.write_bytes(raw)


def test_resource_verification_checks_archive_and_manifest_bytes(tmp_path):
    artifact, manifest = tmp_path / "synthetic-container.exe", tmp_path / "manifest.xml"
    content = b"<manifest asInvoker='true' />"
    manifest.write_bytes(content)
    payload = b"deterministic resource fixture"
    pe_resource_fixture(artifact, payload, content)
    digest = hashlib.sha256(payload).hexdigest()
    result = builder.verify_embedded_resources(artifact, digest, manifest)
    assert result == {"embedded_payload_sha256": digest, "embedded_manifest_verified": True}
    with pytest.raises(ValueError, match="differs"):
        builder.verify_embedded_resources(artifact, "0" * 64, manifest)
    manifest.write_bytes(b"changed")
    with pytest.raises(ValueError, match="asInvoker"):
        builder.verify_embedded_resources(artifact, digest, manifest)


def test_resource_directory_corruption_fails_visibly(tmp_path):
    artifact, manifest = tmp_path / "synthetic-container.exe", tmp_path / "manifest.xml"
    manifest.write_bytes(b"manifest")
    pe_resource_fixture(artifact, b"payload", b"manifest")
    data = bytearray(artifact.read_bytes())
    struct.pack_into("<I", data, 512 + 20, 0xffffffff)
    artifact.write_bytes(data)
    with pytest.raises(ValueError, match="bounds"):
        builder.verify_embedded_resources(artifact, hashlib.sha256(b"payload").hexdigest(), manifest)
