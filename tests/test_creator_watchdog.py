"""Creator root helper, Phase 5c: the watchdog. Which tier a root command is
in (host_helper/root_helper.py's Watchdog), its settings and who may loosen
them (over real temporary sockets), the /api/creator/root watchdog routes, and
the settings card's helpers in static/js/secrets.js."""

import asyncio
import json
import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest
from fastapi import HTTPException

from tests.test_creator_root_helper import (
    CONTROL, SOCK, Clock, _code, _helper, _raw, _req, _routes, _run, in_tmp, mod,  # noqa: F401
)

_SECRETS_JS = (Path(__file__).resolve().parent.parent / "static" / "js" / "secrets.js").as_posix()
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node binary not on PATH")


@pytest.fixture
def site(in_tmp):
    """An allowed folder with a page in it, and a watchdog that allows it."""
    www = in_tmp / "www"
    (www / "html").mkdir(parents=True)
    (www / "html" / "index.html").write_text("hi")
    dog = mod.Watchdog(str(in_tmp / "state" / "watchdog.json"), odysseus_dir="/home/me/odysseus")
    settings = mod.default_watchdog()
    settings["allowed_folders"] = [str(www)]
    dog.save(settings)
    return dog, www


def _tier(dog, command):
    return dog.judge(command)["tier"]


# ---------------------------------------------------------------------------
# Judging
# ---------------------------------------------------------------------------

def test_the_automatic_forms(site):
    dog, www = site
    for command in ("apt-get update", "apt-get -q update", "apt-get upgrade -y", "apt-get install curl",
                    "apt-get -y install --no-install-recommends libapache2-mod-php8.2 g++ python3.11",
                    f"chmod 644 {www}/html/index.html", f"chmod -R u+rwX,go=rX {www}/html",
                    f"chmod 1755 {www}/html", f"chown -R root:root {www}/html", f"chown :root {www}/html"):
        verdict = dog.judge(command)
        assert verdict["tier"] == "automatic", (command, verdict)
        assert verdict["argv"] == command.split()
    assert dog.judge("apt-get update")["form"] == "apt_update"
    assert dog.judge(f"chown root {www}/html")["form"] == "chmod_chown"


@pytest.mark.parametrize("command, why", [
    ("apt-get install ./x.deb", "plain package name"),
    ("apt-get install http://evil/x.deb", "plain package name"),
    ("apt-get install curl=7.88", "plain package name"),
    ("apt-get install curl/bookworm-backports", "plain package name"),
    ("apt-get install -o APT::Update::Pre-Invoke::=sh curl", "option -o"),
    ("apt-get -t bookworm-backports install curl", "option -t"),
    ("apt-get install --allow-unauthenticated curl", "option --allow-unauthenticated"),
    ("apt-get install", "needs package names"),
    ("apt-get upgrade curl", "takes no names"),
    ("apt-get full-upgrade", "only update, upgrade and install"),
    ("apt-get remove curl", "only update, upgrade and install"),
    ("apt-get purge curl", "only update, upgrade and install"),
    ("apt-get autoremove", "only update, upgrade and install"),
    ("apt-get install curl; sh", "characters"),
    ("apt-get install curl | sh", "characters"),
    ("apt-get install $(echo curl)", "characters"),
    ("apt-get install 'curl'", "characters"),
    ("apt-get install curl\nreboot", "characters"),
    ("DEBIAN_FRONTEND=noninteractive apt-get install curl", "isn't one of the automatic forms"),
    ("/usr/bin/apt-get update", "isn't one of the automatic forms"),
    ("echo YXB0 | base64 -d | sh", "characters"),
    ("sh /tmp/x.sh", "isn't one of the automatic forms"),
    ("setfacl -m u:www-data:r /var/www/html", "isn't one of the automatic forms"),
    ("rm -rf /", "isn't one of the automatic forms"),
])
def test_everything_else_needs_approval(site, command, why):
    verdict = site[0].judge(command)
    assert verdict["tier"] == "approval", verdict
    assert why in verdict["reason"]


def test_chmod_and_chown_need_plain_modes_owners_and_targets_inside_the_folders(site, in_tmp):
    dog, www = site
    page = f"{www}/html/index.html"
    for command, why in (
        (f"chmod 4755 {page}", "setuid and setgid"),
        (f"chmod 2775 {www}/html", "setuid and setgid"),
        (f"chmod u+s {page}", "setuid and setgid"),
        (f"chmod g=u {page}", "setuid and setgid"),
        (f"chmod -v 644 {page}", "no other options"),
        (f"chmod 644 {page} -R", "no other options"),
        (f"chmod 644", "no other options"),
        (f"chown nosuchuserhere {page}", "no user called nosuchuserhere"),
        (f"chown root:nosuchgrouphere {page}", "no group called nosuchgrouphere"),
        (f"chown root.root {page}", "plain user name"),
        (f"chmod 644 html/index.html", "isn't a full path"),
        (f"chmod 644 {www}/html/missing", "doesn't exist"),
        (f"chmod 755 {www}", "an allowed folder itself"),
        (f"chmod 644 {in_tmp}/etc/totp.key", "isn't inside an allowed folder"),
        (f"chmod 644 {www}/html/../../etc/totp.key", "isn't inside an allowed folder"),
    ):
        verdict = dog.judge(command)
        assert verdict["tier"] == "approval", (command, verdict)
        assert why in verdict["reason"], (command, verdict["reason"])


def test_symlinks_are_resolved_before_the_folder_check(site, in_tmp):
    dog, www = site
    os.symlink(in_tmp / "etc", www / "html" / "escape")
    verdict = dog.judge(f"chmod -R 777 {www}/html/escape")
    assert verdict["tier"] == "approval" and "really" in verdict["reason"]
    # A link to a refused path is refused, though the text doesn't name it.
    os.symlink("/etc/shadow", www / "html" / "shadowlink")
    verdict = dog.judge(f"chmod 644 {www}/html/shadowlink")
    assert verdict["tier"] == "refused" and verdict["refused_by"] == "/etc/shadow" and verdict["switch_off"]


@pytest.mark.parametrize("command, hit", [
    ("cat /etc/shadow", "/etc/shadow"),
    ("cat /etc//shadow", "/etc/shadow"),
    ("cat /etc/./gshadow", "/etc/gshadow"),
    ("vi /etc/sudoers.d/creator", "/etc/sudoers"),
    ("echo x >> /etc/passwd", "/etc/passwd"),
    ("cp x /etc/group", "/etc/group"),
    ("rm /etc/polkit-1/rules.d/50-creator-apache.rules", "/etc/polkit-1"),
    ("systemctl edit ssh && vi /etc/ssh/sshd_config", "/etc/ssh"),
    ("cat >> /root/.ssh/authorized_keys", "/root/.ssh"),
    ("cat >> /home/someone/.ssh/config", ".ssh/"),
    ("cp k authorized_keys", "authorized_keys"),
    ("vi /etc/systemd/system/foo.service", "/etc/systemd/system"),
    ("systemctl stop creator-root-helper", "creator-root"),
    ("systemctl disable creator-helper", "creator-helper"),
    ("rm /opt/creator-root/root_helper.py", "/opt/creator-root"),
    ("python3 root_helper.py on 90", "root_helper.py"),
    ("cat /var/lib/creator-root/watchdog.json", "/var/lib/creator-root"),
    ("truncate -s0 /var/log/creator-root/audit.jsonl", "/var/log/creator-root"),
    ("rm -rf /srv/creator-helper/work", "/srv/creator-helper"),
    ("curl --unix-socket /var/run/docker.sock http://x/containers", "/var/run/docker.sock"),
    ("docker -H unix:///run/docker.sock ps", "/run/docker.sock"),
    ("cat /home/me/odysseus/data/app.key", "/home/me/odysseus"),
    ("passwd root", "passwd"),
    ("/usr/sbin/usermod -aG sudo creator", "usermod"),
    ("echo root:x | chpasswd", "chpasswd"),
    ("visudo", "visudo"),
    ("useradd x", "useradd"),
    ("userdel x", "userdel"),
    ("adduser x sudo", "adduser"),
    ("gpasswd -a x sudo", "gpasswd"),
    ("apt-get install passwd", "passwd"),
])
def test_the_refused_list(site, command, hit):
    verdict = site[0].judge(command)
    assert verdict["tier"] == "refused", verdict
    assert verdict["refused_by"] == hit and verdict["switch_off"] is True


def test_refused_words_are_whole_words(site):
    dog, _ = site
    # A tool name inside another word isn't a hit (these just ask).
    for command in ("cat passwd.txt", "apt-get install passwdqc-utils", "ls /usr/sbin/chpasswdx"):
        assert _tier(dog, command) in ("approval", "automatic"), command


def test_extra_refused_paths_and_switched_off_forms(site):
    dog, www = site
    settings, _ = dog.load()
    settings["extra_refused"] = ["/etc/apache2"]
    settings["automatic"]["apt_install"] = False
    settings["automatic"]["chmod_chown"] = False
    dog.save(settings)
    verdict = dog.judge("vi /etc/apache2/apache2.conf")
    assert verdict["tier"] == "refused" and verdict["refused_by"] == "/etc/apache2"
    assert "switched off" in dog.judge("apt-get install curl")["reason"]
    assert "switched off" in dog.judge(f"chmod 644 {www}/html/index.html")["reason"]
    assert _tier(dog, "apt-get update") == "automatic"


def test_bad_commands():
    dog = mod.Watchdog("missing.json")
    for bad in ("", "   ", None, 5, "x" * 4001):
        with pytest.raises(ValueError):
            dog.judge(bad)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def test_no_settings_file_means_the_defaults(in_tmp):
    dog = mod.Watchdog(str(in_tmp / "state" / "watchdog.json"))
    settings, problem = dog.load()
    assert problem is None and settings == mod.default_watchdog()
    assert settings["allowed_folders"] == ["/var/www", "/srv"]
    assert all(settings["automatic"].values())


def test_a_broken_or_loose_settings_file_makes_nothing_automatic(site):
    dog, www = site
    os.chmod(dog.path, 0o666)
    settings, problem = dog.load()
    assert "nobody else can change" in problem and not any(settings["automatic"].values())
    verdict = dog.judge("apt-get update")
    assert verdict["tier"] == "approval" and verdict["problem"]
    os.chmod(dog.path, 0o600)
    Path(dog.path).write_text("{not json")
    assert "broken" in dog.load()[1]
    Path(dog.path).write_text(json.dumps({"automatic": {}}))
    assert "broken" in dog.load()[1]
    # The built-in refused list still holds.
    assert _tier(dog, "cat /etc/shadow") == "refused"


def test_settings_are_checked_and_tidied():
    good = mod.default_watchdog()
    tidy = mod.validate_watchdog({**good, "allowed_folders": [" /var/www/ ", "/srv//data", "/var/www"],
                                  "extra_refused": ["/etc/apache2/"]})
    assert tidy["allowed_folders"] == ["/var/www", "/srv/data"] and tidy["extra_refused"] == ["/etc/apache2"]
    for change, why in (
        ({"allowed_folders": ["/"]}, "system folder"),
        ({"allowed_folders": ["/etc"]}, "system folder"),
        ({"allowed_folders": ["/usr"]}, "system folder"),
        ({"allowed_folders": ["/etc/ssh/keys"]}, "inside /etc/ssh"),
        ({"allowed_folders": ["/srv/creator-root"]}, "inside /srv/creator-root"),
        ({"allowed_folders": ["var/www"]}, "full path"),
        ({"allowed_folders": ["/var/www/../../etc"]}, "can't contain .."),
        ({"allowed_folders": [""]}, "empty"),
        ({"allowed_folders": "/var/www"}, "a list"),
        ({"allowed_folders": [f"/srv/{i}" for i in range(21)]}, "at most 20"),
        ({"extra_refused": ["/"]}, "every command"),
        ({"extra_refused": ["etc"]}, "full path"),
        ({"max_command_s": 9}, "from 10 to 3600"),
        ({"max_command_s": 3601}, "from 10 to 3600"),
        ({"max_command_s": True}, "from 10 to 3600"),
        ({"automatic": {"apt_update": True}}, "true/false for each"),
        ({"automatic": {**good["automatic"], "apt_update": "yes"}}, "true/false for each"),
    ):
        with pytest.raises(ValueError, match=why):
            mod.validate_watchdog({**good, **change})
    with pytest.raises(ValueError):
        mod.validate_watchdog([])


def test_what_counts_as_loosening():
    old = mod.default_watchdog()
    old["automatic"]["apt_install"] = False
    old["extra_refused"] = ["/etc/apache2"]
    same = json.loads(json.dumps(old))
    assert mod.loosenings(old, same) == []
    tighter = json.loads(json.dumps(old))
    tighter["automatic"]["apt_update"] = False
    tighter["allowed_folders"] = ["/var/www/html"]   # narrower
    tighter["extra_refused"].append("/etc/cron.d")
    tighter["max_command_s"] = 60
    assert mod.loosenings(old, tighter) == []
    looser = json.loads(json.dumps(old))
    looser["automatic"]["apt_install"] = True
    looser["allowed_folders"].append("/opt/site")
    looser["extra_refused"] = []
    looser["max_command_s"] = 3600
    assert mod.loosenings(old, looser) == [
        "turns on automatic apt-get install <package names>", "allows /opt/site",
        "stops refusing /etc/apache2", "raises the time limit to 3600 s"]


# ---------------------------------------------------------------------------
# Over the sockets
# ---------------------------------------------------------------------------

def _wd_helper(in_tmp, clock):
    h = _helper(in_tmp, clock)
    h.watchdog = mod.Watchdog(str(in_tmp / "state" / "watchdog.json"), odysseus_dir="/home/me/odysseus")
    return h


def _audit(in_tmp):
    return [json.loads(line) for line in (in_tmp / "log" / "audit.jsonl").read_text().splitlines()]


def test_check_judges_and_never_switches_root_off(in_tmp):
    clock = Clock()
    h = _wd_helper(in_tmp, clock)

    async def go():
        hello = await _raw(SOCK, {"type": "hello"})
        assert {"check", "watchdog", "watchdog_save"} <= set(hello["capabilities"])
        assert hello["runs_commands"] is False and hello["version"] == 2
        assert (await _raw(CONTROL, {"type": "enable", "minutes": 10}))["on"] is True
        reply = await _raw(SOCK, {"type": "check", "command": "apt-get install curl"})
        assert reply["ok"] and reply["tier"] == "automatic" and reply["max_command_s"] == 900
        reply = await _raw(SOCK, {"type": "check", "command": "cat /etc/shadow"})
        assert reply["ok"] and reply["tier"] == "refused" and reply["switch_off"] is True
        assert (await _raw(SOCK, {"type": "status"}))["on"] is True
        bad = await _raw(SOCK, {"type": "check", "command": ""})
        assert bad["ok"] is False and bad["reason"] == "bad_request"
        assert (await _raw(CONTROL, {"type": "check", "command": "ls"}))["tier"] == "approval"

    _run(h, go)
    checks = [e for e in _audit(in_tmp) if e.get("request_type") == "check"]
    assert [e.get("tier") for e in checks] == ["automatic", "refused", None, "approval"]
    assert checks[1]["command"] == "cat /etc/shadow"


def test_tightening_needs_no_code_loosening_does(in_tmp):
    clock = Clock()
    h = _wd_helper(in_tmp, clock)
    wrong = "000000" if _code(clock) != "000000" else "111111"

    async def go():
        current = (await _raw(SOCK, {"type": "watchdog"}))
        assert current["ok"] and current["problem"] is None and current["settings"] == mod.default_watchdog()
        assert "/home/me/odysseus" in current["builtin_refused"]["paths"]
        assert "passwd" in current["builtin_refused"]["tools"]

        tighter = mod.default_watchdog()
        tighter["automatic"]["apt_upgrade"] = False
        tighter["allowed_folders"] = ["/var/www"]
        reply = await _raw(SOCK, {"type": "watchdog_save", "settings": tighter})
        assert reply["ok"] and reply["loosened"] is False and reply["settings"] == tighter

        looser = mod.default_watchdog()
        reply = await _raw(SOCK, {"type": "watchdog_save", "settings": looser})
        assert reply["ok"] is False and reply["reason"] == "code_needed"
        assert reply["loosens"] == ["turns on automatic apt-get upgrade", "allows /srv"]
        reply = await _raw(SOCK, {"type": "watchdog_save", "settings": looser, "code": wrong})
        assert reply["ok"] is False and reply["reason"] == "wrong" and reply["attempts_left"] == 4
        code = _code(clock)
        reply = await _raw(SOCK, {"type": "watchdog_save", "settings": looser, "code": code})
        assert reply["ok"] and reply["loosened"] is True and reply["settings"] == looser
        # The code is used up: it can't loosen again, nor switch root on.
        again = mod.default_watchdog()
        again["max_command_s"] = 1200
        reply = await _raw(SOCK, {"type": "watchdog_save", "settings": again, "code": code})
        assert reply["reason"] == "reused"
        assert (await _raw(SOCK, {"type": "enable", "code": code, "minutes": 5}))["reason"] == "reused"
        # Bad settings are refused before any code is looked at.
        reply = await _raw(SOCK, {"type": "watchdog_save", "settings": {"automatic": {}}, "code": wrong})
        assert reply["reason"] == "bad_request"
        assert (await _raw(SOCK, {"type": "status"}))["attempts_left"] == 3
        # Root, on the control socket, needs no code.
        reply = await _raw(CONTROL, {"type": "watchdog_save", "settings": again})
        assert reply["ok"] and reply["loosened"] is True

    _run(h, go)
    assert oct(os.stat(in_tmp / "state" / "watchdog.json").st_mode & 0o777) == "0o600"
    saved = [e for e in _audit(in_tmp) if e["type"] == "watchdog_saved"]
    assert [(e["via"], e["loosened"]) for e in saved] == [("odysseus", False), ("odysseus", True), ("control", True)]
    assert saved[1]["loosens"] == ["turns on automatic apt-get upgrade", "allows /srv"]
    assert saved[1]["old"]["allowed_folders"] == ["/var/www"] and saved[1]["new"]["allowed_folders"] == ["/var/www", "/srv"]
    assert _code(clock) not in (in_tmp / "log" / "audit.jsonl").read_text()


def test_wrong_codes_for_the_watchdog_count_towards_the_lockout(in_tmp):
    clock = Clock()
    h = _wd_helper(in_tmp, clock)
    wrong = "000000" if _code(clock) != "000000" else "111111"
    looser = mod.default_watchdog()
    looser["max_command_s"] = 3600

    async def go():
        for _ in range(mod.MAX_BAD_CODES):
            await _raw(SOCK, {"type": "watchdog_save", "settings": looser, "code": wrong})
        reply = await _raw(SOCK, {"type": "watchdog_save", "settings": looser, "code": _code(clock)})
        assert reply["reason"] == "locked"
        assert (await _raw(SOCK, {"type": "enable", "code": _code(clock), "minutes": 5}))["reason"] == "locked"

    _run(h, go)


def test_the_terminal_check_command(in_tmp, capsys):
    h = _wd_helper(in_tmp, Clock())

    async def go():
        args = mod._parse_args(["check", "--control-socket", CONTROL, "apt-get install curl"])
        return await asyncio.to_thread(mod._control, args)

    assert _run(h, go) == 0
    assert capsys.readouterr().out.startswith("automatic: ")


def test_the_unit_names_the_odysseus_folder():
    unit = (Path(mod.__file__).parent / "creator-root-helper.service").read_text()
    assert "serve --allow-uid 1000 --odysseus-dir /home/madsen/odysseus" in unit


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def test_watchdog_routes_are_for_logged_in_admins_only(monkeypatch, in_tmp):
    from types import SimpleNamespace as NS
    routes = _routes(monkeypatch)
    calls = (
        lambda req: routes[("/api/creator/root/watchdog", "GET")](request=req),
        lambda req: routes[("/api/creator/root/watchdog", "POST")](
            body=NS(settings=mod.default_watchdog(), code=None), request=req),
        lambda req: routes[("/api/creator/root/check", "POST")](body=NS(command="ls"), request=req),
    )
    for call in calls:
        for req in (_req("bob"), _req("carol"), _req("alice", token=True), _req("", auth=False)):
            with pytest.raises(HTTPException) as exc:
                asyncio.run(call(req))
            assert exc.value.status_code == 403


def test_watchdog_routes_reach_the_helper(monkeypatch, in_tmp):
    from types import SimpleNamespace as NS
    routes = _routes(monkeypatch)
    clock = Clock()
    h = _wd_helper(in_tmp, clock)
    get = routes[("/api/creator/root/watchdog", "GET")]
    save = routes[("/api/creator/root/watchdog", "POST")]
    check = routes[("/api/creator/root/check", "POST")]

    async def go():
        current = await get(request=_req("alice"))
        assert "ok" not in current and current["settings"] == mod.default_watchdog()
        verdict = await check(body=NS(command="apt-get update"), request=_req("alice"))
        assert verdict["tier"] == "automatic"
        tighter = mod.default_watchdog()
        tighter["max_command_s"] = 60
        assert (await save(body=NS(settings=tighter, code=None), request=_req("alice")))["loosened"] is False
        with pytest.raises(HTTPException) as exc:
            await save(body=NS(settings=mod.default_watchdog(), code=None), request=_req("alice"))
        assert exc.value.status_code == 428 and "raises the time limit to 900 s" in exc.value.detail
        with pytest.raises(HTTPException) as exc:
            await save(body=NS(settings={"automatic": 1}, code=None), request=_req("alice"))
        assert exc.value.status_code == 400
        saved = await save(body=NS(settings=mod.default_watchdog(), code=_code(clock)), request=_req("alice"))
        assert saved["loosened"] is True and saved["settings"]["max_command_s"] == 900

    _run(h, go)


def test_watchdog_routes_without_the_helper(monkeypatch, in_tmp):
    from types import SimpleNamespace as NS
    routes = _routes(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(routes[("/api/creator/root/check", "POST")](body=NS(command="ls"), request=_req("alice")))
    assert exc.value.status_code == 503


def test_watchdog_request_models():
    from pydantic import ValidationError
    from routes import creator_routes
    from src.creator_mode import CreatorManager
    router = creator_routes.setup_creator_routes(CreatorManager.__new__(CreatorManager))

    def model(path):
        route = next(r for r in router.routes if getattr(r, "path", "") == path and "POST" in r.methods)
        return route.body_field.type_ if hasattr(route.body_field, "type_") else route.body_field.field_info.annotation

    check, save = model("/api/creator/root/check"), model("/api/creator/root/watchdog")
    assert check(command="ls").command == "ls"
    for bad in ({"command": ""}, {"command": "x" * 4001}):
        with pytest.raises(ValidationError):
            check(**bad)
    assert save(settings={}).code is None
    with pytest.raises(ValidationError):
        save(settings={}, code="12345")


# ---------------------------------------------------------------------------
# The settings card's helpers (static/js/secrets.js)
# ---------------------------------------------------------------------------

def _js(expr: str):
    proc = subprocess.run(
        ["node", "--input-type=module"],
        input=textwrap.dedent(f"""
            const m = await import('{_SECRETS_JS}');
            console.log(JSON.stringify({expr}));
        """), capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip())


@needs_node
def test_card_builds_the_settings_from_the_form():
    out = _js("""m.watchdogSettingsFromForm({automatic: {apt_update: true}, folders: ' /var/www \\n\\n/srv ',
                                             extra: '/etc/apache2', maxSeconds: '900'})""")
    assert out == {"settings": {"automatic": {"apt_update": True}, "allowed_folders": ["/var/www", "/srv"],
                                "extra_refused": ["/etc/apache2"], "max_command_s": 900}}
    for value in ("9", "3601", "", "12.5", "abc"):
        out = _js(f"m.watchdogSettingsFromForm({{automatic: {{}}, folders: '', extra: '', maxSeconds: '{value}'}})")
        assert out == {"error": "Longest root command must be 10–3600 seconds."}


@needs_node
def test_card_says_what_a_verdict_means():
    assert _js("m.verdictText({tier: 'automatic', reason: 'It is apt-get update.'})") == \
        "Automatic: It is apt-get update."
    text = _js("m.verdictText({tier: 'refused', reason: 'It names /etc/shadow.', problem: 'x'})")
    assert text.startswith("Refused: It names /etc/shadow.") and "root switches off" in text and "(Note: x)" in text
    assert _js("m.verdictText({tier: 'approval', reason: 'r'})") == "Needs your approval: r"
    assert _js("m.verdictText(null)") == ""
