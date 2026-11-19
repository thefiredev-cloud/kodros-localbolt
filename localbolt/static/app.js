'use strict';
(() => {
  const $ = (s) => document.querySelector(s);
  const S = {
    status: null, projects: [], pid: null, project: null, files: [], versions: [], chat: [],
    file: null, base: 0, text: '', dirty: false, view: null, // view: null = latest, else version number
    generating: false, abort: null, live: null, bust: 0,
  };
  const EXAMPLES = [
    'A pomodoro timer with a task list and a sound-free finish animation',
    'A landing page for a small bakery with a menu and an opening-hours table',
    'A markdown note pad with live preview and a word count',
  ];

  // ---- helpers -------------------------------------------------------
  function el(tag, attrs, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === 'class') n.className = v;
      else if (k === 'text') n.textContent = v;
      else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
      else if (v !== false && v != null) n.setAttribute(k, v === true ? '' : v);
    }
    for (const k of kids) if (k != null) n.append(k);
    return n;
  }
  class ApiError extends Error {
    constructor(status, data) { super((data && data.message) || ('HTTP ' + status)); this.status = status; this.data = data || {}; }
  }
  async function api(method, url, body, opts) {
    const res = await fetch(url, {
      method, headers: body ? { 'Content-Type': 'application/json' } : {},
      body: body ? JSON.stringify(body) : undefined, signal: opts && opts.signal,
    });
    let data = null;
    try { data = await res.json(); } catch (_) { /* non-JSON error body */ }
    if (!res.ok) throw new ApiError(res.status, data);
    return data;
  }
  const kb = (n) => (n < 1024 ? n + ' B' : (n / 1024).toFixed(1) + ' KB');
  const headVersion = () => (S.project ? S.project.head : 0);

  // ---- dialog --------------------------------------------------------
  function ask({ title, text, value, select, confirmOnly, ok }) {
    return new Promise((resolve) => {
      const d = $('#dlg'), form = $('#dlg-form'), input = $('#dlg-input'), sel = $('#dlg-select');
      $('#dlg-title').textContent = title;
      $('#dlg-text').textContent = text || '';
      $('#dlg-ok').textContent = ok || 'OK';
      input.hidden = !!confirmOnly;
      input.value = value || '';
      sel.hidden = !select;
      sel.replaceChildren();
      if (select) for (const [v, t] of select) sel.append(el('option', { value: v, text: t }));
      let done = false;
      const finish = (val) => { if (done) return; done = true; d.close(); resolve(val); };
      form.onsubmit = (e) => { e.preventDefault(); finish(confirmOnly ? true : { value: input.value.trim(), select: sel.value }); };
      $('#dlg-cancel').onclick = () => finish(null);
      d.oncancel = () => finish(null);
      d.showModal();
      if (!confirmOnly) { input.focus(); input.select(); } else $('#dlg-ok').focus();
    });
  }

  // ---- status / banner ----------------------------------------------
  function renderModel() {
    const m = S.status && S.status.model;
    const badge = $('#model-badge'), banner = $('#banner');
    if (!m) { badge.textContent = 'Model: checking'; badge.className = 'badge'; return; }
    if (m.available) {
      badge.textContent = 'Model online: ' + m.model;
      badge.className = 'badge ok';
      if (!banner.classList.contains('err')) banner.hidden = true;
    } else {
      badge.textContent = 'Model unavailable';
      badge.className = 'badge down';
      banner.classList.remove('err');
      banner.textContent = 'Model unavailable (' + (m.reason || 'no answer') + '). Prompts are paused. You can still create a project from a template and edit every file by hand.';
      banner.hidden = false;
    }
    renderSend();
  }
  async function refreshStatus() {
    try { S.status = await api('GET', '/api/status'); } catch (e) {
      S.status = { model: { available: false, reason: 'LocalBolt server not answering', model: '' }, preview_origin: S.status && S.status.preview_origin };
    }
    renderModel();
  }
  function showError(msg) {
    const b = $('#banner');
    b.classList.add('err'); b.textContent = msg; b.hidden = false;
  }
  function clearError() { const b = $('#banner'); b.classList.remove('err'); renderModel(); }

  // ---- projects ------------------------------------------------------
  async function loadProjects(select) {
    const { projects } = await api('GET', '/api/projects');
    S.projects = projects;
    const sel = $('#project-select');
    sel.replaceChildren();
    if (!projects.length) sel.append(el('option', { value: '', text: 'No projects yet' }));
    for (const p of projects) sel.append(el('option', { value: p.id, text: p.name + '  (v' + p.head + ')' }));
    const target = select || S.pid || (projects[0] && projects[0].id) || null;
    if (target && projects.some((p) => p.id === target)) { sel.value = target; await openProject(target); }
    else { S.pid = null; S.project = null; S.files = []; S.versions = []; S.chat = []; closeFile(); renderAll(); }
  }
  async function openProject(pid, keepFile) {
    const d = await api('GET', '/api/projects/' + pid);
    S.pid = pid; S.project = d.project; S.files = d.files; S.versions = d.versions; S.chat = d.chat;
    S.status = S.status || {}; S.status.preview_origin = d.preview.replace(/\/p-[0-9a-f]{8}$/, '');
    S.view = null;
    $('#project-select').value = pid;
    const paths = S.files.map((f) => f.path);
    const want = keepFile && paths.includes(S.file) ? S.file : (paths.includes('index.html') ? 'index.html' : paths[0]);
    if (want) await openFile(want, true); else closeFile();
    renderAll();
  }
  async function refreshProject() {
    const d = await api('GET', '/api/projects/' + S.pid);
    S.project = d.project; S.files = d.files; S.versions = d.versions; S.chat = d.chat;
    const opt = [...$('#project-select').options].find((o) => o.value === S.pid);
    if (opt) opt.textContent = d.project.name + '  (v' + d.project.head + ')';
  }

  async function newProject() {
    const r = await ask({ title: 'New project', text: 'Starter files work without the model. Or skip them and describe an app in the prompt box.', value: 'My app', ok: 'Create',
      select: [['starter', 'Starter files'], ['empty', 'Empty page']] });
    if (!r) return;
    try {
      const p = await api('POST', '/api/projects', { name: r.value, template: r.select });
      await loadProjects(p.id);
    } catch (e) { showError(e.message); }
  }

  // ---- files / editor -----------------------------------------------
  function closeFile() { S.file = null; S.text = ''; S.dirty = false; S.base = 0; }
  async function openFile(path, force) {
    if (!force && S.dirty && !(await ask({ title: 'Discard changes?', text: S.file + ' has unsaved edits.', confirmOnly: true, ok: 'Discard' }))) return;
    const q = S.view ? '&v=' + S.view : '';
    try {
      const f = await api('GET', '/api/projects/' + S.pid + '/file?path=' + encodeURIComponent(path) + q);
      S.file = path; S.text = f.content; S.base = f.version; S.dirty = false;
    } catch (e) { showError(e.message); return; }
    renderTree(); renderEditor();
  }
  function renderTree() {
    const ul = $('#tree');
    ul.replaceChildren();
    let lastDir = '';
    for (const f of S.files) {
      const i = f.path.lastIndexOf('/');
      const dir = i < 0 ? '' : f.path.slice(0, i);
      if (dir !== lastDir && dir) ul.append(el('li', { class: 'dir', text: dir + '/' }));
      lastDir = dir;
      const name = i < 0 ? f.path : f.path.slice(i + 1);
      ul.append(el('li', {}, el('button', {
        type: 'button', 'aria-current': f.path === S.file ? 'true' : 'false', title: f.path,
        onclick: () => openFile(f.path),
      }, el('span', { text: name }), el('span', { class: 'sz', text: kb(f.size) }))));
    }
    if (!S.files.length) ul.append(el('li', { class: 'dir', text: S.pid ? 'No files' : '' }));
  }
  function renderEditor() {
    const ed = $('#editor'), readonly = S.view != null;
    ed.disabled = !S.file;
    if (ed.value !== S.text) ed.value = S.text;
    ed.readOnly = readonly;
    $('#file-name').textContent = S.file ? S.file + (readonly ? '  (v' + S.view + ', read only)' : '') : 'No file';
    $('#dirty').hidden = !S.dirty;
    for (const id of ['#btn-save', '#btn-file-rename', '#btn-file-delete']) $(id).disabled = !S.file || readonly;
    $('#btn-newfile').disabled = !S.pid || readonly;
    updateStatus();
  }
  function updateStatus() {
    const ed = $('#editor');
    const upto = ed.value.slice(0, ed.selectionStart);
    const ln = upto.split('\n').length, col = upto.length - upto.lastIndexOf('\n');
    $('#editor-status').textContent = S.file ? 'Ln ' + ln + ', Col ' + col + '   ' + new Blob([ed.value]).size + ' bytes' : '';
  }
  async function saveFile(overwrite) {
    if (!S.file || S.view != null) return;
    const body = { path: S.file, content: $('#editor').value };
    if (!overwrite) body.base = S.base;
    try {
      const r = await api('PUT', '/api/projects/' + S.pid + '/file', body);
      S.text = body.content; S.dirty = false;
      if (!r.unchanged) S.base = r.version;
      await refreshProject();
      S.base = headVersion();
      renderTree(); renderEditor(); renderVersions(); renderMessages(); reloadFrame();
    } catch (e) {
      if (e.status === 409) {
        const ok = await ask({ title: 'File changed', text: S.file + ' was changed by a newer version since you opened it. Overwrite it with your edit?', confirmOnly: true, ok: 'Overwrite' });
        if (ok) return saveFile(true);
      } else showError(e.message);
    }
  }
  async function newFile() {
    const r = await ask({ title: 'New file', text: 'Path such as about.html or js/util.js', value: 'page.html', ok: 'Create' });
    if (!r || !r.value) return;
    try {
      await api('PUT', '/api/projects/' + S.pid + '/file', { path: r.value, content: '' });
      await refreshProject(); await openFile(r.value, true); renderAll();
    } catch (e) { showError(e.message); }
  }
  async function renameFile() {
    const r = await ask({ title: 'Rename file', value: S.file, ok: 'Rename' });
    if (!r || !r.value || r.value === S.file) return;
    try {
      await api('POST', '/api/projects/' + S.pid + '/file/rename', { from: S.file, to: r.value });
      await refreshProject(); await openFile(r.value, true); renderAll();
    } catch (e) { showError(e.message); }
  }
  async function deleteFile() {
    if (!(await ask({ title: 'Delete ' + S.file + '?', text: 'This makes a new version. Older versions keep the file.', confirmOnly: true, ok: 'Delete' }))) return;
    try {
      await api('DELETE', '/api/projects/' + S.pid + '/file?path=' + encodeURIComponent(S.file));
      await refreshProject();
      const next = S.files.find((f) => f.path === 'index.html') || S.files[0];
      if (next) await openFile(next.path, true); else closeFile();
      renderAll();
    } catch (e) { showError(e.message); }
  }

  // ---- versions / preview -------------------------------------------
  function renderVersions() {
    const sel = $('#version-select');
    sel.replaceChildren();
    if (!S.versions.length) { sel.append(el('option', { value: '', text: 'No versions' })); sel.disabled = true; return; }
    sel.disabled = false;
    const head = S.versions[S.versions.length - 1].n;
    sel.append(el('option', { value: 'head', text: 'Latest (v' + head + ')' }));
    for (const v of [...S.versions].reverse()) {
      const t = (v.label || v.kind || '').slice(0, 44);
      sel.append(el('option', { value: String(v.n), text: 'v' + v.n + '  ' + t }));
    }
    sel.value = S.view == null ? 'head' : String(S.view);
    $('#btn-restore').hidden = S.view == null;
  }
  // Reached through a gateway (another port or host), the browser cannot use 127.0.0.1:8878;
  // the gateway publishes the preview server on the same host at status.gateway_preview_port.
  function previewOrigin() {
    const s = S.status;
    if (s.gateway_preview_port && String(s.app_port) !== location.port) {
      return location.protocol + '//' + location.hostname + ':' + s.gateway_preview_port;
    }
    return s.preview_origin;
  }
  function previewUrl() {
    if (!S.pid || !S.status || !S.status.preview_origin || !S.files.some((f) => f.path === 'index.html')) return null;
    return previewOrigin() + '/' + S.pid + '/' + (S.view == null ? 'head' : 'v' + S.view) + '/index.html';
  }
  function reloadFrame() {
    const url = previewUrl(), frame = $('#frame');
    $('#frame-empty').hidden = !!url;
    $('#btn-open').href = url || '#';
    $('#btn-open').toggleAttribute('hidden', !url);
    // A fresh query string forces a reload even when the URL is otherwise unchanged.
    frame.src = url ? url + '?r=' + (++S.bust) : 'about:blank';
  }
  async function selectVersion(value) {
    if (S.dirty && !(await ask({ title: 'Discard changes?', text: S.file + ' has unsaved edits.', confirmOnly: true, ok: 'Discard' }))) {
      renderVersions();
      return;
    }
    S.view = value === 'head' ? null : Number(value);
    try {
      S.files = (await api('GET', '/api/projects/' + S.pid + '/files' + (S.view ? '?v=' + S.view : ''))).files;
      const paths = S.files.map((f) => f.path);
      const want = paths.includes(S.file) ? S.file : (paths.includes('index.html') ? 'index.html' : paths[0]);
      if (want) await openFile(want, true); else closeFile();
    } catch (e) { showError(e.message); }
    renderAll();
  }
  async function restore() {
    if (S.view == null) return;
    if (!(await ask({ title: 'Restore v' + S.view + '?', text: 'This creates a new latest version with the files of v' + S.view + '. History is kept.', confirmOnly: true, ok: 'Restore' }))) return;
    try {
      await api('POST', '/api/projects/' + S.pid + '/restore', { version: S.view });
      await openProject(S.pid, true);
    } catch (e) { showError(e.message); }
  }

  // ---- chat / generation --------------------------------------------
  function renderMessages() {
    const ol = $('#messages');
    ol.replaceChildren();
    if (!S.chat.length && !S.live) {
      const li = el('li', { class: 'empty' }, el('div', { text: S.pid ? 'Describe a change to this app.' : 'Describe an app and LocalBolt builds it on this machine.' }));
      if (!S.pid || !S.versions.length || S.versions.length <= 1) {
        const ex = el('div', { class: 'examples' });
        for (const t of EXAMPLES) ex.append(el('button', { type: 'button', text: t, onclick: () => { $('#prompt').value = t; $('#prompt').focus(); } }));
        li.append(ex);
      }
      ol.append(li);
    }
    for (const m of S.chat) {
      const li = el('li', { class: m.role });
      const meta = el('div', { class: 'meta' }, el('span', { text: m.role === 'user' ? 'You' : m.role === 'assistant' ? 'LocalBolt' : 'Problem' }));
      if (m.version) meta.append(el('button', { class: 'chip', type: 'button', title: 'Preview this version', text: 'v' + m.version, onclick: () => { $('#version-select').value = String(m.version); selectVersion(String(m.version)); } }));
      li.append(meta, el('div', { text: m.text }));
      if (m.changed && m.changed.length) li.append(el('div', { class: 'files', text: m.changed.join('  ') }));
      ol.append(li);
    }
    if (S.live) {
      const li = el('li', { class: 'live' });
      li.append(el('div', { class: 'meta', text: S.live.state }));
      if (S.live.files.length) li.append(el('div', { class: 'files', text: S.live.files.join('\n') }));
      ol.append(li);
    }
    ol.scrollTop = ol.scrollHeight;
  }
  function renderSend() {
    const m = S.status && S.status.model;
    const down = m && !m.available;
    const send = $('#btn-send');
    send.disabled = S.generating || !!down;
    send.textContent = S.generating ? 'Building...' : 'Build';
    $('#btn-stop').hidden = !S.generating;
    $('#hint').textContent = down ? 'Model offline' : 'Ctrl+Enter to send';
    $('#prompt').disabled = S.generating;
  }
  async function send() {
    const prompt = $('#prompt').value.trim();
    if (!prompt || S.generating) return;
    if (S.dirty) { showError('Save or discard your unsaved edit first, so the model sees your latest file.'); return; }
    clearError();
    try {
      if (!S.pid) {
        const p = await api('POST', '/api/projects', { name: prompt.slice(0, 40), template: null });
        await loadProjects(p.id);
      }
      if (S.view != null) { S.view = null; await openProject(S.pid, true); }
    } catch (e) { showError(e.message); return; }
    S.generating = true; S.abort = new AbortController();
    S.live = { state: 'Waiting for the model...', files: [] };
    S.chat.push({ role: 'user', text: prompt });
    $('#prompt').value = '';
    renderSend(); renderMessages();
    try {
      const res = await fetch('/api/projects/' + S.pid + '/generate', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ prompt, think: $('#think').checked }), signal: S.abort.signal,
      });
      if (!res.ok) {
        let data = {}; try { data = await res.json(); } catch (_) { /* ignore */ }
        if (data.model) S.status.model = data.model;
        throw new ApiError(res.status, data);
      }
      const reader = res.body.getReader(), dec = new TextDecoder();
      let buf = '';
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let i;
        while ((i = buf.indexOf('\n')) >= 0) { const line = buf.slice(0, i); buf = buf.slice(i + 1); if (line.trim()) onEvent(JSON.parse(line)); }
      }
    } catch (e) {
      if (e.name === 'AbortError') { /* user pressed Stop */ }
      else showError(e.status === 503 ? (e.message) : ('Build failed: ' + e.message));
    } finally {
      S.generating = false; S.live = null; S.abort = null;
      await refreshStatus();
      if (S.pid) { await openProject(S.pid, true); reloadFrame(); }
      renderSend(); renderMessages();
    }
  }
  function onEvent(ev) {
    if (ev.type === 'progress') S.live.state = 'Thinking and writing... ' + ev.elapsed + 's, ' + Math.round((ev.reasoning_chars + ev.content_chars) / 4) + ' tokens (approx)';
    else if (ev.type === 'file_start') S.live.files.push('writing ' + ev.path + ' ...');
    else if (ev.type === 'file') { S.live.files = S.live.files.filter((x) => x !== 'writing ' + ev.path + ' ...'); S.live.files.push('wrote ' + ev.path + ' (' + kb(ev.bytes) + ')'); }
    else if (ev.type === 'delete') S.live.files.push('deleted ' + ev.path);
    else if (ev.type === 'warning') S.live.files.push('warning: ' + ev.message);
    else if (ev.type === 'error') { showError(ev.message); if (ev.error === 'model_unavailable') refreshStatus(); }
    renderMessages();
  }

  // ---- render + wire -------------------------------------------------
  function renderAll() {
    renderTree(); renderEditor(); renderVersions(); renderMessages(); renderSend(); reloadFrame();
    const has = !!S.pid;
    for (const id of ['#btn-rename', '#btn-archive']) $(id).disabled = !has;
    const zip = $('#btn-zip');
    zip.href = has ? '/api/projects/' + S.pid + '/download.zip' + (S.view ? '?v=' + S.view : '') : '#';
    zip.toggleAttribute('hidden', !has || !S.versions.length);
  }
  function setMode(mode) {
    $('#layout').dataset.mode = mode;
    for (const b of document.querySelectorAll('.seg button')) b.setAttribute('aria-pressed', String(b.dataset.mode === mode));
  }

  function wire() {
    for (const b of document.querySelectorAll('.seg button')) b.addEventListener('click', () => setMode(b.dataset.mode));
    $('#project-select').addEventListener('change', (e) => e.target.value && openProject(e.target.value));
    $('#btn-new').addEventListener('click', newProject);
    $('#btn-rename').addEventListener('click', async () => {
      const r = await ask({ title: 'Rename project', value: S.project.name, ok: 'Rename' });
      if (!r || !r.value) return;
      try { await api('POST', '/api/projects/' + S.pid + '/rename', { name: r.value }); await loadProjects(S.pid); } catch (e) { showError(e.message); }
    });
    $('#btn-archive').addEventListener('click', async () => {
      if (!(await ask({ title: 'Archive ' + S.project.name + '?', text: 'It moves to the archive folder inside the LocalBolt data directory. Nothing is deleted.', confirmOnly: true, ok: 'Archive' }))) return;
      try { await api('POST', '/api/projects/' + S.pid + '/archive', {}); S.pid = null; await loadProjects(); } catch (e) { showError(e.message); }
    });
    $('#btn-newfile').addEventListener('click', newFile);
    $('#btn-file-rename').addEventListener('click', renameFile);
    $('#btn-file-delete').addEventListener('click', deleteFile);
    $('#btn-save').addEventListener('click', () => saveFile(false));
    $('#btn-reload').addEventListener('click', reloadFrame);
    $('#btn-restore').addEventListener('click', restore);
    $('#btn-stop').addEventListener('click', () => S.abort && S.abort.abort());
    $('#version-select').addEventListener('change', (e) => selectVersion(e.target.value));
    const ed = $('#editor');
    ed.addEventListener('input', () => { S.dirty = ed.value !== S.text; $('#dirty').hidden = !S.dirty; updateStatus(); });
    for (const ev of ['keyup', 'click', 'select']) ed.addEventListener(ev, updateStatus);
    ed.addEventListener('keydown', (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === 's') { e.preventDefault(); saveFile(false); return; }
      if (e.key === 'Tab' && !e.shiftKey && !e.ctrlKey && !e.metaKey && !ed.readOnly) {
        e.preventDefault();
        const s = ed.selectionStart;
        ed.setRangeText('  ', s, ed.selectionEnd, 'end');
        ed.dispatchEvent(new Event('input'));
      }
    });
    $('#prompt-form').addEventListener('submit', (e) => { e.preventDefault(); send(); });
    $('#prompt').addEventListener('keydown', (e) => { if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); send(); } });
    window.addEventListener('beforeunload', (e) => { if (S.dirty) { e.preventDefault(); e.returnValue = ''; } });
  }

  async function init() {
    wire();
    await refreshStatus();
    try { await loadProjects(); } catch (e) { showError(e.message); }
    setInterval(() => { if (!S.generating) refreshStatus(); }, 10000);
  }
  init();
})();
