"""Creator mode routes — /api/creator/*.

Modelled on routes/research/research_routes.py. Every route requires the
`can_use_creator` privilege, which is off by default for non-admin users.
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from core.middleware import INTERNAL_TOOL_USER
from src.auth_helpers import require_user
from src.creator_mode import CreatorManager, is_valid_job_id, privilege_disabled_tools
from src.endpoint_resolver import resolve_endpoint

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

    class CreatorStartRequest(BaseModel):
        task: str = Field(..., min_length=1, max_length=20000)
        endpoint_id: Optional[str] = None
        model: Optional[str] = None

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

        job_id = creator_manager.start_job(
            task=task,
            endpoint_url=ep_url,
            model=ep_model,
            headers=ep_headers,
            owner=user,
            disabled_tools=disabled,
        )
        return {"job_id": job_id, "status": "running", "model": ep_model}

    @router.get("/api/creator/status/{job_id}")
    async def creator_status(job_id: str, request: Request, since: int = 0):
        """Job status plus the event log from index `since` onwards."""
        user = _require_creator_user(request)
        job = _owned_job(job_id, user)
        events = job.get("events") or []
        since = max(0, since)
        return {
            "job_id": job["id"],
            "task": job["task"],
            "status": job["status"],
            "started_at": job["started_at"],
            "finished_at": job["finished_at"],
            "model": job["model"],
            "error": job["error"],
            "event_count": len(events),
            "events": events[since:],
            "has_report": bool(job.get("report")),
        }

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
        }

    return router
