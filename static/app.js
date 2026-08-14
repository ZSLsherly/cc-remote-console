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
let curRunning = false;      // 所选会话是否被电脑端 CC 占用（决定按钮是否可终止电脑端）
let bannerTimer = null, lastErrKey = null;
let firstRender = true;
const PAGE = 300;          // 长会话分页：首屏只加载最新 300 条
let base = 0;              // 已加载消息中最早的索引（0 = 已加载全部）
let unseen = 0;            // 上翻期间累计的新消息数

function showBanner(txt, ms) {
  const b = $('banner');
  b.textContent = txt; b.style.display = 'block';
  clearTimeout(bannerTimer);
  bannerTimer = setTimeout(() => { b.style.display = 'none'; }, ms || 6000);
}

function msgEl(m) {
  const div = document.createElement('div');
  div.className = 'msg';
  const who = m.role === 'user'
    ? '<span class="who me">我</span>'
    : '<span class="who ai">AI</span>';
  const ts = '<div class="meta-line"><span class="t">' + fmtTs(m.ts) + '</span>' + who + '</div>';
  if (m.role === 'user') {
    if (!m.text || !m.text.trim()) return null;   // 纯工具结果消息：结果已并入 AI 工具卡片，不再渲染空蓝泡
    div.innerHTML = ts + '<div class="u">' + esc(m.text) + '</div>';
    return div;
  }
  if (m.role === 'attachment' && m.attachment) {
    div.innerHTML = ts + '<details class="att"><summary>附件 · ' + esc(m.attachment.kind)
      + '</summary><pre>' + esc(m.attachment.content) + '</pre></details>';
    return div;
  }
  const parts = [];
  if (m.thinking) parts.push('<details class="think"><summary>思考过程</summary>' + esc(m.thinking) + '</details>');
  if (m.text && m.text.trim()) parts.push('<div class="text">' + esc(m.text) + '</div>');
  for (const t of m.tools || []) {
    const state = t.result === null ? '<span class="tstate run" title="进行中">●</span>'
      : (t.isError ? '<span class="tstate err" title="出错">●</span>'
                   : '<span class="tstate ok" title="完成">●</span>');
    const errCls = t.isError ? ' err' : '';
    parts.push('<div class="tool' + errCls + '"><div class="tool-head">'
      + state
      + '<span class="badge' + errCls + '">' + esc(t.name) + '</span>'
      + '<code>' + esc(t.summary) + '</code></div>'
      + (t.result !== null
          ? '<details' + (t.isError ? ' open' : '') + '><summary>结果' + (t.isError ? '（出错）' : '') + '</summary>'
            + '<pre>' + esc(t.result === '' ? '（无输出）' : t.result) + '</pre></details>'
          : '')
      + '</div>');
  }
  if (!parts.length) return null;   // 空帧不渲染
  div.innerHTML = ts + '<div class="a">' + parts.join('') + '</div>';
  return div;
}

function scrollToBottom(force) {
  const el = $('msglist');
  el.scrollTop = el.scrollHeight;
  if (force) stick = true;
}
function renderNew(newMsgs) {
  if (!cur || !newMsgs.length) return;
  const ph = $('msglist').querySelector('.empty');
  if (ph) ph.remove();               // 清掉「暂无消息」占位
  const els = newMsgs.map(msgEl).filter(Boolean);
  if (!els.length) return;
  const frag = document.createDocumentFragment();
  for (const el of els) frag.appendChild(el);
  $('msglist').appendChild(frag);
  if (stick) scrollToBottom(false);
  if (firstRender) {            // 开局强制到底部
    firstRender = false;
    scrollToBottom(true);
  }
  layoutMsgbar();
}
let scrollbarTimer = null;
function flashScrollbar() {   // 滚动时浮现滚动条，1.5 秒无操作淡出
  const bar = $('msgbar');
  bar.classList.add('show');
  clearTimeout(scrollbarTimer);
  scrollbarTimer = setTimeout(() => {
    if (!thumbDrag) bar.classList.remove('show');
  }, 1500);
}
$('msglist').addEventListener('scroll', () => {
  stick = $('msglist').scrollHeight - $('msglist').scrollTop - $('msglist').clientHeight < 120;
  if (stick) unseen = 0;
  $('tobottom').textContent = stick ? '↓ 底部'
    : (unseen > 0 ? ('↓ ' + unseen + ' 条新消息') : '↓ 底部');
  $('tobottom').classList.toggle('hidden', stick);   // 滑条/回底按钮联动
  updateMsgThumb();
  flashScrollbar();
});
$('tobottom').addEventListener('click', () => scrollToBottom(true));

/* ---- 自绘滚动条：手机上没有原生滚动条，自绘一个可见可拖的 ---- */
function updateMsgThumb() {
  const el = $('msglist');
  const sh = el.scrollHeight, ch = el.clientHeight;
  if (sh <= ch) { $('msgbar').style.display = 'none'; return; }
  $('msgbar').style.display = 'block';
  const th = Math.max(26, ch * ch / sh);
  const t = (el.scrollTop / (sh - ch)) * (ch - th);
  $('msgbar-thumb').style.height = th + 'px';
  $('msgbar-thumb').style.transform = 'translateY(' + t + 'px)';
}
function layoutMsgbar() {   // 位置由 CSS 固定（贴 msgshell 右缘），这里只更新滑块
  const el = $('msglist');
  if (!el || el.offsetWidth === 0) { $('msgbar').style.display = 'none'; return; }
  updateMsgThumb();
}
let thumbDrag = null;
function scrollRatioAt(e) {   // 触点位置对应的滚动比例（浏览器式：滑块中心跟手）
  const r = $('msgbar').getBoundingClientRect();
  const th = $('msgbar-thumb').offsetHeight;
  const usable = Math.max(1, r.height - th);
  return Math.max(0, Math.min(1, (e.clientY - r.top - th / 2) / usable));
}
$('msgbar').addEventListener('pointerdown', (e) => {   // 整条右缘都可抓：按下即定位并开始拖动
  const el = $('msglist');
  thumbDrag = { sh: el.scrollHeight, ch: el.clientHeight };
  try { $('msgbar').setPointerCapture(e.pointerId); } catch (err) {}
  e.preventDefault();
  flashScrollbar();
  el.scrollTop = scrollRatioAt(e) * (thumbDrag.sh - thumbDrag.ch);
});
$('msgbar').addEventListener('pointermove', (e) => {
  if (!thumbDrag) return;
  const el = $('msglist');
  el.scrollTop = scrollRatioAt(e) * (thumbDrag.sh - thumbDrag.ch);
});
$('msgbar').addEventListener('pointerup', () => { thumbDrag = null; flashScrollbar(); });
$('msgbar').addEventListener('pointercancel', () => { thumbDrag = null; flashScrollbar(); });
window.addEventListener('resize', layoutMsgbar);
window.addEventListener('orientationchange', () => setTimeout(layoutMsgbar, 300));

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
  base = 0; unseen = 0;
  firstRender = true;
  stick = true;
  $('msglist').innerHTML = '';
  $('tobottom').classList.add('hidden');
  pollMsgs(true);
}

function applyMeta(s) {
  $('meta').textContent = (s.cwd ? s.cwd : '') + (s.branch ? '  [' + s.branch + ']' : '');
  $('dot').className = s.running ? 'run' : '';
  $('status').textContent = cur === phoneSid ? '手机会话' : (s.running ? '运行中' : '空闲');
  const showBar = control;                       // 输入框常驻（不发消息即等价于监视）
  $('phonebar').classList.toggle('hidden', !control);
  curRunning = !!s.running;
  updateBtn();
  if (control) {
    const note = $('phonenote');
    if (s.running) {
      note.textContent = '🟡 电脑端在该会话：点「⏹ 终止电脑端」可强制接管';
      note.className = 'on';
    } else {
      note.textContent = '支持 /clear /skills /status /model /memory /export /help 及技能调用';
      note.className = 'on';
    }
  }
  $('sendbar').classList.toggle('hidden', !showBar);
  $('msglist').style.paddingBottom = showBar ? '80px' : '16px';
  layoutMsgbar();
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

async function pollMsgs(initial) {
  if (!cur) return;
  let url;
  let after = total;
  if (initial) {                    // 首屏只加载最新 PAGE 条，长会话不卡
    const info = sessions.find((s) => s.sessionId === cur);
    const cnt = info && info.msgCount ? info.msgCount : 0;
    after = Math.max(0, cnt - PAGE);
    url = 'messages?session=' + encodeURIComponent(cur) + '&after=' + after + '&limit=' + PAGE;
  } else {
    url = 'messages?session=' + encodeURIComponent(cur) + '&after=' + total;
  }
  const d = await api(url).catch(() => null);
  if (!d) return;
  if (d.total < total) {            // 会话被清空重建：整表重来
    msgs = []; total = 0; base = 0;
    $('msglist').innerHTML = '';
    pollMsgs(true);
    return;
  }
  if (initial) {
    msgs = d.messages.slice();
    total = d.total;
    base = after;
    if (msgs.length) {
      renderNew(msgs);
      flashScrollbar();             // 开局闪现滚动条，提示可拉
    } else {
      $('msglist').innerHTML = '<div class="empty">暂无消息</div>';
    }
  } else if (d.total !== total) {   // 有新消息：只渲染增量
    const n = d.messages.length;
    msgs = msgs.concat(d.messages);
    total = d.total;
    if (n) {
      if (!stick) unseen += n;
      renderNew(d.messages);
    }
  } else if (!$('msglist').children.length) {
    $('msglist').innerHTML = '<div class="empty">暂无消息</div>';
  }
  $('earlier').classList.toggle('hidden', base === 0);
  applyMeta(d);
}

async function loadEarlier() {      // 分批加载更早的历史消息
  if (!cur || base === 0) return;
  const earlier = Math.max(0, base - PAGE);
  const d = await api('messages?session=' + encodeURIComponent(cur)
    + '&after=' + earlier + '&limit=' + (base - earlier)).catch(() => null);
  if (!d || !d.messages.length) return;
  const el = $('msglist');
  const prevH = el.scrollHeight, prevTop = el.scrollTop;
  const els = d.messages.map(msgEl).filter(Boolean);
  const frag = document.createDocumentFragment();
  for (const e of els) frag.appendChild(e);
  el.insertBefore(frag, el.firstChild);
  msgs = d.messages.concat(msgs);
  base = earlier;
  el.scrollTop = el.scrollHeight - prevH + prevTop;   // 保持视觉位置不跳动
  $('earlier').classList.toggle('hidden', base === 0);
}
$('earlier').addEventListener('click', loadEarlier);

function updateBtn() {   // 三态：发送 / ⏹ 停止（手机任务） / ⏹ 终止电脑端（强制接管会话）
  $('btn').textContent = sending ? '⏹ 停止' : (curRunning ? '⏹ 终止电脑端' : '发送');
  $('btn').disabled = false;
  $('inp').disabled = false;
}

async function pollStatus() {
  const d = await api('status').catch(() => null);
  if (!d) return;
  control = d.control || false;
  phoneSid = d.phoneSessionId || null;
  sending = d.sending || false;
  updateBtn();
  if (d.lastError && d.lastError !== lastErrKey) {
    lastErrKey = d.lastError;
    showBanner(d.lastError, 10000);   // 含"任务已被电脑终止"等情况
  }
}

function showCmdBox(title, text) {   // 命令结果弹窗（/help /skills /status /model /memory /export）
  $('cmdbox-title').textContent = title;
  $('cmdbox-body').textContent = text;
  $('cmdbox').classList.remove('hidden');
}
$('cmdbox').addEventListener('click', (e) => {
  if (e.target === $('cmdbox')) $('cmdbox').classList.add('hidden');
});
$('cmdbox-close').addEventListener('click', () => $('cmdbox').classList.add('hidden'));

async function sendMsg() {
  if (sending) { showBanner('任务进行中：点「停止」可中止'); return; }
  const t = $('inp').value.trim();
  if (!t || !cur) return;
  try {
    const d = await api('send', {method: 'POST', body: {text: t, sessionId: cur}});
    if (d && d.cmd) {                      // 命令结果：弹窗展示，保留输入框
      showCmdBox(d.cmd, d.text);
      return;
    }
    if (d && d.note) showBanner(d.note, 8000);   // 排队 / /clear 等结果说明
    $('inp').value = '';
  } catch (e) {
    if (e.message !== 'unauthorized' && e.message !== 'csrf') showBanner(e.message);
  }
}
$('btn').addEventListener('click', () => {
  if (sending) {   // 手机强制停止：杀掉正在跑的手机任务，方便在外面改思路
    api('stop', {method: 'POST'}).catch(() => {});
    showBanner('已强制停止任务，可重新发送');
    return;
  }
  if (curRunning) {   // 手机打断电脑端：向电脑端 CC 发 Ctrl+C（等同电脑上按一次 Ctrl+C，窗口保留）
    if (!confirm('确定打断电脑端当前任务？（等同按 Ctrl+C，不关闭窗口）')) return;
    api('stop-pc', {method: 'POST', body: {sessionId: cur}}).then((d) => {
      if (d && d.stopped) { showBanner('已打断电脑端任务（窗口保留）'); return; }
      showBanner((d && d.note) || '电脑端没有在该会话运行', 12000);
    }).catch((e) => {
      if (e.message !== 'unauthorized' && e.message !== 'csrf') showBanner(e.message);
    });
    return;
  }
  sendMsg();
});
$('inp').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMsg(); }
});

/* ================= 终端视图 ================= */
let term = null, fit = null, termId = localStorage.getItem('cc_term_id') || '';
let ws = null, wsRetry = 0, wsWanted = false;
let termList = [];

function setTermStatus(s) { $('term-status').textContent = s; }

/* ---- 多终端：下拉框切换 + 关闭 ---- */
function renderTermSel() {
  const box = $('term-sel');
  if (!termList.length) { box.innerHTML = ''; return; }
  let html = '';
  for (const t of termList) {
    html += '<option value="' + esc(t.term_id) + '"' + (t.term_id === termId ? ' selected' : '') + '>'
      + esc(t.name || t.term_id.slice(0, 6))
      + (t.idle_seconds < 60 ? ' · 活跃' : ' · 空闲' + Math.floor(t.idle_seconds / 60) + '分')
      + '</option>';
  }
  box.innerHTML = html;
}
function refreshTermList() {
  api('terms').then((d) => {
    if (d && d.terms) { termList = d.terms; renderTermSel(); }
  }).catch(() => {});   // 旧服务无此接口时静默忽略
}
function switchTerm(id) {
  if (!id || id === termId) return;
  termId = id;
  localStorage.setItem('cc_term_id', id);
  if (term) term.reset();
  if (ws && ws.readyState === 1) {
    ws.send(JSON.stringify({action: 'attach', term_id: id, rows: term.rows, cols: term.cols}));
  } else {
    connectTerm();
  }
}
$('term-sel').addEventListener('change', (e) => { switchTerm(e.target.value); });
$('term-close').addEventListener('click', () => {
  if (!termId) return;
  if (ws && ws.readyState === 1) {
    ws.send(JSON.stringify({action: 'kill', term_id: termId}));
  }
  setTimeout(refreshTermList, 500);
});
async function renameTerm(id) {
  if (!id) return;
  const cur2 = termList.find((t) => t.term_id === id);
  const old = (cur2 && cur2.name) || id.slice(0, 6);
  const name = prompt('重命名终端（最长 20 字）：', old);
  if (!name || name.trim() === old) return;
  try {
    await api('terms/rename', {method: 'POST', body: {term_id: id, name: name.trim()}});
    refreshTermList();
  } catch (e) {
    if (e.message !== 'unauthorized' && e.message !== 'csrf') showBanner(e.message);
  }
}
$('term-rename').addEventListener('click', () => renameTerm(termId));

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
  $('term-focus').addEventListener('click', () => {   // 用户手势内 focus，Android 键盘可靠弹出
    try { term.focus(); term.textarea.focus(); } catch (e) {}
  });
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
      setTermStatus('已连接 · ' + (m.name || termId.slice(0, 6)));
      refreshTermList();
    } else if (m.type === 'output') {
      term.write(m.data);
    } else if (m.type === 'exit') {
      setTermStatus('有终端进程已退出');
      setTimeout(() => {            // 当前终端死了就自动切到第一个活着的，否则新建
        api('terms').then((d) => {
          const alive = (d && d.terms) ? d.terms : [];
          if (!alive.length) {
            termId = '';
            localStorage.removeItem('cc_term_id');
            if (term) term.reset();
            if (sock.readyState === 1) {
              sock.send(JSON.stringify({action: 'attach', term_id: null, rows: term.rows, cols: term.cols}));
            }
          } else if (!alive.some((t) => t.term_id === termId)) {
            switchTerm(alive[0].term_id);
          }
        }).catch(() => {});
      }, 300);
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

$('term-int').addEventListener('click', () => {   // 打断当前命令：发 Ctrl+C（手机键盘没有 Ctrl）
  if (ws && ws.readyState === 1) {
    ws.send(JSON.stringify({action: 'input', data: '\x03'}));
    setTermStatus('已发送打断信号 (Ctrl+C)');
    const b = $('term-int');
    b.style.borderColor = '#ff6b5e'; b.style.color = '#ff6b5e';
    setTimeout(() => { b.style.borderColor = ''; b.style.color = ''; }, 800);
  }
});

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
  const isMain = name !== 'term';
  $('monitor-view').classList.toggle('hidden', !isMain);
  $('term-view').classList.toggle('hidden', name !== 'term');
  const curInfo = sessions.find((s) => s.sessionId === cur);
  if (curInfo) applyMeta(curInfo);                  // 立即刷新提示条/输入框，不等轮询
  if (isMain) setTimeout(layoutMsgbar, 0);
  if (name === 'term') {
    initTerm();
    refreshTermList();
    if (!ws || ws.readyState > 1) connectTerm();
    setTimeout(onTermResize, 50);
  } else {
    // 保留当前选中的会话，切换下拉框即可监视/控制任意会话
    if (!cur && sessions.length) {
      cur = sessions[0].sessionId;
      $('sel').value = cur;
      resetView();
    }
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
  switchView('send');
  pollOverview();
  pollStatus();
  setInterval(pollOverview, 10000);
  setInterval(pollMsgs, 2000);
  setInterval(pollStatus, 2000);
  setInterval(refreshTermList, 15000);   // 多终端列表定期刷新
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
