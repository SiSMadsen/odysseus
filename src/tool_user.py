"""Run the agent's commands as a separate tool user (docs/creator-plan.md, Phase 5a).

In Docker, docker/entrypoint.sh creates the tool user (default `odytools`),
one sudo rule that lets the app user run commands as it, and a group that
shares the agent's work folder; it then sets ODYSSEUS_TOOL_USER (and
ODYSSEUS_TOOL_GROUP) for the app. With those set, bash, python and
background jobs start through `sudo -u <tool user>` with a clean environment,
so they can't read the app's key, database or settings, can't see the
server's API keys, and are refused by the host helpers (which accept only
the app user's uid).

Without ODYSSEUS_TOOL_USER (outside Docker, Windows, or set-up disabled or
failed) every function here leaves things as they were.

Inside the container, `python -m src.tool_user --check` checks the set-up.
"""

import os
import shlex
import subprocess
import sys
from typing import Dict, Iterable, List, Optional, Set

# The environment a tool command gets: nothing from the server's environment
# (API keys, the admin password, ...) except these harmless settings.
_PASS_ENV = ("TERM", "COLUMNS", "LINES", "LANG", "LC_ALL", "LC_CTYPE", "TZ",
             "PYTHONIOENCODING", "PYTHONUTF8", "NO_COLOR", "FORCE_COLOR", "HOME")
TOOL_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
# Files the tool user creates: group-writable (shared work folder), not
# readable by anyone else.
TOOL_UMASK = "007"

_shared_paths: Set[str] = set()


def tool_user() -> str:
    """The tool user's name, or "" when commands run as the app user."""
    if sys.platform == "win32":
        return ""
    return (os.environ.get("ODYSSEUS_TOOL_USER") or "").strip()


def tool_group() -> str:
    return (os.environ.get("ODYSSEUS_TOOL_GROUP") or "").strip()


def clean_env(env: Optional[Dict[str, str]], user: str) -> Dict[str, str]:
    out = {k: str(v) for k, v in (env or {}).items() if k in _PASS_ENV and v is not None}
    out.setdefault("HOME", f"/home/{user}")
    out.setdefault("LANG", "C.UTF-8")
    out.update(USER=user, LOGNAME=user, PATH=TOOL_PATH, SHELL="/bin/bash")
    return out


def wrap_argv(argv: Iterable[str], env: Optional[Dict[str, str]] = None) -> List[str]:
    """`argv`, run as the tool user with a clean environment and umask 007.
    Unchanged when there is no tool user."""
    argv = list(argv)
    user = tool_user()
    if not user:
        return argv
    pairs = [f"{k}={v}" for k, v in clean_env(env, user).items()]
    return ["sudo", "-n", "-u", user, "--", "/usr/bin/env", "-i", *pairs,
            "/bin/sh", "-c", f'umask {TOOL_UMASK}; exec "$@"', "odysseus-tool", *argv]


def wrap_shell(argv: Iterable[str], env: Optional[Dict[str, str]] = None) -> str:
    """wrap_argv as one shell-quoted command line (for scripts)."""
    return shlex.join(wrap_argv(argv, env))


# ---------------------------------------------------------------------------
# Killing what a command started
# ---------------------------------------------------------------------------

def _proc_stat(pid: int):
    """(ppid, uid) of a process from /proc, or None."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            stat = f.read().decode("utf-8", "replace")
        ppid = int(stat.rsplit(")", 1)[1].split()[1])
        uid = os.stat(f"/proc/{pid}").st_uid
        return ppid, uid
    except (OSError, ValueError, IndexError):
        return None


def descendants(pid: int) -> List[int]:
    """All processes below `pid`, from /proc (POSIX only)."""
    children: Dict[int, List[int]] = {}
    try:
        names = os.listdir("/proc")
    except OSError:
        return []
    for name in names:
        if not name.isdigit():
            continue
        info = _proc_stat(int(name))
        if info:
            children.setdefault(info[0], []).append(int(name))
    out, todo = [], [pid]
    while todo:
        for child in children.get(todo.pop(), []):
            if child not in out:
                out.append(child)
                todo.append(child)
    return out


def kill_tool_tree(pid: Optional[int]) -> None:
    """Kill every process below `pid` that runs as the tool user. The app
    user can't signal those itself, so this goes through the same sudo rule.
    A no-op without a tool user."""
    user = tool_user()
    if not pid or not user:
        return
    import pwd
    try:
        tool_uid = pwd.getpwnam(user).pw_uid
    except KeyError:
        return
    for _ in range(2):   # a second pass for anything forked meanwhile
        pids = [p for p in descendants(pid) if (_proc_stat(p) or (0, -1))[1] == tool_uid]
        if not pids:
            return
        try:
            subprocess.run(["sudo", "-n", "-u", user, "--", "/bin/kill", "-KILL", *map(str, pids)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return


def kill_process(proc) -> None:
    """Kill a started command: what it runs as the tool user, then the
    process itself (sudo, which the app user may signal)."""
    try:
        kill_tool_tree(proc.pid)
    finally:
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass


# ---------------------------------------------------------------------------
# Sharing a workspace folder with the tool user
# ---------------------------------------------------------------------------

def share_with_tools(path: str) -> None:
    """Give the tool group read/write on `path` (and new files in it), once per
    path per process. Only the owner can, so other people's files stay as they
    are; the agent then gets "Permission denied" there."""
    group = tool_group()
    if not tool_user() or not group or not path or path in _shared_paths:
        return
    _shared_paths.add(path)
    acl = ["-m", f"g:{group}:rwX", "-m", f"d:g:{group}:rwX"]
    try:
        # The folder itself now (instant), so the first command can start
        # there; everything inside in the background, without blocking the
        # server on a big tree.
        subprocess.run(["setfacl", *acl, path], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=10)
        subprocess.Popen(["setfacl", "-R", *acl, path], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, start_new_session=True)
    except (OSError, subprocess.SubprocessError):
        pass


# ---------------------------------------------------------------------------
# Self-check: python -m src.tool_user --check (inside the container)
# ---------------------------------------------------------------------------

def _as_tool(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(wrap_argv(["/bin/sh", "-c", script], {"HOME": "/tmp"}),
                          capture_output=True, text=True, timeout=30)


def check() -> int:
    from src.constants import AGENT_WORKSPACE_DIR, DATA_DIR
    results = []

    def report(ok: bool, what: str, detail: str = "") -> None:
        results.append(ok)
        print(f"{'PASS' if ok else 'FAIL'}  {what}" + (f"  ({detail})" if detail else ""))

    user = tool_user()
    report(bool(user), "the tool user is configured", user or "ODYSSEUS_TOOL_USER is not set")
    if not user:
        return 1
    r = _as_tool("id -un")
    report(r.returncode == 0 and r.stdout.strip() == user, "commands run as the tool user",
           (r.stdout or r.stderr).strip())
    for name in (".app_key", "app.db", "auth.json", "settings.json", "sessions.json", "memory.json"):
        path = os.path.join(DATA_DIR, name)
        if not os.path.exists(path):
            continue
        r = _as_tool(f"cat {shlex.quote(path)} > /dev/null")
        report(r.returncode != 0, f"the tool user can't read data/{name}")
    r = _as_tool("ls " + shlex.quote(DATA_DIR))
    report(r.returncode != 0, "the tool user can't list data/")
    probe = os.path.join(AGENT_WORKSPACE_DIR, ".tool-user-check")
    r = _as_tool(f"echo ok > {shlex.quote(probe)}")
    readable = False
    try:
        with open(probe) as f:
            readable = f.read().strip() == "ok"
        os.remove(probe)
    except OSError:
        pass
    report(r.returncode == 0 and readable, "the tool user and the app share data/agent_workspace",
           r.stderr.strip())
    secret_names = [k for k in os.environ if any(s in k.upper() for s in ("KEY", "TOKEN", "PASSWORD", "SECRET"))]
    r = _as_tool("env")
    leaked = [k for k in secret_names if f"{k}=" in r.stdout]
    report(not leaked, "the server's secret environment variables don't reach tools", ", ".join(leaked))
    from src import creator_host_helper
    sock = creator_host_helper.socket_path()
    if os.path.exists(sock):
        code = ("import socket; s = socket.socket(socket.AF_UNIX); "
                f"s.connect({sock!r}); s.sendall(b'{{\"type\": \"hello\"}}\\n'); "
                "print(s.makefile().readline())")
        r = _as_tool("python3 -c " + shlex.quote(code))
        refused = "not allowed" in r.stdout or r.returncode != 0
        report(refused, "the host helper refuses the tool user", (r.stdout or r.stderr).strip()[:120])
    print()
    print("All checks passed." if all(results) else "Some checks FAILED.")
    return 0 if all(results) else 1


if __name__ == "__main__":
    if "--check" in sys.argv[1:]:
        sys.exit(check())
    print(__doc__)
