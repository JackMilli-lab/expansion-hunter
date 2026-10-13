#ifndef EXPANSION_HUNTER_PAYLOAD_H
#define EXPANSION_HUNTER_PAYLOAD_H

/* A deliberately small ZIP_STORED reader. The release builder must disable
 * compression. ZIP64, links, encrypted entries and traversal are rejected. */
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <stdio.h>
#include <wchar.h>

typedef int (*eh_zip_visitor)(const char *, const unsigned char *, size_t, int, void *);

static uint16_t eh_u16(const unsigned char *p) {
    return (uint16_t)((uint16_t)p[0] | ((uint16_t)p[1] << 8));
}
static uint32_t eh_u32(const unsigned char *p) {
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) |
           ((uint32_t)p[3] << 24);
}
static int eh_bounds(size_t start, size_t length, size_t limit) {
    return start <= limit && length <= limit - start;
}
static uint32_t eh_crc32(const unsigned char *data, size_t length) {
    uint32_t crc = 0xffffffffu;
    size_t n;
    for (n = 0; n < length; ++n) {
        unsigned int bit;
        crc ^= data[n];
        for (bit = 0; bit < 8; ++bit)
            crc = (crc >> 1) ^ (0xedb88320u & (0u - (crc & 1u)));
    }
    return crc ^ 0xffffffffu;
}
static int eh_ascii_equal(const char *left, size_t size, const char *right) {
    size_t n;
    if (strlen(right) != size) return 0;
    for (n = 0; n < size; ++n) {
        unsigned char c = (unsigned char)left[n];
        if (c >= 'a' && c <= 'z') c = (unsigned char)(c - 'a' + 'A');
        if (c != (unsigned char)right[n]) return 0;
    }
    return 1;
}
static int eh_reserved_component(const char *value, size_t length) {
    size_t stem = 0;
    while (stem < length && value[stem] != '.') ++stem;
    if (eh_ascii_equal(value, stem, "CON") || eh_ascii_equal(value, stem, "PRN") ||
        eh_ascii_equal(value, stem, "AUX") || eh_ascii_equal(value, stem, "NUL") ||
        eh_ascii_equal(value, stem, "CONIN$") || eh_ascii_equal(value, stem, "CONOUT$")) return 1;
    if (stem == 4 && value[3] >= '1' && value[3] <= '9' &&
        (eh_ascii_equal(value, 3, "COM") || eh_ascii_equal(value, 3, "LPT"))) return 1;
    return 0;
}
static int eh_valid_relative_path(const char *name, size_t length) {
    size_t n = 0, segment = 0;
    if (!length || length >= 1024 || name[0] == '/' || name[0] == '\\') return 0;
    for (; n <= length; ++n) {
        unsigned char c = n == length ? '/' : (unsigned char)name[n];
        if (c == '/') {
            size_t count = n - segment;
            if (!count) return 0;
            if ((count == 1 && name[segment] == '.') ||
                (count == 2 && name[segment] == '.' && name[segment + 1] == '.') ||
                name[n - 1] == '.' || name[n - 1] == ' ' ||
                eh_reserved_component(name + segment, count)) return 0;
            segment = n + 1;
            if (segment == length) return 1; /* directory's final slash */
        } else if (c < 32 || c == '\\' || c == ':' || c == '*' || c == '?' ||
                   c == '"' || c == '<' || c == '>' || c == '|') return 0;
    }
    return 1;
}
static int eh_zip_error(char *error, size_t capacity, const char *message) {
    if (capacity) snprintf(error, capacity, "%s", message);
    return 0;
}
static int eh_zip_walk(const unsigned char *data, size_t size,
                       eh_zip_visitor visit, void *context,
                       char *error, size_t error_capacity) {
    size_t end, lower, cursor, directory_end;
    uint16_t count, index;
    if (size < 22 || size > 0x7fffffffu)
        return eh_zip_error(error, error_capacity, "Invalid payload size");
    lower = size > 65557 ? size - 65557 : 0;
    end = size - 22;
    for (;;) {
        if (eh_u32(data + end) == 0x06054b50u &&
            eh_bounds(end, 22, size) && end + 22 + eh_u16(data + end + 20) == size) break;
        if (end == lower) return eh_zip_error(error, error_capacity, "ZIP directory missing");
        --end;
    }
    count = eh_u16(data + end + 10);
    if (eh_u16(data + end + 4) || eh_u16(data + end + 6) || !count || count == 0xffffu ||
        eh_u16(data + end + 8) != count)
        return eh_zip_error(error, error_capacity, "Split or ZIP64 payload is unsupported");
    cursor = eh_u32(data + end + 16);
    directory_end = cursor + (size_t)eh_u32(data + end + 12);
    if (directory_end != end || !eh_bounds(cursor, directory_end - cursor, end))
        return eh_zip_error(error, error_capacity, "Invalid ZIP directory bounds");
    for (index = 0; index < count; ++index) {
        uint16_t flags, method, name_size, extra_size, comment_size;
        uint32_t compressed, expanded, local, expected_crc, attributes;
        size_t header_size, file_start;
        char name[1024];
        int directory;
        if (!eh_bounds(cursor, 46, directory_end) || eh_u32(data + cursor) != 0x02014b50u)
            return eh_zip_error(error, error_capacity, "Invalid ZIP entry");
        flags = eh_u16(data + cursor + 8); method = eh_u16(data + cursor + 10);
        expected_crc = eh_u32(data + cursor + 16);
        compressed = eh_u32(data + cursor + 20); expanded = eh_u32(data + cursor + 24);
        name_size = eh_u16(data + cursor + 28); extra_size = eh_u16(data + cursor + 30);
        comment_size = eh_u16(data + cursor + 32); attributes = eh_u32(data + cursor + 38);
        local = eh_u32(data + cursor + 42);
        header_size = 46u + (size_t)name_size + extra_size + comment_size;
        if ((flags & 1u) || method || compressed != expanded || expanded > 0x20000000u ||
            eh_u16(data + cursor + 34) || (attributes >> 16 & 0170000u) == 0120000u ||
            !eh_bounds(cursor, header_size, directory_end) ||
            !eh_valid_relative_path((const char *)(data + cursor + 46), name_size))
            return eh_zip_error(error, error_capacity, "Unsafe or compressed payload entry");
        memcpy(name, data + cursor + 46, name_size); name[name_size] = 0;
        directory = name[name_size - 1] == '/';
        if (!eh_bounds(local, 30, cursor) || eh_u32(data + local) != 0x04034b50u ||
            eh_u16(data + local + 8) != method || eh_u16(data + local + 6) != flags ||
            eh_u16(data + local + 26) != name_size ||
            !eh_bounds(local + 30, name_size + (size_t)eh_u16(data + local + 28), cursor) ||
            memcmp(data + local + 30, name, name_size))
            return eh_zip_error(error, error_capacity, "ZIP local header mismatch");
        file_start = local + 30u + name_size + (size_t)eh_u16(data + local + 28);
        if (!eh_bounds(file_start, expanded, cursor) ||
            eh_crc32(data + file_start, expanded) != expected_crc)
            return eh_zip_error(error, error_capacity, "Payload CRC validation failed");
        if (!visit(name, data + file_start, expanded, directory, context))
            return eh_zip_error(error, error_capacity, "Payload write cancelled or failed");
        cursor += header_size;
    }
    if (cursor != directory_end)
        return eh_zip_error(error, error_capacity, "ZIP entry count mismatch");
    return 1;
}

/* CommandLineToArgvW-compatible quoting, including quotes and trailing slashes.
 * Always quote; returns required WCHAR count including the terminating NUL. */
static size_t eh_quote_windows_arg(const wchar_t *input, wchar_t *output, size_t capacity) {
    size_t used = 0, n = 0;
#define EH_PUT(value) do { if (output && used < capacity) output[used] = (value); ++used; } while (0)
    EH_PUT(L'"');
    while (input[n]) {
        size_t slashes = 0, k;
        while (input[n] == L'\\') { ++slashes; ++n; }
        if (!input[n] || input[n] == L'"') {
            for (k = 0; k < slashes * 2; ++k) EH_PUT(L'\\');
            if (!input[n]) break;
            EH_PUT(L'\\'); EH_PUT(L'"'); ++n;
        } else {
            for (k = 0; k < slashes; ++k) EH_PUT(L'\\');
            EH_PUT(input[n]); ++n;
        }
    }
    EH_PUT(L'"'); EH_PUT(0);
#undef EH_PUT
    return used;
}
#endif
