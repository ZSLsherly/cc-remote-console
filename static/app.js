/* CC 远程控制台 — 前端逻辑（登录 / 监视 / 终端） */
'use strict';
const $ = (id) => document.getElementById(id);

/* ================= 工具 ================= */
function getCookie(name) {
  for (const part of document.cookie.split(';')) {
    const [k, ...rest] = part.trim().split('=');
    if (k === name) return rest.join('=');
  }
  return '';
}
function esc(s) {
  return String(s).replace(/[&<>"']/g,
    c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function fmtTs(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return isNaN(d) ? '' : d.toLocaleTimeString('zh-CN', {hour12: false});
}

/* ================= API ================= */
let wsUrl = null;
let csrfReloaded = false;

async function api(path, opts = {}) {
  const headers = {'Content-Type': 'application/json'};
  if (opts.method && opts.method !== 'GET') headers['X-CSRF-Token'] = getCookie('cc_csrf');
  const r = await fetch('/api/' + path, {
    method: opts.method || 'GET',
    headers,
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
  });
  if (r.status === 401) { showLogin(); throw new Error('unauthorized'); }
  if (r.status === 403) {
    if (!csrfReloaded) { csrfReloaded = true; location.reload(); }
    throw new Error('csrf');
  }
  if (!r.ok) {
    const d = await r.json().catch(() => ({}));
    throw new Error(d.error || ('HTTP ' + r.status));
  }
  return r.json();
}

/* ================= 登录 ================= */
function showLogin() {
  $('login-view').classList.remove('hidden');
  $('app').classList.add('hidden');
}
function showApp() {
  $('app').classList.remove('hidden');
  $('login-view').classList.add('hidden');
}

$('login-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  $('login-err').textContent = '';
  try {
    const r = await fetch('/api/login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-CSRF-Token': getCookie('cc_csrf')},
      body: JSON.stringify({user: $('login-user').value.trim(), password: $('login-pass').value}),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) {
      if (r.status === 403 && !csrfReloaded) { csrfReloaded = true; location.reload(); return; }
      $('login-err').textContent = d.error || '登录失败';
      return;
    }
    $('login-pass').value = '';
    const w = await api('whoami');
    wsUrl = w.ws_url;
    showApp();
    startApp();
  } catch (err) {
    $('login-err').textContent = err.message || '网络错误';
  }
});

/* ================= 监视视图 ================= */
let sessions = [], cur = null, msgs = [], total = 0, stick = true;
let phoneSid = null, control = false, sending = false;
let bannerTimer = null, lastErrKey = null;

function showBanner(txt) {
  const b = $('banner');
  b.textContent = txt; b.style.display = 'block';
  clearTimeout(bannerTimer);
  bannerTimer = setTimeout(() => { b.style.display = 'none'; }, 6000);
}

function msgEl(m) {
  const div = document.createElement('div');
  div.className = 'msg';
  const ts = '<div class="meta-line">' + fmtTs(m.ts) + '</div>';
  if (m.role === 'user') {
    div.innerHTML = ts + '<div class="u">' + esc(m.text) + '</div>';
    return div;
  }
  if (m.role === 'attachment' && m.attachment) {
    div.innerHTML = ts + '<details class="att"><summary>附件 · ' + esc(m.attachment.kind)
      + '</summary><pre>' + esc(m.attachment.content) + '</pre></details>';
    return div;
  }
  let h = ts + '<div class="a">';
  if (m.thinking) h += '<details class="think"><summary>思考</summary>' + esc(m.thinking) + '</details>';
  if (m.text) h += '<div class="text">' + esc(m.text) + '</div>';
  for (const t of m.tools || []) {
    const errCls = t.isError ? ' err' : '';
    h += '<div class="tool' + errCls + '"><div class="tool-head">'
      + '<span class="badge' + errCls + '">' + esc(t.name) + '</span>'
      + '<code>' + esc(t.summary) + '</code></div>';
    if (t.result !== null) {
      h += '<details' + (t.isError ? ' open' : '') + '><summary>结果' + (t.isError ? '（出错）' : '') + '</summary>'
        + '<pre>' + esc(t.result === '' ? '（无输出）' : t.result) + '</pre></details>';
    }
    h += '</div>';
  }
  h += '</div>';
  div.innerHTML = h;
  return div;
}

function renderNew() {
  if (!cur || !msgs.length) return;
  const frag = document.createDocumentFragment();
  for (const m of msgs) frag.appendChild(msgEl(m));
  $('msglist').appendChild(frag);
  if (stick) $('msglist').scrollTop = $('msglist').scrollHeight;
}
$('msglist').addEventListener('scroll', () => {
  stick = $('msglist').scrollHeight - $('msglist').scrollTop - $('msglist').clientHeight < 120;
});

function fillSel() {
  const curId = $('sel').value || cur;
  let html = '';
  for (const s of sessions) {
    html += '<option value="' + esc(s.sessionId) + '"' + (s.sessionId === curId ? ' selected' : '')
      + '>' + (s.sessionId === phoneSid ? '[手机] ' : '') + (s.running ? '● ' : '')
      + esc(s.title) + ' — ' + esc(s.project) + '</option>';
  }
  $('sel').innerHTML = html;
  if (!curId && sessions.length) {
    cur = sessions[0].sessionId;
    $('sel').value = cur;
    resetView();
  }
}
$('sel').addEventListener('change', () => { cur = $('sel').value; resetView(); });

function resetView() {
  msgs = []; total = 0;
  $('msglist').innerHTML = '<div class="empty">加载中…</div>';
  pollMsgs();
}

function applyMeta(s) {
  $('meta').textContent = (s.cwd ? s.cwd : '') + (s.branch ? '  [' + s.branch + ']' : '');
  $('dot').className = s.running ? 'run' : '';
  $('status').textContent = s.running ? '运行中' : '空闲';
  const isPhone = control && cur === phoneSid;
  $('warn').style.display = isPhone ? 'block' : 'none';
  $('sendbar').classList.toggle('hidden', !isPhone);
  $('msglist').style.paddingBottom = isPhone ? '80px' : '16px';
}

async function pollOverview() {
  const d = await api('overview').catch(() => null);
  if (!d) return;
  sessions = d.sessions;
  if (control && phoneSid && !sessions.find(s => s.sessionId === phoneSid)) {
    sessions = sessions.concat([{sessionId: phoneSid, title: '手机会话', project: '专用会话',
      cwd: null, branch: null, lastTs: null, msgCount: 0, running: false}]);
  }
  const curInfo = sessions.find(s => s.sessionId === cur) || sessions[0];
  if (curInfo) applyMeta(curInfo);
  fillSel();
}

async function pollMsgs() {
  if (!cur) return;
  const d = await api('messages?session=' + encodeURIComponent(cur) + '&after=' + total).catch(() => null);
  if (!d) return;
  if (d.total !== total) {
    if (d.total < total) { $('msglist').innerHTML = ''; msgs = []; }
    msgs = msgs.concat(d.messages);
    total = d.total;
    if (!msgs.length) {
      $('msglist').innerHTML = '<div class="empty">' +
        (control && cur === phoneSid ? '手机会话：发第一条消息开始使用' : '暂无消息') + '</div>';
    } else {
      renderNew();
    }
  }
  applyMeta(d);
}

async function pollStatus() {
  const d = await api('status').catch(() => null);
  if (!d) return;
  control = d.control || false;
  phoneSid = d.phoneSessionId || null;
  sending = d.sending || false;
  $('btn').disabled = sending;
  $('btn').textContent = sending ? '处理中…' : '发送';
  $('inp').disabled = sending;
  if (d.lastError && d.lastError !== lastErrKey) {
    lastErrKey = d.lastError;
    showBanner('发送失败：' + d.lastError);
  }
}

async function sendMsg() {
  if (sending) return;
  const t = $('inp').value.trim();
  if (!t) return;
  try {
    await api('send', {method: 'POST', body: {text: t}});
    $('inp').value = '';
  } catch (e) {
    if (e.message !== 'unauthorized' && e.message !== 'csrf') showBanner(e.message);
  }
}
$('btn').addEventListener('click', sendMsg);
$('inp').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMsg(); }
});

/* ================= 终端视图 ================= */
let term = null, fit = null, termId = localStorage.getItem('cc_term_id') || '';
let ws = null, wsRetry = 0, wsWanted = false;

function setTermStatus(s) { $('term-status').textContent = s; }

function initTerm() {
  if (term) return;
  term = new Terminal({
    fontSize: 13,
    fontFamily: 'Consolas, "Courier New", monospace',
    theme: {background: '#010409', foreground: '#e6edf3', cursor: '#58a6ff'},
    scrollback: 5000,
  });
  fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open($('terminal'));
  try { fit.fit(); } catch (e) {}
  term.onData((d) => {
    if (ws && ws.readyState === 1) ws.send(JSON.stringify({action: 'input', data: d}));
  });
  $('terminal').addEventListener('click', () => { try { term.focus(); } catch (e) {} });
  window.addEventListener('resize', onTermResize);
  window.addEventListener('orientationchange', () => setTimeout(onTermResize, 300));
}

function onTermResize() {
  if (!term || !fit) return;
  try { fit.fit(); } catch (e) {}
  if (ws && ws.readyState === 1) {
    ws.send(JSON.stringify({action: 'resize', rows: term.rows, cols: term.cols}));
  }
}

function connectTerm() {
  if (!wsUrl) return;
  wsWanted = true;
  setTermStatus('连接中…');
  let sock;
  try { sock = new WebSocket(wsUrl); } catch (e) { retryTerm(); return; }
  ws = sock;
  sock.onopen = () => {
    wsRetry = 0;
    const rows = term ? term.rows : 24, cols = term ? term.cols : 80;
    sock.send(JSON.stringify({action: 'attach', term_id: termId || null, rows, cols}));
  };
  sock.onmessage = (e) => {
    let m;
    try { m = JSON.parse(e.data); } catch (err) { return; }
    if (m.type === 'attached') {
      termId = m.term_id;
      localStorage.setItem('cc_term_id', termId);
      if (m.history) term.write(m.history);
      setTermStatus('已连接 · ' + termId);
    } else if (m.type === 'output') {
      term.write(m.data);
    } else if (m.type === 'exit') {
      setTermStatus('进程已退出 — 点「新终端」重开');
      termId = '';
      localStorage.removeItem('cc_term_id');
    }
  };
  sock.onclose = () => {
    if (wsWanted) { setTermStatus('已断开，重连中…'); retryTerm(); }
  };
}

function retryTerm() {
  const delay = Math.min(15000, 2000 * Math.pow(1.5, wsRetry++));
  setTimeout(() => { if (wsWanted) connectTerm(); }, delay);
}

$('term-new').addEventListener('click', () => {
  termId = '';
  localStorage.removeItem('cc_term_id');
  if (term) term.reset();
  setTermStatus('新终端…');
  if (ws && ws.readyState === 1) {
    ws.send(JSON.stringify({action: 'attach', term_id: null, rows: term.rows, cols: term.cols}));
  } else {
    connectTerm();
  }
});

/* ================= 视图切换 / 登出 ================= */
function switchView(name) {
  document.querySelectorAll('.tab').forEach((b) =>
    b.classList.toggle('active', b.dataset.view === name));
  $('monitor-view').classList.toggle('hidden', name !== 'monitor');
  $('term-view').classList.toggle('hidden', name !== 'term');
  if (name === 'term') {
    initTerm();
    if (!ws || ws.readyState > 1) connectTerm();
    setTimeout(onTermResize, 50);
  }
}
document.querySelectorAll('.tab').forEach((b) =>
  b.addEventListener('click', () => switchView(b.dataset.view)));

$('logout').addEventListener('click', async () => {
  try { await api('logout', {method: 'POST'}); } catch (e) {}
  wsWanted = false;
  if (ws) { try { ws.close(); } catch (e) {} }
  showLogin();
});

/* ================= 启动 ================= */
let started = false;
function startApp() {
  if (started) return;
  started = true;
  switchView('monitor');
  pollOverview();
  pollStatus();
  setInterval(pollOverview, 10000);
  setInterval(pollMsgs, 2000);
  setInterval(pollStatus, 2000);
  if (sessions.length && !cur) { cur = sessions[0].sessionId; resetView(); }
}

(async function boot() {
  try {
    const w = await api('whoami');
    wsUrl = w.ws_url;
    showApp();
    startApp();
  } catch (e) {
    if (e.message === 'unauthorized') showLogin();
    else $('err').classList.remove('hidden');
  }
})();
