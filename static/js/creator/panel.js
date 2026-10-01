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
const RAIL_BTN_ID = 'rail-creator';
const OPTIONS_KEY = 'odysseus-creator-options';

let _open = false;
let _onDocKeydown = null;
let _selectedId = null;      // null: "New job"
let _jobs = [];
// An unsent task survives closing the window (Esc, a click outside it).
let _draftTask = '';
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
  _setAttention(false);
}

// A pause while the window is minimized: a dot on the sidebar/rail buttons,
// and a browser notification if you've already allowed them (this window
// never asks for the permission).
function _setAttention(on) {
  [SIDEBAR_BTN_ID, RAIL_BTN_ID].forEach(id => byId(id)?.classList.toggle('creator-needs-you', !!on));
}

function _notifyPause(question) {
  const overlay = byId(OVERLAY_ID);
  const hidden = !overlay || overlay.style.display === 'none' || document.hidden;
  if (!hidden) return;
  _setAttention(true);
  try {
    if ('Notification' in window && Notification.permission === 'granted') {
      new Notification('Creator is waiting for you', { body: String(question || '').slice(0, 200), tag: 'creator-pause' });
    }
  } catch (_) { /* notifications unavailable */ }
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

  if (jobId) {
    refreshHistory();
    selectJob(jobId);
    return;
  }
  showNewJob();
  // With a job running or paused, open on it rather than on "New job" —
  // unless you've already clicked somewhere or started typing a task.
  const token = _viewToken;
  refreshHistory().then(() => {
    const active = _jobs.find(j => view.isActive(j.status));
    if (!active || token !== _viewToken || (byId('creator-task')?.value || '').trim()) return;
    selectJob(active.job_id);
  });
}

export function closePanel() {
  if (!_open) return;
  _open = false;
  _draftTask = byId('creator-task')?.value || '';
  _viewToken++;
  _stopLive();
  _view = null;
  if (_onDocKeydown) {
    document.removeEventListener('keydown', _onDocKeydown);
    _onDocKeydown = null;
  }
  const btn = byId(SIDEBAR_BTN_ID);
  if (btn) btn.classList.remove('active', 'minimized');
  _setAttention(false);
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
    _buildReplyBar(),
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
  task.value = _draftTask;
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

// The reply bar: shown instead of the composer while the job on screen is
// paused. Approvals get their choices (plus an optional note); questions and
// "blocked" get the agent's options and a text answer.
function _buildReplyBar() {
  const text = make('textarea', { id: 'creator-reply-text', class: 'creator-task creator-reply-text', rows: '2' });
  const send = make('button', { id: 'creator-reply-send', type: 'button', class: 'creator-start-btn', text: view.sendLabel('') });
  text.addEventListener('input', () => { send.textContent = view.sendLabel(text.value); });
  text.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey) && !send.hidden) { e.preventDefault(); send.click(); }
  });
  send.addEventListener('click', () => _sendReply({ answer: text.value }));
  const bar = make('div', { id: 'creator-reply', class: 'creator-composer creator-reply' }, [
    make('div', { id: 'creator-reply-notice', class: 'creator-reply-notice' }),
    make('div', { id: 'creator-reply-choices', class: 'creator-reply-choices' }),
    text,
    make('div', { class: 'creator-options' }, [
      make('span', { id: 'creator-reply-msg', class: 'creator-composer-msg', role: 'status' }),
      send,
    ]),
  ]);
  bar.hidden = true;
  return bar;
}

function _setReplyMessage(text, isError) {
  const msg = byId('creator-reply-msg');
  if (!msg) return;
  msg.textContent = text || '';
  msg.classList.toggle('error', !!isError);
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
  _stopLive();
  _view = null;
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
  const reply = byId('creator-reply');
  if (reply) reply.hidden = true;
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
    _draftTask = '';
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
//
// The job on screen is `_view`: its status record, its events and (once it's
// finished) its report. While it's active, a live stream adds events as they
// happen; when the stream says the job ended, the job is loaded again to get
// the report.

let _view = null;

export async function selectJob(jobId) {
  const token = ++_viewToken;
  _stopLive();
  _view = null;
  _selectedId = jobId;
  _renderHistory();
  _hideHistoryOnMobile();
  const composer = byId('creator-composer');
  if (composer) composer.hidden = true;
  const reply = byId('creator-reply');
  if (reply) reply.hidden = true;
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

  _view = { token, status, events: status.events || [], report };
  _renderHead();
  _renderTimeline({ scrollToEnd: true });
  _renderReply();
  if (view.isActive(status.status)) _startLive();
}

function _setJobStatus(jobId, status) {
  const entry = _jobs.find(j => j.job_id === jobId);
  if (entry && entry.status !== status) {
    entry.status = status;
    _renderHistory();
  }
}

// ── Header: status, time left, Stop ────────────────────────────────────

function _renderHead() {
  const head = byId('creator-job-head');
  if (!head || !_view) return;
  const status = _view.status;
  const active = view.isActive(status.status);
  const firstLine = (status.task || '').split('\n')[0];
  const meta = [
    status.model,
    active ? '' : view.formatDuration(status.started_at, status.finished_at),
    status.max_minutes ? `limit ${status.max_minutes} min` : '',
  ].filter(Boolean).join(' · ');

  const sub = make('div', { class: 'creator-job-sub' }, [
    make('span', { class: `creator-status-pill status-${status.status}`, text: view.statusLabel(status.status) }),
    make('span', { class: 'creator-job-meta', text: meta }),
  ]);
  if (active) {
    sub.appendChild(make('span', { id: 'creator-time-left', class: 'creator-time-left' }));
    sub.appendChild(make('span', { id: 'creator-live-state', class: 'creator-live-state', role: 'status' }));
    const stop = make('button', {
      id: 'creator-stop-btn', type: 'button', class: 'creator-stop-btn', text: 'Stop',
      title: 'Stop this job now. The running command is killed and a report is written.',
    });
    stop.addEventListener('click', _handleStop);
    sub.appendChild(stop);
  }
  head.replaceChildren(
    make('div', { class: 'creator-job-title', text: firstLine || '(no task)', title: status.task || '' }),
    sub,
  );
  _updateTimeLeft();
}

function _updateTimeLeft() {
  const el = byId('creator-time-left');
  if (!el || !_view) return;
  const text = view.timeLeft(_view.status.deadline_at);
  el.textContent = text;
  el.title = _view.status.deadline_at
    ? `Hard time limit at ${new Date(_view.status.deadline_at).toLocaleTimeString()} (paused time counts)`
    : '';
}

function _setLiveState(text) {
  const el = byId('creator-live-state');
  if (el) el.textContent = text || '';
}

async function _handleStop() {
  if (!_view) return;
  const btn = byId('creator-stop-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Stopping…'; }
  try {
    const out = await api(`${API}/stop/${encodeURIComponent(_view.status.job_id)}`, { method: 'POST' });
    // The stream's final message reloads the job. If the job had already
    // ended, there may be no stream left to say so.
    if (!out.stopped) selectJob(_view.status.job_id);
  } catch (e) {
    if (btn) { btn.disabled = false; btn.textContent = 'Stop'; }
    _setLiveState(`Stop failed: ${e.message}`);
  }
}

// ── Live stream ────────────────────────────────────────────────────────

let _live = null;   // { token, source, clock, retryTimer, attempts, seq, renderQueued }

function _startLive() {
  if (!_view) return;
  _live = {
    token: _view.token, source: null, retryTimer: null, attempts: 0,
    seq: view.lastSeq(_view.events), renderQueued: false,
    clock: setInterval(_updateTimeLeft, 1000),
  };
  _connect();
}

function _stopLive() {
  if (!_live) return;
  if (_live.source) _live.source.close();
  clearTimeout(_live.retryTimer);
  clearInterval(_live.clock);
  _live = null;
}

function _connect() {
  const live = _live;
  if (!live || !_view || live.token !== _view.token) return;
  const jobId = _view.status.job_id;
  const source = new EventSource(`${API}/stream/${encodeURIComponent(jobId)}?since=${live.seq}`);
  live.source = source;

  source.onopen = () => {
    if (_live !== live) return;
    live.attempts = 0;
    _setLiveState('');
  };
  source.onmessage = (msg) => {
    if (_live !== live) return;
    let data;
    try { data = JSON.parse(msg.data); } catch (_) { return; }
    if (data && data.final) {
      _stopLive();
      if (data.status) _setJobStatus(jobId, data.status);
      refreshHistory();
      if (_view && _view.token === live.token) selectJob(jobId);
      return;
    }
    _onLiveEvent(data);
  };
  source.onerror = () => {
    if (_live !== live) return;
    // EventSource would retry with the same ?since=, replaying events, so
    // reconnect by hand from the last one seen.
    source.close();
    live.source = null;
    const delay = view.reconnectDelay(live.attempts++);
    _setLiveState(`Live view lost, reconnecting in ${Math.round(delay / 1000)} s…`);
    live.retryTimer = setTimeout(_connect, delay);
  };
}

function _onLiveEvent(event) {
  const live = _live;
  if (!live || !_view || !event || typeof event !== 'object') return;
  if (typeof event.seq === 'number') {
    if (event.seq <= live.seq) return;   // already have it
    live.seq = event.seq;
  }
  _view.events.push(event);
  const next = view.statusAfterEvent(_view.status.status, event);
  if (next !== _view.status.status) {
    _view.status.status = next;
    _setJobStatus(_view.status.job_id, next);
    _renderHead();
  }
  if (event.type === 'paused') {
    // The event has the question; the choices come from /status.
    _view.status.pause = null;
    _fetchPause();
    _notifyPause(event.question);
  } else if (event.type === 'resumed') {
    _view.status.pause = null;
    _setAttention(false);
    _renderReply();
  }
  if (!live.renderQueued) {
    live.renderQueued = true;
    requestAnimationFrame(() => {
      live.renderQueued = false;
      if (_live === live) _renderTimeline({});
    });
  }
}

// ── Answering a pause ──────────────────────────────────────────────────

async function _fetchPause() {
  const v = _view;
  if (!v) return;
  try {
    const st = await api(`${API}/status/${encodeURIComponent(v.status.job_id)}?since=${view.lastSeq(v.events)}`);
    if (_view !== v || v.status.status !== 'paused') return;
    v.status.pause = st.pause || null;
    if (st.deadline_at) v.status.deadline_at = st.deadline_at;
    _renderReply();
  } catch (_) {
    // The stream carries on; the next pause/resume or a reload tries again.
  }
}

let _replyKey = null;   // which pause the reply bar was built for

function _renderReply() {
  const bar = byId('creator-reply');
  if (!bar) return;
  const pause = _view && _view.status.status === 'paused' ? _view.status.pause : null;
  const controls = view.replyControls(pause);
  if (!controls) {
    bar.hidden = true;
    _replyKey = null;
    return;
  }
  const key = `${_view.status.job_id}|${pause.since || ''}|${pause.kind}`;
  const text = byId('creator-reply-text');
  const send = byId('creator-reply-send');
  if (key !== _replyKey) {
    // A new pause: start with an empty box. (Re-renders for the same pause
    // keep what you've typed.)
    _replyKey = key;
    if (text) text.value = '';
    _setReplyMessage('');
  }
  if (text) text.placeholder = controls.placeholder;
  if (send) {
    send.hidden = controls.mode !== 'answer';
    send.disabled = false;
    send.textContent = view.sendLabel(text ? text.value : '');
  }
  const notice = byId('creator-reply-notice');
  if (notice) {
    notice.textContent = controls.notice;
    notice.hidden = !controls.notice;
  }
  const choices = byId('creator-reply-choices');
  if (choices) {
    choices.replaceChildren(...controls.buttons.map((b) => {
      const btn = make('button', {
        type: 'button',
        class: controls.mode === 'approval' ? `creator-choice-btn tone-${b.tone}` : 'creator-option-chip',
        text: b.label, title: b.hint || null,
      });
      btn.addEventListener('click', () => _sendReply(
        controls.mode === 'approval'
          ? { decision: b.decision, answer: text ? text.value : '' }
          : { answer: b.answer },
      ));
      return btn;
    }));
    choices.hidden = !controls.buttons.length;
  }
  bar.hidden = false;
}

async function _sendReply(body) {
  const v = _view;
  if (!v) return;
  const bar = byId('creator-reply');
  const buttons = bar ? [...bar.querySelectorAll('button')] : [];
  buttons.forEach(b => { b.disabled = true; });
  _setReplyMessage('Sending…');
  const payload = {};
  if (body.decision) payload.decision = body.decision;
  payload.answer = String(body.answer || '').trim();
  try {
    await api(`${API}/resume/${encodeURIComponent(v.status.job_id)}`, {
      method: 'POST', body: JSON.stringify(payload),
    });
    if (_view !== v) return;
    // The stream's "resumed" event flips the status; hide the bar now.
    v.status.pause = null;
    _setReplyMessage('');
    _renderReply();
  } catch (e) {
    if (_view !== v) return;
    buttons.forEach(b => { b.disabled = false; });
    if (e.status === 409) {
      // Not paused any more (answered elsewhere, stopped, timed out).
      _setReplyMessage('');
      selectJob(v.status.job_id);
      return;
    }
    _setReplyMessage(e.message, true);
  }
}

// ── Timeline ───────────────────────────────────────────────────────────

function _renderTimeline({ scrollToEnd = false }) {
  const timeline = byId('creator-timeline');
  if (!timeline || !_view) return;
  // Re-rendered whole on each batch of live events: keep the commands you
  // opened open, and only follow new output if you were already at the end.
  const atEnd = timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight < 40;
  const opened = new Set();
  timeline.querySelectorAll('details.creator-cmd').forEach((d, i) => { if (d.open) opened.add(i); });

  const { status, report } = _view;
  const nodes = [make('div', { class: 'creator-msg creator-msg-user', text: status.task || '' })];
  view.buildTimeline(_view.events).forEach((item) => nodes.push(_renderItem(item)));
  if (report && report.report) {
    const body = make('div', { class: 'creator-report-body' });
    body.innerHTML = markdownModule.mdToHtml(report.report);   // same renderer as chat replies
    const audit = report.audit_log
      ? make('div', { class: 'creator-report-foot', text: `Full audit log on the server: ${report.audit_log}` })
      : null;
    nodes.push(make('div', { class: 'creator-msg creator-msg-report' }, [
      make('div', { class: 'creator-msg-label', text: 'Report' }), body, audit,
    ]));
  } else if (status.error && !view.isActive(status.status)) {
    nodes.push(make('div', { class: 'creator-system level-error', text: status.error }));
  } else if (view.isActive(status.status)) {
    nodes.push(make('div', { class: 'creator-working', text: status.status === 'paused' ? 'Waiting for you…' : 'Working…' }));
  }
  const prevTop = timeline.scrollTop;
  timeline.replaceChildren(...nodes);
  timeline.querySelectorAll('details.creator-cmd').forEach((d, i) => { if (opened.has(i)) d.open = true; });
  timeline.scrollTop = (scrollToEnd || atEnd) ? timeline.scrollHeight : prevTop;
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
