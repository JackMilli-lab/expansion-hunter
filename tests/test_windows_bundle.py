from __future__ import annotations

import importlib.util
import struct
import zipfile
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("windows_bundle_builder", Path(__file__).parents[1] / "scripts" / "build_windows_bundle.py")
assert spec and spec.loader
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


@pytest.mark.parametrize("name", ["../outside", "/absolute", "C:/secret", "..\\outside", "a/../../outside", "a\x00b"])
def test_archive_traversal_rejected(name):
    with pytest.raises(ValueError):
        builder.safe_archive_path(name)


def test_wheel_data_remapping(tmp_path):
    wheel = tmp_path / "fixture.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("example.data/purelib/example/__init__.py", "VALUE = 1")
        archive.writestr("example.data/scripts/private.exe", "script")
        archive.writestr("example-1.dist-info/licenses/LICENSE", "license")
    target = tmp_path / "packages"
    builder.extract_archive(wheel, target, wheel=True)
    assert (target / "example/__init__.py").exists()
    assert (target / "example-1.dist-info/licenses/LICENSE").read_text() == "license"
    assert not list(target.rglob("private.exe"))


def test_upstream_wheel_development_metadata_excluded(tmp_path):
    wheel = tmp_path / "upstream.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("fastapi/__init__.py", "VALUE = 1")
        archive.writestr("fastapi/.agents/skills/internal/SKILL.md", "development only")
        archive.writestr("fastapi/.env", "private configuration")
    target = tmp_path / "packages"
    builder.extract_archive(wheel, target, wheel=True)
    assert (target / "fastapi/__init__.py").exists()
    assert not (target / "fastapi/.agents").exists()
    assert not (target / "fastapi/.env").exists()


def test_symbolic_link_rejected(tmp_path):
    archive_path = tmp_path / "bad.zip"
    info = zipfile.ZipInfo("link")
    info.external_attr = 0o120777 << 16
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(info, "../../secret")
    with pytest.raises(ValueError, match="Symbolic link"):
        builder.extract_archive(archive_path, tmp_path / "target")


def test_deterministic_payload_and_required_files(tmp_path):
    stage = tmp_path / "stage"
    for name in builder.REQUIRED_PATHS:
        target = stage / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"fixture: not a working executable")
    one, two = tmp_path / "one.zip", tmp_path / "two.zip"
    builder.deterministic_zip(stage, one)
    builder.deterministic_zip(stage, two)
    assert one.read_bytes() == two.read_bytes()
    assert builder.verify_payload(one)["entries"] == len(builder.REQUIRED_PATHS)


def test_missing_desktop_resource_rejected(tmp_path):
    empty = tmp_path / "empty.zip"
    with zipfile.ZipFile(empty, "w"):
        pass
    with pytest.raises(ValueError, match="Missing required"):
        builder.verify_payload(empty)


def test_private_user_database_not_packaged(tmp_path):
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "cockpit.sqlite3").write_bytes(b"private")
    with pytest.raises(ValueError, match="private/user-data"):
        builder.deterministic_zip(stage, tmp_path / "bad.zip")


def test_windows_x64_pe_validation(tmp_path):
    path = tmp_path / "pythonw.exe"
    content = bytearray(128)
    content[:2] = b"MZ"
    struct.pack_into("<I", content, 0x3C, 80)
    content[80:84] = b"PE\0\0"
    struct.pack_into("<H", content, 84, 0x8664)
    path.write_bytes(content)
    builder.verify_windows_executable(path)
    struct.pack_into("<H", content, 84, 0x14C)
    path.write_bytes(content)
    with pytest.raises(ValueError, match="Windows x64"):
        builder.verify_windows_executable(path)
