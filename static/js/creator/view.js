// Creator window (Phase 7 of docs/creator-plan.md): turning a job's event log
// into what the window shows. No DOM here, so it can be tested with node.

// Statuses a job can have (src/creator_mode.py). "blocked" only appears on
// jobs from before Phase 2.
export const STATUS_LABELS = {
  running: 'Running',
  paused: 'Waiting for you',
  done: 'Done',
  error: 'Error',
  stopped: 'Stopped',
  timeout: 'Time limit reached',
  limit: 'Step limit reached',
  interrupted: 'Interrupted',
  blocked: 'Blocked',
};

export const ACTIVE_STATUSES = ['running', 'paused'];

export const HOST_TOOL = 'host_exec';

/** The command as you'd type it: host_exec's JSON args become the bare command. */
export function displayCommand(tool, command) {
  const text = command == null ? '' : String(command);
  if (tool !== HOST_TOOL) return text;
  try {
    const args = JSON.parse(text);
    if (args && typeof args.command === 'string') return args.command;
  } catch (_) { /* a bare command string */ }
  return text;
}

export function statusLabel(status) {
  return STATUS_LABELS[status] || String(status || 'Unknown');
}

export function isActive(status) {
  return ACTIVE_STATUSES.includes(status);
}

const DECISION_LABELS = {
  approve_once: 'Approved once',
  approve_job: 'Approved for the rest of this job',
  deny: 'Denied',
};

const PAUSE_LABELS = {
  approval: 'Needs your approval',
  question: 'Question for you',
  blocked: 'Blocked',
};

/**
 * Build the conversation timeline from a job's events (in seq order).
 *
 * Returns items of these kinds:
 *   {kind: 'note', text, auto}                       a PROGRESS: note (auto: Creator's own checkpoint)
 *   {kind: 'command', tool, command, output, exitCode, approved, done}
 *   {kind: 'pause', label, pauseKind, question, action}
 *   {kind: 'resumed', text, answer}
 *   {kind: 'system', text, level}                    level: 'info' | 'warn' | 'error'
 * Round markers are dropped. A tool_output is joined to the open tool_start
 * for the same tool (and command, when both have one).
 */
export function buildTimeline(events) {
  const items = [];
  const open = [];   // command items still waiting for their output

  const takeOpen = (tool, command) => {
    for (let i = open.length - 1; i >= 0; i--) {
      const it = open[i];
      if (it.tool !== tool) continue;
      if (command && it.command && it.command !== command) continue;
      open.splice(i, 1);
      return it;
    }
    return null;
  };

  for (const ev of events || []) {
    if (!ev || typeof ev !== 'object') continue;
    switch (ev.type) {
      case 'note':
        items.push({ kind: 'note', text: String(ev.text || ''), auto: ev.source === 'auto' });
        break;
      case 'tool_start': {
        const it = {
          kind: 'command', tool: ev.tool || 'tool', command: ev.command || '',
          output: '', exitCode: null, approved: !!ev.approved, done: false,
          host: ev.tool === HOST_TOOL,
        };
        items.push(it);
        open.push(it);
        break;
      }
      case 'tool_output': {
        let it = takeOpen(ev.tool || 'tool', ev.command || '');
        if (!it) {
          it = { kind: 'command', tool: ev.tool || 'tool', command: ev.command || '', approved: false,
                 host: ev.tool === HOST_TOOL };
          items.push(it);
        }
        it.output = ev.output == null ? '' : String(ev.output);
        it.exitCode = ev.exit_code == null ? null : ev.exit_code;
        it.approved = it.approved || !!ev.approved;
        it.done = true;
        break;
      }
      case 'paused':
        items.push({
          kind: 'pause', pauseKind: ev.kind || '', label: PAUSE_LABELS[ev.kind] || 'Paused',
          question: ev.question || '', action: ev.action || null,
        });
        break;
      case 'resumed': {
        const answer = ev.answer ? String(ev.answer) : '';
        const text = DECISION_LABELS[ev.decision] || (answer ? 'You answered' : 'Resumed');
        items.push({ kind: 'resumed', text, answer });
        break;
      }
      case 'failure_limit':
        items.push({
          kind: 'system', level: 'warn',
          text: `Refused from now on (failed 3 times the same way): ${ev.command || ev.tool || ''}`,
        });
        break;
      case 'model_error':
        items.push({ kind: 'system', level: 'error', text: `Model error: ${ev.error || 'unknown'}` });
        break;
      case 'timeout':
        items.push({ kind: 'system', level: 'warn', text: `Time limit reached (${ev.max_minutes} min).` });
        break;
      case 'stopped':
        items.push({ kind: 'system', level: 'warn', text: 'Stopped.' });
        break;
      case 'rounds_exhausted':
      case 'budget_exceeded':
        items.push({ kind: 'system', level: 'warn', text: 'A step limit was reached.' });
        break;
      default:
        break;
    }
  }
  return items;
}

/** A pause's action, as one readable line ("bash: rm -rf build"). */
export function describeAction(action) {
  if (!action || typeof action !== 'object') return '';
  const tool = action.tool || action.name || '';
  const detail = displayCommand(tool, action.command || action.content || action.path || '');
  if (tool && detail) return `${tool}: ${detail}`;
  return tool || String(detail || '');
}

/** "3 min ago" / "2 h ago" / a date, for the history list. */
export function relativeTime(iso, now = Date.now()) {
  if (!iso) return '';
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return '';
  const s = Math.max(0, Math.round((now - t) / 1000));
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  if (s < 7 * 86400) return `${Math.floor(s / 86400)} d ago`;
  return new Date(t).toISOString().slice(0, 10);
}

/** Elapsed or total run time, "1 h 05 min" / "4 min" / "12 s". */
export function formatDuration(startIso, endIso, now = Date.now()) {
  const a = Date.parse(startIso || '');
  if (Number.isNaN(a)) return '';
  const bParsed = Date.parse(endIso || '');
  const b = Number.isNaN(bParsed) ? now : bParsed;
  const s = Math.max(0, Math.round((b - a) / 1000));
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min`;
  return `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, '0')} min`;
}

/** Highest event seq seen (events without one count by position, as the server does). */
export function lastSeq(events) {
  let max = 0;
  (events || []).forEach((e, i) => {
    const seq = e && typeof e.seq === 'number' ? e.seq : i + 1;
    if (seq > max) max = seq;
  });
  return max;
}

/** A job's status after one live event: a pause or resume flips it. */
export function statusAfterEvent(status, event) {
  if (!event || !isActive(status)) return status;
  if (event.type === 'paused') return 'paused';
  if (event.type === 'resumed') return 'running';
  return status;
}

/** Time left before the hard limit: "42 min left", "35 s left", "time's up". */
export function timeLeft(deadlineIso, now = Date.now()) {
  const d = Date.parse(deadlineIso || '');
  if (Number.isNaN(d)) return '';
  const s = Math.round((d - now) / 1000);
  if (s <= 0) return "time's up";
  if (s < 60) return `${s} s left`;
  const m = Math.ceil(s / 60);
  if (m < 60) return `${m} min left`;
  return `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, '0')} min left`;
}

/** Delay before stream reconnect attempt n (0-based): 1 s, 2 s, 4 s … capped at 30 s. */
export function reconnectDelay(attempt) {
  return Math.min(30000, 1000 * 2 ** Math.max(0, attempt));
}

const CHOICE_LABELS = {
  approve_once: 'Approve once',
  approve_job: 'Approve for this job',
  deny: 'Deny',
};

const CHOICE_HINTS = {
  approve_once: 'Run exactly this action, then ask again next time.',
  approve_job: 'Run it, and stop asking at the untrusted-content check for the rest of this job. Protected paths still ask every time.',
  deny: "Don't run it. Creator is told to find another way.",
};

// Why a job's first action usually asks (Polishing): skills, memories and
// integration descriptions in the prompt count as outside content.
export const UNTRUSTED_NOTICE = 'Asked because outside content can steer what Creator does: even the first '
  + 'action asks, since skills, memories and integration descriptions in its instructions count as outside '
  + 'content. "Approve for this job" covers the rest of these, or tick "Approve untrusted actions up front" '
  + 'when starting a job.';

// A host command (Phase 6b: pause.scope === 'host'): "for this job" lifts the
// host-command gate, not the untrusted-content one.
const HOST_CHOICE_LABELS = {
  approve_once: 'Allow once',
  approve_job: 'Allow all host commands for this job',
  deny: 'Deny',
};

const HOST_CHOICE_HINTS = {
  approve_once: 'Run this one command on the host, then ask again for the next.',
  approve_job: 'Run it, and run the rest of this job\'s host commands without asking. Protected paths still ask every time.',
  deny: "Don't run it. Creator is told to find another way.",
};

/**
 * What the reply bar offers for a pause (the `pause` object from /status).
 *   approval:          {mode: 'approval', buttons: [{label, hint, decision, tone}], placeholder, notice}
 *   question/blocked:  {mode: 'answer', buttons: [{label, answer}], placeholder, notice}
 * Returns null when there's nothing to answer.
 */
export function replyControls(pause) {
  if (!pause || typeof pause !== 'object') return null;
  if (pause.kind === 'approval') {
    const choices = Array.isArray(pause.choices) && pause.choices.length
      ? pause.choices
      : ['approve_once', 'deny'];
    return {
      mode: 'approval',
      buttons: choices.filter(c => CHOICE_LABELS[c]).map(c => ({
        label: (pause.scope === 'host' ? HOST_CHOICE_LABELS : CHOICE_LABELS)[c],
        hint: (pause.scope === 'host' ? HOST_CHOICE_HINTS : CHOICE_HINTS)[c],
        decision: c,
        tone: c === 'deny' ? 'deny' : 'approve',
      })),
      placeholder: 'Optional note for Creator, sent with your choice',
      notice: pause.protected
        ? 'This touches a protected path, so it can only be approved one action at a time.'
        : pause.scope === 'host'
          ? 'This command runs on the host machine, outside the container, as the user creator.'
          : pause.scope === 'untrusted'
            ? UNTRUSTED_NOTICE
            : '',
    };
  }
  const options = (Array.isArray(pause.options) ? pause.options : [])
    .map(o => String(o || '').trim()).filter(Boolean);
  return {
    mode: 'answer',
    buttons: options.map(o => ({ label: o, answer: o })),
    placeholder: pause.kind === 'blocked'
      ? 'Give Creator what it needs to continue…'
      : 'Answer Creator…',
    notice: '',
  };
}

/** The Send button's label: an empty answer means "carry on as best you can". */
export function sendLabel(text) {
  return String(text || '').trim() ? 'Send' : 'Carry on without an answer';
}

/** "creator-cr-1a2b3c4d5e6f-2026-10-02.md": job id plus the day it finished
 *  (or started). The same stem names the printed PDF. */
export function reportFilename(jobId, isoDate, ext = 'md', now = Date.now()) {
  const t = Date.parse(isoDate || '');
  const day = new Date(Number.isNaN(t) ? now : t).toISOString().slice(0, 10);
  const id = String(jobId || 'job').replace(/[^A-Za-z0-9_-]/g, '');
  return `creator-${id}-${day}${ext ? `.${ext}` : ''}`;
}
