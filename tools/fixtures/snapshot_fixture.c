/* 32-bit fixture target for the host-side snapshot PoC (issue #33).
 *
 * It allocates a writable block, fills it with a deterministic pattern, starts a
 * worker thread that keeps mutating the block, prints its own PID and then idles.
 * The snapshot tool must suspend the worker before capturing, so the image is
 * stable even though the target is otherwise busy.
 */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdio.h>

static const size_t BLOCK_BYTES = 6u * 1024u * 1024u;
static unsigned char *g_block = NULL;
static volatile LONG g_spins = 0;

static DWORD WINAPI worker(LPVOID unused) {
    unsigned x = 0x12345678u;
    (void)unused;
    for (;;) {
        x = x * 1664525u + 1013904223u;
        g_block[(x >> 8) % BLOCK_BYTES] = (unsigned char)(x >> 24);
        InterlockedIncrement(&g_spins);
    }
}

int main(void) {
    size_t i;
    HANDLE thread;
    g_block = (unsigned char *)VirtualAlloc(NULL, BLOCK_BYTES,
                                            MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    if (g_block == NULL) {
        fprintf(stderr, "VirtualAlloc failed: %lu\n", (unsigned long)GetLastError());
        return 2;
    }
    for (i = 0; i < BLOCK_BYTES; ++i) {
        g_block[i] = (unsigned char)(i * 31u + 7u);
    }
    printf("PID=%lu\n", (unsigned long)GetCurrentProcessId());
    fflush(stdout);
    thread = CreateThread(NULL, 0, worker, NULL, 0, NULL);
    if (thread == NULL) {
        fprintf(stderr, "CreateThread failed: %lu\n", (unsigned long)GetLastError());
        return 3;
    }
    for (;;) {
        Sleep(50);
    }
}
