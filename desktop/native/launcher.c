#ifndef UNICODE
#define UNICODE
#endif
#ifndef _UNICODE
#define _UNICODE
#endif
#define COBJMACROS
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <shlobj.h>
#include <shobjidl.h>
#include <shellapi.h>
#include <strsafe.h>
#include <stdlib.h>
#include "payload.h"

#ifndef EH_RELEASE_ID
#define EH_RELEASE_ID L"development"
#endif

/* This executable contains no order client, private API key or shell command.
 * Its sole job is installing the bundled read-only app and launching pythonw. */
static HWND g_window;
static wchar_t g_status[256] = L"Preparing Expansion Hunter…";
static unsigned int g_files;
static int g_cancelled;
static wchar_t g_release[32768];
static wchar_t g_logs[32768];
static HANDLE g_log = INVALID_HANDLE_VALUE;

static void log_message(const wchar_t *message) {
    SYSTEMTIME now;
    wchar_t line[2048];
    char encoded[8192];
    int length;
    DWORD written;
    if (g_log == INVALID_HANDLE_VALUE) return;
    GetSystemTime(&now);
    StringCchPrintfW(line, 2048, L"%04u-%02u-%02uT%02u:%02u:%02uZ %ls\r\n",
        now.wYear, now.wMonth, now.wDay, now.wHour, now.wMinute, now.wSecond, message);
    length = WideCharToMultiByte(CP_UTF8, 0, line, -1, encoded, sizeof encoded, NULL, NULL);
    if (length > 1) {
        WriteFile(g_log, encoded, (DWORD)(length - 1), &written, NULL);
        FlushFileBuffers(g_log);
    }
}

static void pump_messages(void) {
    MSG message;
    while (PeekMessageW(&message, NULL, 0, 0, PM_REMOVE)) {
        TranslateMessage(&message); DispatchMessageW(&message);
    }
}
static void status(const wchar_t *text) {
    StringCchCopyW(g_status, 256, text);
    InvalidateRect(g_window, NULL, TRUE); UpdateWindow(g_window); pump_messages();
}
static LRESULT CALLBACK window_proc(HWND window, UINT message, WPARAM wp, LPARAM lp) {
    if (message == WM_CLOSE) { g_cancelled = 1; return 0; }
    if (message == WM_PAINT) {
        PAINTSTRUCT paint;
        HDC dc = BeginPaint(window, &paint);
        RECT area;
        HBRUSH background = CreateSolidBrush(RGB(12, 20, 29));
        GetClientRect(window, &area); FillRect(dc, &area, background); DeleteObject(background);
        SetBkMode(dc, TRANSPARENT); SetTextColor(dc, RGB(231, 243, 239));
        SelectObject(dc, GetStockObject(DEFAULT_GUI_FONT));
        area.left += 24; area.top += 26; area.right -= 24;
        DrawTextW(dc, L"EXPANSION HUNTER", -1, &area, DT_TOP | DT_LEFT | DT_SINGLELINE);
        area.top += 42;
        DrawTextW(dc, g_status, -1, &area, DT_TOP | DT_LEFT | DT_WORDBREAK);
        EndPaint(window, &paint); return 0;
    }
    return DefWindowProcW(window, message, wp, lp);
}
static void fail(const wchar_t *reason) {
    wchar_t message[2048];
    DWORD code = GetLastError();
    StringCchPrintfW(message, 2048,
        L"%ls\n\nExpansion Hunter did not start. Your existing data has been preserved."
        L"\nError code: %lu."
        L"\nLog files: %%LOCALAPPDATA%%\\ExpansionHunter\\logs.",
        reason, code);
    log_message(message);
    MessageBoxW(g_window, message, L"Expansion Hunter", MB_OK | MB_ICONERROR);
}
static int join(wchar_t *output, size_t capacity, const wchar_t *left, const wchar_t *right) {
    return SUCCEEDED(StringCchPrintfW(output, capacity, L"%ls\\%ls", left, right));
}
static int ensure_directory(const wchar_t *path) {
    wchar_t temporary[32768];
    size_t n, length = wcslen(path);
    if (length >= 32768) return 0;
    StringCchCopyW(temporary, 32768, path);
    for (n = 3; n <= length; ++n) {
        wchar_t current = temporary[n];
        if (current != L'\\' && current != 0) continue;
        temporary[n] = 0;
        if (!CreateDirectoryW(temporary, NULL) && GetLastError() != ERROR_ALREADY_EXISTS)
            return 0;
        if (GetFileAttributesW(temporary) & FILE_ATTRIBUTE_REPARSE_POINT) {
            SetLastError(ERROR_ACCESS_DENIED); return 0;
        }
        temporary[n] = current;
    }
    return 1;
}
static int open_launcher_log(const wchar_t *app_root) {
    wchar_t path[32768], previous[32768];
    WIN32_FILE_ATTRIBUTE_DATA attributes;
    if (!join(g_logs, 32768, app_root, L"logs") || !ensure_directory(g_logs) ||
        !join(path, 32768, g_logs, L"launcher.log") ||
        !join(previous, 32768, g_logs, L"launcher.previous.log")) return 0;
    if (GetFileAttributesExW(path, GetFileExInfoStandard, &attributes) &&
        (attributes.nFileSizeHigh || attributes.nFileSizeLow > 1048576))
        MoveFileExW(path, previous, MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH);
    g_log = CreateFileW(path, FILE_APPEND_DATA, FILE_SHARE_READ | FILE_SHARE_WRITE, NULL,
                         OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (g_log == INVALID_HANDLE_VALUE) return 0;
    log_message(L"Expansion Hunter launcher started; observation mode, no trading execution");
    return 1;
}
static int extract_entry(const char *name, const unsigned char *bytes,
                         size_t length, int directory, void *unused) {
    wchar_t relative[2048], target[32768], parent[32768], text[256];
    int converted;
    HANDLE file;
    size_t written = 0;
    (void)unused;
    if (g_cancelled) { SetLastError(ERROR_CANCELLED); return 0; }
    converted = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, name, -1, relative, 2048);
    if (!converted) return 0;
    for (int i = 0; relative[i]; ++i) if (relative[i] == L'/') relative[i] = L'\\';
    if (!join(target, 32768, g_release, relative)) return 0;
    StringCchCopyW(parent, 32768, target);
    if (directory) {
        size_t end = wcslen(parent);
        if (end && parent[end - 1] == L'\\') parent[end - 1] = 0;
    } else {
        wchar_t *separator = wcsrchr(parent, L'\\');
        if (!separator) return 0;
        *separator = 0;
    }
    if (!ensure_directory(parent)) return 0;
    if (directory) return 1;
    if (GetFileAttributesW(target) != INVALID_FILE_ATTRIBUTES &&
        (GetFileAttributesW(target) & FILE_ATTRIBUTE_REPARSE_POINT)) {
        SetLastError(ERROR_ACCESS_DENIED); return 0;
    }
    file = CreateFileW(target, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS,
                       FILE_ATTRIBUTE_NORMAL, NULL);
    if (file == INVALID_HANDLE_VALUE) return 0;
    while (written < length) {
        DWORD part = (DWORD)((length - written) > 4194304 ? 4194304 : length - written);
        DWORD actual;
        if (!WriteFile(file, bytes + written, part, &actual, NULL) || actual != part) {
            CloseHandle(file); return 0;
        }
        written += part;
        pump_messages();
        if (g_cancelled) { CloseHandle(file); SetLastError(ERROR_CANCELLED); return 0; }
    }
    if (!FlushFileBuffers(file)) { CloseHandle(file); return 0; }
    CloseHandle(file); ++g_files;
    if (g_files % 20 == 0) {
        StringCchPrintfW(text, 256, L"Installing the application… %u files."
                            L"\nNo Python installation or terminal is required.", g_files);
        status(text);
    }
    return 1;
}
static int exists(const wchar_t *path) {
    DWORD attributes = GetFileAttributesW(path);
    return attributes != INVALID_FILE_ATTRIBUTES && !(attributes & FILE_ATTRIBUTE_DIRECTORY);
}
static int install_payload(HINSTANCE instance) {
    wchar_t marker[32768], python[32768], desktop_module[32768];
    HRSRC resource;
    HGLOBAL memory;
    const unsigned char *bytes;
    DWORD length;
    char error[256];
    HANDLE file;
    DWORD written;
    if (!join(marker, 32768, g_release, L".installed") ||
        !join(python, 32768, g_release, L"runtime\\pythonw.exe") ||
        !join(desktop_module, 32768, g_release, L"app\\expansion_hunter\\desktop.py")) return 0;
    if (exists(marker) && exists(python) && exists(desktop_module)) return 1;
    status(L"First launch: safely unpacking the complete application…");
    if (!ensure_directory(g_release)) return 0;
    resource = FindResourceW(instance, MAKEINTRESOURCEW(101), RT_RCDATA);
    if (!resource) return 0;
    memory = LoadResource(instance, resource);
    length = SizeofResource(instance, resource); bytes = LockResource(memory);
    if (!bytes || !length || !eh_zip_walk(bytes, length, extract_entry, NULL, error, sizeof error))
    {
        if (bytes && length) {
            wchar_t detail[512];
            MultiByteToWideChar(CP_UTF8, 0, error, -1, detail, 512);
            log_message(detail);
        }
        if (!g_cancelled && GetLastError() == ERROR_SUCCESS) SetLastError(ERROR_INVALID_DATA);
        return 0;
    }
    if (!exists(python) || !exists(desktop_module)) {
        SetLastError(ERROR_FILE_NOT_FOUND); return 0;
    }
    file = CreateFileW(marker, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (file == INVALID_HANDLE_VALUE) return 0;
    if (!WriteFile(file, "ExpansionHunter installed\n", 26, &written, NULL) || written != 26 ||
        !FlushFileBuffers(file)) { CloseHandle(file); return 0; }
    CloseHandle(file); return 1;
}
static int install_shortcut(const wchar_t *launcher, const wchar_t *app_root) {
    IShellLinkW *link = NULL;
    IPersistFile *persistent = NULL;
    PWSTR desktop = NULL;
    wchar_t shortcut[32768];
    HRESULT result;
    result = SHGetKnownFolderPath(&FOLDERID_Desktop, KF_FLAG_CREATE, NULL, &desktop);
    if (FAILED(result)) return 0;
    if (!join(shortcut, 32768, desktop, L"EXPANSION HUNTER.lnk")) {
        CoTaskMemFree(desktop); return 0;
    }
    CoTaskMemFree(desktop);
    result = CoCreateInstance(&CLSID_ShellLink, NULL, CLSCTX_INPROC_SERVER,
                             &IID_IShellLinkW, (void **)&link);
    if (SUCCEEDED(result)) result = IShellLinkW_SetPath(link, launcher);
    if (SUCCEEDED(result)) result = IShellLinkW_SetWorkingDirectory(link, app_root);
    if (SUCCEEDED(result)) result = IShellLinkW_SetDescription(link,
                                   L"Expansion Hunter — live crypto cockpit, observation only");
    if (SUCCEEDED(result)) result = IShellLinkW_SetIconLocation(link, launcher, 0);
    if (SUCCEEDED(result)) result = IShellLinkW_QueryInterface(link, &IID_IPersistFile,
                                                             (void **)&persistent);
    if (SUCCEEDED(result)) result = IPersistFile_Save(persistent, shortcut, TRUE);
    if (persistent) IPersistFile_Release(persistent);
    if (link) IShellLinkW_Release(link);
    return SUCCEEDED(result);
}
static int append_arg(wchar_t *command, size_t capacity, const wchar_t *value) {
    size_t length = wcslen(command);
    size_t needed = eh_quote_windows_arg(value, NULL, 0);
    if (length && length + 1 < capacity) command[length++] = L' ';
    if (needed > capacity - length) return 0;
    eh_quote_windows_arg(value, command + length, capacity - length); return 1;
}
#include "webview_prerequisite.h"

static int launch_desktop(const wchar_t *app_root) {
    wchar_t python[32768], resources[32768], command[32768] = L"", log_path[32768], detail[256];
    STARTUPINFOW startup = {0};
    PROCESS_INFORMATION process = {0};
    SECURITY_ATTRIBUTES inheritable = {0};
    HANDLE runtime_log, input;
    DWORD code;
    const wchar_t *arguments[9];
    if (!join(python, 32768, g_release, L"runtime\\pythonw.exe") ||
        !join(resources, 32768, g_release, L"app")) return 0;
    arguments[0] = python; arguments[1] = L"-I"; arguments[2] = L"-X";
    arguments[3] = L"utf8"; arguments[4] = L"-m";
    arguments[5] = L"expansion_hunter.desktop"; arguments[6] = L"--resource-dir";
    arguments[7] = resources; arguments[8] = NULL;
    for (int n = 0; arguments[n]; ++n) if (!append_arg(command, 32768, arguments[n])) return 0;
    if (!join(log_path, 32768, g_logs, L"desktop-runtime.log")) return 0;
    inheritable.nLength = sizeof inheritable; inheritable.bInheritHandle = TRUE;
    runtime_log = CreateFileW(log_path, FILE_APPEND_DATA, FILE_SHARE_READ | FILE_SHARE_WRITE,
                              &inheritable, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (runtime_log == INVALID_HANDLE_VALUE) return 0;
    input = CreateFileW(L"NUL", GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE,
                       &inheritable, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    if (input == INVALID_HANDLE_VALUE) { CloseHandle(runtime_log); return 0; }
    startup.cb = sizeof startup;
    startup.dwFlags = STARTF_USESHOWWINDOW | STARTF_USESTDHANDLES; startup.wShowWindow = SW_HIDE;
    startup.hStdOutput = runtime_log; startup.hStdError = runtime_log; startup.hStdInput = input;
    status(L"Starting the scanner, database and live cockpit…");
    if (!CreateProcessW(python, command, NULL, NULL, TRUE, CREATE_NO_WINDOW,
                         NULL, app_root, &startup, &process)) {
        DWORD failure = GetLastError(); CloseHandle(input); CloseHandle(runtime_log);
        SetLastError(failure); return 0;
    }
    CloseHandle(input); CloseHandle(runtime_log); CloseHandle(process.hThread);
    log_message(L"Bundled graphical Python runtime started without a console");
    if (WaitForSingleObject(process.hProcess, 1500) == WAIT_OBJECT_0 &&
        GetExitCodeProcess(process.hProcess, &code) && code != 0) {
        StringCchPrintfW(detail, 256, L"Desktop runtime exited during startup: %lu", code);
        log_message(detail); CloseHandle(process.hProcess); SetLastError(code); return 0;
    }
    CloseHandle(process.hProcess); return 1;
}
int WINAPI wWinMain(HINSTANCE instance, HINSTANCE previous, PWSTR arguments, int show) {
    wchar_t app_root[32768], releases[32768], launcher[32768], temporary[32768], self[32768];
    PWSTR local_data = NULL;
    WNDCLASSW window_class = {0};
    HANDLE mutex;
    int result = 1;
    HRESULT ole;
    (void)previous; (void)arguments; (void)show;
    mutex = CreateMutexW(NULL, TRUE, L"Local\\ExpansionHunterInstaller");
    if (!mutex) return 1;
    if (GetLastError() == ERROR_ALREADY_EXISTS) {
        HWND existing = FindWindowW(NULL, L"EXPANSION HUNTER");
        if (existing) { ShowWindow(existing, SW_RESTORE); SetForegroundWindow(existing); }
        CloseHandle(mutex); return 0;
    }
    window_class.lpfnWndProc = window_proc; window_class.hInstance = instance;
    window_class.lpszClassName = L"ExpansionHunterInstallWindow";
    window_class.hCursor = LoadCursorW(NULL, IDC_ARROW);
    window_class.hIcon = LoadIconW(instance, MAKEINTRESOURCEW(1));
    RegisterClassW(&window_class);
    g_window = CreateWindowW(window_class.lpszClassName, L"EXPANSION HUNTER",
        WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU, CW_USEDEFAULT, CW_USEDEFAULT,
        490, 180, NULL, NULL, instance, NULL);
    ShowWindow(g_window, SW_SHOW); UpdateWindow(g_window);
    ole = OleInitialize(NULL);
    if (FAILED(ole)) { fail(L"The Windows interface could not be opened."); goto cleanup; }
    if (FAILED(SHGetKnownFolderPath(&FOLDERID_LocalAppData, KF_FLAG_CREATE, NULL, &local_data)) ||
        !join(app_root, 32768, local_data, L"ExpansionHunter") ||
        !join(releases, 32768, app_root, L"releases") ||
        !join(g_release, 32768, releases, EH_RELEASE_ID) ||
        !join(launcher, 32768, app_root, L"ExpansionHunter.exe") ||
        !join(temporary, 32768, app_root, L"ExpansionHunter.exe.new") ||
        !ensure_directory(app_root)) {
        fail(L"Your application folder could not be opened."); goto ole_cleanup;
    }
    if (!open_launcher_log(app_root)) {
        fail(L"Your application log folder could not be opened."); goto ole_cleanup;
    }
    if (!install_payload(instance)) {
        if (!g_cancelled) fail(L"The application could not be fully installed.");
        goto ole_cleanup;
    }
    if (!GetModuleFileNameW(instance, self, 32768)) {
        fail(L"The application launcher could not be found."); goto ole_cleanup;
    }
    if (_wcsicmp(self, launcher)) {
        if (!CopyFileW(self, temporary, FALSE) ||
            !MoveFileExW(temporary, launcher, MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)) {
            fail(L"The desktop application could not be saved."); goto ole_cleanup;
        }
    }
    if (!install_shortcut(launcher, app_root)) {
        fail(L"The desktop shortcut could not be created."); goto ole_cleanup;
    }
    if (g_cancelled) goto ole_cleanup;
    if (!eh_ensure_webview()) {
        if (!g_cancelled) fail(L"Microsoft WebView2 could not be prepared."
            L" Check your internet connection and reopen Expansion Hunter.");
        goto ole_cleanup;
    }
    if (!launch_desktop(app_root)) {
        fail(L"The cockpit could not be started."); goto ole_cleanup;
    }
    result = 0;
ole_cleanup:
    if (g_log != INVALID_HANDLE_VALUE) { CloseHandle(g_log); g_log = INVALID_HANDLE_VALUE; }
    if (local_data) CoTaskMemFree(local_data);
    OleUninitialize();
cleanup:
    DestroyWindow(g_window); ReleaseMutex(mutex); CloseHandle(mutex); return result;
}
