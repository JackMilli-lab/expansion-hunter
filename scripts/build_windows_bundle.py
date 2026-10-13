#!/usr/bin/env python3
"""Build a real Windows x64 embedded-runtime payload; never include user data.

This build runs on Linux or Windows and downloads official CPython and PyPI
Windows wheels. It does not prove execution on Windows. Network access is needed
unless every required download is already cached. No user credentials are read.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

PYTHON_VERSION = "3.12.10"
PYTHON_URL = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-embed-amd64.zip"
WEBVIEW_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"
REQUIRED_PATHS = (
    "runtime/pythonw.exe", "runtime/python312.dll", "runtime/python312.zip",
    "runtime/python312._pth", "app/expansion_hunter/__init__.py",
    "app/expansion_hunter/desktop.py", "app/expansion_hunter/desktop_backend.py",
    "app/expansion_hunter/static/cockpit.html", "app/config/settings.json",
)
ZIP_TIME = (2026, 1, 1, 0, 0, 0)
SECRET_NAMES = {".env", ".aws", ".codex", ".agents", "cockpit.sqlite3", "smoke.sqlite3"}


def sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def safe_archive_path(name: str) -> PurePosixPath:
    """Reject traversal, Windows drives, and ambiguity before writing archives."""
    normalized = name.replace("\\", "/")
    parts = PurePosixPath(normalized)
    if (not name or parts.is_absolute() or ".." in parts.parts
            or any(":" in part or "\x00" in part for part in parts.parts)):
        raise ValueError(f"Unsafe archive member: {name!r}")
    return parts


def extract_archive(source: Path, target: Path, *, wheel: bool = False) -> None:
    with zipfile.ZipFile(source) as archive:
        for item in archive.infolist():
            parts = safe_archive_path(item.filename)
            if item.is_dir():
                continue
            mode = (item.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise ValueError(f"Symbolic link in archive: {item.filename}")
            components = parts.parts
            if wheel and any(part in SECRET_NAMES for part in components):
                # Some distributions ship their development-agent instructions.
                # They are not runtime modules and must not enter a user bundle.
                continue
            if wheel and len(components) > 2 and components[0].endswith(".data"):
                if components[1] in {"purelib", "platlib"}:
                    components = components[2:]
                else:
                    # Console scripts are deliberately not installed in the desktop runtime.
                    continue
            destination = target.joinpath(*components)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(item) as src, destination.open("wb") as dst:
                shutil.copyfileobj(src, dst)


def download(url: str, target: Path) -> None:
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "ExpansionHunter-WindowsBuilder/1"})
    temporary = target.with_suffix(target.suffix + ".partial")
    try:
        # urllib retains the inherited HTTPS proxy and system CA verification.
        with urllib.request.urlopen(request, timeout=60) as response, temporary.open("wb") as handle:
            shutil.copyfileobj(response, handle)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def run_pip(arguments: list[str]) -> None:
    subprocess.run([sys.executable, "-m", "pip", *arguments], check=True, timeout=600)


def resolve_wheels(requirements: Path, cache: Path) -> list[Path]:
    cache.mkdir(parents=True, exist_ok=True)
    # proxy_tools is a source-only, pure-Python package. Build its universal wheel
    # with no dependencies; reject platform/native outputs before accepting it.
    if not list(cache.glob("proxy_tools-0.1.0-*-none-any.whl")):
        run_pip(["wheel", "--no-deps", "--wheel-dir", str(cache), "proxy_tools==0.1.0"])
    run_pip([
        "download", "--dest", str(cache), "--find-links", str(cache),
        "--platform", "win_amd64", "--python-version", "312",
        "--implementation", "cp", "--abi", "cp312", "--only-binary=:all:",
        "-r", str(requirements),
    ])
    wheels = sorted(cache.glob("*.whl"))
    for wheel in wheels:
        if not (wheel.name.endswith("-none-any.whl") or wheel.name.endswith("-win_amd64.whl")):
            raise ValueError(f"Not a Windows x64/universal wheel: {wheel.name}")
    return wheels


def verify_windows_executable(path: Path) -> None:
    content = path.read_bytes()
    if len(content) < 64 or content[:2] != b"MZ":
        raise ValueError(f"Not a PE executable: {path.name}")
    pe_offset = struct.unpack_from("<I", content, 0x3C)[0]
    if content[pe_offset:pe_offset + 4] != b"PE\0\0":
        raise ValueError(f"Invalid PE header: {path.name}")
    if struct.unpack_from("<H", content, pe_offset + 4)[0] != 0x8664:
        raise ValueError(f"Not Windows x64: {path.name}")


def copy_application(project: Path, target: Path) -> None:
    app = target / "app"
    shutil.copytree(project / "src" / "expansion_hunter", app / "expansion_hunter",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".env"))
    (app / "config").mkdir(parents=True)
    for source in sorted((project / "config").glob("*.json")):
        # Config files contain public provider settings, not runtime credentials.
        data = json.loads(source.read_text(encoding="utf-8"))
        if re.search(r'"(?:api_key|token|secret|password)"\s*:\s*"[^"\s]+"', json.dumps(data), re.I):
            raise ValueError(f"Refusing credentials in packaged configuration: {source.name}")
        shutil.copyfile(source, app / "config" / source.name)
    shutil.copyfile(project / "pyproject.toml", app / "pyproject.toml")


def deterministic_zip(directory: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Native no-dependency installer extracts stored ZIP entries and validates CRC.
    # The bundled CPython stdlib ZIP remains compressed internally.
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_STORED) as archive:
        for source in sorted(directory.rglob("*")):
            if not source.is_file():
                continue
            relative = source.relative_to(directory).as_posix()
            safe_archive_path(relative)
            if any(part in SECRET_NAMES for part in Path(relative).parts):
                raise ValueError(f"Refusing private/user-data file: {relative}")
            info = zipfile.ZipInfo(relative, ZIP_TIME)
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, source.read_bytes())


def verify_payload(path: Path) -> dict[str, object]:
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        for name in names:
            safe_archive_path(name)
        missing = set(REQUIRED_PATHS) - names
        if missing:
            raise ValueError(f"Missing required payload resources: {sorted(missing)}")
        bad = archive.testzip()
        if bad:
            raise ValueError(f"Corrupt ZIP member: {bad}")
        forbidden = [name for name in names if any(part in SECRET_NAMES for part in PurePosixPath(name).parts)]
        if forbidden:
            raise ValueError(f"Private/user-data payload members: {forbidden}")
        return {"entries": len(names), "size_bytes": path.stat().st_size, "sha256": sha256(path)}


def build(project: Path, output: Path, *, include_webview: bool = False) -> dict[str, object]:
    cache = output / "downloads"
    embedded = cache / f"python-{PYTHON_VERSION}-embed-amd64.zip"
    download(PYTHON_URL, embedded)
    wheels = resolve_wheels(project / "scripts" / "windows_bundle_requirements.txt", cache / "wheels")
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="payload-", dir=output) as tmp:
        staging = Path(tmp)
        extract_archive(embedded, staging / "runtime")
        verify_windows_executable(staging / "runtime" / "pythonw.exe")
        (staging / "runtime" / "python312._pth").write_text(
            "python312.zip\n.\n../packages\n../app\nimport site\n", encoding="utf-8")
        for wheel in wheels:
            extract_archive(wheel, staging / "packages", wheel=True)
        copy_application(project, staging)
        licenses = staging / "licenses"
        licenses.mkdir()
        license_source = staging / "runtime" / "LICENSE.txt"
        if license_source.exists():
            shutil.copyfile(license_source, licenses / "CPython-LICENSE.txt")
        sources: list[dict[str, object]] = [{"file": embedded.name, "source": PYTHON_URL, "sha256": sha256(embedded)}]
        for wheel in wheels:
            sources.append({"file": wheel.name, "source": "https://pypi.org/ (pip TLS-verified wheel resolution)",
                            "sha256": sha256(wheel)})
        if include_webview:
            bootstrapper = cache / "WebView2Setup.exe"
            download(WEBVIEW_URL, bootstrapper)
            if bootstrapper.read_bytes()[:2] != b"MZ":
                raise ValueError("Downloaded WebView2 bootstrapper is not an executable")
            shutil.copyfile(bootstrapper, staging / "runtime" / "WebView2Setup.exe")
            sources.append({"file": bootstrapper.name, "source": WEBVIEW_URL, "sha256": sha256(bootstrapper)})
        inventory = {source.relative_to(staging).as_posix(): sha256(source)
                     for source in sorted(staging.rglob("*")) if source.is_file()}
        manifest = {"target": "Windows 10/11 x64", "python": PYTHON_VERSION,
                    "production_trading": "DISABLED", "windows_execution_verified": False,
                    "sources": sources, "files": inventory,
                    "note": "Dependency license notices retained inside .dist-info directories."}
        (staging / "payload-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        payload = output / "payload.zip"
        deterministic_zip(staging, payload)
    result = {"created_at": datetime.now(UTC).isoformat(), "payload": str(payload),
              "windows_execution_verified": False, **verify_payload(payload)}
    (output / "payload-build-report.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, default=Path("build/windows"))
    parser.add_argument("--include-webview-bootstrapper", action="store_true")
    parser.add_argument("--verify-only", type=Path)
    args = parser.parse_args()
    result = verify_payload(args.verify_only) if args.verify_only else build(
        args.project.resolve(), args.output.resolve(), include_webview=args.include_webview_bootstrapper)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
