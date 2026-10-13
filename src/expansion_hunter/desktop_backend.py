"""Private, loopback-only backend owned by the Windows desktop application.

This entry point reuses the existing FastAPI lifespan and observation workers.
The port is assigned by the OS, credentials stay in the inherited environment,
and the readiness marker describes backend startup rather than feed freshness.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
from pathlib import Path

import uvicorn

from .api import create_app
from .config import Settings
from .intelligence_store import IntelligenceStore
from .models import utcnow


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    temporary.replace(path)


def parent_alive(pid: int | None) -> bool:
    if pid is None:
        return True
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value == 259)
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


async def serve_desktop_backend(
    settings: Settings,
    ready_file: Path,
    stop_file: Path,
    *,
    parent_pid: int | None = None,
    auto_start: bool = True,
) -> None:
    if not os.environ.get("COCKPIT_API_TOKEN"):
        raise RuntimeError("Desktop backend requires its private session credential")
    ready_file.unlink(missing_ok=True)
    stop_file.unlink(missing_ok=True)
    if auto_start:
        store = IntelligenceStore(settings.db_path)
        try:
            store.put_state("app_control", "app", utcnow(), {
                "automatic_observation_requested": True,
                "source": "WINDOWS_DESKTOP",
                "live_trading": False,
            })
        finally:
            store.close()

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if os.name == "nt":
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(
        create_app(settings), host="127.0.0.1", port=port,
        log_level="warning", access_log=False, timeout_graceful_shutdown=20,
    ))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        while not server.started:
            if task.done():
                await task
                raise RuntimeError("Desktop backend stopped before startup completed")
            if stop_file.exists() or not parent_alive(parent_pid):
                server.should_exit = True
                await task
                return
            await asyncio.sleep(0.05)
        atomic_json(ready_file, {
            "pid": os.getpid(), "port": port,
            "database_path": str(Path(settings.db_path).resolve()),
            "backend": "STARTED", "market_feed": "NOT_YET_VERIFIED",
            "live_trading": False, "created_at": utcnow().isoformat(),
        })
        while not task.done():
            if stop_file.exists() or not parent_alive(parent_pid):
                server.should_exit = True
                break
            await asyncio.sleep(0.2)
        await task
    finally:
        server.should_exit = True
        if not task.done():
            await task
        listener.close()
        ready_file.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Expansion Hunter private desktop backend")
    parser.add_argument("--settings", type=Path, required=True)
    parser.add_argument("--ready-file", type=Path, required=True)
    parser.add_argument("--stop-file", type=Path, required=True)
    parser.add_argument("--parent-pid", type=int)
    parser.add_argument("--no-auto-start", action="store_true", help="Controlled integration testing only")
    args = parser.parse_args(argv)
    settings = Settings.model_validate_json(args.settings.read_text(encoding="utf-8"))
    asyncio.run(serve_desktop_backend(
        settings, args.ready_file, args.stop_file,
        parent_pid=args.parent_pid, auto_start=not args.no_auto_start,
    ))


if __name__ == "__main__":
    main()
