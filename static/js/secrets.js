// Settings → Secrets (Creator mode, Phase 4 of docs/creator-plan.md).
//
// Passwords/tokens a Creator run can ask for with the get_secret tool. Each
// secret has an on/off switch; the SERVER checks it, this screen only flips it.
// Values are write-only: the API never returns one, so this screen never
// shows one. Built with DOM methods (no innerHTML) because names and
// descriptions are user text.

const API = '/api/creator/secrets';

function byId(id) { return document.getElementById(id); }

function make(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  Object.entries(props).forEach(([k, v]) => {
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k === 'style') node.setAttribute('style', v);
    else node.setAttribute(k, v);
  });
  children.forEach(c => node.appendChild(c));
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
    throw new Error(detail || `Request failed (${res.status})`);
  }
  return data;
}

function setMessage(text, isError) {
  const msg = byId('secrets-msg');
  if (!msg) return;
  msg.textContent = text || '';
  msg.className = text ? (isError ? 'admin-error' : 'admin-success') : '';
}

function formatUsed(iso) {
  if (!iso) return 'never used';
  try { return 'last used ' + new Date(iso).toLocaleString(); } catch (_) { return 'used'; }
}

function renderRow(secret) {
  const toggle = make('input', { type: 'checkbox', 'aria-label': `Switch ${secret.name} on or off` });
  toggle.checked = !!secret.enabled;
  toggle.addEventListener('change', async () => {
    try {
      await api(`${API}/${encodeURIComponent(secret.id)}`, {
        method: 'PATCH', body: JSON.stringify({ enabled: toggle.checked }),
      });
      setMessage(`${secret.name} switched ${toggle.checked ? 'on' : 'off'}.`, false);
    } catch (e) {
      toggle.checked = !toggle.checked;
      setMessage(e.message, true);
    }
  });

  const editBtn = make('button', { type: 'button', class: 'admin-btn-sm', text: 'Edit' });
  editBtn.addEventListener('click', () => openForm(secret));

  const delBtn = make('button', { type: 'button', class: 'admin-btn-sm', style: 'color:var(--red)', text: 'Delete' });
  delBtn.addEventListener('click', async () => {
    if (delBtn.dataset.confirm !== '1') {
      delBtn.dataset.confirm = '1';
      delBtn.textContent = 'Confirm delete';
      setTimeout(() => { delBtn.dataset.confirm = ''; delBtn.textContent = 'Delete'; }, 4000);
      return;
    }
    try {
      await api(`${API}/${encodeURIComponent(secret.id)}`, { method: 'DELETE' });
      setMessage(`${secret.name} deleted.`, false);
      load();
    } catch (e) { setMessage(e.message, true); }
  });

  const info = make('div', { style: 'flex:1;min-width:0' }, [
    make('div', { style: 'font-weight:600', text: secret.name }),
    make('div', { class: 'admin-toggle-sub', text: [secret.description, formatUsed(secret.last_used)].filter(Boolean).join(' · ') }),
  ]);
  const sw = make('label', { class: 'admin-switch', style: 'flex-shrink:0', title: 'On: Creator runs may use it. Off: requests are refused.' },
    [toggle, make('span', { class: 'admin-slider' })]);

  return make('div', { class: 'admin-user-row', style: 'display:flex;align-items:center;gap:10px;padding:8px 0' },
    [info, sw, editBtn, delBtn]);
}

function openForm(secret) {
  const form = byId('secrets-form');
  if (!form) return;
  const editing = !!secret;
  form.textContent = '';

  const name = make('input', { type: 'text', class: 'admin-input', placeholder: 'Name, e.g. github_token', maxlength: '64', autocomplete: 'off' });
  const desc = make('input', { type: 'text', class: 'admin-input', placeholder: 'What is it for? (optional)', maxlength: '1000', autocomplete: 'off' });
  const value = make('input', {
    type: 'password', class: 'admin-input', maxlength: '10000', autocomplete: 'new-password',
    placeholder: editing ? 'New value (leave blank to keep the current one)' : 'Value',
  });
  const enabled = make('input', { type: 'checkbox' });
  if (editing) {
    name.value = secret.name;
    desc.value = secret.description || '';
    enabled.checked = !!secret.enabled;
  }

  const save = make('button', { type: 'button', class: 'admin-btn-add', text: editing ? 'Save' : 'Add secret' });
  const cancel = make('button', { type: 'button', class: 'admin-btn-sm', text: 'Cancel' });
  cancel.addEventListener('click', closeForm);
  save.addEventListener('click', async () => {
    const body = { name: name.value.trim(), description: desc.value.trim(), enabled: enabled.checked };
    if (value.value) body.value = value.value;
    if (!editing && !body.value) { setMessage('Enter a value.', true); return; }
    try {
      if (editing) {
        await api(`${API}/${encodeURIComponent(secret.id)}`, { method: 'PATCH', body: JSON.stringify(body) });
      } else {
        await api(API, { method: 'POST', body: JSON.stringify(body) });
      }
      value.value = '';
      setMessage(`${body.name} saved.`, false);
      closeForm();
      load();
    } catch (e) { setMessage(e.message, true); }
  });

  form.append(
    make('div', { style: 'display:flex;flex-direction:column;gap:8px;padding:8px 0' }, [
      name, desc, value,
      make('label', { style: 'display:flex;align-items:center;gap:8px' }, [
        make('span', { class: 'admin-switch' }, [enabled, make('span', { class: 'admin-slider' })]),
        make('span', { text: 'Switched on (Creator runs may use it)' }),
      ]),
      make('div', { style: 'display:flex;gap:8px;justify-content:flex-end' }, [cancel, save]),
    ]),
  );
  form.hidden = false;
  name.focus();
}

function closeForm() {
  const form = byId('secrets-form');
  if (!form) return;
  form.textContent = '';
  form.hidden = true;
}

async function load() {
  const list = byId('secrets-list');
  if (!list) return;
  try {
    const data = await api(API);
    list.textContent = '';
    const rows = data.secrets || [];
    if (!rows.length) {
      list.appendChild(make('div', { class: 'admin-empty', text: 'No secrets yet.' }));
      return;
    }
    rows.forEach(s => list.appendChild(renderRow(s)));
  } catch (e) {
    list.textContent = '';
    list.appendChild(make('div', { class: 'admin-empty', text: e.message }));
  }
}

// Host helper connection test (Phase 6a): the server sends the helper a
// "hello" and reports what came back.
async function testHelper() {
  const out = byId('helper-test-result');
  const btn = byId('helper-test-btn');
  if (!out) return;
  out.className = '';
  out.textContent = 'Testing…';
  if (btn) btn.disabled = true;
  try {
    const res = await api('/api/creator/helper/hello');
    if (res.ok) {
      const r = res.reply || {};
      out.className = 'admin-success';
      out.textContent = `Connected: ${r.helper || 'helper'} v${r.version} running as ${r.user} (uid ${r.uid}); can do: ${(r.capabilities || []).join(', ')}.`;
    } else {
      out.className = 'admin-error';
      out.textContent = res.error || 'Not connected.';
    }
  } catch (e) {
    out.className = 'admin-error';
    out.textContent = e.message;
  } finally {
    if (btn) btn.disabled = false;
  }
}

let _bound = false;
function init() {
  if (!_bound) {
    const add = byId('secrets-add-btn');
    if (add) add.addEventListener('click', () => openForm(null));
    const test = byId('helper-test-btn');
    if (test) test.addEventListener('click', testHelper);
    _bound = true;
  }
  setMessage('');
  closeForm();
  load();
}

export default { init, load };
