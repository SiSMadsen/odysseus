"""Creator mode routes — /api/creator/*.

Modelled on routes/research/research_routes.py. Every route requires the
`can_use_creator` privilege, which is off by default for non-admin users.
"""

import asyncio
import json
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from core.middleware import INTERNAL_TOOL_USER
from src.auth_helpers import require_user
from src.creator_mode import (
    MAX_MAX_MINUTES,
    MIN_MAX_MINUTES,
    CreatorBusyError,
    CreatorManager,
    is_valid_job_id,
    privilege_disabled_tools,
)
from src.creator_safety import protected_paths_from_settings
from src.creator_secrets import SecretError
from src.endpoint_resolver import resolve_endpoint

_STREAM_POLL_S = 0.5

logger = logging.getLogger(__name__)

_PRIVILEGE = "can_use_creator"


def _get_privileges(request: Request, user: str) -> dict:
    auth_mgr = getattr(request.app.state, "auth_manager", None)
    if auth_mgr is None or not user:
        return {}
    try:
        privs = auth_mgr.get_privileges(user) or {}
    except Exception:
        return {}
    return privs if isinstance(privs, dict) else {}


def _require_creator_user(request: Request) -> str:
    """Like src.auth_helpers.require_privilege, but fails CLOSED: a missing
    `can_use_creator` key means not allowed. The agent's own loopback user
    may never drive Creator jobs."""
    user = require_user(request)
    if user == INTERNAL_TOOL_USER:
        raise HTTPException(403, "Creator jobs can't be controlled from inside an agent run.")
    if not user:
        # Auth disabled / single-user mode: privileges aren't enforced.
        return user
    auth_mgr = getattr(request.app.state, "auth_manager", None)
    if auth_mgr is None:
        return user
    if not _get_privileges(request, user).get(_PRIVILEGE, False):
        raise HTTPException(403, "Your account is not allowed to use Creator mode.")
    return user


def _resolve_creator_endpoint(user: str, endpoint_id: Optional[str], model: Optional[str]):
    """(endpoint_url, model, headers) for a Creator run: an explicit owned
    endpoint, else the user's chat/default model."""
    from routes.research.research_routes import _owned_enabled_endpoint, _resolve_endpoint_runtime

    if endpoint_id:
        from src.database import SessionLocal
        db = SessionLocal()
        try:
            ep = _owned_enabled_endpoint(db, user, endpoint_id)
            if not ep:
                raise HTTPException(404, "Endpoint not found or disabled")
            resolved = _resolve_endpoint_runtime(ep, owner=user, model=model)
            if not resolved:
                raise HTTPException(400, "Endpoint is not configured with a usable model.")
            return resolved
        finally:
            db.close()

    ep_url, ep_model, ep_headers = "", "", {}
    for prefix in ("default", "chat"):
        ep_url, ep_model, ep_headers = resolve_endpoint(prefix, owner=user)
        if ep_url:
            break
    if not ep_url:
        raise HTTPException(400, "No endpoints configured. Add one in Settings first.")
    if model:
        ep_model = model
    if not ep_model:
        raise HTTPException(400, "No model selected for Creator mode.")
    return ep_url, ep_model, ep_headers or {}


def setup_creator_routes(creator_manager: CreatorManager) -> APIRouter:
    router = APIRouter(tags=["creator"])

    def _owned_job(job_id: str, user: str) -> dict:
        """404 (not 403) for a missing, malformed or someone else's job."""
        if not is_valid_job_id(job_id):
            raise HTTPException(404, "Creator job not found")
        job = creator_manager.get_job(job_id)
        if job is None or job.get("owner", "") != (user or ""):
            raise HTTPException(404, "Creator job not found")
        return job

    def _events_after(events: list, since: int) -> list:
        # Jobs from before Phase 3 have no `seq`; their position stands in.
        return [e for i, e in enumerate(events) if e.get("seq", i + 1) > since]

    class CreatorStartRequest(BaseModel):
        task: str = Field(..., min_length=1, max_length=20000)
        endpoint_id: Optional[str] = None
        model: Optional[str] = None
        # Time limit for this run; defaults to the creator_max_minutes setting.
        max_minutes: Optional[int] = Field(default=None, ge=MIN_MAX_MINUTES, le=MAX_MAX_MINUTES)

    @router.post("/api/creator/start")
    async def creator_start(body: CreatorStartRequest, request: Request):
        """Start a Creator job in the background."""
        user = _require_creator_user(request)
        task = body.task.strip()
        if not task:
            raise HTTPException(400, "Task is empty")
        ep_url, ep_model, ep_headers = _resolve_creator_endpoint(user, body.endpoint_id, body.model)

        disabled = privilege_disabled_tools(_get_privileges(request, user))
        from src.settings import get_setting
        global_disabled = get_setting("disabled_tools", [])
        if isinstance(global_disabled, list):
            disabled.update(global_disabled)

        try:
            job_id = creator_manager.start_job(
                task=task,
                endpoint_url=ep_url,
                model=ep_model,
                headers=ep_headers,
                owner=user,
                disabled_tools=disabled,
                max_minutes=body.max_minutes,
                protected_paths=protected_paths_from_settings(),
            )
        except CreatorBusyError as e:
            # Don't reveal another user's job id; the owner can find their own.
            raise HTTPException(409, str(e))
        job = creator_manager.get_job(job_id) or {}
        return {
            "job_id": job_id,
            "status": "running",
            "model": ep_model,
            "max_minutes": job.get("max_minutes"),
        }

    @router.get("/api/creator/status/{job_id}")
    async def creator_status(job_id: str, request: Request, since: int = 0):
        """Job status plus the events after sequence number `since`."""
        user = _require_creator_user(request)
        job = _owned_job(job_id, user)
        events = job.get("events") or []
        return {
            "job_id": job["id"],
            "task": job["task"],
            "status": job["status"],
            "started_at": job["started_at"],
            "finished_at": job["finished_at"],
            "max_minutes": job.get("max_minutes"),
            "model": job["model"],
            "error": job["error"],
            "events": _events_after(events, max(0, since)),
            "has_report": bool(job.get("report")),
        }

    @router.get("/api/creator/stream/{job_id}")
    async def creator_stream(job_id: str, request: Request, since: int = 0):
        """Live log: SSE stream of the job's events as they happen, ending
        with a {"final": true, "status": ...} message when the job ends."""
        user = _require_creator_user(request)
        _owned_job(job_id, user)

        async def _generate():
            last = max(0, since)
            while True:
                if await request.is_disconnected():
                    return
                fresh = creator_manager.live_events_after(job_id, last)
                if fresh is None:
                    # Finished: send whatever the live view hadn't, then end.
                    job = creator_manager.get_job(job_id) or {}
                    for event in _events_after(job.get("events") or [], last):
                        yield f"data: {json.dumps(event, default=str)}\n\n"
                    final = {"final": True, "status": job.get("status"), "error": job.get("error")}
                    yield f"data: {json.dumps(final)}\n\n"
                    return
                for event in fresh:
                    last = max(last, event.get("seq", last))
                    yield f"data: {json.dumps(event, default=str)}\n\n"
                await asyncio.sleep(_STREAM_POLL_S)

        return StreamingResponse(
            _generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.post("/api/creator/stop/{job_id}")
    async def creator_stop(job_id: str, request: Request):
        """Stop a running Creator job."""
        user = _require_creator_user(request)
        _owned_job(job_id, user)
        return {"stopped": creator_manager.stop_job(job_id)}

    @router.get("/api/creator/report/{job_id}")
    async def creator_report(job_id: str, request: Request):
        """The final report of a finished Creator job."""
        user = _require_creator_user(request)
        job = _owned_job(job_id, user)
        if job["status"] == "running":
            raise HTTPException(409, "Creator job is still running")
        return {
            "job_id": job["id"],
            "task": job["task"],
            "status": job["status"],
            "report": job.get("report") or "",
            "error": job["error"],
            # Where the full redacted audit log is on the server's disk.
            "audit_log": str(creator_manager.audit_log_path(job["id"])),
        }

    # ------------------------------------------------------------------
    # Secrets section. Values are write-only: no route ever returns one.
    # ------------------------------------------------------------------

    class SecretCreateRequest(BaseModel):
        name: str = Field(..., min_length=1, max_length=64)
        value: str = Field(..., min_length=1, max_length=10_000)
        description: str = Field(default="", max_length=1_000)
        enabled: bool = False

    class SecretUpdateRequest(BaseModel):
        name: Optional[str] = Field(default=None, min_length=1, max_length=64)
        # Empty or missing keeps the current value.
        value: Optional[str] = Field(default=None, max_length=10_000)
        description: Optional[str] = Field(default=None, max_length=1_000)
        enabled: Optional[bool] = None

    def _secret_error(e: Exception):
        raise HTTPException(400, str(e))

    @router.get("/api/creator/secrets")
    async def secrets_list(request: Request):
        """The caller's secrets: names, descriptions, switches. No values."""
        user = _require_creator_user(request)
        return {"secrets": creator_manager.secrets.list(user)}

    @router.post("/api/creator/secrets")
    async def secrets_create(body: SecretCreateRequest, request: Request):
        user = _require_creator_user(request)
        try:
            return creator_manager.secrets.create(
                user, body.name, body.value, body.description, body.enabled)
        except SecretError as e:
            _secret_error(e)

    @router.patch("/api/creator/secrets/{secret_id}")
    async def secrets_update(secret_id: str, body: SecretUpdateRequest, request: Request):
        """Edit a secret or flip its on/off switch."""
        user = _require_creator_user(request)
        try:
            out = creator_manager.secrets.update(
                user, secret_id, name=body.name, value=body.value,
                description=body.description, enabled=body.enabled)
        except SecretError as e:
            _secret_error(e)
        if out is None:
            raise HTTPException(404, "Secret not found")
        return out

    @router.delete("/api/creator/secrets/{secret_id}")
    async def secrets_delete(secret_id: str, request: Request):
        user = _require_creator_user(request)
        if not creator_manager.secrets.delete(user, secret_id):
            raise HTTPException(404, "Secret not found")
        return {"deleted": True}

    return router
