// Creator window (Phase 7 of docs/creator-plan.md).
//
// Opened from Tools in the sidebar (and the icon rail). The job history is on
// the left; the right side is a conversation with the selected job: your task,
// then its notes, commands and pauses, then the report. With "New job"
// selected, the composer starts a job.
//
// Task text, notes, commands and outputs are user/model/tool text, so they're
// put in with textContent, never innerHTML. Only the report goes through the
// chat's markdown renderer (mdToHtml), the same as a chat reply.

import markdownModule from '../markdown.js';
import themeModule from '../theme.js';
import * as view from './view.js';

const API = '/api/creator';
const OVERLAY_ID = 'creator-overlay';
const SIDEBAR_BTN_ID = 'tool-creator-btn';
const OPTIONS_KEY = 'odysseus-creator-options';

let _open = false;
let _onDocKeydown = null;
let _selectedId = null;      // null: "New job"
let _jobs = [];
// Bumped on every job switch, so a slow response for an earlier selection
// can't overwrite the job now on screen.
let _viewToken = 0;

function byId(id) { return document.getElementById(id); }

function make(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  Object.entries(props).forEach(([k, v]) => {
    if (v === undefined || v === null || v === false) return;
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k === 'style') node.setAttribute('style', v);
    else node.setAttribute(k, v === true ? '' : v);
  });
  children.forEach(c => { if (c) node.appendChild(c); });
  return node;
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  let data = {};
  try { data = await res.json(); } catch (_) { /* empty body */ }
  if (!res.ok) {
    const detail = Array.isArray(data.detail)
      ? data.detail.map(d => d.msg).join('; ')
      : data.detail;
    const err = new Error(detail || `Request failed (${res.status})`);
    err.status = res.status;
    throw err;
  }
  return data;
}

const ICON = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg>';

// ── Open / close / minimize ────────────────────────────────────────────

export function isOpen() { return _open; }

export function toggle() {
  if (!_open) { openPanel(); return; }
  const overlay = byId(OVERLAY_ID);
  if (overlay && overlay.style.display === 'none') { _restore(); return; }
  closePanel();
}

function _restore() {
  const overlay = byId(OVERLAY_ID);
  if (overlay) overlay.style.display = '';
  byId(SIDEBAR_BTN_ID)?.classList.remove('minimized');
}

export function openPanel(jobId) {
  if (_open) {
    _restore();
    if (jobId) selectJob(jobId);
    return;
  }
  _open = true;
  byId(SIDEBAR_BTN_ID)?.classList.add('active');

  const overlay = make('div', { id: OVERLAY_ID, class: 'modal creator-overlay' });
  const pane = make('div', { id: 'creator-pane', class: 'modal-content creator-pane' });
  pane.style.cssText = (window.innerWidth <= 768)
    ? 'width:100vw;max-width:100vw;height:90dvh;max-height:90dvh;border-radius:14px 14px 0 0;background:var(--bg);'
    : 'width:min(980px, 94vw);height:85vh;max-height:85vh;background:var(--bg);';
  _buildPane(pane);
  overlay.appendChild(pane);
  document.body.appendChild(overlay);

  overlay.addEventListener('click', (e) => { if (e.target === overlay) closePanel(); });
  _onDocKeydown = (e) => {
    if (e.key === 'Escape' && _open && overlay.style.display !== 'none') {
      e.preventDefault();
      closePanel();
    }
  };
  document.addEventListener('keydown', _onDocKeydown);

  const header = pane.querySelector('.creator-pane-header');
  if (themeModule && themeModule.makeDraggable && header) themeModule.makeDraggable(pane, header);

  refreshHistory();
  if (jobId) selectJob(jobId); else showNewJob();
}

export function closePanel() {
  if (!_open) return;
  _open = false;
  _viewToken++;
  if (_onDocKeydown) {
    document.removeEventListener('keydown', _onDocKeydown);
    _onDocKeydown = null;
  }
  const btn = byId(SIDEBAR_BTN_ID);
  if (btn) btn.classList.remove('active', 'minimized');
  byId(OVERLAY_ID)?.remove();
}

function _minimize() {
  const overlay = byId(OVERLAY_ID);
  if (overlay) overlay.style.display = 'none';
  byId(SIDEBAR_BTN_ID)?.classList.add('minimized');
}

// ── Layout ─────────────────────────────────────────────────────────────

function _buildPane(pane) {
  const title = make('h4', {}, [make('span', { class: 'creator-title-icon' }), make('span', { text: 'Creator' })]);
  title.firstChild.innerHTML = ICON;   // static markup
  const minBtn = make('button', { type: 'button', class: 'modal-minimize-btn', title: 'Minimize', 'aria-label': 'Minimize' });
  minBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="5" y1="18" x2="19" y2="18"/></svg>';
  minBtn.addEventListener('click', _minimize);
  const closeBtn = make('button', { type: 'button', class: 'close-btn', title: 'Close', 'aria-label': 'Close', text: '✖' });
  closeBtn.addEventListener('click', closePanel);
  const historyBtn = make('button', { type: 'button', class: 'creator-history-toggle', text: 'Jobs', 'aria-expanded': 'false' });
  historyBtn.addEventListener('click', () => {
    const layout = pane.querySelector('.creator-layout');
    const shown = layout.classList.toggle('history-shown');
    historyBtn.setAttribute('aria-expanded', shown ? 'true' : 'false');
  });

  const header = make('div', { class: 'modal-header creator-pane-header' }, [
    title,
    make('div', { class: 'creator-pane-header-actions' }, [historyBtn, minBtn, closeBtn]),
  ]);

  const newBtn = make('button', { type: 'button', class: 'creator-new-btn', text: '+ New job' });
  newBtn.addEventListener('click', showNewJob);
  const history = make('aside', { class: 'creator-history', 'aria-label': 'Creator jobs' }, [
    newBtn,
    make('ul', { id: 'creator-job-list', class: 'creator-job-list', role: 'listbox' }),
  ]);

  const main = make('section', { class: 'creator-main' }, [
    make('div', { id: 'creator-job-head', class: 'creator-job-head' }),
    make('div', { id: 'creator-timeline', class: 'creator-timeline', 'aria-live': 'polite' }),
    _buildComposer(),
  ]);

  pane.appendChild(header);
  pane.appendChild(make('div', { class: 'modal-body creator-pane-body', 'data-no-swipe-dismiss': true }, [
    make('div', { class: 'creator-layout' }, [history, main]),
  ]));
}

function _loadOptions() {
  try { return JSON.parse(localStorage.getItem(OPTIONS_KEY) || '{}') || {}; } catch (_) { return {}; }
}

function _saveOptions(opts) {
  try { localStorage.setItem(OPTIONS_KEY, JSON.stringify(opts)); } catch (_) { /* private mode */ }
}

function _buildComposer() {
  const saved = _loadOptions();
  const task = make('textarea', {
    id: 'creator-task', class: 'creator-task', rows: '3',
    placeholder: 'What should Creator do? e.g. "Find out why nginx won\'t start and fix it."',
  });
  const minutes = make('input', {
    id: 'creator-max-minutes', type: 'number', min: '1', max: '1440', step: '1',
    placeholder: 'default', class: 'creator-minutes',
  });
  if (saved.max_minutes) minutes.value = saved.max_minutes;
  const untrusted = make('input', { id: 'creator-approve-untrusted', type: 'checkbox' });
  // Deliberately not remembered: it's a per-run decision.
  const startBtn = make('button', { id: 'creator-start-btn', type: 'button', class: 'creator-start-btn', text: 'Start' });
  startBtn.addEventListener('click', _handleStart);
  task.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); _handleStart(); }
  });

  return make('div', { id: 'creator-composer', class: 'creator-composer' }, [
    task,
    make('div', { class: 'creator-options' }, [
      make('label', { class: 'creator-option', title: 'Wall-clock limit for the whole run, paused time included. Blank uses the server default.' }, [
        make('span', { text: 'Time limit (min)' }), minutes,
      ]),
      make('label', { class: 'creator-option', title: 'Approve actions that follow untrusted content (command output, web pages) for this whole run, instead of pausing at the first one. Protected paths and secret switches still apply.' }, [
        untrusted, make('span', { text: 'Approve untrusted actions up front' }),
      ]),
      make('span', { id: 'creator-composer-msg', class: 'creator-composer-msg', role: 'status' }),
      startBtn,
    ]),
  ]);
}

function _setComposerMessage(text, isError) {
  const msg = byId('creator-composer-msg');
  if (!msg) return;
  msg.textContent = text || '';
  msg.classList.toggle('error', !!isError);
}

// ── History ────────────────────────────────────────────────────────────

export async function refreshHistory() {
  const list = byId('creator-job-list');
  if (!list) return;
  try {
    _jobs = (await api(`${API}/jobs?limit=100`)).jobs || [];
  } catch (e) {
    list.replaceChildren(make('li', { class: 'creator-job-empty', text: `Couldn't load jobs: ${e.message}` }));
    return;
  }
  _renderHistory();
}

function _renderHistory() {
  const list = byId('creator-job-list');
  if (!list) return;
  if (!_jobs.length) {
    list.replaceChildren(make('li', { class: 'creator-job-empty', text: 'No jobs yet.' }));
    return;
  }
  list.replaceChildren(..._jobs.map((job) => {
    const firstLine = (job.task || '').split('\n')[0];
    const item = make('li', {
      class: 'creator-job-item' + (job.job_id === _selectedId ? ' selected' : ''),
      role: 'option', tabindex: '0', 'data-job-id': job.job_id,
      'aria-selected': job.job_id === _selectedId ? 'true' : 'false',
      title: job.task || '',
    }, [
      make('span', { class: `creator-status-dot status-${job.status}`, title: view.statusLabel(job.status) }),
      make('span', { class: 'creator-job-text' }, [
        make('span', { class: 'creator-job-task', text: firstLine || '(no task)' }),
        make('span', { class: 'creator-job-meta', text: `${view.statusLabel(job.status)} · ${view.relativeTime(job.started_at)}` }),
      ]),
    ]);
    item.addEventListener('click', () => selectJob(job.job_id));
    item.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); selectJob(job.job_id); }
    });
    return item;
  }));
}

function _hideHistoryOnMobile() {
  document.querySelector('#creator-pane .creator-layout')?.classList.remove('history-shown');
  document.querySelector('#creator-pane .creator-history-toggle')?.setAttribute('aria-expanded', 'false');
}

// ── New job ────────────────────────────────────────────────────────────

export function showNewJob() {
  _viewToken++;
  _selectedId = null;
  _renderHistory();
  _hideHistoryOnMobile();
  byId('creator-job-head')?.replaceChildren(
    make('div', { class: 'creator-job-title', text: 'New job' }),
  );
  byId('creator-timeline')?.replaceChildren(make('div', { class: 'creator-intro' }, [
    make('p', { text: 'Give Creator a task on this server. It works through it, tries other approaches when something fails, and asks you only when it\'s truly blocked. When it finishes, it writes a report.' }),
    make('p', { text: 'One job runs at a time. It stops at its time limit, and you can stop it at any point.' }),
  ]));
  const composer = byId('creator-composer');
  if (composer) composer.hidden = false;
  _setComposerMessage('');
  byId('creator-task')?.focus();
}

async function _handleStart() {
  const taskEl = byId('creator-task');
  const startBtn = byId('creator-start-btn');
  const task = (taskEl?.value || '').trim();
  if (!task) { _setComposerMessage('Write a task first.', true); taskEl?.focus(); return; }
  const minutesRaw = (byId('creator-max-minutes')?.value || '').trim();
  const body = { task, approve_untrusted: !!byId('creator-approve-untrusted')?.checked };
  if (minutesRaw) {
    const n = parseInt(minutesRaw, 10);
    if (!Number.isFinite(n) || n < 1 || n > 1440) {
      _setComposerMessage('Time limit must be 1–1440 minutes.', true);
      return;
    }
    body.max_minutes = n;
  }
  _saveOptions({ max_minutes: body.max_minutes || '' });

  if (startBtn) startBtn.disabled = true;
  _setComposerMessage('Starting…');
  try {
    const out = await api(`${API}/start`, { method: 'POST', body: JSON.stringify(body) });
    if (taskEl) taskEl.value = '';
    const untrusted = byId('creator-approve-untrusted');
    if (untrusted) untrusted.checked = false;
    _setComposerMessage('');
    await refreshHistory();
    selectJob(out.job_id);
  } catch (e) {
    _setComposerMessage(e.status === 409 ? 'A Creator job is already running. Stop it or wait for it to finish.' : e.message, true);
  } finally {
    if (startBtn) startBtn.disabled = false;
  }
}

// ── A job ──────────────────────────────────────────────────────────────

export async function selectJob(jobId) {
  const token = ++_viewToken;
  _selectedId = jobId;
  _renderHistory();
  _hideHistoryOnMobile();
  const composer = byId('creator-composer');
  if (composer) composer.hidden = true;
  const timeline = byId('creator-timeline');
  timeline?.replaceChildren(make('div', { class: 'creator-loading', text: 'Loading…' }));

  let status;
  try {
    status = await api(`${API}/status/${encodeURIComponent(jobId)}?since=0`);
  } catch (e) {
    if (token !== _viewToken) return;
    timeline?.replaceChildren(make('div', { class: 'creator-system level-error', text: `Couldn't load this job: ${e.message}` }));
    return;
  }
  let report = null;
  if (status.has_report && !view.isActive(status.status)) {
    try { report = await api(`${API}/report/${encodeURIComponent(jobId)}`); } catch (_) { report = null; }
  }
  if (token !== _viewToken) return;
  _renderJob(status, report);
}

function _renderHead(status) {
  const head = byId('creator-job-head');
  if (!head) return;
  const firstLine = (status.task || '').split('\n')[0];
  const meta = [
    status.model,
    view.formatDuration(status.started_at, status.finished_at),
    status.max_minutes ? `limit ${status.max_minutes} min` : '',
  ].filter(Boolean).join(' · ');
  const children = [
    make('div', { class: 'creator-job-title', text: firstLine || '(no task)', title: status.task || '' }),
    make('div', { class: 'creator-job-sub' }, [
      make('span', { class: `creator-status-pill status-${status.status}`, text: view.statusLabel(status.status) }),
      make('span', { class: 'creator-job-meta', text: meta }),
    ]),
  ];
  if (view.isActive(status.status)) {
    const refresh = make('button', { type: 'button', class: 'creator-refresh-btn', text: 'Refresh' });
    refresh.addEventListener('click', () => { refreshHistory(); selectJob(status.job_id); });
    children[1].appendChild(refresh);
  }
  head.replaceChildren(...children);
}

function _renderJob(status, report) {
  _renderHead(status);
  const timeline = byId('creator-timeline');
  if (!timeline) return;
  const nodes = [make('div', { class: 'creator-msg creator-msg-user', text: status.task || '' })];
  view.buildTimeline(status.events).forEach((item) => nodes.push(_renderItem(item)));

  if (report && report.report) {
    const body = make('div', { class: 'creator-report-body' });
    body.innerHTML = markdownModule.mdToHtml(report.report);   // same renderer as chat replies
    const audit = report.audit_log
      ? make('div', { class: 'creator-report-foot', text: `Full audit log on the server: ${report.audit_log}` })
      : null;
    nodes.push(make('div', { class: 'creator-msg creator-msg-report' }, [
      make('div', { class: 'creator-msg-label', text: 'Report' }), body, audit,
    ]));
  } else if (status.error) {
    nodes.push(make('div', { class: 'creator-system level-error', text: status.error }));
  }
  timeline.replaceChildren(...nodes);
  timeline.scrollTop = timeline.scrollHeight;
}

function _renderItem(item) {
  switch (item.kind) {
    case 'note':
      return make('div', { class: 'creator-note' + (item.auto ? ' auto' : '') }, [
        make('span', { class: 'creator-note-label', text: item.auto ? 'checkpoint' : 'progress' }),
        make('span', { class: 'creator-note-text', text: item.text }),
      ]);
    case 'command': {
      let exitText = '…';
      let exitCls = 'pending';
      if (item.done) {
        exitText = item.exitCode == null ? 'done' : `exit ${item.exitCode}`;
        exitCls = (item.exitCode == null || item.exitCode === 0) ? 'ok' : 'fail';
      }
      const summary = make('summary', {}, [
        make('span', { class: 'creator-cmd-tool', text: item.tool }),
        make('code', { class: 'creator-cmd-text', text: item.command || '(no command text)' }),
        item.approved ? make('span', { class: 'creator-chip approved', text: 'approved' }) : null,
        make('span', { class: `creator-chip exit ${exitCls}`, text: exitText }),
      ]);
      return make('details', { class: 'creator-cmd' }, [
        summary,
        make('pre', { class: 'creator-cmd-output', text: item.output || (item.done ? '(no output)' : 'running…') }),
      ]);
    }
    case 'pause': {
      const action = view.describeAction(item.action);
      return make('div', { class: `creator-pause kind-${item.pauseKind}` }, [
        make('div', { class: 'creator-pause-label', text: item.label }),
        item.question ? make('div', { class: 'creator-pause-question', text: item.question }) : null,
        action ? make('code', { class: 'creator-pause-action', text: action }) : null,
      ]);
    }
    case 'resumed':
      return make('div', { class: 'creator-msg creator-msg-user creator-msg-small' }, [
        make('div', { class: 'creator-msg-label', text: item.text }),
        item.answer ? make('div', { text: item.answer }) : null,
      ]);
    case 'system':
    default:
      return make('div', { class: `creator-system level-${item.level || 'info'}`, text: item.text || '' });
  }
}
