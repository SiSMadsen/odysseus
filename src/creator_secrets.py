# src/creator_secrets.py
"""
Creator mode Secrets section (Phase 4 of docs/creator-plan.md).

Secrets are rows in `creator_secrets` (core/database.py, value encrypted at
rest through src/secret_storage.py). Each has an on/off switch. A Creator run
asks for one with the `get_secret` tool; `request_secret` is the server-side
check: the switch must be on, the secret must belong to the run's owner, and
the call must come from a running Creator job. If any check fails the value
never reaches the agent. Every request, allowed or denied, is logged to
data/creator/secret_access.jsonl and the job's audit log.

Known limit (see the plan doc): the agent's bash tool runs unsandboxed in the
same container, so an agent that sets out to can read data/.app_key and the
database and decrypt secrets itself. `secret_store_tripwire_paths` adds those
files to every Creator run's protected paths so a command naming them is
stopped, but that is a tripwire, not a wall.
"""
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Callable, List, Optional

from core.database import CreatorSecret, SessionLocal, utcnow_naive
from src.constants import APP_KEY_FILE, DATA_DIR
from src.creator_safety import append_jsonl

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
MAX_VALUE_LEN = 10_000
MAX_DESCRIPTION_LEN = 1_000


def is_valid_name(name: str) -> bool:
    return isinstance(name, str) and bool(_NAME_RE.fullmatch(name))


def access_log_path() -> Path:
    return Path(DATA_DIR) / "creator" / "secret_access.jsonl"


def _owner_key(owner: Optional[str]) -> str:
    # "" rather than NULL so the (owner, name) unique index also holds in
    # single-user mode (SQL treats NULLs as distinct).
    return owner or ""


def _public(row: CreatorSecret) -> dict:
    """A secret as the API shows it. Never includes the value."""
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description or "",
        "enabled": bool(row.enabled),
        "last_used": row.last_used.isoformat() + "Z" if row.last_used else None,
        "created_at": row.created_at.isoformat() + "Z" if row.created_at else None,
        "updated_at": row.updated_at.isoformat() + "Z" if row.updated_at else None,
    }


class SecretError(ValueError):
    pass


class SecretStore:
    """CRUD for the Secrets section. `session_factory` is injectable for tests."""

    def __init__(self, session_factory: Callable = SessionLocal):
        self._session_factory = session_factory

    def list(self, owner: str) -> List[dict]:
        db = self._session_factory()
        try:
            rows = (db.query(CreatorSecret)
                    .filter(CreatorSecret.owner == _owner_key(owner))
                    .order_by(CreatorSecret.name).all())
            return [_public(r) for r in rows]
        finally:
            db.close()

    def create(self, owner: str, name: str, value: str, description: str = "",
               enabled: bool = False) -> dict:
        name = (name or "").strip()
        if not is_valid_name(name):
            raise SecretError("Name must be 1-64 letters, digits, '_', '.' or '-'.")
        if not isinstance(value, str) or not value:
            raise SecretError("Value is empty.")
        if len(value) > MAX_VALUE_LEN:
            raise SecretError("Value is too long.")
        db = self._session_factory()
        try:
            exists = (db.query(CreatorSecret)
                      .filter(CreatorSecret.owner == _owner_key(owner), CreatorSecret.name == name)
                      .first())
            if exists:
                raise SecretError(f"A secret named '{name}' already exists.")
            row = CreatorSecret(
                id=uuid.uuid4().hex,
                owner=_owner_key(owner),
                name=name,
                description=(description or "")[:MAX_DESCRIPTION_LEN],
                value=value,
                enabled=bool(enabled),
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            return _public(row)
        finally:
            db.close()

    def update(self, owner: str, secret_id: str, *, name: Optional[str] = None,
               value: Optional[str] = None, description: Optional[str] = None,
               enabled: Optional[bool] = None) -> Optional[dict]:
        db = self._session_factory()
        try:
            row = (db.query(CreatorSecret)
                   .filter(CreatorSecret.owner == _owner_key(owner), CreatorSecret.id == secret_id)
                   .first())
            if row is None:
                return None
            if name is not None:
                name = name.strip()
                if not is_valid_name(name):
                    raise SecretError("Name must be 1-64 letters, digits, '_', '.' or '-'.")
                clash = (db.query(CreatorSecret)
                         .filter(CreatorSecret.owner == _owner_key(owner),
                                 CreatorSecret.name == name, CreatorSecret.id != secret_id)
                         .first())
                if clash:
                    raise SecretError(f"A secret named '{name}' already exists.")
                row.name = name
            if value is not None:
                # Empty means "keep the current value" (the form leaves it blank).
                if value:
                    if len(value) > MAX_VALUE_LEN:
                        raise SecretError("Value is too long.")
                    row.value = value
            if description is not None:
                row.description = description[:MAX_DESCRIPTION_LEN]
            if enabled is not None:
                row.enabled = bool(enabled)
            db.commit()
            db.refresh(row)
            return _public(row)
        finally:
            db.close()

    def delete(self, owner: str, secret_id: str) -> bool:
        db = self._session_factory()
        try:
            row = (db.query(CreatorSecret)
                   .filter(CreatorSecret.owner == _owner_key(owner), CreatorSecret.id == secret_id)
                   .first())
            if row is None:
                return False
            db.delete(row)
            db.commit()
            return True
        finally:
            db.close()

    def all_values(self, owner: str) -> List[str]:
        """Every value this owner has stored, switched on or not. Used only to
        build a run's redactor; never sent anywhere."""
        db = self._session_factory()
        try:
            rows = db.query(CreatorSecret).filter(CreatorSecret.owner == _owner_key(owner)).all()
            return [r.value for r in rows if r.value]
        finally:
            db.close()

    def request_secret(self, owner: str, name: str, *, job_id: Optional[str],
                       job_running: bool) -> dict:
        """The server-side check behind get_secret. Returns
        {"allowed": True, "value": ...} or {"allowed": False, "reason": ...}.
        Logs every request."""
        name = (name or "").strip()
        decision = self._decide(owner, name, job_id=job_id, job_running=job_running)
        append_jsonl(access_log_path(), {
            "at": utcnow_naive().isoformat() + "Z",
            "owner": owner,
            "job_id": job_id,
            "name": name,
            "allowed": decision["allowed"],
            "reason": decision.get("reason"),
        })
        return decision

    def _decide(self, owner: str, name: str, *, job_id: Optional[str], job_running: bool) -> dict:
        if not job_running:
            return {"allowed": False,
                    "reason": "get_secret only works inside a running Creator job."}
        if not is_valid_name(name):
            return {"allowed": False, "reason": "Invalid secret name."}
        db = self._session_factory()
        try:
            row = (db.query(CreatorSecret)
                   .filter(CreatorSecret.owner == _owner_key(owner), CreatorSecret.name == name)
                   .first())
            if row is None:
                return {"allowed": False, "reason": f"No secret named '{name}'."}
            if not row.enabled:
                return {"allowed": False,
                        "reason": f"Secret '{name}' is switched off. Ask the user to switch it on "
                                  "in Settings > Secrets if this task needs it."}
            value = row.value
            if not value:
                # secret_storage.decrypt returns "" on a wrong key / corrupt row.
                return {"allowed": False,
                        "reason": f"Secret '{name}' could not be decrypted on the server."}
            row.last_used = utcnow_naive()
            db.commit()
            return {"allowed": True, "value": value}
        finally:
            db.close()


async def do_get_secret(content, owner: Optional[str] = None,
                        session_id: Optional[str] = None) -> dict:
    """The get_secret agent tool. Args: {"name": "<secret name>"} or the bare
    name. The value is returned only when the server-side checks pass."""
    import json
    name = ""
    if isinstance(content, dict):
        name = str(content.get("name") or "")
    else:
        raw = (content or "").strip()
        if raw.startswith("{"):
            try:
                name = str((json.loads(raw) or {}).get("name") or "")
            except (ValueError, AttributeError):
                return {"error": 'Invalid arguments. Use {"name": "<secret name>"}.', "exit_code": 1}
        else:
            name = raw
    name = name.strip()
    if not name:
        return {"error": 'Missing secret name. Use {"name": "<secret name>"}.', "exit_code": 1}

    from src.creator_mode import get_active_manager
    manager = get_active_manager()
    if manager is None:
        return {"error": "get_secret only works inside a running Creator job.", "exit_code": 1}
    decision = manager.request_secret(session_id, owner, name)
    if not decision["allowed"]:
        return {"error": decision["reason"], "exit_code": 1}
    return {"output": decision["value"], "exit_code": 0}


def secret_store_tripwire_paths() -> List[str]:
    """Files that would let a run read every secret directly: the encryption
    key and the SQLite database (with its sidecars). Added to every Creator
    run's protected paths, both as full paths and as bare file names (the
    agent's shell starts inside the data folder, so a relative path works)."""
    paths = [APP_KEY_FILE, os.path.basename(APP_KEY_FILE)]
    try:
        from core.database import _SQLITE_SIDECARS, _sqlite_db_path, engine
        db_path = _sqlite_db_path(engine.url)
    except Exception:
        db_path = None
    if db_path:
        base = os.path.basename(db_path)
        for suffix in ("",) + tuple(_SQLITE_SIDECARS):
            paths.append(db_path + suffix)
            paths.append(base + suffix)
    seen, out = set(), []
    for p in paths:
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out
