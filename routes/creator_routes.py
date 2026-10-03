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
from src.auth_helpers import is_delegated_credential, require_user
from src.creator_mode import (
    ACTIVE_STATUSES,
    MAX_MAX_MINUTES,
    MIN_MAX_MINUTES,
    CreatorBusyError,
    CreatorManager,
    CreatorNotPausedError,
    CreatorResumeError,
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
        # "approve_job" given up front: don't pause for the untrusted-content
        # check during this run. Off by default. Protected paths and the
        # secret switch still apply.
        approve_untrusted: bool = False
        # "Allow all host commands for this job" given up front: host_exec
        # doesn't ask before each command. Off by default; protected paths
        # still ask every time.
        approve_host: bool = False
        # Follow up on an earlier job of yours that has ended: the new job is
        # given that job's task and report before its own task.
        follow_up_of: Optional[str] = None

    @router.post("/api/creator/start")
    async def creator_start(body: CreatorStartRequest, request: Request):
        """Start a Creator job in the background."""
        user = _require_creator_user(request)
        task = body.task.strip()
        if not task:
            raise HTTPException(400, "Task is empty")
        earlier = None
        if body.follow_up_of:
            earlier = _owned_job(body.follow_up_of, user)
            if earlier["status"] in ACTIVE_STATUSES:
                raise HTTPException(400, "That job is still running. Follow up once it has ended.")
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
                approve_untrusted=body.approve_untrusted,
                approve_host=body.approve_host,
                follow_up=earlier,
                allow_root=_root_allowed(request),
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
            "approve_untrusted": body.approve_untrusted,
            "approve_host": body.approve_host,
            "follow_up_of": earlier["id"] if earlier else None,
        }

    @router.get("/api/creator/jobs")
    async def creator_jobs(request: Request, limit: int = 50, archived: bool = False):
        """The caller's jobs, newest first: the Creator window's history.
        Archived jobs only with `archived=true`; `archived_count` says how
        many there are either way."""
        user = _require_creator_user(request)
        return {"jobs": creator_manager.list_jobs(user, limit=limit, include_archived=archived),
                "archived_count": creator_manager.archived_count(user)}

    class ArchiveRequest(BaseModel):
        archived: bool = True

    @router.post("/api/creator/archive/{job_id}")
    async def creator_archive(job_id: str, body: ArchiveRequest, request: Request):
        """Archive (or unarchive) one of your jobs that has ended."""
        user = _require_creator_user(request)
        job = _owned_job(job_id, user)
        if job["status"] in ACTIVE_STATUSES:
            raise HTTPException(400, "A running or paused job can't be archived. Stop it first.")
        creator_manager.set_archived(job_id, body.archived)
        return {"job_id": job_id, "archived": bool(body.archived)}

    @router.get("/api/creator/status/{job_id}")
    async def creator_status(job_id: str, request: Request, since: int = 0):
        """Job status plus the events after sequence number `since`."""
        user = _require_creator_user(request)
        job = _owned_job(job_id, user)
        events = job.get("events") or []
        state = job.get("state") or {}
        return {
            "job_id": job["id"],
            "name": job.get("name"),
            "archived": bool(job.get("archived")),
            "task": job["task"],
            "status": job["status"],
            "started_at": job["started_at"],
            "finished_at": job["finished_at"],
            "max_minutes": job.get("max_minutes"),
            # The time limit runs on while paused; this is when it ends.
            "deadline_at": state.get("deadline_at"),
            "model": job["model"],
            "error": job["error"],
            # What the job is waiting for, when paused: kind ("approval",
            # "question", "blocked"), question, action, and the choices
            # /resume accepts.
            "pause": state.get("pause") if job["status"] == "paused" else None,
            "notes": (state.get("notes") or [])[-20:],
            "events": _events_after(events, max(0, since)),
            "has_report": bool(job.get("report")),
            # The earlier job this one follows up on, if any.
            "follow_up_of": state.get("follow_up_of"),
        }

    class CreatorResumeRequest(BaseModel):
        # Approval pauses: "approve_once", "approve_job" or "deny".
        decision: Optional[str] = None
        # Question / blocked pauses: your answer (may be empty). Also allowed
        # with an approval, as an extra note to the agent.
        answer: Optional[str] = Field(default=None, max_length=10_000)

    @router.post("/api/creator/learn-skill/{job_id}")
    async def creator_learn_skill(job_id: str, request: Request):
        """Learn a skill from one of your finished jobs, now. Says why not
        when it doesn't (the model declined, too unsure, a duplicate...)."""
        user = _require_creator_user(request)
        job = _owned_job(job_id, user)
        if job["status"] != "done":
            raise HTTPException(400, "Only a finished job can teach a skill.")
        ep_url, ep_model, ep_headers = _resolve_creator_endpoint(user, None, None)
        return await creator_manager.learn_skill(job_id, ep_url, ep_model, ep_headers)

    @router.post("/api/creator/resume/{job_id}")
    async def creator_resume(job_id: str, body: CreatorResumeRequest, request: Request):
        """Answer or approve a paused job so it continues."""
        user = _require_creator_user(request)
        _owned_job(job_id, user)
        try:
            return creator_manager.resume_job(job_id, decision=body.decision, answer=body.answer,
                                              interactive=not is_delegated_credential(request))
        except CreatorNotPausedError as e:
            raise HTTPException(409, str(e))
        except CreatorResumeError as e:
            raise HTTPException(400, str(e))

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
        """Stop a running or paused Creator job."""
        user = _require_creator_user(request)
        _owned_job(job_id, user)
        return {"stopped": creator_manager.stop_job(job_id)}

    @router.get("/api/creator/report/{job_id}")
    async def creator_report(job_id: str, request: Request):
        """The final report of a finished Creator job."""
        user = _require_creator_user(request)
        job = _owned_job(job_id, user)
        if job["status"] in ACTIVE_STATUSES:
            raise HTTPException(409, f"Creator job is still {job['status']}")
        return {
            "job_id": job["id"],
            "task": job["task"],
            "status": job["status"],
            # Markdown: asked / done / worked / didn't work / left / exact
            # commands / notes.
            "report": job.get("report") or "",
            # The same, as data (older jobs: empty).
            "report_data": (job.get("state") or {}).get("report") or {},
            "error": job["error"],
            # Where the full redacted audit log is on the server's disk.
            "audit_log": str(creator_manager.audit_log_path(job["id"])),
        }

    @router.get("/api/creator/helper/hello")
    async def creator_helper_hello(request: Request):
        """Connection test for the host helper (Phase 6a): sends `hello`."""
        _require_creator_user(request)
        from src import creator_host_helper
        return await creator_host_helper.hello()

    # ------------------------------------------------------------------
    # The root switch (Phase 5b). The root helper on the host holds it and
    # checks the authenticator code; these routes only pass requests on.
    # ------------------------------------------------------------------

    class RootEnableRequest(BaseModel):
        code: str = Field(..., min_length=6, max_length=6, pattern=r"^[0-9]{6}$")
        minutes: int = Field(..., ge=1, le=90)

    def _require_root_user(request: Request) -> str:
        """Creator privilege, plus: a logged-in admin, in a browser session.
        Fails closed: no login (auth off), no auth manager, or an API token
        (it resolves to an admin, but acts for someone else) is refused."""
        user = _require_creator_user(request)
        auth_mgr = getattr(request.app.state, "auth_manager", None)
        if not user or auth_mgr is None:
            raise HTTPException(403, "Root needs an admin account (log-in turned on).")
        if is_delegated_credential(request):
            raise HTTPException(403, "Root can't be controlled with an API token.")
        try:
            admin = bool(auth_mgr.is_admin(user))
        except Exception:
            admin = False
        if not admin:
            raise HTTPException(403, "Only an admin can switch root.")
        return user

    def _root_allowed(request: Request) -> bool:
        """Whether a job started by this request may be offered run_as_root:
        the same rule as the root routes (logged-in admin, browser session)."""
        try:
            _require_root_user(request)
        except HTTPException:
            return False
        return True

    def _root_unreachable(e: Exception):
        raise HTTPException(503, str(e))

    @router.get("/api/creator/root/status")
    async def creator_root_status(request: Request):
        """Is root on, and until when. `installed` is false when the root
        helper's folder isn't mounted at all (the window then shows nothing)."""
        _require_root_user(request)
        from src import creator_root_helper
        return await creator_root_helper.status()

    @router.post("/api/creator/root/enable")
    async def creator_root_enable(body: RootEnableRequest, request: Request):
        """Switch root on with a code from the authenticator app. The root
        helper checks the code; a code works once."""
        user = _require_root_user(request)
        from src import creator_root_helper
        from src.creator_host_helper import HelperError
        try:
            reply = await creator_root_helper.enable(body.code, body.minutes)
        except HelperError as e:
            _root_unreachable(e)
        logger.info("Creator root switch: enable by %s for %s min: %s", user, body.minutes,
                    "on" if reply.get("ok") else reply.get("reason") or "refused")
        if not reply.get("ok"):
            raise HTTPException(429 if reply.get("reason") == "locked" else 400,
                                reply.get("error") or "The root helper refused.")
        return await creator_root_helper.status()

    @router.post("/api/creator/root/revoke")
    async def creator_root_revoke(request: Request):
        """Switch root off now. No code needed."""
        user = _require_root_user(request)
        from src import creator_root_helper
        from src.creator_host_helper import HelperError
        try:
            reply = await creator_root_helper.revoke()
        except HelperError as e:
            _root_unreachable(e)
        logger.info("Creator root switch: revoked by %s", user)
        if not reply.get("ok"):
            raise HTTPException(502, reply.get("error") or "The root helper refused.")
        return await creator_root_helper.status()

    # ------------------------------------------------------------------
    # The watchdog (Phase 5c). The root helper judges commands and keeps the
    # settings; it decides whether a change loosens them (then it wants an
    # authenticator code). These routes only pass requests on.
    # ------------------------------------------------------------------

    class RootCheckRequest(BaseModel):
        command: str = Field(..., min_length=1, max_length=4000)

    class WatchdogSaveRequest(BaseModel):
        settings: dict
        code: Optional[str] = Field(default=None, pattern=r"^[0-9]{6}$")

    async def _ask_root(call, *args):
        from src.creator_host_helper import HelperError
        try:
            return await call(*args)
        except HelperError as e:
            _root_unreachable(e)

    @router.get("/api/creator/root/watchdog")
    async def creator_root_watchdog(request: Request):
        """The watchdog's settings, built-in refused list and limits."""
        _require_root_user(request)
        from src import creator_root_helper
        reply = await _ask_root(creator_root_helper.watchdog)
        if not reply.get("ok"):
            raise HTTPException(502, reply.get("error") or "The root helper refused.")
        reply.pop("ok", None)
        reply.pop("type", None)
        return reply

    @router.post("/api/creator/root/watchdog")
    async def creator_root_watchdog_save(body: WatchdogSaveRequest, request: Request):
        """Save the watchdog's settings. 428 when the change loosens it and no
        code was sent: send it again with a code from the authenticator app."""
        user = _require_root_user(request)
        from src import creator_root_helper
        reply = await _ask_root(creator_root_helper.save_watchdog, body.settings, body.code)
        logger.info("Creator watchdog settings: save by %s: %s", user,
                    ("saved, loosened" if reply.get("loosened") else "saved") if reply.get("ok")
                    else reply.get("reason") or "refused")
        if not reply.get("ok"):
            status = {"code_needed": 428, "locked": 429, "write_failed": 502}.get(reply.get("reason"), 400)
            raise HTTPException(status, reply.get("error") or "The root helper refused.")
        reply.pop("ok", None)
        reply.pop("type", None)
        return reply

    @router.post("/api/creator/root/check")
    async def creator_root_check(body: RootCheckRequest, request: Request):
        """Which tier the watchdog puts a command in. Runs nothing, and a
        refused verdict here doesn't switch root off."""
        _require_root_user(request)
        from src import creator_root_helper
        reply = await _ask_root(creator_root_helper.check, body.command)
        if not reply.get("ok"):
            raise HTTPException(400, reply.get("error") or "The root helper refused.")
        reply.pop("ok", None)
        reply.pop("type", None)
        return reply

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
