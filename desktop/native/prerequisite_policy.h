#ifndef EXPANSION_HUNTER_PREREQUISITE_POLICY_H
#define EXPANSION_HUNTER_PREREQUISITE_POLICY_H
#include <stddef.h>
#include <wchar.h>

/* Pure decision helpers, executed in host C tests. They do not replace Windows
 * Authenticode verification or claim that a registry entry loads successfully. */
static int eh_webview_version_suitable(const wchar_t *value) {
    unsigned int major = 0, component = 0, digits = 0;
    size_t n = 0;
    if (!value || !value[0]) return 0;
    while (value[n]) {
        wchar_t c = value[n++];
        if (c >= L'0' && c <= L'9') {
            if (++digits > 6) return 0;
            if (component == 0) major = major * 10 + (unsigned int)(c - L'0');
        } else if (c == L'.' && digits && component < 3) {
            ++component; digits = 0;
        } else return 0;
    }
    /* WebView2's initial evergreen runtime is Chromium 86. Current SDK features
     * are still checked by the application's real environment creation. */
    return component == 3 && digits && major >= 86;
}
static int eh_is_microsoft_publisher(const wchar_t *publisher) {
    return publisher && wcscmp(publisher, L"Microsoft Corporation") == 0;
}
#endif
