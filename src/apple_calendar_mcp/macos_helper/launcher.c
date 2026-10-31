/*
 * Launcher for "Apple Calendar MCP.app".
 *
 * macOS asks for calendar permission on behalf of the *responsible* app: the
 * GUI app that started the process (Claude, Terminal...). Since macOS 14 that
 * app must declare NSCalendarsFullAccessUsageDescription, or the request is
 * refused without a prompt. Terminal and Claude don't declare it.
 *
 * This launcher lives inside a small app bundle that does declare it. It
 * re-launches itself with "disclaim" set, which makes the bundle responsible
 * for its own permissions, then starts the real server as a child. The child
 * inherits the bundle's identity, so macOS asks for "Apple Calendar MCP".
 *
 * Usage: apple-calendar-mcp-launcher <program> [args...]
 * stdin/stdout/stderr pass straight through, so MCP over stdio works.
 */
#include <mach-o/dyld.h>
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>

extern char **environ;
/* Private but stable libsystem API, used by Terminal, LLDB and others. */
int responsibility_spawnattrs_setdisclaim(posix_spawnattr_t *attrs, int disclaim);

#define STAGE_ENV "APPLE_CALENDAR_MCP_LAUNCHER_STAGE"

static pid_t child = 0;

static void forward(int sig) {
    if (child > 0) kill(child, sig);
}

static int run(const char *path, char *const argv[], int disclaim) {
    posix_spawnattr_t attrs;
    posix_spawnattr_init(&attrs);
    if (disclaim) responsibility_spawnattrs_setdisclaim(&attrs, 1);
    int rc = posix_spawn(&child, path, NULL, &attrs, argv, environ);
    posix_spawnattr_destroy(&attrs);
    if (rc != 0) {
        fprintf(stderr, "apple-calendar-mcp-launcher: cannot start %s: %s\n", path, strerror(rc));
        return 127;
    }
    signal(SIGTERM, forward);
    signal(SIGINT, forward);
    signal(SIGHUP, forward);
    int status;
    while (waitpid(child, &status, 0) < 0) {
    }
    return WIFEXITED(status) ? WEXITSTATUS(status) : 128 + WTERMSIG(status);
}

int main(int argc, char *argv[]) {
    if (argc < 2) {
        fprintf(stderr, "usage: %s <program> [args...]\n", argv[0]);
        return 2;
    }
    if (getenv(STAGE_ENV) == NULL) {
        char self[4096];
        uint32_t size = sizeof self;
        if (_NSGetExecutablePath(self, &size) != 0) return 127;
        setenv(STAGE_ENV, "1", 1);
        return run(self, argv, 1);
    }
    unsetenv(STAGE_ENV);
    return run(argv[1], argv + 1, 0);
}
