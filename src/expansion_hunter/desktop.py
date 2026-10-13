"""Native-window launcher for the existing Expansion Hunter application.

The release bundles Python and pywebview; users never install Python or enter
server commands. Controller/storage code remains testable without a GUI runtime.
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import os
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .config import Settings

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class DesktopPaths:
    root: Path
    resources: Path

    @property
    def database(self) -> Path:
        return self.root / "data" / "cockpit.sqlite3"

    @property
    def settings(self) -> Path:
        return self.root / "config" / "settings.json"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    def create(self) -> None:
        for directory in (self.database.parent, self.settings.parent, self.logs, self.root / "sessions"):
            directory.mkdir(parents=True, exist_ok=True)


def default_data_directory() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "ExpansionHunter"
    return Path.home() / "AppData" / "Local" / "ExpansionHunter"


def default_resource_directory() -> Path:
    return Path(__file__).resolve().parents[2]


def backup_existing_database(source: Path, destination: Path) -> bool:
    """Import once via SQLite backup, including committed WAL content.

    A prior desktop database always wins. Copying individual SQLite/WAL files is
    deliberately avoided, and incomplete imports never become the active file.
    """
    if destination.exists() or not source.is_file():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".importing")
    temporary.unlink(missing_ok=True)
    try:
        with sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=30) as existing:
            with sqlite3.connect(temporary) as target:
                existing.backup(target)
                if target.execute("PRAGMA quick_check").fetchone() != ("ok",):
                    raise RuntimeError("The existing database could not be copied safely")
        # The instance lock protects normal starts. Preserve even a concurrently
        # created database from a separate maintenance tool rather than replace it.
        if destination.exists():
            return False
        temporary.replace(destination)
        return True
    finally:
        temporary.unlink(missing_ok=True)


def prepare_desktop(paths: DesktopPaths, seed_database: Path | None = None) -> Settings:
    paths.create()
    backup_existing_database(seed_database or paths.resources / "data" / "cockpit.sqlite3", paths.database)
    source = paths.settings if paths.settings.exists() else paths.resources / "config" / "settings.json"
    settings = Settings.model_validate_json(source.read_text(encoding="utf-8")) if source.exists() else Settings()
    changes = {"db_path": str(paths.database.resolve())}
    for name in ("symbol_map_path", "catalyst_path"):
        value = getattr(settings, name)
        if value and not Path(value).is_absolute():
            resolved = paths.resources / value
            changes[name] = str(resolved.resolve()) if resolved.is_file() else None
    settings = settings.model_copy(update=changes)
    temporary = paths.settings.with_name("settings.json.tmp")
    temporary.write_text(settings.model_dump_json(indent=2), encoding="utf-8")
    temporary.replace(paths.settings)
    return settings


class SingleInstance:
    """OS-released per-user lock; a second launch requests focus rather than scans."""

    def __init__(self, root: Path):
        self.root = root
        self.handle = None
        self.focus_file = root / "focus.request"

    def acquire(self) -> bool:
        self.root.mkdir(parents=True, exist_ok=True)
        handle = (self.root / "desktop.lock").open("a+b")
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            self.focus_file.write_text(str(time.time_ns()), encoding="ascii")
            return False
        self.handle = handle
        self.focus_file.unlink(missing_ok=True)
        return True

    def close(self) -> None:
        if self.handle is not None:
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None


class WindowsChildJob:
    """Kill owned descendants after abnormal desktop exit; not other applications."""

    def __init__(self):
        self.handle = None
        self.kernel = None

    def attach(self, process) -> None:
        if os.name != "nt":
            return
        import ctypes
        from ctypes import wintypes

        class BasicLimit(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )]

        class ExtendedLimit(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimit), ("IoInfo", IoCounters)] + [
                (name, ctypes.c_size_t) for name in (
                    "ProcessMemoryLimit", "JobMemoryLimit", "PeakProcessMemoryUsed", "PeakJobMemoryUsed",
                )
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(ctypes.get_last_error(), "Could not create desktop process job")
        info = ExtendedLimit()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            kernel.CloseHandle(handle)
            raise OSError(ctypes.get_last_error(), "Could not configure desktop process job")
        if not kernel.AssignProcessToJobObject(handle, wintypes.HANDLE(process._handle)):
            kernel.CloseHandle(handle)
            raise OSError(ctypes.get_last_error(), "Could not protect desktop child process")
        self.handle, self.kernel = handle, kernel

    def close(self) -> None:
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class DesktopController:
    def __init__(self, paths: DesktopPaths, *, spawn=subprocess.Popen, startup_timeout: float = 60):
        self.paths, self.spawn, self.startup_timeout = paths, spawn, startup_timeout
        self.token = secrets.token_urlsafe(48)
        self.session = paths.root / "sessions" / secrets.token_hex(16)
        self.ready_file, self.stop_file = self.session / "ready.json", self.session / "stop.request"
        self.process = None
        self.origin = None
        self._log = None
        self._stopping = threading.Event()
        self._job = WindowsChildJob()

    def start(self) -> str:
        self.session.mkdir(parents=True, exist_ok=False)
        log_path = self.paths.logs / "backend.log"
        if log_path.exists() and log_path.stat().st_size > 5_000_000:
            log_path.replace(self.paths.logs / "backend.previous.log")
        self._log = log_path.open("a", encoding="utf-8")
        environment = os.environ.copy()
        environment["COCKPIT_API_TOKEN"] = self.token
        environment["PYTHONUNBUFFERED"] = "1"
        # The embedded distribution disables normal Python path discovery. The
        # release adds the application via its audited ._pth file instead.
        command = [
            sys.executable, "-m", "expansion_hunter.desktop_backend",
            "--settings", str(self.paths.settings), "--ready-file", str(self.ready_file),
            "--stop-file", str(self.stop_file), "--parent-pid", str(os.getpid()),
        ]
        try:
            self.process = self.spawn(
                command, cwd=str(self.paths.resources), env=environment,
                stdout=self._log, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            self._job.attach(self.process)
            deadline = time.monotonic() + self.startup_timeout
            while time.monotonic() < deadline and not self._stopping.is_set():
                if self.process.poll() is not None:
                    raise RuntimeError("The market service could not start. See the application log.")
                if self.ready_file.exists():
                    try:
                        ready = json.loads(self.ready_file.read_text(encoding="utf-8"))
                        if ready.get("pid") != self.process.pid or ready.get("backend") != "STARTED":
                            raise RuntimeError("Invalid startup confirmation from the market service")
                        port = ready.get("port")
                        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
                            raise RuntimeError("Invalid internal connection")
                        origin = f"http://127.0.0.1:{port}"
                        with httpx.Client(timeout=2, trust_env=False) as client:
                            health = client.get(origin + "/api/live/health", headers={
                                "Authorization": "Bearer " + self.token,
                            })
                            health.raise_for_status()
                            if health.json().get("database") != "AVAILABLE":
                                raise RuntimeError("The application database is unavailable")
                        self.origin = origin
                        return origin + "/cockpit"
                    except (OSError, httpx.HTTPError, json.JSONDecodeError):
                        pass  # Marker is atomic; retry genuine startup/socket races.
                self._stopping.wait(0.1)
            raise RuntimeError("The market service did not respond in time. Close and reopen Expansion Hunter.")
        except BaseException:
            self.stop()
            raise

    def inject_session(self, window) -> None:
        if not self.origin:
            return
        current = window.get_current_url() or ""
        parsed, expected = urlsplit(current), urlsplit(self.origin)
        if (parsed.scheme, parsed.netloc) != (expected.scheme, expected.netloc):
            return  # Never hand a session credential to external navigation.
        detail = json.dumps({"token": self.token, "desktop": True})
        window.evaluate_js("window.dispatchEvent(new CustomEvent('eh-desktop-session', {detail:" + detail + "}))")

    def stop(self) -> None:
        self._stopping.set()
        if self.process and self.process.poll() is None:
            self.stop_file.parent.mkdir(parents=True, exist_ok=True)
            self.stop_file.write_text("STOP", encoding="ascii")
            try:
                self.process.wait(timeout=25)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
        self._job.close()
        if self._log is not None:
            self._log.close()
            self._log = None
        # Remove only this launch's IPC markers, never settings or observations.
        self.ready_file.unlink(missing_ok=True)
        self.stop_file.unlink(missing_ok=True)
        if self.session.is_dir() and not any(self.session.iterdir()):
            self.session.rmdir()


def startup_html(message: str, *, failed: bool = False) -> str:
    title = "Startup failed" if failed else "Starting Expansion Hunter"
    return (
        '<!doctype html><html lang="en"><meta charset="utf-8"><title>Expansion Hunter</title>'
        '<style>body{background:#101714;color:#edf1e7;font:16px "Segoe UI",sans-serif;'
        'padding:10vh 8vw;line-height:1.7}h1{color:#b9e876}p{max-width:650px}</style>'
        f"<h1>{title}</h1><p>{html.escape(message)}</p>"
        "<p>Observation only. No orders are placed.</p></html>"
    )


def configure_logging(paths: DesktopPaths) -> None:
    handler = RotatingFileHandler(paths.logs / "desktop.log", maxBytes=2_000_000,
                                  backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)


def show_native_error(message: str) -> None:
    if os.name == "nt":
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, "Expansion Hunter", 0x10)
    else:
        _LOG.error(message)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Expansion Hunter desktop application")
    parser.add_argument("--resource-dir", type=Path, default=default_resource_directory())
    parser.add_argument("--data-dir", type=Path, default=default_data_directory())
    parser.add_argument("--seed-database", type=Path)
    args = parser.parse_args(argv)
    paths = DesktopPaths(args.data_dir.resolve(), args.resource_dir.resolve())
    instance = SingleInstance(paths.root)
    if not instance.acquire():
        return 0
    controller = None
    try:
        paths.create()
        configure_logging(paths)
        prepare_desktop(paths, args.seed_database)
        try:
            import webview
        except ImportError as exc:
            raise RuntimeError("The desktop runtime is missing. Install the complete Expansion Hunter application.") from exc
        controller = DesktopController(paths)
        window = webview.create_window(
            "EXPANSION HUNTER", html=startup_html("The database and market service are starting automatically."),
            width=1380, height=900, min_size=(800, 600), background_color="#101714",
        )
        window.events.loaded += lambda: controller.inject_session(window)
        closed = threading.Event()
        window.events.closed += closed.set

        def initialize():
            try:
                window.load_url(controller.start())
                while not closed.wait(0.5):
                    if instance.focus_file.exists():
                        instance.focus_file.unlink(missing_ok=True)
                        window.restore()
                        window.show()
            except Exception:
                _LOG.exception("Desktop startup failed")
                if not closed.is_set():
                    window.load_html(startup_html(
                        "The application could not start. Close this window and reopen Expansion Hunter. "
                        "Your saved data is preserved. The log is in your application data folder.",
                        failed=True,
                    ))

        webview.start(initialize, gui="edgechromium" if os.name == "nt" else None, debug=False,
                      private_mode=True)
        return 0
    except Exception:
        _LOG.exception("Desktop could not initialize")
        show_native_error("Expansion Hunter could not start. Reinstall the complete application. "
                          "Your existing data will not be deleted.")
        return 1
    finally:
        if controller:
            controller.stop()
        instance.close()


if __name__ == "__main__":
    raise SystemExit(main())
