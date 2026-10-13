#ifndef EXPANSION_HUNTER_WEBVIEW_PREREQUISITE_H
#define EXPANSION_HUNTER_WEBVIEW_PREREQUISITE_H

#include <wintrust.h>
#include <softpub.h>
#include <wincrypt.h>
#include "prerequisite_policy.h"

/* Official Microsoft evergreen distribution registry locations:
 * https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/distribution
 * A native SDK version query is preferred when the bundled loader is present. */
static int eh_webview_registry_present(void) {
    const wchar_t *key = L"SOFTWARE\\Microsoft\\EdgeUpdate\\Clients\\"
                        L"{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}";
    HKEY roots[] = {HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE};
    DWORD views[] = {RRF_SUBKEY_WOW6432KEY, RRF_SUBKEY_WOW6464KEY};
    for (unsigned int root = 0; root < 2; ++root) {
        for (unsigned int view = 0; view < 2; ++view) {
            wchar_t version[128] = L"";
            DWORD bytes = sizeof version;
            if (RegGetValueW(roots[root], key, L"pv", RRF_RT_REG_SZ | views[view],
                             NULL, version, &bytes) == ERROR_SUCCESS &&
                eh_webview_version_suitable(version)) return 1;
        }
    }
    return 0;
}
typedef HRESULT (WINAPI *eh_available_webview_version)(LPCWSTR, LPWSTR *);
static int eh_webview_installed(void) {
    const wchar_t *candidates[] = {
        L"packages\\webview\\lib\\x64\\WebView2Loader.dll",
        L"packages\\webview\\lib\\WebView2Loader.dll",
        L"packages\\webview\\lib\\runtimes\\win-x64\\native\\WebView2Loader.dll"
    };
    wchar_t path[32768];
    for (unsigned int n = 0; n < sizeof candidates / sizeof candidates[0]; ++n) {
        HMODULE module;
        eh_available_webview_version query;
        LPWSTR version = NULL;
        HRESULT result;
        int suitable;
        if (!join(path, 32768, g_release, candidates[n]) || !exists(path)) continue;
        module = LoadLibraryExW(path, NULL,
            LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_DEFAULT_DIRS);
        if (!module) continue;
        query = (eh_available_webview_version)(void *)GetProcAddress(module,
            "GetAvailableCoreWebView2BrowserVersionString");
        if (!query) { FreeLibrary(module); continue; }
        result = query(NULL, &version);
        suitable = SUCCEEDED(result) && eh_webview_version_suitable(version);
        if (version) CoTaskMemFree(version);
        FreeLibrary(module);
        /* A loadable SDK's answer is authoritative. Do not hide its failure
         * behind a leftover registry entry after a damaged installation. */
        return suitable;
    }
    return eh_webview_registry_present();
}
static int eh_authenticode_microsoft(const wchar_t *path) {
    WINTRUST_FILE_INFO file = {0};
    WINTRUST_DATA trust = {0};
    GUID action = WINTRUST_ACTION_GENERIC_VERIFY_V2;
    LONG verification;
    HCERTSTORE store = NULL;
    HCRYPTMSG message = NULL;
    CMSG_SIGNER_INFO *signer = NULL;
    PCCERT_CONTEXT certificate = NULL;
    CERT_INFO identity = {0};
    DWORD size = 0;
    wchar_t publisher[256] = L"";
    wchar_t detail[256];
    int accepted = 0;
    file.cbStruct = sizeof file; file.pcwszFilePath = path;
    trust.cbStruct = sizeof trust; trust.dwUIChoice = WTD_UI_NONE;
    trust.fdwRevocationChecks = WTD_REVOKE_NONE; trust.dwUnionChoice = WTD_CHOICE_FILE;
    trust.pFile = &file; trust.dwStateAction = WTD_STATEACTION_VERIFY;
    trust.dwProvFlags = WTD_CACHE_ONLY_URL_RETRIEVAL;
    verification = WinVerifyTrust(NULL, &action, &trust);
    StringCchPrintfW(detail, 256, L"WebView2 Authenticode verification: 0x%08lx",
                      (unsigned long)verification);
    log_message(detail);
    trust.dwStateAction = WTD_STATEACTION_CLOSE;
    WinVerifyTrust(NULL, &action, &trust);
    if (verification != ERROR_SUCCESS) goto cleanup;
    if (!CryptQueryObject(CERT_QUERY_OBJECT_FILE, path,
                          CERT_QUERY_CONTENT_FLAG_PKCS7_SIGNED_EMBED,
                          CERT_QUERY_FORMAT_FLAG_BINARY, 0, NULL, NULL, NULL,
                          &store, &message, NULL) ||
        !CryptMsgGetParam(message, CMSG_SIGNER_INFO_PARAM, 0, NULL, &size) ||
        size == 0 || size > 1048576) goto cleanup;
    signer = (CMSG_SIGNER_INFO *)malloc(size);
    if (!signer || !CryptMsgGetParam(message, CMSG_SIGNER_INFO_PARAM, 0, signer, &size)) goto cleanup;
    identity.Issuer = signer->Issuer; identity.SerialNumber = signer->SerialNumber;
    certificate = CertFindCertificateInStore(store, X509_ASN_ENCODING | PKCS_7_ASN_ENCODING,
                                             0, CERT_FIND_SUBJECT_CERT, &identity, NULL);
    if (!certificate || !CertGetNameStringW(certificate, CERT_NAME_SIMPLE_DISPLAY_TYPE,
                                            0, NULL, publisher, 256)) goto cleanup;
    accepted = eh_is_microsoft_publisher(publisher);
    if (!accepted) log_message(L"WebView2 bootstrapper publisher is not Microsoft Corporation");
cleanup:
    if (certificate) CertFreeCertificateContext(certificate);
    if (signer) free(signer);
    if (message) CryptMsgClose(message);
    if (store) CertCloseStore(store, 0);
    if (!accepted) SetLastError(ERROR_ACCESS_DENIED);
    return accepted;
}
static int eh_ensure_webview(void) {
    wchar_t bootstrapper[32768], command[32768] = L"", detail[256];
    STARTUPINFOW startup = {0};
    PROCESS_INFORMATION process = {0};
    DWORD code = 0;
    ULONGLONG deadline;
    if (eh_webview_installed()) { log_message(L"WebView2 runtime detected"); return 1; }
    if (!join(bootstrapper, 32768, g_release, L"runtime\\WebView2Setup.exe") ||
        !exists(bootstrapper)) {
        log_message(L"WebView2 missing and official bootstrapper not bundled");
        SetLastError(ERROR_FILE_NOT_FOUND); return 0;
    }
    status(L"Checking Microsoft WebView2…");
    if (!eh_authenticode_microsoft(bootstrapper)) return 0;
    if (!append_arg(command, 32768, bootstrapper) ||
        !append_arg(command, 32768, L"/silent") ||
        !append_arg(command, 32768, L"/install")) return 0;
    startup.cb = sizeof startup; startup.dwFlags = STARTF_USESHOWWINDOW;
    startup.wShowWindow = SW_HIDE;
    status(L"Installing Microsoft WebView2 for your application…"
           L"\nAn internet connection is required.");
    log_message(L"Starting signed Microsoft WebView2 bootstrapper without elevation");
    if (!CreateProcessW(bootstrapper, command, NULL, NULL, FALSE, CREATE_NO_WINDOW,
                         NULL, g_release, &startup, &process)) return 0;
    CloseHandle(process.hThread);
    deadline = GetTickCount64() + 900000;
    while (WaitForSingleObject(process.hProcess, 150) == WAIT_TIMEOUT) {
        pump_messages();
        if (g_cancelled || GetTickCount64() > deadline) {
            TerminateProcess(process.hProcess, ERROR_CANCELLED);
            CloseHandle(process.hProcess);
            SetLastError(g_cancelled ? ERROR_CANCELLED : ERROR_TIMEOUT); return 0;
        }
    }
    if (!GetExitCodeProcess(process.hProcess, &code)) { CloseHandle(process.hProcess); return 0; }
    CloseHandle(process.hProcess);
    StringCchPrintfW(detail, 256, L"Microsoft WebView2 installer exit code: %lu", code);
    log_message(detail);
    if (code != 0 && code != 3010) { SetLastError(code); return 0; }
    deadline = GetTickCount64() + 15000;
    while (GetTickCount64() < deadline) {
        if (eh_webview_installed()) return 1;
        pump_messages();
        if (g_cancelled) { SetLastError(ERROR_CANCELLED); return 0; }
        Sleep(200);
    }
    log_message(L"WebView2 installer finished but runtime is still unavailable");
    SetLastError(code == 3010 ? ERROR_SUCCESS_REBOOT_REQUIRED : ERROR_NOT_READY); return 0;
}
#endif
