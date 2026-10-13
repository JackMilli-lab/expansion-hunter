"""Build a genuine Windows GUI self-extracting launcher on Linux or Windows.

This is a developer release command; end users only run the resulting EXE.
Zig is fetched from its official HTTPS release index and archive hashes are
verified. No Windows build is claimed unless a real PE/COFF artifact is inspected.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mmap
import os
import shutil
import struct
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
ZIG_VERSION = "0.15.2"
PAYLOAD_ROOTS = {"runtime", "packages", "app", "licenses", "payload-manifest.json"}


def validate_payload(payload: Path) -> dict:
    """Reject traversal, compressed files and missing executable app content."""
    from pathlib import PurePosixPath

    if payload.stat().st_size >= 2**31:
        raise ValueError("Native installer supports ZIP_STORED payloads below 2 GiB")
    required = {"runtime/pythonw.exe", "app/expansion_hunter/desktop.py"}
    seen: set[str] = set()
    windows_names: set[str] = set()
    total = 0
    with zipfile.ZipFile(payload) as archive:
        for item in archive.infolist():
            path = PurePosixPath(item.filename)
            components = path.parts
            windows_name = item.filename.rstrip("/").casefold()
            reserved = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
            reserved.update(f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10))
            invalid = (
                not components or components[0] not in PAYLOAD_ROOTS or path.is_absolute()
                or (components[0] == "payload-manifest.json" and (len(components) != 1 or item.is_dir()))
                or "\\" in item.filename or "\x00" in item.filename
                or "//" in item.filename or len(item.filename.encode("utf-8")) >= 1024
                or any(
                    component in {".", ".."} or component.endswith((".", " "))
                    or any(character in '<>:"|?*' or ord(character) < 32 for character in component)
                    or component.split(".", 1)[0].upper() in reserved
                    for component in components
                )
                or item.filename in seen or windows_name in windows_names
            )
            if invalid:
                raise ValueError(f"Unsafe or duplicate payload entry: {item.filename!r}")
            if item.compress_type != zipfile.ZIP_STORED:
                raise ValueError("Native extractor requires ZIP_STORED entries")
            if item.flag_bits & 1 or (item.external_attr >> 16 & 0o170000) == 0o120000:
                raise ValueError("Encrypted entries and symbolic links are prohibited")
            if item.file_size >= 512 * 1024 * 1024:
                raise ValueError("Individual payload file exceeds extraction limit")
            seen.add(item.filename)
            windows_names.add(windows_name)
            total += item.file_size
        if len(seen) >= 65535:
            raise ValueError("Native extractor does not support ZIP64 entry counts")
        if required - seen:
            raise ValueError(f"Incomplete Windows bundle: {sorted(required - seen)}")
        damaged = archive.testzip()
        if damaged:
            raise ValueError(f"Payload CRC failed: {damaged}")
    return {
        "payload_bytes": payload.stat().st_size,
        "uncompressed_bytes": total,
        "entries": len(seen),
        "sha256": hashlib.sha256(payload.read_bytes()).hexdigest(),
    }


def official_zig(work: Path) -> Path:
    """Use an existing compiler or download and verify the official pinned release."""
    configured = os.environ.get("ZIG")
    if configured:
        return Path(configured).resolve()
    present = shutil.which("zig")
    if present:
        return Path(present)
    work.mkdir(parents=True, exist_ok=True)
    if sys.platform.startswith("linux"):
        platform = "x86_64-linux"
        name = "zig"
    elif sys.platform == "win32":
        platform = "x86_64-windows"
        name = "zig.exe"
    else:
        raise RuntimeError("Set ZIG to a compiler path on this build platform")
    cached = list(work.glob(f"zig-{platform}-{ZIG_VERSION}/{name}"))
    if cached:
        return cached[0]
    request = urllib.request.Request(
        "https://ziglang.org/download/index.json", headers={"User-Agent": "ExpansionHunterBuild/1"}
    )
    with urllib.request.urlopen(request, timeout=40) as response:
        release = json.load(response)[ZIG_VERSION][platform]
    archive = work / Path(release["tarball"]).name
    if not archive.exists() or hashlib.sha256(archive.read_bytes()).hexdigest() != release["shasum"]:
        with urllib.request.urlopen(release["tarball"], timeout=90) as response, archive.open("wb") as target:
            shutil.copyfileobj(response, target)
    actual = hashlib.sha256(archive.read_bytes()).hexdigest()
    if actual != release["shasum"]:
        raise RuntimeError("Official Zig archive hash mismatch")
    (work / "zig-release.json").write_text(json.dumps(release, indent=2), encoding="utf-8")
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as zipped:
            for member in zipped.infolist():
                if member.filename.startswith(("/", "\\")) or ".." in Path(member.filename).parts:
                    raise RuntimeError("Unsafe compiler archive member")
            zipped.extractall(work)
    else:
        with tarfile.open(archive) as tar:
            tar.extractall(work, filter="data")
    matches = list(work.glob(f"zig-{platform}-{ZIG_VERSION}/{name}"))
    if not matches:
        raise RuntimeError("Compiler missing after verified archive extraction")
    return matches[0]


def pe_metadata(path: Path) -> dict:
    """Read actual DOS/PE headers, rather than inferring success from an .exe suffix."""
    with path.open("rb") as binary:
        first = binary.read(64)
        if len(first) != 64 or first[:2] != b"MZ":
            raise ValueError("Artifact is not a Windows DOS/PE executable")
        offset = struct.unpack_from("<I", first, 60)[0]
        binary.seek(offset)
        header = binary.read(24)
        if header[:4] != b"PE\x00\x00":
            raise ValueError("Artifact PE signature is missing")
        machine, sections, _, _, _, optional_size, _ = struct.unpack_from("<HHIIIHH", header, 4)
        optional = binary.read(optional_size)
        if len(optional) < 70:
            raise ValueError("Artifact PE optional header is truncated")
        magic = struct.unpack_from("<H", optional)[0]
        subsystem = struct.unpack_from("<H", optional, 68)[0]
        if machine != 0x8664 or magic != 0x20B or subsystem != 2:
            raise ValueError("Launcher must be Windows x64 PE32+ GUI subsystem")
    return {"machine": "AMD64", "format": "PE32+", "subsystem": "WINDOWS_GUI", "sections": sections}


def resource_string(path: Path) -> str:
    return str(path.resolve()).replace("\\", "\\\\").replace('"', '\\"')


def verify_embedded_resources(executable: Path, payload_sha256: str, manifest: Path) -> dict:
    """Verify actual PE resource bytes, including the archive and asInvoker manifest."""
    pe_metadata(executable)
    with executable.open("rb") as binary, mmap.mmap(binary.fileno(), 0, access=mmap.ACCESS_READ) as data:
        pe = struct.unpack_from("<I", data, 60)[0]
        sections = struct.unpack_from("<H", data, pe + 6)[0]
        optional_size = struct.unpack_from("<H", data, pe + 20)[0]
        optional = pe + 24
        if optional_size < 144:
            raise ValueError("PE resource directory is missing")
        rva, resource_size = struct.unpack_from("<II", data, optional + 112 + 16)
        section_start = optional + optional_size

        def file_offset(address: int) -> int:
            for index in range(sections):
                entry = section_start + 40 * index
                virtual_size, virtual, raw_size, raw = struct.unpack_from("<IIII", data, entry + 8)
                if virtual <= address < virtual + max(virtual_size, raw_size):
                    position = raw + address - virtual
                    if position >= len(data):
                        raise ValueError("PE resource RVA exceeds file bounds")
                    return position
            raise ValueError("PE resource RVA has no section")

        base = file_offset(rva)
        if resource_size == 0 or base + resource_size > len(data):
            raise ValueError("PE resource section is invalid")

        def children(relative: int) -> list[tuple[int, int]]:
            position = base + relative
            if relative < 0 or position + 16 > base + resource_size:
                raise ValueError("PE resource directory exceeds section bounds")
            named, identifiers = struct.unpack_from("<HH", data, position + 12)
            count = named + identifiers
            if position + 16 + count * 8 > base + resource_size:
                raise ValueError("PE resource entries exceed section bounds")
            return [struct.unpack_from("<II", data, position + 16 + index * 8) for index in range(count)]

        def entry(resource_type: int, identifier: int) -> tuple[int, int]:
            relative = 0
            for identifier_at_level in (resource_type, identifier):
                candidates = [(name, target) for name, target in children(relative)
                              if name == identifier_at_level and target & 0x80000000]
                if len(candidates) != 1:
                    raise ValueError(f"Required PE resource is absent: {resource_type}/{identifier}")
                relative = candidates[0][1] & 0x7fffffff
            leaves = children(relative)
            if len(leaves) != 1 or leaves[0][1] & 0x80000000:
                raise ValueError("Expected exactly one resource language/data entry")
            leaf = base + leaves[0][1]
            if leaf + 16 > base + resource_size:
                raise ValueError("PE resource data entry is outside its section")
            address, size = struct.unpack_from("<II", data, leaf)
            position = file_offset(address)
            if size == 0 or position + size > len(data):
                raise ValueError("PE embedded resource data is truncated")
            return position, size

        start, length = entry(10, 101)  # RT_RCDATA, immutable payload archive.
        digest = hashlib.sha256(memoryview(data)[start:start + length]).hexdigest()
        if digest != payload_sha256:
            raise ValueError("Compiled executable archive differs from the verified payload")
        start_manifest, manifest_length = entry(24, 1)  # RT_MANIFEST.
        if data[start_manifest:start_manifest + manifest_length] != manifest.read_bytes():
            raise ValueError("Compiled executable does not contain the required asInvoker manifest")
    return {"embedded_payload_sha256": digest, "embedded_manifest_verified": True}


def build(payload: Path, output: Path, compiler: Path | None = None) -> dict:
    details = validate_payload(payload)
    work = REPOSITORY / "build" / "windows"
    work.mkdir(parents=True, exist_ok=True)
    zig = compiler or official_zig(work / "toolchain")
    source = REPOSITORY / "desktop" / "native"
    resources = work / "launcher.rc"
    compiled_resources = work / "launcher.res"
    icon = source / "expansion-hunter.ico"
    contents = f'101 RCDATA "{resource_string(payload)}"\n'
    if icon.exists():
        contents += f'1 ICON "{resource_string(icon)}"\n'
    manifest = source / "launcher.manifest"
    if not manifest.is_file():
        raise ValueError("Required asInvoker Windows manifest is missing")
    contents += f'1 24 "{resource_string(manifest)}"\n'
    contents += '''1 VERSIONINFO
FILEVERSION 2,0,0,0
PRODUCTVERSION 2,0,0,0
FILEOS 0x40004
FILETYPE 1
BEGIN
 BLOCK "StringFileInfo"
 BEGIN
  BLOCK "040904b0"
  BEGIN
   VALUE "FileDescription", "Expansion Hunter Desktop Launcher\\0"
   VALUE "ProductName", "Expansion Hunter\\0"
   VALUE "LegalCopyright", "Expansion Hunter project\\0"
  END
 END
 BLOCK "VarFileInfo"
 BEGIN
  VALUE "Translation", 0x0409, 1200
 END
END
'''
    resources.write_text(contents, encoding="utf-8")
    output.parent.mkdir(parents=True, exist_ok=True)
    # Build caches belong to the build directory, not a potentially read-only
    # user profile. This also makes the restricted Linux cross-build reproducible.
    environment = dict(os.environ,
        ZIG_GLOBAL_CACHE_DIR=str(work / "zig-global-cache"),
        ZIG_LOCAL_CACHE_DIR=str(work / "zig-local-cache"))
    resource_digest = hashlib.sha256(contents.encode("utf-8"))
    resource_digest.update(details["sha256"].encode("ascii"))
    resource_digest.update(manifest.read_bytes())
    if icon.exists():
        resource_digest.update(icon.read_bytes())
    resource_key = resource_digest.hexdigest()
    resource_state = work / "launcher-resource.sha256"
    if not (compiled_resources.is_file() and resource_state.is_file() and
            resource_state.read_text(encoding="ascii").strip() == resource_key):
        subprocess.run([str(zig), "rc", "/nologo", "/fo", str(compiled_resources), str(resources)],
                       check=True, env=environment)
        resource_state.write_text(resource_key, encoding="ascii")
    release_id = details["sha256"][:12]
    command = [
        str(zig), "cc", "-target", "x86_64-windows-gnu", "-std=c11", "-O2",
        "-municode", "-Wl,--subsystem,windows", "-Wall", "-Wextra", "-Werror",
        "-Wno-unused-function", "-Wno-unused-command-line-argument",
        f'-DEH_RELEASE_ID=L"{release_id}"', str(source / "launcher.c"), str(compiled_resources),
        "-lole32", "-lshell32", "-luuid", "-lgdi32", "-luser32",
        "-lwintrust", "-lcrypt32", "-ladvapi32", "-Wl,--stack,8388608",
        "-o", str(output),
    ]
    subprocess.run(command, check=True, env=environment)
    details.update(pe_metadata(output))
    details.update(verify_embedded_resources(output, details["sha256"], manifest))
    details.update({
        "release_id": release_id,
        "launcher_bytes": output.stat().st_size,
        "launcher_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "output": str(output.resolve()),
        "windows_execution_tested": False,
        "trading_execution": "DISABLED",
        "native_extract_format": "ZIP_STORED",
        "requested_execution_level": "asInvoker",
        "webview_prerequisite": "MICROSOFT_AUTHENTICODE_VERIFIED_BOOTSTRAPPER_IF_MISSING",
        "compiler": subprocess.check_output([str(zig), "version"], text=True).strip(),
    })
    report = REPOSITORY / "reports" / "windows-launcher-build.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(details, indent=2), encoding="utf-8")
    return details


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--payload", type=Path, default=REPOSITORY / "build/windows/payload.zip")
    parser.add_argument("--output", type=Path, default=REPOSITORY / "release/ExpansionHunter-Setup.exe")
    parser.add_argument("--zig", type=Path)
    args = parser.parse_args()
    print(json.dumps(build(args.payload, args.output, args.zig), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
