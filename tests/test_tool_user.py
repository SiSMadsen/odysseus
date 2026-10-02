"""Phase 5a (docs/creator-plan.md): the agent's commands run as a separate
tool user.

No passwordless sudo here, so a fake `sudo` first on PATH checks its
arguments, logs them, and runs the command as the same user. That still
exercises the real paths (wrapper, clean environment, umask, bash, python,
background jobs, killing) end to end; the real user switch and the data/
lockdown are checked in the container with `python -m src.tool_user --check`.
"""

import asyncio
import os
import pwd
import stat
import sys
import time
from pathlib import Path

import pytest

from src import tool_user
from src.tool_user import descendants, wrap_argv

ME = pwd.getpwuid(os.getuid()).pw_name

FAKE_SUDO = """#!/bin/sh
# Fake sudo: expects  -n -u USER -- CMD...  and runs CMD as the same user.
printf '%s\n' "$*" >> "$FAKE_SUDO_LOG"
[ "$1" = "-n" ] && [ "$2" = "-u" ] && [ "$4" = "--" ] || { echo "bad sudo args: $*" >&2; exit 99; }
shift 4
exec "$@"
"""


@pytest.fixture
def fake_sudo(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    sudo = bin_dir / "sudo"
    sudo.write_text(FAKE_SUDO)
    sudo.chmod(0o755)
    log = tmp_path / "sudo.log"
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_SUDO_LOG", str(log))
    # Our own name as the "tool user", so the uid filter in kill_tool_tree
    # matches the processes the tests start.
    monkeypatch.setenv("ODYSSEUS_TOOL_USER", ME)
    monkeypatch.setenv("ODYSSEUS_TOOL_GROUP", "odyshare")
    monkeypatch.setenv("ODY_TEST_SECRET_TOKEN", "server-only-value")
    return log


def test_without_a_tool_user_nothing_changes(monkeypatch):
    monkeypatch.delenv("ODYSSEUS_TOOL_USER", raising=False)
    assert tool_user.tool_user() == ""
    assert wrap_argv(["bash", "-c", "ls"], {"OPENAI_API_KEY": "x"}) == ["bash", "-c", "ls"]
    tool_user.kill_tool_tree(12345)       # no-op, no error
    tool_user.share_with_tools("/tmp")    # no-op


def test_wrapper_runs_as_the_tool_user_with_a_clean_environment(monkeypatch):
    monkeypatch.setenv("ODYSSEUS_TOOL_USER", "odytools")
    argv = wrap_argv(["/bin/bash", "-c", "env"],
                     {"OPENAI_API_KEY": "sk-x", "ODYSSEUS_ADMIN_PASSWORD": "pw", "TERM": "xterm",
                      "HOME": "/app/data/agent_workspace", "PATH": "/evil"})
    assert argv[:6] == ["sudo", "-n", "-u", "odytools", "--", "/usr/bin/env"]
    assert argv[6] == "-i"
    pairs = argv[7:argv.index("/bin/sh")]
    assert "TERM=xterm" in pairs and "HOME=/app/data/agent_workspace" in pairs
    assert "USER=odytools" in pairs and f"PATH={tool_user.TOOL_PATH}" in pairs
    assert not [p for p in pairs if p.startswith(("OPENAI_API_KEY", "ODYSSEUS_ADMIN_PASSWORD", "PATH=/evil"))]
    assert argv[-4:] == ["odysseus-tool", "/bin/bash", "-c", "env"]
    assert "umask 007" in argv[argv.index("/bin/sh") + 2]


def test_bash_runs_through_the_wrapper(fake_sudo):
    from src.agent_tools.subprocess_tools import BashTool
    env = {**os.environ, "HOME": "/tmp"}
    out = asyncio.run(BashTool().execute(
        'echo "user=$USER"; echo "secret=${ODY_TEST_SECRET_TOKEN:-none}"; umask',
        {"subproc_env": env, "session_id": None}))
    assert out["exit_code"] == 0
    lines = out["output"].splitlines()
    assert lines == [f"user={ME}", "secret=none", "0007"]
    assert f"-n -u {ME} -- /usr/bin/env -i" in fake_sudo.read_text()


def test_python_runs_through_the_wrapper(fake_sudo):
    from src.agent_tools.subprocess_tools import PythonTool
    out = asyncio.run(PythonTool().execute(
        "import os; print(os.environ.get('ODY_TEST_SECRET_TOKEN'), os.environ['USER'])",
        {"subproc_env": {**os.environ}}))
    assert out["exit_code"] == 0 and out["output"] == f"None {ME}"
    assert fake_sudo.exists()


def test_tmux_shell_starts_through_the_wrapper(fake_sudo, monkeypatch):
    from src.agent_tools import subprocess_tools as st
    calls = []

    async def fake_exec(*args, timeout=10):
        calls.append(args)
        if args[:2] == ("tmux", "has-session"):
            return "", "", 0 if len(calls) > 1 else 1
        return "", "", 0

    monkeypatch.setattr(st, "_run_exec", fake_exec)
    asyncio.run(st._ensure_tmux_session("ody-agent-x", "/work", {"TERM": "xterm", "HOME": "/work",
                                                                 "OPENAI_API_KEY": "sk"}))
    new = [c for c in calls if c[:2] == ("tmux", "new-session")][0]
    shell = list(new[new.index("/work") + 1:])
    assert shell[:5] == ["sudo", "-n", "-u", ME, "--"]
    assert shell[-3:] == ["/bin/bash", "--noprofile", "--norc"]
    assert "HOME=/work" in shell and not [a for a in shell if a.startswith("OPENAI_API_KEY")]


def test_a_cancelled_command_is_killed_with_everything_it_started(fake_sudo, tmp_path):
    from src.agent_tools.subprocess_tools import BashTool
    pidfile = tmp_path / "bg.pid"

    async def go():
        task = asyncio.ensure_future(BashTool().execute(
            f"sleep 300 & echo $! > {pidfile}; sleep 300", {"subproc_env": {**os.environ}, "session_id": None}))
        for _ in range(100):
            if pidfile.exists() and pidfile.read_text().strip():
                break
            await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(go())
    bg = int(pidfile.read_text())
    for _ in range(60):
        if not Path(f"/proc/{bg}").exists() or _zombie(bg):
            break
        time.sleep(0.05)
    assert not Path(f"/proc/{bg}").exists() or _zombie(bg)
    assert "/bin/kill -KILL" in fake_sudo.read_text()


def _zombie(pid: int) -> bool:
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1][0] == "Z"
    except (OSError, IndexError):
        return True


def test_descendants_walks_the_whole_tree():
    import subprocess
    proc = subprocess.Popen(["/bin/sh", "-c", "sleep 30 & sleep 30 & wait"])
    try:
        for _ in range(50):
            found = descendants(proc.pid)
            if len(found) >= 2:
                break
            time.sleep(0.05)
        assert len(found) >= 2
    finally:
        for p in descendants(proc.pid):
            try:
                os.kill(p, 9)
            except ProcessLookupError:
                pass
        proc.kill()
        proc.wait()


def test_background_jobs_run_through_the_wrapper(fake_sudo, tmp_path, monkeypatch):
    from src import bg_jobs
    monkeypatch.setattr(bg_jobs, "_JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(bg_jobs, "_STORE", tmp_path / "jobs.json")   # never the real data/bg_jobs.json
    rec = bg_jobs.launch('echo "user=$USER secret=${ODY_TEST_SECRET_TOKEN:-none}"; umask',
                         session_id="s1", cwd=str(tmp_path))
    log = tmp_path / "jobs" / f"{rec['id']}.log"
    exitf = tmp_path / "jobs" / f"{rec['id']}.exit"
    for _ in range(100):
        if exitf.exists() and exitf.read_text().strip():
            break
        time.sleep(0.05)
    assert exitf.read_text().strip() == "0"
    assert log.read_text().splitlines() == [f"user={ME} secret=none", "0007"]
    script = (tmp_path / "jobs" / f"{rec['id']}.sh").read_text()
    assert script.startswith("sudo -n -u ") and " < " in script


def test_sharing_a_workspace(fake_sudo, monkeypatch, tmp_path):
    import subprocess
    ran, started = [], []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: ran.append(argv))
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: started.append(argv))
    monkeypatch.setattr(tool_user, "_shared_paths", set())
    tool_user.share_with_tools(str(tmp_path))
    tool_user.share_with_tools(str(tmp_path))   # once per folder
    assert ran == [["setfacl", "-m", "g:odyshare:rwX", "-m", "d:g:odyshare:rwX", str(tmp_path)]]
    assert started == [["setfacl", "-R", "-m", "g:odyshare:rwX", "-m", "d:g:odyshare:rwX", str(tmp_path)]]


def test_entrypoint_sets_up_the_tool_user_safely():
    ep = (Path(__file__).resolve().parent.parent / "docker" / "entrypoint.sh").read_text()
    assert "$ODY_USER ALL=($TOOL_USER) NOPASSWD: ALL" in ep
    assert "Defaults:$ODY_USER !requiretty, env_reset, !use_pty" in ep
    assert "visudo -cf" in ep                                     # never installs a broken rule
    assert 'export ODYSSEUS_TOOL_USER="$TOOL_USER"' in ep         # only after the rule validated
    assert ep.index("visudo -cf") < ep.index('export ODYSSEUS_TOOL_USER="$TOOL_USER"')
    assert "|| tool_ok=false" in ep                               # failures don't stop the container
    assert "chmod o-rwx" in ep and "chmod 2770" in ep and "umask 027" in ep
    dockerfile = (Path(__file__).resolve().parent.parent / "Dockerfile").read_text()
    assert "    sudo \\" in dockerfile and "    acl \\" in dockerfile
