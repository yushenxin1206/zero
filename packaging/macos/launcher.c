/*
 * Zero.app 的最小启动器。
 *
 * 只做三件事：定位 bundle、把内嵌解释器所需的环境准备好、exec 成
 * `python -m zero`。不承载任何业务逻辑，也不解析参数（--web/--cli/
 * --feishu 原样透传）。
 */

#include <limits.h>
#include <mach-o/dyld.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <syslog.h>
#include <unistd.h>

#define PYTHON_REL "Contents/Resources/python/bin/python3.14"
#define DEFAULT_PATH "/usr/bin:/bin:/usr/sbin:/sbin"

static void fail(const char *message, const char *detail) {
    char buf[PATH_MAX + 512];
    snprintf(buf, sizeof(buf), "%s%s%s", message, detail ? ": " : "",
             detail ? detail : "");
    syslog(LOG_ERR, "Zero launcher: %s", buf);
    fprintf(stderr, "Zero launcher: %s\n", buf);

    /* Finder 启动时没有终端，错误留在数据根的日志目录里方便排查。 */
    const char *home = getenv("HOME");
    if (home != NULL) {
        char log_path[PATH_MAX];
        snprintf(log_path, sizeof(log_path),
                 "%s/.zero/logs/launcher-error.log", home);
        FILE *log = fopen(log_path, "a");
        if (log != NULL) {
            fprintf(log, "%s\n", buf);
            fclose(log);
        }
    }
    exit(127);
}

static int bundle_root(char *out, size_t capacity) {
    char executable[PATH_MAX];
    uint32_t size = (uint32_t)sizeof(executable);
    if (_NSGetExecutablePath(executable, &size) != 0) {
        return -1;
    }
    char resolved[PATH_MAX];
    if (realpath(executable, resolved) == NULL) {
        return -1;
    }
    char *marker = strstr(resolved, "/Contents/MacOS/");
    if (marker == NULL) {
        return -1;
    }
    size_t length = (size_t)(marker - resolved);
    if (length + 1 > capacity) {
        return -1;
    }
    memcpy(out, resolved, length);
    out[length] = '\0';
    return 0;
}

int main(int argc, char **argv) {
    char root[PATH_MAX];
    char resources[PATH_MAX];
    char python[PATH_MAX];
    char app[PATH_MAX];
    char site[PATH_MAX];
    char python_path[PATH_MAX * 2];
    char search_path[PATH_MAX * 2];

    if (bundle_root(root, sizeof(root)) != 0) {
        fail("cannot locate the application bundle", NULL);
    }

    snprintf(resources, sizeof(resources), "%s/Contents/Resources", root);
    snprintf(python, sizeof(python), "%s/%s", root, PYTHON_REL);
    snprintf(app, sizeof(app), "%s/app", resources);
    snprintf(site, sizeof(site), "%s/site-packages", resources);

    if (access(python, X_OK) != 0) {
        fail("embedded interpreter is missing", python);
    }
    if (access(app, R_OK) != 0 || access(site, R_OK) != 0) {
        fail("bundled Python paths are incomplete", resources);
    }

    /* 不能继承宿主可能残留的 PYTHONHOME/PYTHONPATH，否则会指回构建机。 */
    unsetenv("PYTHONHOME");
    unsetenv("PYTHONSTARTUP");
    snprintf(python_path, sizeof(python_path), "%s:%s", app, site);
    setenv("PYTHONPATH", python_path, 1);
    setenv("PYTHONDONTWRITEBYTECODE", "1", 1);
    setenv("PYTHONNOUSERSITE", "1", 1);

    /*
     * Finder 启动时 PATH 只有系统目录：把内嵌解释器放最前，技能脚本里的
     * `python` 才解析得到；Homebrew 两个常见前缀补上，行为和终端一致。
     */
    const char *inherited = getenv("PATH");
    if (inherited == NULL || inherited[0] == '\0') {
        inherited = DEFAULT_PATH;
    }
    snprintf(search_path, sizeof(search_path),
             "%s/python/bin:/opt/homebrew/bin:/usr/local/bin:%s", resources,
             inherited);
    setenv("PATH", search_path, 1);

    const char *home = getenv("HOME");
    if (home != NULL && chdir(home) != 0) {
        /* 切不过去不算致命：数据根仍然由代码里的绝对路径决定。 */
        syslog(LOG_WARNING, "Zero launcher: cannot chdir to %s", home);
    }

    char **args = calloc((size_t)argc + 3, sizeof(char *));
    if (args == NULL) {
        fail("out of memory", NULL);
    }
    args[0] = "python3";
    args[1] = "-m";
    args[2] = "zero";
    for (int i = 1; i < argc; i++) {
        args[i + 2] = argv[i];
    }
    args[argc + 2] = NULL;

    execv(python, args);
    fail("cannot start the embedded interpreter", python);
    return 127;
}
