#!/usr/bin/env python3
"""Creator root helper (Phases 5b and 5c of docs/creator-plan.md).

Runs on the HOST, outside Docker, as root (see creator-root-helper.service and
README.md in this folder). It holds the root switch: whether root is on, and
for how long (5b). It also holds the watchdog (5c), which judges a command
before it may run as root: automatic, needs your approval, or refused. It
still runs no commands; running them comes in 5d, and will be refused unless
the switch is on.

Two sockets, one request per connection (one line of JSON in, one out):

- The Odysseus socket (/srv/creator-root/root.sock, mounted read-only into the
  container). Only uids given with --allow-uid (the container's user, 1000)
  get an answer; root is never one of them. Requests:
    {"type": "hello"}
    {"type": "status"}
    {"type": "enable", "code": "123456", "minutes": 30}
    {"type": "revoke"}
    {"type": "check", "command": "apt-get install curl"}
    {"type": "watchdog"}
    {"type": "watchdog_save", "settings": {...}, "code": "123456"}
  `enable` needs a code from your authenticator app (TOTP, RFC 6238). The key
  is in a root-only file on the host; Odysseus never sees it. A code works
  once. After 5 wrong codes in a row, `enable` is locked for 15 minutes.
  `revoke` needs no code. `check` only judges a command (it never runs one
  and never switches root off). `watchdog_save` needs a code only when the
  change loosens the watchdog; the helper decides that, not Odysseus.

- The control socket (/run/creator-root/control.sock), root only, never
  mounted into the container: the terminal fallback,
    sudo python3 root_helper.py on 30 | off | status | check "<command>"
  No code is needed there: you're already root.

Root is on for at most 90 minutes and switches itself off. The window lives in
memory only, so a restart (or a crash) means root is off.

Every connection and every change goes to an audit log (JSONL, mode 0600).
Codes are never logged, right or wrong.

Standard library only: the host Python has no pip.
"""

import argparse
import asyncio
import base64
import grp
import hashlib
import hmac
import json
import os
import pwd
import re
import secrets
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import time
from datetime import datetime, timezone
from urllib.parse import quote

VERSION = 2
# Request types each socket answers.
CAPABILITIES = ("hello", "status", "enable", "revoke", "check", "watchdog", "watchdog_save")
CONTROL_CAPABILITIES = ("status", "enable", "revoke", "check", "watchdog", "watchdog_save")

DEFAULT_SOCKET = "/srv/creator-root/root.sock"
DEFAULT_CONTROL_SOCKET = "/run/creator-root/control.sock"
DEFAULT_AUDIT_LOG = "/var/log/creator-root/audit.jsonl"
DEFAULT_KEY_FILE = "/etc/creator-root/totp.key"
DEFAULT_STATE_FILE = "/var/lib/creator-root/state.json"
DEFAULT_WATCHDOG_FILE = "/var/lib/creator-root/watchdog.json"

# Decision (2026-10-02): at most 90 minutes. --max-minutes can lower it, not raise it.
MAX_MINUTES = 90
DEFAULT_ON_MINUTES = 30
MAX_BAD_CODES = 5
LOCKOUT_S = 15 * 60

TOTP_STEP_S = 30
TOTP_DIGITS = 6
# Codes from one step before or after now are accepted too (clock drift, typing time).
TOTP_DRIFT_STEPS = 1
KEY_BYTES = 20

# Room for a watchdog_save with its folder lists.
MAX_REQUEST_BYTES = 16384
READ_TIMEOUT_S = 10.0
# The Odysseus socket's own mode. Its folder is what limits who reaches it
# (root-owned, only uid 1000 may enter: README.md), and the peer check decides.
SOCKET_MODE = 0o666
CONTROL_SOCKET_MODE = 0o600

SETUP_HINT = "sudo python3 /opt/creator-root/root_helper.py setup-totp"


def _now_iso(t=None) -> str:
    dt = datetime.fromtimestamp(time.time() if t is None else t, timezone.utc)
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def peer_credentials(sock: socket.socket):
    """(pid, uid, gid) of the process on the other end, from the kernel."""
    raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return struct.unpack("3i", raw)


class AuditLog:
    """Append-only JSONL, created 0600. A failed write never stops the helper."""

    def __init__(self, path: str):
        self.path = path

    def write(self, entry: dict) -> None:
        try:
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, mode=0o700, exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(json.dumps({"at": _now_iso(), **entry}, default=str) + "\n")
        except OSError as e:
            print(f"creator-root-helper: audit log write failed: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# TOTP (RFC 6238 over RFC 4226 HOTP, HMAC-SHA-1), what authenticator apps use
# ---------------------------------------------------------------------------

def hotp(key: bytes, counter: int, digits: int = TOTP_DIGITS) -> str:
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    value = (struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(value).zfill(digits)


def time_step(t: float) -> int:
    return int(t // TOTP_STEP_S)


def parse_key(text: str) -> bytes:
    """The key file's base32 text (spaces and case ignored) as bytes.
    Raises ValueError if it isn't a usable key."""
    cleaned = "".join(text.split()).upper().rstrip("=")
    if not cleaned:
        raise ValueError("the key file is empty")
    try:
        key = base64.b32decode(cleaned + "=" * (-len(cleaned) % 8))
    except ValueError:   # binascii.Error is a ValueError
        raise ValueError("the key file isn't base32")
    if len(key) < 10:
        raise ValueError("the key is too short")
    return key


def new_key_text() -> str:
    return base64.b32encode(secrets.token_bytes(KEY_BYTES)).decode("ascii").rstrip("=")


def otpauth_uri(key_text: str, account: str) -> str:
    issuer = "Odysseus Creator"
    return (f"otpauth://totp/{quote(issuer)}:{quote(account)}"
            f"?secret={key_text}&issuer={quote(issuer)}&algorithm=SHA1&digits={TOTP_DIGITS}&period={TOTP_STEP_S}")


def load_key(path: str, owner_uid: int) -> bytes:
    """The TOTP key, or ValueError saying why it can't be used. A key file that
    anyone but its owner (root) could read or change is refused: the key would
    no longer be a secret."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise ValueError(f"no authenticator key yet (set one up on the host: {SETUP_HINT})")
    except OSError as e:
        raise ValueError(f"can't read the key file: {e.strerror or e}")
    if not stat.S_ISREG(st.st_mode):
        raise ValueError("the key file isn't a regular file")
    if st.st_uid != owner_uid or st.st_mode & 0o077:
        raise ValueError("the key file must be owned by root with mode 0600")
    with open(path, encoding="ascii", errors="replace") as f:
        return parse_key(f.read())


class TotpVerifier:
    """Checks codes against the key file. Each code works once: the last time
    step used is saved to `state_file`, so a restart doesn't make an old code
    good again."""

    def __init__(self, key_file: str, state_file: str, clock=time.time, owner_uid=None):
        self.key_file = key_file
        self.state_file = state_file
        self.clock = clock
        self.owner_uid = os.geteuid() if owner_uid is None else owner_uid

    def problem(self):
        """None when a key is ready, else why not."""
        try:
            load_key(self.key_file, self.owner_uid)
            return None
        except ValueError as e:
            return str(e)

    def _last_step(self) -> int:
        try:
            with open(self.state_file, encoding="utf-8") as f:
                value = json.load(f).get("last_totp_step", -1)
            return int(value) if isinstance(value, int) else -1
        except FileNotFoundError:
            return -1
        except (OSError, ValueError, AttributeError):
            # Unreadable state: fail closed. No code is accepted until it's fixed.
            raise RuntimeError("the helper's state file can't be read")

    def _save_last_step(self, step: int) -> None:
        directory = os.path.dirname(self.state_file) or "."
        os.makedirs(directory, mode=0o700, exist_ok=True)
        tmp = self.state_file + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"last_totp_step": step}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.state_file)

    def check(self, code: str):
        """(ok, reason). reason: None, "no_key", "wrong", "reused", "state"."""
        try:
            key = load_key(self.key_file, self.owner_uid)
        except ValueError:
            return False, "no_key"
        now = time_step(self.clock())
        matched = None
        for step in range(now - TOTP_DRIFT_STEPS, now + TOTP_DRIFT_STEPS + 1):
            if hmac.compare_digest(hotp(key, step), code):
                matched = step
        if matched is None:
            return False, "wrong"
        try:
            if matched <= self._last_step():
                return False, "reused"
            self._save_last_step(matched)
        except (RuntimeError, OSError):
            return False, "state"
        return True, None


# ---------------------------------------------------------------------------
# The switch
# ---------------------------------------------------------------------------

_REFUSALS = {
    "wrong": "That code isn't right.",
    "reused": "That code was already used. Wait for the next one.",
    "no_key": None,   # filled from TotpVerifier.problem()
    "state": "The helper couldn't record the code's use, so it refused it. Check /var/lib/creator-root.",
}


def parse_minutes(value, max_minutes: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("minutes must be a whole number")
    if not 1 <= value <= max_minutes:
        raise ValueError(f"minutes must be between 1 and {max_minutes}")
    return value


class RootSwitch:
    """Whether root is on, and until when. In memory only."""

    def __init__(self, verifier: TotpVerifier, audit: AuditLog, max_minutes: int = MAX_MINUTES,
                 clock=time.monotonic, wall=time.time):
        self.verifier = verifier
        self.audit = audit
        self.max_minutes = min(int(max_minutes), MAX_MINUTES)
        if self.max_minutes < 1:
            raise ValueError("--max-minutes must be at least 1")
        self.clock = clock
        self.wall = wall
        self._until = None          # monotonic deadline while on
        self._expires_wall = None
        self._minutes = None
        self._since_wall = None
        self._bad_codes = 0
        self._locked_until = None   # monotonic

    # -- state ------------------------------------------------------------

    def tick(self) -> None:
        """Switch off when the time is up (also called before every answer)."""
        if self._until is not None and self.clock() >= self._until:
            self._until = self._expires_wall = self._minutes = self._since_wall = None
            self.audit.write({"type": "root_off", "why": "expired"})

    def is_on(self) -> bool:
        self.tick()
        return self._until is not None

    def _locked_s(self) -> int:
        if self._locked_until is None:
            return 0
        left = self._locked_until - self.clock()
        if left <= 0:
            self._locked_until = None
            self._bad_codes = 0
            return 0
        return int(left + 0.999)

    def status(self) -> dict:
        self.tick()
        on = self._until is not None
        problem = self.verifier.problem()
        return {
            "on": on,
            "remaining_s": int(self._until - self.clock() + 0.999) if on else 0,
            "expires_at": _now_iso(self._expires_wall) if on else None,
            "since": _now_iso(self._since_wall) if on else None,
            "minutes": self._minutes if on else None,
            "max_minutes": self.max_minutes,
            "totp_ready": problem is None,
            "totp_problem": problem,
            "locked_s": self._locked_s(),
            "attempts_left": MAX_BAD_CODES - self._bad_codes,
        }

    def _switch_on(self, minutes: int, via: str) -> None:
        self._until = self.clock() + minutes * 60
        self._since_wall = self.wall()
        self._expires_wall = self._since_wall + minutes * 60
        self._minutes = minutes
        self.audit.write({"type": "root_on", "via": via, "minutes": minutes,
                          "expires_at": _now_iso(self._expires_wall)})

    # -- requests ---------------------------------------------------------

    def use_code(self, code):
        """Checks a code from the authenticator app: None when it's right (and
        now used up), else the refusal to send back. Wrong and reused codes
        count towards the lockout, whatever the code was for (switching root
        on, or loosening the watchdog)."""
        locked = self._locked_s()
        if locked:
            return {"ok": False, "reason": "locked", "locked_s": locked,
                    "error": f"Too many wrong codes. Try again in {(locked + 59) // 60} min."}
        if not isinstance(code, str) or len(code) != TOTP_DIGITS or not code.isdigit():
            # Not counted: it can't be a guess that matches.
            return {"ok": False, "reason": "bad_request", "error": f"The code is {TOTP_DIGITS} digits."}
        ok, reason = self.verifier.check(code)
        if not ok:
            if reason in ("wrong", "reused"):
                self._bad_codes += 1
                if self._bad_codes >= MAX_BAD_CODES:
                    self._locked_until = self.clock() + LOCKOUT_S
                    self.audit.write({"type": "locked", "seconds": LOCKOUT_S})
            error = _REFUSALS.get(reason) or self.verifier.problem() or "No authenticator key."
            return {"ok": False, "reason": reason, "error": error,
                    "attempts_left": max(0, MAX_BAD_CODES - self._bad_codes),
                    "locked_s": self._locked_s()}
        self._bad_codes = 0
        return None

    def enable_with_code(self, code, minutes) -> dict:
        """From Odysseus: needs a fresh code from the authenticator app."""
        self.tick()
        try:
            minutes = parse_minutes(minutes, self.max_minutes)
        except ValueError as e:
            return {"ok": False, "reason": "bad_request", "error": str(e)}
        refusal = self.use_code(code)
        if refusal:
            return refusal
        self._switch_on(minutes, "code")
        return {"ok": True, **self.status()}

    def enable_from_control(self, minutes) -> dict:
        self.tick()
        try:
            minutes = parse_minutes(minutes, self.max_minutes)
        except ValueError as e:
            return {"ok": False, "reason": "bad_request", "error": str(e)}
        self._switch_on(minutes, "control")
        return {"ok": True, **self.status()}

    def revoke(self, via: str) -> dict:
        was_on = self.is_on()
        self._until = self._expires_wall = self._minutes = self._since_wall = None
        if was_on:
            self.audit.write({"type": "root_off", "why": "revoked", "via": via})
        return {"ok": True, "was_on": was_on, **self.status()}


# ---------------------------------------------------------------------------
# The watchdog (5c): which tier a root command is in
# ---------------------------------------------------------------------------
#
# automatic  parses exactly as one of the allowed forms below
# approval   everything else: you approve it, one command at a time (5d)
# refused    names something the root helper guards; root switches off (5d)
#
# The refused list is a tripwire on the command's text, not a wall: a disguised
# command gets past it, but a disguised command can't parse as automatic, so it
# still needs your approval. The automatic forms are what has to be exact.

# Decision 4 (2026-10-03). Anything that names one of these is refused.
BUILTIN_REFUSED_PATHS = (
    # The helpers' own files, sockets, config, state and logs.
    "/opt/creator-helper", "/opt/creator-root",
    "/srv/creator-helper", "/srv/creator-root",
    "/etc/creator-root", "/var/lib/creator-root", "/run/creator-root",
    "/var/log/creator-helper", "/var/log/creator-root",
    # Units, sudo, accounts, polkit, SSH, Docker.
    "/etc/systemd/system", "/etc/sudoers", "/etc/shadow", "/etc/gshadow",
    "/etc/passwd", "/etc/group", "/etc/polkit-1", "/etc/ssh", "/root/.ssh",
    "/var/run/docker.sock", "/run/docker.sock",
)
# Matched anywhere in the command: the services' names (in `systemctl stop
# creator-root-helper`, say) and the helpers' file names, wherever they are.
BUILTIN_REFUSED_FRAGMENTS = (
    ".ssh/", "authorized_keys", "creator-helper", "creator-root", "creator_helper.py", "root_helper.py",
)
# Matched as whole words: the tools that change users and passwords.
BUILTIN_REFUSED_TOOLS = (
    "visudo", "passwd", "chpasswd", "useradd", "usermod", "userdel", "adduser", "gpasswd",
)
_TOOL_RE = re.compile(r"(?<![\w.+-])(" + "|".join(BUILTIN_REFUSED_TOOLS) + r")(?![\w.+-])")

# Decision 3. Each can be switched off in the settings.
AUTOMATIC_FORMS = {
    "apt_update": "apt-get update",
    "apt_upgrade": "apt-get upgrade",
    "apt_install": "apt-get install <package names>",
    "chmod_chown": "chmod / chown inside the allowed folders",
}
DEFAULT_ALLOWED_FOLDERS = ("/var/www", "/srv")
DEFAULT_MAX_COMMAND_S = 900
MIN_COMMAND_S = 10
MAX_COMMAND_S = 3600
MAX_FOLDERS = 20
MAX_EXTRA_REFUSED = 50
MAX_PATH_CHARS = 200
MAX_COMMAND_CHARS = 4000

# Folders that can't be allowed for chmod/chown as a whole: the system's own.
SYSTEM_FOLDERS = frozenset((
    "/", "/bin", "/boot", "/dev", "/etc", "/home", "/lib", "/lib32", "/lib64", "/libx32", "/media",
    "/mnt", "/opt", "/proc", "/root", "/run", "/sbin", "/sys", "/tmp", "/usr", "/var",
))

# An automatic command has no shell syntax at all: these characters only, split
# on spaces. In 5d it runs without a shell, as exactly these words.
_PLAIN_RE = re.compile(r"^[A-Za-z0-9 _./:+,=-]+$")
_PACKAGE_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]+$")
_OCTAL_MODE_RE = re.compile(r"^[01]?[0-7]{3}$")   # no setuid (4) or setgid (2) digit
_SYMBOLIC_MODE_RE = re.compile(r"^[ugoa]*[-+=][rwxXt]*(,[ugoa]*[-+=][rwxXt]*)*$")   # no s
_NAME_RE = re.compile(r"^([a-z_][a-z0-9_-]{0,31}|[0-9]{1,10})$")
_APT_OPTIONS = {"update": {"-y", "-q"}, "upgrade": {"-y", "-q"},
                "install": {"-y", "-q", "--no-install-recommends"}}
_RECURSIVE = {"-R", "--recursive"}


def default_watchdog() -> dict:
    return {
        "automatic": {form: True for form in AUTOMATIC_FORMS},
        "allowed_folders": list(DEFAULT_ALLOWED_FOLDERS),
        "extra_refused": [],
        "max_command_s": DEFAULT_MAX_COMMAND_S,
    }


def closed_watchdog() -> dict:
    """What's used while the settings file can't be trusted: nothing automatic."""
    return {
        "automatic": {form: False for form in AUTOMATIC_FORMS},
        "allowed_folders": [],
        "extra_refused": [],
        "max_command_s": MIN_COMMAND_S,
    }


def _inside(path: str, folder: str) -> bool:
    """path is folder or anything under it."""
    folder = folder.rstrip("/") or "/"
    return path == folder or path.startswith(folder if folder == "/" else folder + "/")


def _clean_path(value, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{what}: an empty entry")
    value = value.strip()
    if len(value) > MAX_PATH_CHARS or any(ord(c) < 32 for c in value):
        raise ValueError(f"{what}: {value[:60]!r} is too long or has control characters")
    if not value.startswith("/"):
        raise ValueError(f"{what}: {value!r} must be a full path, starting with /")
    if any(part == ".." for part in value.split("/")):
        raise ValueError(f"{what}: {value!r} can't contain ..")
    return os.path.normpath(value).replace("//", "/")


def validate_watchdog(raw) -> dict:
    """The settings as sent, checked and tidied; ValueError says what's wrong."""
    if not isinstance(raw, dict):
        raise ValueError("settings must be an object")
    automatic = raw.get("automatic")
    if not isinstance(automatic, dict) or set(automatic) != set(AUTOMATIC_FORMS) \
            or not all(isinstance(v, bool) for v in automatic.values()):
        raise ValueError(f"automatic must give true/false for each of: {', '.join(AUTOMATIC_FORMS)}")
    folders, extra = raw.get("allowed_folders"), raw.get("extra_refused")
    if not isinstance(folders, list) or len(folders) > MAX_FOLDERS:
        raise ValueError(f"allowed_folders must be a list of at most {MAX_FOLDERS} folders")
    if not isinstance(extra, list) or len(extra) > MAX_EXTRA_REFUSED:
        raise ValueError(f"extra_refused must be a list of at most {MAX_EXTRA_REFUSED} paths")
    clean_folders = []
    for value in folders:
        folder = _clean_path(value, "Allowed folders")
        if folder in SYSTEM_FOLDERS:
            raise ValueError(f"Allowed folders: {folder} is a system folder; name a folder inside it")
        hit = next((p for p in BUILTIN_REFUSED_PATHS if _inside(folder, p)), None)
        if hit:
            raise ValueError(f"Allowed folders: {folder} is inside {hit}, which the watchdog refuses")
        if folder not in clean_folders:
            clean_folders.append(folder)
    clean_extra = []
    for value in extra:
        path = _clean_path(value, "Extra refused paths")
        if path == "/":
            raise ValueError("Extra refused paths: / would refuse every command")
        if path not in clean_extra:
            clean_extra.append(path)
    limit = raw.get("max_command_s")
    if isinstance(limit, bool) or not isinstance(limit, int) or not MIN_COMMAND_S <= limit <= MAX_COMMAND_S:
        raise ValueError(f"max_command_s must be a whole number from {MIN_COMMAND_S} to {MAX_COMMAND_S}")
    return {"automatic": {form: automatic[form] for form in AUTOMATIC_FORMS},
            "allowed_folders": clean_folders, "extra_refused": clean_extra, "max_command_s": limit}


def loosenings(old: dict, new: dict) -> list:
    """What in `new` lets more through than `old` (empty: the same or tighter)."""
    out = []
    for form, label in AUTOMATIC_FORMS.items():
        if new["automatic"][form] and not old["automatic"][form]:
            out.append(f"turns on automatic {label}")
    for folder in new["allowed_folders"]:
        if not any(_inside(folder, f) for f in old["allowed_folders"]):
            out.append(f"allows {folder}")
    for path in old["extra_refused"]:
        if path not in new["extra_refused"]:
            out.append(f"stops refusing {path}")
    if new["max_command_s"] > old["max_command_s"]:
        out.append(f"raises the time limit to {new['max_command_s']} s")
    return out


class _Refused(Exception):
    pass


class _AsksApproval(Exception):
    pass


class Watchdog:
    """Judges commands against the built-in refused list and the settings in
    `path` (root-only JSON, written by the helper). The file is read on every
    request, so a hand edit as root counts at once; if it can't be trusted,
    nothing is automatic until it's fixed (fail closed)."""

    def __init__(self, path: str, odysseus_dir: str = "", owner_uid=None):
        self.path = path
        self.odysseus_dir = os.path.normpath(odysseus_dir) if odysseus_dir else ""
        self.owner_uid = os.geteuid() if owner_uid is None else owner_uid

    # -- settings -----------------------------------------------------------

    def load(self):
        """(settings, problem). problem is None when the file is fine or absent."""
        try:
            st = os.lstat(self.path)
        except FileNotFoundError:
            return default_watchdog(), None
        except OSError as e:
            return closed_watchdog(), f"can't read the watchdog settings: {e.strerror or e}"
        if not stat.S_ISREG(st.st_mode) or st.st_uid != self.owner_uid or st.st_mode & 0o022:
            return closed_watchdog(), (f"{self.path} must be a file owned by root that nobody else can "
                                       "change; nothing is automatic until it's fixed")
        try:
            with open(self.path, encoding="utf-8") as f:
                return validate_watchdog(json.load(f)), None
        except (OSError, ValueError) as e:
            return closed_watchdog(), f"the watchdog settings are broken ({e}); nothing is automatic until they're fixed"

    def save(self, settings: dict) -> None:
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, mode=0o700, exist_ok=True)
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def refused_paths(self) -> list:
        return list(BUILTIN_REFUSED_PATHS) + ([self.odysseus_dir] if self.odysseus_dir else [])

    def describe(self) -> dict:
        settings, problem = self.load()
        return {
            "settings": settings,
            "problem": problem,
            "defaults": default_watchdog(),
            "forms": dict(AUTOMATIC_FORMS),
            "builtin_refused": {"paths": self.refused_paths(), "anywhere": list(BUILTIN_REFUSED_FRAGMENTS),
                                "tools": list(BUILTIN_REFUSED_TOOLS)},
            "limits": {"min_command_s": MIN_COMMAND_S, "max_command_s": MAX_COMMAND_S,
                       "max_folders": MAX_FOLDERS, "max_extra_refused": MAX_EXTRA_REFUSED},
        }

    # -- judging ------------------------------------------------------------

    def _refused_hit(self, text: str, settings: dict):
        """What in `text` the refused list names, or None."""
        squashed = re.sub(r"/+", "/", text)
        while "/./" in squashed:
            squashed = squashed.replace("/./", "/")
        for candidate in (text, squashed):
            for path in self.refused_paths() + settings["extra_refused"]:
                if path in candidate:
                    return path
            for fragment in BUILTIN_REFUSED_FRAGMENTS:
                if fragment in candidate:
                    return fragment
            m = _TOOL_RE.search(candidate)
            if m:
                return m.group(1)
        return None

    def judge(self, command) -> dict:
        """{"tier", "reason", ...}. Pure: runs nothing and changes nothing."""
        if not isinstance(command, str) or not command.strip():
            raise ValueError("command must be a non-empty string")
        if len(command) > MAX_COMMAND_CHARS:
            raise ValueError(f"command is longer than {MAX_COMMAND_CHARS} characters")
        settings, problem = self.load()
        verdict = {"max_command_s": settings["max_command_s"]}
        if problem:
            verdict["problem"] = problem
        hit = self._refused_hit(command, settings)
        if hit:
            return {**verdict, "tier": "refused", "refused_by": hit, "switch_off": True,
                    "reason": f"It names {hit}, which root commands may never touch."}
        try:
            form, argv = self._automatic(command, settings)
        except _Refused as e:
            return {**verdict, "tier": "refused", "refused_by": str(e), "switch_off": True,
                    "reason": f"A target resolves to {e}, which root commands may never touch."}
        except _AsksApproval as e:
            return {**verdict, "tier": "approval", "reason": str(e)}
        return {**verdict, "tier": "automatic", "form": form, "argv": argv,
                "reason": f"It is {AUTOMATIC_FORMS[form]}, an automatic form."}

    def _automatic(self, command: str, settings: dict):
        """(form, argv) for an automatic command; raises _AsksApproval (why
        not) or _Refused (a target resolves into a refused path)."""
        if not _PLAIN_RE.match(command):
            bad = sorted({c for c in command if not _PLAIN_RE.match(c)})
            shown = " ".join("space" if c == " " else repr(c) for c in bad[:5])
            raise _AsksApproval(f"It uses characters an automatic command can't have ({shown}): "
                                "shell syntax, quotes and globs always need approval.")
        argv = command.split()
        name = argv[0]
        if name == "apt-get":
            return self._apt(argv, settings), argv
        if name in ("chmod", "chown"):
            return self._chmod_chown(argv, settings), argv
        raise _AsksApproval(f"{name} isn't one of the automatic forms (apt-get update/upgrade/install, chmod, chown).")

    @staticmethod
    def _apt(argv, settings) -> str:
        options = [a for a in argv[1:] if a.startswith("-")]
        words = [a for a in argv[1:] if not a.startswith("-")]
        unknown = [o for o in options if not any(o in allowed for allowed in _APT_OPTIONS.values())]
        if unknown:
            # Before the subcommand: an option's value (-t <release>) isn't one.
            raise _AsksApproval(f"apt-get option {unknown[0]} isn't allowed automatically "
                                "(only -y, -q, and --no-install-recommends for install).")
        if not words or words[0] not in _APT_OPTIONS:
            sub = words[0] if words else "(nothing)"
            raise _AsksApproval(f"apt-get {sub} isn't automatic: only update, upgrade and install are.")
        sub, rest = words[0], words[1:]
        form = "apt_" + sub
        if not settings["automatic"][form]:
            raise _AsksApproval(f"Automatic {AUTOMATIC_FORMS[form]} is switched off in the watchdog settings.")
        bad = [o for o in options if o not in _APT_OPTIONS[sub]]
        if bad:
            raise _AsksApproval(f"apt-get option {bad[0]} isn't allowed automatically "
                                f"(only {', '.join(sorted(_APT_OPTIONS[sub]))}).")
        if sub != "install":
            if rest:
                raise _AsksApproval(f"apt-get {sub} takes no names here.")
            return form
        if not rest:
            raise _AsksApproval("apt-get install needs package names.")
        for pkg in rest:
            if not _PACKAGE_RE.match(pkg) or pkg.endswith(".deb"):
                raise _AsksApproval(f"{pkg} isn't a plain package name (no .deb files, paths, URLs, "
                                    "versions or releases automatically).")
        return form

    def _chmod_chown(self, argv, settings) -> str:
        tool = argv[0]
        if not settings["automatic"]["chmod_chown"]:
            raise _AsksApproval(f"Automatic {AUTOMATIC_FORMS['chmod_chown']} is switched off in the watchdog settings.")
        args = argv[1:]
        while args and args[0] in _RECURSIVE:
            args = args[1:]
        if len(args) < 2 or any(a.startswith("-") for a in args):
            raise _AsksApproval(f"Automatic {tool} is `{tool} [-R] <{'mode' if tool == 'chmod' else 'owner[:group]'}> "
                                "<paths>`, with no other options.")
        spec, targets = args[0], args[1:]
        if tool == "chmod":
            if not (_OCTAL_MODE_RE.match(spec) or _SYMBOLIC_MODE_RE.match(spec)):
                raise _AsksApproval(f"Mode {spec} isn't automatic (setuid and setgid always need approval).")
        else:
            self._check_owner(spec)
        folders = settings["allowed_folders"]
        if not folders:
            raise _AsksApproval(f"No folders are allowed for automatic {tool}.")
        for target in targets:
            self._check_target(target, folders, settings)
        return "chmod_chown"

    @staticmethod
    def _check_owner(spec: str) -> None:
        user, _, group = spec.partition(":")
        if not user and not group:
            raise _AsksApproval(f"{spec} isn't owner[:group].")
        for value, lookup, what in ((user, pwd.getpwnam, "user"), (group, grp.getgrnam, "group")):
            if not value:
                continue
            if not _NAME_RE.match(value):
                raise _AsksApproval(f"{value} isn't a plain {what} name.")
            if not value.isdigit():
                try:
                    lookup(value)
                except KeyError:
                    raise _AsksApproval(f"There's no {what} called {value} on this machine.")

    def _check_target(self, target: str, folders, settings) -> None:
        if not target.startswith("/"):
            raise _AsksApproval(f"{target} isn't a full path (automatic targets start with /).")
        plain = os.path.normpath(target)
        try:
            os.lstat(target)
        except OSError:
            raise _AsksApproval(f"{target} doesn't exist (or can't be seen).")
        real = os.path.realpath(target)
        hit = self._refused_hit(real, settings)
        if hit:
            raise _Refused(hit)
        roots = set(folders) | {os.path.realpath(f) for f in folders}
        for path in (plain, real):
            # Below a folder, not the folder itself.
            if not any(path.startswith(f.rstrip("/") + "/") for f in roots):
                shown = path if path == plain else f"{plain} (really {real})"
                if path in roots:
                    raise _AsksApproval(f"{shown} is an allowed folder itself; only what's inside it is automatic.")
                raise _AsksApproval(f"{shown} isn't inside an allowed folder ({', '.join(folders)}).")


# ---------------------------------------------------------------------------
# Sockets
# ---------------------------------------------------------------------------

def check_socket_folder(path: str, owner_uid: int) -> None:
    """The folder a socket lives in must be the helper's own: owned by it, and
    neither writable by others (they could swap the socket) nor open to
    everyone (it should be entered only by those let in with an ACL)."""
    folder = os.path.dirname(os.path.abspath(path))
    try:
        st = os.stat(folder)
    except FileNotFoundError:
        raise RuntimeError(f"{folder} doesn't exist (see README.md, install)")
    if st.st_uid != owner_uid:
        raise RuntimeError(f"{folder} must be owned by root")
    if st.st_mode & 0o022 or st.st_mode & 0o007:
        raise RuntimeError(f"{folder} must be mode 0700 (plus an ACL for uid 1000), "
                           f"not {stat.S_IMODE(st.st_mode):04o}")


class RootHelper:
    def __init__(self, switch: RootSwitch, audit: AuditLog, socket_path: str, allow_uids,
                 control_socket_path: str, control_uids=(0,), owner_uid=None, watchdog: Watchdog = None):
        allow = frozenset(int(u) for u in allow_uids)
        if not allow:
            raise ValueError("refusing to start with no --allow-uid: nobody could use it")
        if 0 in allow:
            raise ValueError("uid 0 can't be allowed on the Odysseus socket; root uses the control socket")
        self.switch = switch
        self.audit = audit
        self.watchdog = watchdog or Watchdog(DEFAULT_WATCHDOG_FILE, owner_uid=owner_uid)
        self.socket_path = socket_path
        self.control_socket_path = control_socket_path
        self.allow_uids = allow
        self.control_uids = frozenset(int(u) for u in control_uids)
        self.owner_uid = os.geteuid() if owner_uid is None else owner_uid
        self.started = time.time()
        self._servers = []
        self._ticker = None

    def hello(self) -> dict:
        return {
            "ok": True,
            "type": "hello",
            "helper": "creator-root-helper",
            "version": VERSION,
            "capabilities": list(CAPABILITIES),
            # 5b/5c: the switch and the watchdog. Nothing runs as root through
            # this helper yet (5d).
            "runs_commands": False,
            "uptime_s": int(time.time() - self.started),
            **self.switch.status(),
        }

    def answer(self, request: dict, control: bool) -> dict:
        rtype = request.get("type")
        via = "control" if control else "odysseus"
        if rtype == "hello" and not control:
            return self.hello()
        if rtype == "status":
            return {"ok": True, "type": "status", **self.switch.status()}
        if rtype == "enable":
            if control:
                return {"type": "enable", **self.switch.enable_from_control(request.get("minutes"))}
            return {"type": "enable", **self.switch.enable_with_code(request.get("code"), request.get("minutes"))}
        if rtype == "revoke":
            return {"type": "revoke", **self.switch.revoke(via)}
        if rtype == "check":
            try:
                verdict = self.watchdog.judge(request.get("command"))
            except ValueError as e:
                return {"ok": False, "type": "check", "reason": "bad_request", "error": str(e)}
            return {"ok": True, "type": "check", **verdict}
        if rtype == "watchdog":
            return {"ok": True, "type": "watchdog", **self.watchdog.describe()}
        if rtype == "watchdog_save":
            return {"type": "watchdog_save", **self.save_watchdog(request, via)}
        caps = CONTROL_CAPABILITIES if control else CAPABILITIES
        return {"ok": False, "error": f"unknown request type: {str(rtype)[:40]!r}", "capabilities": list(caps)}

    def save_watchdog(self, request: dict, via: str) -> dict:
        """New watchdog settings. A change that loosens it needs a code from
        the authenticator app, except from the control socket (root). The
        helper decides what loosens, so Odysseus can't call a loosening a
        tightening."""
        try:
            new = validate_watchdog(request.get("settings"))
        except ValueError as e:
            return {"ok": False, "reason": "bad_request", "error": str(e)}
        old, _ = self.watchdog.load()
        loosens = loosenings(old, new)
        if loosens and via != "control":
            code = request.get("code")
            if not code:
                return {"ok": False, "reason": "code_needed", "loosens": loosens,
                        "error": "This loosens the watchdog (" + "; ".join(loosens) + "), so it needs a code "
                                 "from your authenticator app."}
            refusal = self.switch.use_code(code)
            if refusal:
                return {**refusal, "loosens": loosens}
        try:
            self.watchdog.save(new)
        except OSError as e:
            return {"ok": False, "reason": "write_failed",
                    "error": f"The helper couldn't write {self.watchdog.path}: {e.strerror or e}"}
        self.audit.write({"type": "watchdog_saved", "via": via, "loosened": bool(loosens), "loosens": loosens,
                          "old": old, "new": new})
        return {"ok": True, "loosened": bool(loosens), **self.watchdog.describe()}

    async def _send(self, writer, reply: dict) -> None:
        writer.write((json.dumps(reply) + "\n").encode("utf-8"))
        try:
            await asyncio.wait_for(writer.drain(), timeout=READ_TIMEOUT_S)
        except (asyncio.TimeoutError, ConnectionError):
            pass

    async def _on_connection(self, reader, writer, control: bool) -> None:
        sock = writer.get_extra_info("socket")
        entry = {"type": "connection", "socket": "control" if control else "odysseus"}
        try:
            try:
                pid, uid, gid = peer_credentials(sock)
            except OSError as e:
                entry.update(ok=False, error=f"no peer credentials: {e}")
                return
            entry.update(peer_pid=pid, peer_uid=uid, peer_gid=gid)
            allowed = self.control_uids if control else self.allow_uids
            if uid not in allowed:
                # Refused before anything the peer sent is read.
                entry.update(ok=False, error="peer uid not allowed")
                await self._send(writer, {"ok": False, "error": "not allowed"})
                return
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=READ_TIMEOUT_S)
            except asyncio.TimeoutError:
                entry.update(ok=False, error="read timeout")
                await self._send(writer, {"ok": False, "error": "read timeout"})
                return
            except (ValueError, asyncio.LimitOverrunError):
                entry.update(ok=False, error="request too large")
                await self._send(writer, {"ok": False, "error": "request too large"})
                return
            try:
                request = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                entry.update(ok=False, error="bad JSON")
                await self._send(writer, {"ok": False, "error": "request must be one line of JSON"})
                return
            if not isinstance(request, dict):
                entry.update(ok=False, error="not an object")
                await self._send(writer, {"ok": False, "error": "request must be a JSON object"})
                return
            entry["request_type"] = str(request.get("type"))[:40]
            if request.get("type") == "enable":
                # Never the code itself.
                entry["minutes"] = request.get("minutes") if isinstance(request.get("minutes"), int) else None
            if request.get("type") == "check" and isinstance(request.get("command"), str):
                entry["command"] = request["command"][:2000]
            reply = self.answer(request, control)
            entry.update(ok=reply.get("ok"), error=reply.get("error"), reason=reply.get("reason"))
            if reply.get("tier"):
                entry["tier"] = reply["tier"]
            await self._send(writer, reply)
        finally:
            self.audit.write(entry)
            try:
                writer.close()
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    @staticmethod
    def _remove_stale_socket(path: str) -> None:
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(st.st_mode):
            raise RuntimeError(f"{path} exists and is not a socket; not touching it")
        os.unlink(path)

    async def _listen(self, path: str, mode: int, control: bool):
        check_socket_folder(path, self.owner_uid)
        self._remove_stale_socket(path)
        old_umask = os.umask(0o177)   # no window where the socket is open to others
        try:
            server = await asyncio.start_unix_server(
                lambda r, w: self._on_connection(r, w, control), path=path, limit=MAX_REQUEST_BYTES)
        finally:
            os.umask(old_umask)
        os.chmod(path, mode)
        return server

    async def _tick_forever(self) -> None:
        while True:
            await asyncio.sleep(1)
            self.switch.tick()

    async def start(self) -> None:
        self._servers.append(await self._listen(self.socket_path, SOCKET_MODE, control=False))
        self._servers.append(await self._listen(self.control_socket_path, CONTROL_SOCKET_MODE, control=True))
        self._ticker = asyncio.ensure_future(self._tick_forever())
        self.audit.write({"type": "start", "socket": self.socket_path, "control_socket": self.control_socket_path,
                          "allow_uids": sorted(self.allow_uids), "version": VERSION,
                          "max_minutes": self.switch.max_minutes,
                          "totp_ready": self.switch.verifier.problem() is None,
                          "watchdog_problem": self.watchdog.load()[1]})

    async def stop(self) -> None:
        if self._ticker is not None:
            self._ticker.cancel()
            self._ticker = None
        was_on = self.switch.is_on()
        for server in self._servers:
            server.close()
            await server.wait_closed()
        self._servers = []
        for path in (self.socket_path, self.control_socket_path):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
        self.audit.write({"type": "stop", "root_was_on": was_on})


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def _require_root() -> None:
    if os.geteuid() != 0:
        raise SystemExit("creator-root-helper: run this as root (sudo)")


async def _serve(args) -> None:
    audit = AuditLog(args.audit_log)
    switch = RootSwitch(TotpVerifier(args.key_file, args.state_file), audit, max_minutes=args.max_minutes)
    watchdog = Watchdog(args.watchdog_file, odysseus_dir=args.odysseus_dir)
    helper = RootHelper(switch, audit, args.socket, args.allow_uid, args.control_socket, watchdog=watchdog)
    await helper.start()
    problem = switch.verifier.problem()
    print(f"creator-root-helper {VERSION}: listening on {args.socket} for uid(s) {sorted(helper.allow_uids)}, "
          f"control on {args.control_socket}" + (f"; WARNING: {problem}" if problem else ""),
          file=sys.stderr, flush=True)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    await helper.stop()


def _setup_totp(args) -> int:
    if os.path.exists(args.key_file) and not args.force:
        print(f"{args.key_file} already exists. Use --force to replace it (your app's current entry "
              "stops working).", file=sys.stderr)
        return 1
    key_text = new_key_text()
    os.makedirs(os.path.dirname(args.key_file), mode=0o700, exist_ok=True)
    tmp = args.key_file + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(key_text + "\n")
    os.replace(tmp, args.key_file)
    account = args.account or f"root@{socket.gethostname()}"
    uri = otpauth_uri(key_text, account)
    print(f"Wrote {args.key_file} (root, 0600).\n")
    print("Add it to your authenticator app, by scanning the QR code below or by typing the key:\n")
    print(f"  key:  {' '.join(key_text[i:i + 4] for i in range(0, len(key_text), 4))}")
    print(f"  link: {uri}\n")
    qrencode = shutil.which("qrencode")
    if qrencode:
        subprocess.run([qrencode, "-t", "ansiutf8", uri], check=False)
    else:
        print("(Install qrencode to get a QR code here: sudo apt-get install qrencode)\n")
    if sys.stdin.isatty():
        key = parse_key(key_text)
        for _ in range(3):
            code = input("Type a code from the app to check it (Enter to skip): ").strip()
            if not code:
                break
            now = time_step(time.time())
            if any(hmac.compare_digest(hotp(key, s), code)
                   for s in range(now - TOTP_DRIFT_STEPS, now + TOTP_DRIFT_STEPS + 1)):
                print("That code is right. The app is set up.")
                break
            print("That code doesn't match. Check the app's entry and the clock on both devices.")
    print("\nThe running helper reads the key on each code; no restart needed.")
    return 0


def _control(args) -> int:
    request = {"type": {"on": "enable", "off": "revoke", "status": "status", "check": "check"}[args.command]}
    if args.command == "on":
        request["minutes"] = args.minutes
    if args.command == "check":
        request["command"] = args.text
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(READ_TIMEOUT_S)
        s.connect(args.control_socket)
        s.sendall((json.dumps(request) + "\n").encode("utf-8"))
        reply = json.loads(s.makefile(encoding="utf-8").readline() or "{}")
        s.close()
    except (OSError, ValueError) as e:
        print(f"Couldn't reach the root helper at {args.control_socket}: {e}. "
              "Is it running? systemctl status creator-root-helper", file=sys.stderr)
        return 2
    if not reply.get("ok"):
        print(f"Refused: {reply.get('error') or 'no reason given'}", file=sys.stderr)
        return 1
    if args.command == "check":
        print(f"{reply.get('tier')}: {reply.get('reason')}")
        if reply.get("problem"):
            print(f"Note: {reply['problem']}")
        return 0
    if reply.get("on"):
        print(f"Root is ON until {reply.get('expires_at')} ({(reply.get('remaining_s') or 0) // 60} min left).")
    else:
        print("Root is off.")
    if reply.get("totp_problem"):
        print(f"Note: {reply['totp_problem']}")
    return 0


def _parse_args(argv):
    p = argparse.ArgumentParser(description="Creator root helper (the root switch and the watchdog).")
    sub = p.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the helper (the systemd unit does this)")
    serve.add_argument("--allow-uid", type=int, action="append", default=[], required=True,
                       help="uid allowed on the Odysseus socket (the container's user, normally 1000); repeatable")
    serve.add_argument("--socket", default=DEFAULT_SOCKET)
    serve.add_argument("--control-socket", default=DEFAULT_CONTROL_SOCKET)
    serve.add_argument("--audit-log", default=DEFAULT_AUDIT_LOG)
    serve.add_argument("--key-file", default=DEFAULT_KEY_FILE)
    serve.add_argument("--state-file", default=DEFAULT_STATE_FILE)
    serve.add_argument("--watchdog-file", default=DEFAULT_WATCHDOG_FILE)
    serve.add_argument("--odysseus-dir", default="",
                       help="the Odysseus folder on the host; root commands naming it are refused")
    serve.add_argument("--max-minutes", type=int, default=MAX_MINUTES,
                       help=f"longest root window, minutes (at most {MAX_MINUTES})")

    setup = sub.add_parser("setup-totp", help="create the authenticator key and show it")
    setup.add_argument("--key-file", default=DEFAULT_KEY_FILE)
    setup.add_argument("--account", default="", help="name shown in the app (default root@<hostname>)")
    setup.add_argument("--force", action="store_true", help="replace an existing key")

    for name, text in (("on", "switch root on (no code needed: you're root)"),
                       ("off", "switch root off now"), ("status", "is root on?"),
                       ("check", "which tier the watchdog puts a command in (runs nothing)")):
        c = sub.add_parser(name, help=text)
        c.add_argument("--control-socket", default=DEFAULT_CONTROL_SOCKET)
        if name == "on":
            c.add_argument("minutes", type=int, nargs="?", default=DEFAULT_ON_MINUTES,
                           help=f"how long (default {DEFAULT_ON_MINUTES}, at most {MAX_MINUTES})")
        if name == "check":
            c.add_argument("text", metavar="command", help="the command, in quotes")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    _require_root()
    if args.command == "serve":
        asyncio.run(_serve(args))
        return 0
    if args.command == "setup-totp":
        return _setup_totp(args)
    return _control(args)


if __name__ == "__main__":
    sys.exit(main())
