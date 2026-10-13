# Native Windows launcher

`launcher.c` is the graphical Windows x64 bootstrap for the **existing**
Expansion Hunter application. It does not run PowerShell, CMD or an installer
for Python. The release includes the official CPython embedded runtime,
dependencies and the application. The end user opens `ExpansionHunter-Setup.exe`.

The launcher writes its own executable to
`%LOCALAPPDATA%\ExpansionHunter\ExpansionHunter.exe`, creates the user's
`EXPANSION HUNTER.lnk` desktop shortcut through Windows COM and installs resources
under `releases/<payload-sha256-prefix>/`. The desktop Python controller opens
the persistent database under the separate `data/` directory. Existing
application data is neither removed nor replaced by the native launcher.

Installation has a visible progress window. Each embedded ZIP entry is checked
for traversal, reserved Windows names, links, unsupported compression and CRC
integrity. The `.installed` marker is written only after extraction and required
runtime/app files have been verified. Interrupted extraction can be repeated.
The archive intentionally uses ZIP_STORED to keep the extractor independent of
downloaded compression libraries. The Python standard-library ZIP remains
compressed internally.

After installation, `CreateProcessW` opens `pythonw.exe` with a correctly quoted,
Unicode command line and `CREATE_NO_WINDOW`. The bundled desktop controller owns
backend startup/shutdown, user data, single-instance handling and the native
WebView window. No browser address is presented to the user.

The embedded manifest explicitly requests `asInvoker`: this per-user installer
does not request administrator elevation. The launcher checks WebView2 through
the bundled x64 SDK loader when available, otherwise through Microsoft's
documented runtime registry keys. If it is missing, the **bundled official
Microsoft** evergreen bootstrapper can run `/silent /install`. Before launching
it, Windows `WinVerifyTrust` must validate Authenticode and the signer must be
exactly `Microsoft Corporation`. An unsigned, modified or other-publisher
bootstrapper is refused. The app does not download a replacement executable
from arbitrary URLs. Internet is required for Microsoft's bootstrapper to obtain
the runtime; a missing/failed runtime is reported through the native window.

`logs/launcher.log` records UTC launch/install errors and rolls at 1 MiB.
`logs/desktop-runtime.log` captures early bundled-runtime failures. A process
exiting unsuccessfully immediately after `CreateProcessW` produces a visible
error; process creation alone is not interpreted as market connectivity. The
application's own readiness/feed indicators continue to establish actual
backend and Binance health.

Developer build:

```sh
python scripts/build_windows_bundle.py --include-webview-bootstrapper
python scripts/build_windows_launcher.py
PYTHONPATH=src:.tooling python -m pytest tests/test_windows_launcher.py
```

An existing Zig compiler can be passed with `--zig /path/to/zig`. Otherwise the
builder downloads pinned Zig 0.15.2 from the official HTTPS index and validates
the archive against that index's SHA256. The compiler cross-builds the actual
Windows x64 GUI PE file. The resulting binary is then inspected for `MZ`, `PE`,
AMD64, PE32+ and the Windows GUI subsystem; an `.exe` suffix alone is insufficient.

Native parser and quoting tests execute the same production C header through a
real host C compiler. They are **not** a substitute for starting the installer
on Windows. A Linux cross-build cannot demonstrate Windows WebView2, desktop
shortcut behavior, antivirus decisions, or an unattended 24/7 Windows run.
Pure version/publisher predicates and the manifest are also covered by host
tests; this does not claim that Windows certificate verification was executed
on Linux.
The generated `reports/windows-launcher-build.json` explicitly records whether
Windows execution was tested. Signing an executable requires a separate
legitimate signing certificate and is not fabricated by this build.
