# src/creator_safety.py
"""
Creator mode safety net (Phase 3 of docs/creator-plan.md):

- Redactor: blanks secrets out of anything Creator stores (event log, audit
  log, report). Known secret values (secret-shaped settings and environment
  variables, the run's own API key) plus common token shapes. Phase 4 adds
  the values from the Secrets section.
- Protected actions: a tool call whose input mentions a protected path is not
  run without the user's OK.
- Audit log: one JSONL file per job under data/creator/audit, mode 0600.
- tmux cleanup: the bash tool runs commands in a persistent tmux session named
  after the session id, so stopping the job alone would leave a running
  command behind. Killing the session ends it.
"""
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Callable, Iterable, List, Optional

from src.constants import DATA_DIR
from src.settings_scrub import is_secret_key

logger = logging.getLogger(__name__)

REDACTED = "[REDACTED]"

# Shorter values are too likely to collide with ordinary output.
_MIN_SECRET_LEN = 8

_SECRET_PATTERNS = (
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
     "[REDACTED PRIVATE KEY]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), REDACTED),                 # OpenAI / Anthropic style
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"), REDACTED),  # GitHub tokens
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), REDACTED),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), REDACTED),          # Slack
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), REDACTED),                    # AWS access key id
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), REDACTED),               # Google API key
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{16,}"), r"\1" + REDACTED),
    # NAME=value / NAME: value where NAME looks like a secret.
    (re.compile(
        r"(?i)\b([A-Z0-9_]*(?:PASSWORD|PASSWD|SECRET|TOKEN|API_?KEY|PRIVATE_KEY)[A-Z0-9_]*)"
        r"(\s*[=:]\s*)([\"']?)([^\s\"']{4,})"),
     r"\1\2\3" + REDACTED),
)


def _collect_secret_values(obj: Any, key: str = "", out: Optional[set] = None) -> set:
    out = set() if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            _collect_secret_values(v, str(k), out)
    elif isinstance(obj, list):
        for item in obj:
            _collect_secret_values(item, key, out)
    elif isinstance(obj, str) and key and is_secret_key(key) and len(obj) >= _MIN_SECRET_LEN:
        out.add(obj)
    return out


def _header_secret_values(headers: Optional[dict]) -> set:
    out = set()
    for k, v in (headers or {}).items():
        if not isinstance(v, str):
            continue
        name = str(k).lower()
        if name in ("authorization", "x-api-key", "api-key", "proxy-authorization") or is_secret_key(name):
            value = v.split(None, 1)[1] if v.lower().startswith(("bearer ", "basic ")) else v
            if len(value) >= _MIN_SECRET_LEN:
                out.add(value)
    return out


class Redactor:
    def __init__(self, known_values: Iterable[str] = ()):
        # Longest first so a secret that contains another is blanked whole.
        self._values: List[str] = sorted(
            {v for v in known_values if isinstance(v, str) and len(v) >= _MIN_SECRET_LEN},
            key=len, reverse=True,
        )

    @classmethod
    def for_run(cls, headers: Optional[dict] = None) -> "Redactor":
        values = set(_header_secret_values(headers))
        try:
            from src.settings import load_settings
            values |= _collect_secret_values(load_settings())
        except Exception:
            logger.debug("Creator redactor: could not read settings", exc_info=True)
        values |= {v for k, v in os.environ.items() if is_secret_key(k)}
        return cls(values)

    def add(self, *values: str) -> None:
        """Learn more secret values mid-run (e.g. one handed out by get_secret)."""
        merged = set(self._values) | {
            v for v in values if isinstance(v, str) and len(v) >= _MIN_SECRET_LEN
        }
        self._values = sorted(merged, key=len, reverse=True)

    def _replace_known(self, value: str) -> str:
        for secret in self._values:
            if secret in value:
                value = value.replace(secret, REDACTED)
        return value

    def text(self, value: str) -> str:
        """Known values and common token shapes. For anything stored."""
        if not isinstance(value, str) or not value:
            return value
        value = self._replace_known(value)
        for pattern, repl in _SECRET_PATTERNS:
            value = pattern.sub(repl, value)
        return value

    def known_text(self, value: str) -> str:
        """Known values only. For what the agent itself reads: the token-shape
        patterns would also blank things it legitimately needs to see (a
        config file's PASSWORD= line it is debugging, say)."""
        if not isinstance(value, str) or not value:
            return value
        return self._replace_known(value)

    def obj(self, value: Any) -> Any:
        return self._walk(value, self.text)

    def known_obj(self, value: Any) -> Any:
        return self._walk(value, self.known_text)

    def _walk(self, value: Any, fn: Callable[[str], str]) -> Any:
        if isinstance(value, str):
            return fn(value)
        if isinstance(value, dict):
            return {k: self._walk(v, fn) for k, v in value.items()}
        if isinstance(value, list):
            return [self._walk(v, fn) for v in value]
        return value


# ---------------------------------------------------------------------------
# Protected actions
# ---------------------------------------------------------------------------

def protected_paths_from_settings() -> List[str]:
    try:
        from src.settings import get_setting
        raw = get_setting("creator_protected_paths", []) or []
    except Exception:
        return []
    if not isinstance(raw, list):
        return []
    return [p.strip() for p in raw if isinstance(p, str) and p.strip()]


def make_protected_action_check(paths: Iterable[str]) -> Optional[Callable[[Any, Any], Optional[str]]]:
    """A check for ToolRunSecurityContext.protected_action_check, or None when
    nothing is protected. Matches a protected path anywhere in the tool input
    as a whole path component: "/etc" matches "cat /etc/passwd" but not
    "/etcetera" or "/home/x/etc". This is a tripwire for honest mistakes, not
    a sandbox: a command can reach a path without spelling it out."""
    compiled = []
    for path in paths:
        norm = path.rstrip("/") or "/"
        rx = re.compile(r"(?<![\w.~-])" + re.escape(norm) + r"(?![\w.-])")
        compiled.append((path, rx))
    if not compiled:
        return None

    def check(tool_name: Any, content: Any) -> Optional[str]:
        if isinstance(content, str):
            text = content
        else:
            try:
                text = json.dumps(content, default=str)
            except Exception:
                text = str(content)
        for path, rx in compiled:
            if rx.search(text or ""):
                return (
                    f"Creator mode needs your OK before {tool_name} touches the "
                    f"protected path {path}."
                )
        return None

    return check


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

def audit_dir() -> Path:
    return Path(DATA_DIR) / "creator" / "audit"


class AuditLog:
    """Append-only JSONL audit log for one job. Every entry is redacted before
    it is written. Write failures are logged, never raised into the run."""

    def __init__(self, job_id: str, redactor: Redactor, directory: Optional[Path] = None):
        self.redactor = redactor
        self.path = (directory or audit_dir()) / f"{job_id}.jsonl"

    def write(self, entry: dict) -> None:
        append_jsonl(self.path, self.redactor.obj(entry))


def append_jsonl(path: Path, entry: dict) -> None:
    """Append one JSON line to a private (0600) log file in a 0700 folder.
    Failures are logged, never raised."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
        line = json.dumps(entry, default=str, ensure_ascii=False)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        logger.error("Creator log write failed for %s", path, exc_info=True)


# ---------------------------------------------------------------------------
# tmux cleanup
# ---------------------------------------------------------------------------

async def kill_job_shell(session_id: str) -> None:
    """Kill the tmux session the bash tool used for this job, ending any
    command still running in it. A missing session or tmux is fine."""
    try:
        import shutil
        if not shutil.which("tmux"):
            return
        from src.agent_tools.subprocess_tools import _run_exec, _tmux_session_name
        await _run_exec("tmux", "kill-session", "-t", _tmux_session_name(session_id), timeout=5)
    except Exception:
        logger.debug("Creator: tmux cleanup failed for %s", session_id, exc_info=True)
