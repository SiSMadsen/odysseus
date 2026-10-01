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
        };
        items.push(it);
        open.push(it);
        break;
      }
      case 'tool_output': {
        let it = takeOpen(ev.tool || 'tool', ev.command || '');
        if (!it) {
          it = { kind: 'command', tool: ev.tool || 'tool', command: ev.command || '', approved: false };
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
  const detail = action.command || action.content || action.path || '';
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
