"""Creator root helper, Phase 5d: running root commands. The helper's `run`
and `denied` (host_helper/root_helper.py) over real temporary sockets, with a
stand-in for systemd-run: it applies --setenv, runs `/bin/bash -c` commands
for real, and only prints any other command's words (so `apt-get` never runs
here). The real systemd-run, as root, is not tested here."""

import asyncio
import json
import os
import sys
import time

import pytest

from tests.test_creator_root_helper import (  # noqa: F401
    CONTROL, SOCK, Clock, _helper, _raw, _run, in_tmp, mod,
)

FAKE_SYSTEMD_RUN = r'''#!{python}
import json, os, sys
args = sys.argv[1:]
cut = args.index("--")
env = {{}}
for a in args[:cut]:
    if a.startswith("--setenv="):
        k, _, v = a[len("--setenv="):].partition("=")
        env[k] = v
words = args[cut + 1:]
with open(os.environ.get("FAKE_LOG", "/dev/null"), "a") as f:
    f.write(json.dumps(args) + "\n")
if words[:1] == ["/bin/bash"]:
    os.execve("/bin/bash", words, {{**env, "PATH": "/usr/bin:/bin"}})
print(json.dumps({{"words": words, "env": env}}))
'''


@pytest.fixture
def root(in_tmp, monkeypatch):
    fake = in_tmp / "fake-systemd-run"
    fake.write_text(FAKE_SYSTEMD_RUN.format(python=sys.executable))
    fake.chmod(0o755)
    monkeypatch.setenv("FAKE_LOG", str(in_tmp / "fake.log"))
    www = in_tmp / "www"
    (www / "html").mkdir(parents=True)
    clock = Clock()
    h = _helper(in_tmp, clock)
    h.watchdog = mod.Watchdog(str(in_tmp / "state" / "watchdog.json"), odysseus_dir="/home/me/odysseus")
    settings = mod.default_watchdog()
    settings["allowed_folders"] = [str(www)]
    h.watchdog.save(settings)
    h.runner = mod.RootRunner("/home/me/odysseus", systemd_run=str(fake), systemctl="/bin/true")
    return h, www


def _audit(in_tmp):
    return [json.loads(line) for line in (in_tmp / "log" / "audit.jsonl").read_text().splitlines()]


def _runs(in_tmp):
    return [e for e in _audit(in_tmp) if e.get("request_type") == "run"]


async def _on():
    assert (await _raw(CONTROL, {"type": "enable", "minutes": 10}))["on"] is True


def test_nothing_runs_while_root_is_off(root, in_tmp):
    h, _ = root

    async def go():
        reply = await _raw(SOCK, {"type": "run", "command": "apt-get update"})
        assert reply["ok"] is False and reply["reason"] == "root_off" and reply["tier"] == "automatic"

    _run(h, go)
    assert not (in_tmp / "fake.log").exists()


def test_automatic_runs_exactly_the_judged_words_without_a_shell(root, in_tmp):
    h, www = root

    async def go():
        await _on()
        reply = await _raw(SOCK, {"type": "run", "command": "apt-get -y install curl"})
        assert reply["ok"] and reply["tier"] == "automatic" and reply["exit_code"] == 0
        out = json.loads(reply["stdout"])
        assert out["words"] == ["apt-get", "-o", "Dpkg::Options::=--force-confdef",
                                "-o", "Dpkg::Options::=--force-confold", "-y", "install", "curl"]
        assert out["env"]["DEBIAN_FRONTEND"] == "noninteractive" and out["env"]["HOME"] == "/root"
        assert json.loads((await _raw(SOCK, {"type": "run", "command": "apt-get update"}))["stdout"])["words"] \
            == ["apt-get", "update"]
        # chmod gets the resolved path, not the link.
        os.symlink(www / "html", www / "link")
        reply = await _raw(SOCK, {"type": "run", "command": f"chmod -R 755 {www}/link"})
        assert json.loads(reply["stdout"])["words"] == ["chmod", "-R", "755", str(www / "html")]

    _run(h, go)


def test_the_unit_gets_the_walls_and_the_time_limit(root, in_tmp):
    h, _ = root

    async def go():
        await _on()
        await _raw(SOCK, {"type": "run", "command": "apt-get update"})

    _run(h, go)
    args = json.loads((in_tmp / "fake.log").read_text().splitlines()[0])
    assert args[:5] == ["--quiet", "--pipe", "--wait", "--collect", "--service-type=exec"]
    assert args[5].startswith("--unit=creator-root-cmd-") and args[5].endswith(".service")
    props = [args[i + 1] for i, a in enumerate(args) if a == "-p"]
    assert "RuntimeMaxSec=900" in props and "UMask=0022" in props
    inaccessible = next(p for p in props if p.startswith("InaccessiblePaths="))
    for path in ("/etc/creator-root", "/var/lib/creator-root", "/var/log/creator-root", "/srv/creator-root",
                 "/run/creator-root", "/srv/creator-helper/helper.sock", "/srv/creator-helper/home",
                 "/run/docker.sock", "/home/me/odysseus"):
        assert f"-{path}" in inaccessible.split("=", 1)[1].split()
    read_only = next(p for p in props if p.startswith("ReadOnlyPaths=")).split("=", 1)[1].split()
    # The host helper's folder (with the staged files) is readable, never writable.
    for path in ("/opt/creator-root", "/etc/systemd/system/creator-root-helper.service", "/srv/creator-helper"):
        assert f"-{path}" in read_only
    assert "CapabilityBoundingSet=~CAP_SYS_ADMIN CAP_SYS_MODULE CAP_SYS_PTRACE" in props
    assert "BindsTo=creator-root-helper.service" in props
    assert args[args.index("--") + 1:] == ["apt-get", "update"]


def test_approval_tier_runs_only_when_approved(root, in_tmp):
    h, _ = root

    async def go():
        await _on()
        reply = await _raw(SOCK, {"type": "run", "command": "echo hi from root; exit 3"})
        assert reply["ok"] is False and reply["reason"] == "needs_approval" and reply["tier"] == "approval"
        # "approved" must be exactly true.
        reply = await _raw(SOCK, {"type": "run", "command": "echo hi from root; exit 3", "approved": "yes"})
        assert reply["reason"] == "needs_approval"
        reply = await _raw(SOCK, {"type": "run", "command": "echo hi from root; exit 3", "approved": True})
        assert reply["ok"] and reply["exit_code"] == 3 and reply["stdout"] == "hi from root\n"
        assert reply["tier"] == "approval" and reply["timeout_s"] == 900

    _run(h, go)
    runs = _runs(in_tmp)
    assert [(r["tier"], r["approved"], r.get("exit_code")) for r in runs] == [
        ("approval", False, None), ("approval", False, None), ("approval", True, 3)]
    assert runs[2]["words"] == ["/bin/bash", "-c", "echo hi from root; exit 3"]
    assert runs[2]["stdout_preview"] == "hi from root\n"


def test_a_refused_command_never_runs_and_switches_root_off(root, in_tmp):
    h, _ = root

    async def go():
        await _on()
        reply = await _raw(SOCK, {"type": "run", "command": "cat /etc/shadow", "approved": True})
        assert reply["ok"] is False and reply["reason"] == "refused" and reply["root_switched_off"] is True
        assert "Root has been switched off" in reply["error"]
        assert (await _raw(SOCK, {"type": "status"}))["on"] is False
        # While off, still refused (and nothing to switch off).
        reply = await _raw(SOCK, {"type": "run", "command": "systemctl stop creator-root-helper"})
        assert reply["reason"] == "refused" and reply["root_switched_off"] is False

    _run(h, go)
    assert not (in_tmp / "fake.log").exists()
    off = [e for e in _audit(in_tmp) if e["type"] == "root_off"]
    assert off == [{**off[0], "why": "refused command (/etc/shadow)", "via": "odysseus"}]


def test_three_denials_in_a_row_switch_root_off(root, in_tmp):
    h, _ = root

    async def go():
        await _on()
        for n in (1, 2):
            reply = await _raw(SOCK, {"type": "denied", "command": "rm -rf /tmp/x"})
            assert reply["denials"] == n and reply["root_switched_off"] is False and reply["on"] is True
        # A command that runs starts the count again.
        await _raw(SOCK, {"type": "run", "command": "apt-get update"})
        for n in (1, 2):
            assert (await _raw(SOCK, {"type": "denied"}))["denials"] == n
        reply = await _raw(SOCK, {"type": "denied"})
        assert reply["root_switched_off"] is True and reply["on"] is False and reply["denials"] == 0
        # The control socket can't record denials or run anything.
        assert (await _raw(CONTROL, {"type": "run", "command": "apt-get update"}))["ok"] is False

    _run(h, go)
    off = [e for e in _audit(in_tmp) if e["type"] == "root_off"]
    assert [e["why"] for e in off] == ["3 root commands denied in a row"]


def test_secrets_are_blanked_in_the_audit_log(root, in_tmp):
    h, _ = root

    async def go():
        await _on()
        reply = await _raw(SOCK, {"type": "run", "command": "echo hunter2-token", "approved": True,
                                  "redact": ["hunter2-token"]})
        assert reply["stdout"] == "hunter2-token\n"   # Odysseus scrubs what the model sees

    _run(h, go)
    text = (in_tmp / "log" / "audit.jsonl").read_text()
    assert "hunter2-token" not in text and "[REDACTED]" in text


def test_closing_the_connection_stops_the_command(root, in_tmp):
    h, _ = root
    pidfile = in_tmp / "pid"

    async def go():
        await _on()
        reader, writer = await asyncio.open_unix_connection(SOCK)
        writer.write((json.dumps({"type": "run", "approved": True,
                                  "command": f"echo $$ > {pidfile}; exec sleep 30"}) + "\n").encode())
        await writer.drain()
        for _ in range(100):
            if pidfile.exists() and pidfile.read_text().strip():
                break
            await asyncio.sleep(0.05)
        pid = int(pidfile.read_text())
        writer.close()
        for _ in range(100):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            await asyncio.sleep(0.05)
        raise AssertionError("the command was still running")

    _run(h, go)
    for _ in range(50):
        runs = _runs(in_tmp)
        if runs:
            break
        time.sleep(0.05)
    assert runs[0]["disconnected"] is True


def test_the_time_limit_stops_the_command(root, in_tmp, monkeypatch):
    h, _ = root
    monkeypatch.setattr(mod, "STOP_GRACE_S", 0.2)

    class Never:
        async def read(self, n):
            await asyncio.sleep(60)

    async def go():
        started = time.monotonic()
        result = await h.runner.run(["/bin/bash", "-c", "sleep 30"], 1, Never())
        assert result["timed_out"] is True and time.monotonic() - started < 5

    asyncio.run(go())


def test_one_root_command_at_a_time(root, in_tmp):
    h, _ = root

    async def go():
        await _on()
        reader, writer = await asyncio.open_unix_connection(SOCK)
        writer.write((json.dumps({"type": "run", "approved": True, "command": "sleep 1"}) + "\n").encode())
        await writer.drain()
        await asyncio.sleep(0.3)
        reply = await _raw(SOCK, {"type": "run", "command": "apt-get update"})
        assert reply["reason"] == "busy"
        first = json.loads(await asyncio.wait_for(reader.readline(), timeout=10))
        assert first["ok"] and first["exit_code"] == 0
        writer.close()

    _run(h, go)


def test_bad_run_requests(root):
    h, _ = root

    async def go():
        await _on()
        for payload in ({"type": "run"}, {"type": "run", "command": ""},
                        {"type": "run", "command": "ls", "redact": "x"}):
            reply = await _raw(SOCK, payload)
            assert reply["ok"] is False and reply["reason"] == "bad_request"

    _run(h, go)


def test_automatic_chmod_needs_protected_hardlinks(root, in_tmp):
    h, www = root
    off = in_tmp / "hardlinks"
    off.write_text("0\n")
    h.watchdog.hardlinks_file = str(off)
    verdict = h.watchdog.judge(f"chmod 644 {www}/html")
    assert verdict["tier"] == "approval" and "protected_hardlinks" in verdict["reason"]
    off.write_text("1\n")
    assert h.watchdog.judge(f"chmod 644 {www}/html")["tier"] == "automatic"
