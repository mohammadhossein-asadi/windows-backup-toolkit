/* Tiny self-extracting launcher for the portable Windows build.
 *
 * The result is:  launcher.exe + payload.zip + "WBKZIP01" + uint64 offset
 * On start it unpacks the embedded zip to %LOCALAPPDATA%\WindowsBackupToolkit\app-<ver>
 * and starts the bundled pythonw.exe with launcher.pyw.
 */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include "miniz.h"

#ifndef APP_VERSION
#define APP_VERSION "0.0.0"
#endif

#define BIG (MAX_PATH * 8)

static void fail(const char *msg) {
    MessageBoxA(NULL, msg, "Windows Backup & Restore Toolkit", MB_ICONERROR | MB_OK);
    ExitProcess(1);
}

static void mkdirs(const char *path) {
    char tmp[BIG];
    strncpy(tmp, path, sizeof(tmp) - 1);
    tmp[sizeof(tmp) - 1] = 0;
    for (char *p = tmp + 3; *p; ++p) {
        if (*p == '\\' || *p == '/') {
            char c = *p;
            *p = 0;
            CreateDirectoryA(tmp, NULL);
            *p = c;
        }
    }
    CreateDirectoryA(tmp, NULL);
}

int WINAPI WinMain(HINSTANCE hInst, HINSTANCE hPrev, LPSTR lpCmd, int nShow) {
    (void)hInst; (void)hPrev; (void)lpCmd; (void)nShow;

    char exe[BIG];
    if (!GetModuleFileNameA(NULL, exe, sizeof(exe))) fail("Cannot locate this program file.");

    FILE *f = fopen(exe, "rb");
    if (!f) fail("Cannot open this program file.");
    _fseeki64(f, 0, SEEK_END);
    long long total = _ftelli64(f);
    if (total < 64) { fclose(f); fail("The program file is incomplete."); }

    unsigned char tail[16];
    _fseeki64(f, total - 16, SEEK_SET);
    if (fread(tail, 1, 16, f) != 16) { fclose(f); fail("The program file is incomplete."); }
    if (memcmp(tail, "WBKZIP01", 8) != 0) {
        fclose(f);
        fail("The embedded application files are missing or corrupted.");
    }
    uint64_t off = 0;
    memcpy(&off, tail + 8, 8);
    if (off + 16 > (uint64_t)total) { fclose(f); fail("The embedded application files are corrupted."); }
    size_t size = (size_t)((uint64_t)total - 16 - off);

    unsigned char *buf = (unsigned char *)malloc(size);
    if (!buf) { fclose(f); fail("Not enough memory to unpack the application."); }
    _fseeki64(f, (long long)off, SEEK_SET);
    if (fread(buf, 1, size, f) != size) {
        fclose(f); free(buf);
        fail("Could not read the embedded application files.");
    }
    fclose(f);

    mz_zip_archive zip;
    memset(&zip, 0, sizeof(zip));
    if (!mz_zip_reader_init_mem(&zip, buf, size, 0)) {
        free(buf);
        fail("Could not open the embedded application archive.");
    }

    const char *local = getenv("LOCALAPPDATA");
    char dest[BIG];
    if (local && *local) {
        snprintf(dest, sizeof(dest), "%s\\WindowsBackupToolkit\\app-" APP_VERSION, local);
    } else {
        char tmp[MAX_PATH];
        if (!GetTempPathA(sizeof(tmp), tmp)) strcpy(tmp, ".\\");
        snprintf(dest, sizeof(dest), "%sWindowsBackupToolkit-" APP_VERSION, tmp);
    }
    mkdirs(dest);

    /* Short paths keep the extraction working with non-ASCII profile names. */
    char destA[BIG];
    if (!GetShortPathNameA(dest, destA, sizeof(destA))) {
        strncpy(destA, dest, sizeof(destA) - 1);
        destA[sizeof(destA) - 1] = 0;
    }

    mz_uint n = mz_zip_reader_get_num_files(&zip);
    for (mz_uint i = 0; i < n; ++i) {
        char name[1024];
        if (mz_zip_reader_get_filename(&zip, i, name, sizeof(name)) == 0) continue;
        for (char *p = name; *p; ++p) if (*p == '/') *p = '\\';

        char full[BIG];
        snprintf(full, sizeof(full), "%s\\%s", destA, name);
        if (mz_zip_reader_is_file_a_directory(&zip, i)) {
            mkdirs(full);
            continue;
        }
        char parent[BIG];
        strncpy(parent, full, sizeof(parent) - 1);
        parent[sizeof(parent) - 1] = 0;
        char *slash = strrchr(parent, '\\');
        if (slash) { *slash = 0; mkdirs(parent); }

        if (!mz_zip_reader_extract_to_file(&zip, i, full, 0)) {
            char msg[BIG];
            snprintf(msg, sizeof(msg),
                     "Could not unpack:\n%s\n\nTry the portable .zip version instead.", full);
            mz_zip_reader_end(&zip);
            free(buf);
            fail(msg);
        }
    }
    mz_zip_reader_end(&zip);
    free(buf);

    char exePath[BIG], cmdline[BIG * 2];
    snprintf(exePath, sizeof(exePath), "%s\\runtime\\pythonw.exe", dest);
    snprintf(cmdline, sizeof(cmdline), "\"%s\" \"%s\\launcher.pyw\"", exePath, dest);
    if (GetFileAttributesA(exePath) == INVALID_FILE_ATTRIBUTES)
        fail("The bundled Python runtime was not unpacked correctly.");

    STARTUPINFOA si;
    PROCESS_INFORMATION pi;
    memset(&si, 0, sizeof(si));
    si.cb = sizeof(si);
    memset(&pi, 0, sizeof(pi));
    if (!CreateProcessA(exePath, cmdline, NULL, NULL, FALSE, 0, NULL, dest, &si, &pi))
        fail("Could not start the toolkit (CreateProcess failed).");
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return 0;
}
