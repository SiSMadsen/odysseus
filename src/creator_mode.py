# src/creator_mode.py
"""
Creator mode engine.

A Creator job takes one task and works through it with the regular agent loop
(src/agent_loop.py) running as a background job, with the round and tool-call
caps set very high. Every job is a row in the `creator_jobs` table: task,
status, start/end time, the final report and an event log of what the agent
did. Routes live in routes/creator_routes.py.

Safety net (src/creator_safety.py): a wall-clock time limit per run, a stop
that also kills the job's running shell command, a redacted audit log on disk,
an optional protected-paths list, and only one job at a time.
"""
import asyncio
import json
import logging
import re
import uuid
from typing import Callable, Dict, List, Optional, Set

from core.database import CreatorJob, SessionLocal, utcnow_naive
from src.creator_safety import (
    AuditLog,
    Redactor,
    kill_job_shell,
    make_protected_action_check,
)

logger = logging.getLogger(__name__)

# "Very high" caps — Creator runs are expected to take many steps. The time
# limit and the stop route are what end a long run.
CREATOR_MAX_ROUNDS = 500
CREATOR_MAX_TOOL_CALLS = 2000

_SECONDS_PER_MINUTE = 60  # patched by tests
DEFAULT_MAX_MINUTES = 60
MIN_MAX_MINUTES = 1
MAX_MAX_MINUTES = 1440

# Keep the stored event log bounded: per-event output is truncated and only
# the most recent events are kept. The audit log on disk keeps more.
_EVENT_OUTPUT_CHARS = 2000
_AUDIT_OUTPUT_CHARS = 100_000
_MAX_EVENTS = 2000
# Flush the event log to the database at most this often (seconds).
_FLUSH_INTERVAL_S = 3.0

_JOB_ID_RE = re.compile(r"^cr-[a-f0-9]{12}$")

CREATOR_SYSTEM_PROMPT = (
    "You are running in Creator mode: you have been given a task to complete "
    "on your own, using the tools available to you. Work through the task "
    "step by step until it is done. When you finish, write a final report: "
    "what was asked, what you did, what worked, what didn't, and what is left."
)


class CreatorBusyError(RuntimeError):
    """Another Creator job is already running."""


def is_valid_job_id(job_id: str) -> bool:
    return isinstance(job_id, str) and bool(_JOB_ID_RE.fullmatch(job_id))


def new_job_id() -> str:
    return f"cr-{uuid.uuid4().hex[:12]}"


def clamp_minutes(value) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_MINUTES
    return max(MIN_MAX_MINUTES, min(MAX_MAX_MINUTES, n))


def default_max_minutes() -> int:
    try:
        from src.settings import get_setting
        return clamp_minutes(get_setting("creator_max_minutes", DEFAULT_MAX_MINUTES))
    except Exception:
        return DEFAULT_MAX_MINUTES


def _now_iso() -> str:
    return utcnow_naive().isoformat() + "Z"


def _truncate(value, limit: int = _EVENT_OUTPUT_CHARS):
    if not isinstance(value, str):
        return value
    if len(value) <= limit:
        return value
    return value[:limit] + f"... [{len(value) - limit} more chars]"


def privilege_disabled_tools(privs: Optional[dict]) -> Set[str]:
    """Tools to switch off for a Creator run, from the owner's privileges.
    Mirrors the per-user privilege gates in routes/chat_routes.py."""
    disabled: Set[str] = set()
    if not privs:
        return disabled
    if not privs.get("can_use_bash", True):
        disabled.update({"bash", "python", "read_file", "write_file"})
    if not privs.get("can_use_browser", True):
        try:
            from routes.chat_routes import _BROWSER_MCP_TOOLS
            disabled.update(_BROWSER_MCP_TOOLS)
        except Exception:
            logger.debug("Could not load browser tool list", exc_info=True)
    if not privs.get("can_use_documents", True):
        disabled.update({"create_document", "edit_document", "update_document", "suggest_document"})
    if not privs.get("can_generate_images", True):
        disabled.add("generate_image")
    if not privs.get("can_manage_memory", True):
        disabled.update({"manage_memory", "manage_skills"})
    return disabled


def _tool_output_text(data: dict) -> str:
    output = data.get("output")
    if output is None:
        output = data.get("stdout") or data.get("result") or data.get("error")
    if output is None:
        return ""
    return output if isinstance(output, str) else json.dumps(output, default=str)


def _event_from_sse(data: dict) -> Optional[dict]:
    """Reduce one agent-loop SSE payload to a compact event-log entry, or None
    for events that aren't worth keeping (text deltas, metrics, ...)."""
    etype = data.get("type")
    if etype == "agent_step":
        return {"type": "round", "round": data.get("round")}
    if etype == "tool_start":
        return {
            "type": "tool_start",
            "tool": data.get("tool"),
            "command": _truncate(data.get("command")),
        }
    if etype == "tool_output":
        return {
            "type": "tool_output",
            "tool": data.get("tool"),
            "command": _truncate(data.get("command")),
            "output": _truncate(_tool_output_text(data)),
            "exit_code": data.get("exit_code"),
        }
    if etype in ("rounds_exhausted", "budget_exceeded"):
        return {k: v for k, v in data.items() if k in ("type", "rounds", "limit", "used")}
    return None


class CreatorManager:
    """Runs Creator jobs as asyncio background tasks and records them in the
    database. `session_factory`, `agent_loop` and `audit_directory` are
    injectable for tests."""

    def __init__(
        self,
        session_factory: Callable = SessionLocal,
        agent_loop: Optional[Callable] = None,
        audit_directory=None,
    ):
        self._session_factory = session_factory
        self._agent_loop = agent_loop
        self._audit_directory = audit_directory
        self._tasks: Dict[str, asyncio.Task] = {}
        # Live state of running jobs, for status/stream without a DB round-trip.
        self._live: Dict[str, dict] = {}
        self._stopping: Set[str] = set()
        self._mark_orphans()

    # ------------------------------------------------------------------
    # Database helpers
    # ------------------------------------------------------------------

    def _mark_orphans(self) -> None:
        """Jobs left 'running' by a previous process can never finish."""
        try:
            db = self._session_factory()
            try:
                rows = db.query(CreatorJob).filter(CreatorJob.status == "running").all()
                for row in rows:
                    row.status = "interrupted"
                    row.finished_at = row.finished_at or utcnow_naive()
                    row.error = row.error or "Server restarted while the job was running."
                if rows:
                    db.commit()
                    logger.info("Creator: marked %d orphaned job(s) as interrupted", len(rows))
            finally:
                db.close()
        except Exception:
            logger.warning("Creator: could not check for orphaned jobs", exc_info=True)

    def _update(self, job_id: str, **fields) -> None:
        db = self._session_factory()
        try:
            row = db.query(CreatorJob).filter(CreatorJob.id == job_id).first()
            if row is None:
                return
            for k, v in fields.items():
                setattr(row, k, v)
            db.commit()
        finally:
            db.close()

    def get_job(self, job_id: str) -> Optional[dict]:
        if not is_valid_job_id(job_id):
            return None
        db = self._session_factory()
        try:
            row = db.query(CreatorJob).filter(CreatorJob.id == job_id).first()
            if row is None:
                return None
            try:
                events = json.loads(row.events) if row.events else []
            except (TypeError, ValueError):
                events = []
            job = {
                "id": row.id,
                "owner": row.owner or "",
                "task": row.task,
                "status": row.status,
                "started_at": row.started_at.isoformat() + "Z" if row.started_at else None,
                "finished_at": row.finished_at.isoformat() + "Z" if row.finished_at else None,
                "report": row.report,
                "error": row.error,
                "model": row.model,
                "events": events,
            }
        finally:
            db.close()
        live = self._live.get(job_id)
        if live is not None and job["status"] == "running":
            # Fresher than the periodic DB flush.
            job["events"] = list(live["events"][-_MAX_EVENTS:])
            job["max_minutes"] = live["max_minutes"]
        return job

    def is_running(self, job_id: str) -> bool:
        task = self._tasks.get(job_id)
        return task is not None and not task.done()

    def running_job_id(self) -> Optional[str]:
        for job_id, task in self._tasks.items():
            if not task.done():
                return job_id
        return None

    def live_events_after(self, job_id: str, seq: int) -> Optional[List[dict]]:
        """Events newer than `seq` for a running job, or None once the job is
        no longer running (read the finished job with get_job then)."""
        live = self._live.get(job_id)
        if live is None:
            return None
        return [e for e in live["events"] if e.get("seq", 0) > seq]

    def audit_log_path(self, job_id: str):
        return AuditLog(job_id, Redactor(), self._audit_directory).path

    # ------------------------------------------------------------------
    # Start / stop
    # ------------------------------------------------------------------

    def start_job(
        self,
        task: str,
        endpoint_url: str,
        model: str,
        headers: Optional[dict] = None,
        owner: str = "",
        disabled_tools: Optional[Set[str]] = None,
        max_minutes: Optional[int] = None,
        protected_paths: Optional[List[str]] = None,
    ) -> str:
        # One job at a time. No await between this check and registering the
        # task below, so two starts can't both get through.
        if self.running_job_id() is not None:
            raise CreatorBusyError("Another Creator job is already running.")

        minutes = clamp_minutes(max_minutes) if max_minutes else default_max_minutes()
        redactor = Redactor.for_run(headers)
        job_id = new_job_id()
        db = self._session_factory()
        try:
            db.add(CreatorJob(
                id=job_id,
                owner=owner or None,
                task=redactor.text(task),
                status="running",
                started_at=utcnow_naive(),
                model=model,
                events="[]",
            ))
            db.commit()
        finally:
            db.close()

        audit = AuditLog(job_id, redactor, self._audit_directory)
        audit.write({
            "at": _now_iso(), "type": "job_start", "job_id": job_id, "owner": owner,
            "task": task, "model": model, "max_minutes": minutes,
            "protected_paths": list(protected_paths or []),
        })
        self._live[job_id] = {"events": [], "max_minutes": minutes, "seq": 0}

        bg = asyncio.create_task(self._run(
            job_id, task, endpoint_url, model, headers or {}, owner,
            set(disabled_tools or ()), minutes, list(protected_paths or []),
            redactor, audit,
        ))
        self._tasks[job_id] = bg
        bg.add_done_callback(lambda _t, jid=job_id: self._tasks.pop(jid, None))
        logger.info("Creator: started job %s (owner=%r, model=%s, limit=%dm)", job_id, owner, model, minutes)
        return job_id

    def stop_job(self, job_id: str) -> bool:
        task = self._tasks.get(job_id)
        if task is None or task.done():
            return False
        if job_id in self._stopping:
            # Already stopping; a second cancel could interrupt the final save.
            return True
        self._stopping.add(job_id)
        task.cancel()
        # The run kills its shell on the way out too; doing it here as well
        # means a command that is ignoring cancellation still dies now.
        asyncio.get_running_loop().create_task(kill_job_shell(job_id))
        return True

    # ------------------------------------------------------------------
    # The run itself
    # ------------------------------------------------------------------

    async def _run(
        self,
        job_id: str,
        task: str,
        endpoint_url: str,
        model: str,
        headers: dict,
        owner: str,
        disabled_tools: Set[str],
        max_minutes: int,
        protected_paths: List[str],
        redactor: Redactor,
        audit: AuditLog,
    ) -> None:
        events: List[dict] = self._live[job_id]["events"]
        text_parts: List[str] = []
        outcome = {"status": "done", "error": None}
        loop = asyncio.get_running_loop()
        last_flush = [loop.time()]

        live = self._live[job_id]

        def _add_event(event: dict) -> None:
            event = redactor.obj(event)
            event["at"] = _now_iso()
            # Monotonic sequence number, so pollers/streams can ask for
            # "everything after N" even once old events are trimmed.
            live["seq"] += 1
            event["seq"] = live["seq"]
            events.append(event)
            if len(events) > _MAX_EVENTS:
                del events[: len(events) - _MAX_EVENTS]

        def _flush() -> None:
            self._update(job_id, events=json.dumps(events, default=str))
            last_flush[0] = loop.time()

        agent_loop = self._agent_loop
        if agent_loop is None:
            from src.agent_loop import stream_agent_loop as agent_loop

        messages = [
            {"role": "system", "content": CREATOR_SYSTEM_PROMPT},
            {"role": "user", "content": task},
        ]

        async def _consume() -> None:
            async for chunk in agent_loop(
                endpoint_url=endpoint_url,
                model=model,
                messages=messages,
                headers=headers,
                max_rounds=CREATOR_MAX_ROUNDS,
                max_tool_calls=CREATOR_MAX_TOOL_CALLS,
                session_id=job_id,
                owner=owner or None,
                disabled_tools=disabled_tools,
                workload="background",
                protected_action_check=make_protected_action_check(protected_paths),
            ):
                if not isinstance(chunk, str) or not chunk.startswith("data: "):
                    continue
                body = chunk[6:].strip()
                if not body or body == "[DONE]":
                    continue
                try:
                    data = json.loads(body)
                except ValueError:
                    continue
                if not isinstance(data, dict):
                    continue

                if "delta" in data:
                    if not data.get("thinking") and isinstance(data["delta"], str):
                        text_parts.append(data["delta"])
                    continue

                event = _event_from_sse(data)
                if event is not None:
                    _add_event(event)
                    if event["type"] in ("tool_start", "tool_output"):
                        entry = {"at": _now_iso(), "type": event["type"], "tool": data.get("tool"),
                                 "command": data.get("full_command") or data.get("command")}
                        if event["type"] == "tool_output":
                            entry["exit_code"] = data.get("exit_code")
                            entry["output"] = _truncate(_tool_output_text(data), _AUDIT_OUTPUT_CHARS)
                        audit.write(entry)

                approval = data.get("ask_user")
                if data.get("type") == "tool_output" and isinstance(approval, dict) \
                        and approval.get("kind") == "tool_approval":
                    # Nobody is watching a background run to approve the
                    # action (a protected path, or a gated action after
                    # untrusted content). Retire the approval — the action
                    # never ran — and end the job as blocked. Phase 2 turns
                    # this into a real pause-and-notify.
                    self._retire_approval(approval.get("approval_id"), owner, job_id)
                    action = approval.get("action") or {}
                    reason = approval.get("description") or "This action needs your OK."
                    outcome["status"] = "blocked"
                    outcome["error"] = (
                        f"{reason} The action was not run. "
                        f"It wanted to run {action.get('tool') or data.get('tool') or 'a tool'}: "
                        f"{_truncate(str(action.get('content') or ''), 500)}"
                    )
                    _add_event({"type": "blocked", "tool": data.get("tool"), "reason": reason,
                                "command": _truncate(str(action.get("content") or ""))})
                    audit.write({"at": _now_iso(), "type": "blocked", "tool": data.get("tool"),
                                 "reason": reason, "command": action.get("content")})
                    return

                if loop.time() - last_flush[0] >= _FLUSH_INTERVAL_S:
                    _flush()

        try:
            await asyncio.wait_for(_consume(), timeout=max_minutes * _SECONDS_PER_MINUTE)
        except asyncio.TimeoutError:
            outcome["status"] = "timeout"
            outcome["error"] = f"Time limit of {max_minutes} minute(s) reached; the job was stopped."
            _add_event({"type": "timeout", "max_minutes": max_minutes})
        except asyncio.CancelledError:
            outcome["status"] = "stopped"
            outcome["error"] = "Stopped by user."
            _add_event({"type": "stopped"})
        except Exception as e:
            logger.error("Creator job %s failed: %s", job_id, e, exc_info=True)
            outcome["status"] = "error"
            outcome["error"] = str(e)[:2000]
        finally:
            # Whatever happened, make sure nothing this job started in its
            # shell keeps running.
            try:
                await kill_job_shell(job_id)
            except asyncio.CancelledError:
                pass

        report = redactor.text("".join(text_parts).strip()) or None
        error = redactor.text(outcome["error"]) if outcome["error"] else None
        audit.write({"at": _now_iso(), "type": "job_end", "status": outcome["status"],
                     "error": outcome["error"], "report": report})
        try:
            self._update(
                job_id,
                status=outcome["status"],
                error=error,
                report=report,
                events=json.dumps(events, default=str),
                finished_at=utcnow_naive(),
            )
        except Exception:
            logger.error("Creator job %s: could not save final state", job_id, exc_info=True)
        finally:
            self._live.pop(job_id, None)
            self._stopping.discard(job_id)
        logger.info("Creator: job %s finished with status %s", job_id, outcome["status"])

    @staticmethod
    def _retire_approval(approval_id, owner: str, job_id: str) -> None:
        if not approval_id:
            return
        try:
            from src.tool_approvals import tool_approval_store
            tool_approval_store.consume(approval_id, decision="deny", owner=owner, session_id=job_id)
        except Exception:
            logger.debug("Could not retire Creator approval", exc_info=True)
