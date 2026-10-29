const $ = (id) => document.getElementById(id);
const api = async (url, opts) => {
  const r = await fetch(url, opts);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
  return j;
};
let accounts = [];
let lastOutDir = "";
let chats = [];
let timer = null;

function show(id) { $(id).classList.remove('hidden'); }
function hide(id) { $(id).classList.add('hidden'); }

$('btnScan').onclick = async () => {
  $('scanMsg').textContent = '正在检测…';
  try {
    const info = await api('/api/scan');
    accounts = info.accounts || [];
    const env = $('envInfo');
    env.innerHTML = '';
    const line = (k, v, cls = '') => {
      const d = document.createElement('div');
      d.className = 'kv';
      d.innerHTML = `<span class="k">${k}</span><span class="${cls}">${v}</span>`;
      env.appendChild(d);
    };
    line('微信版本', info.version || '未检测到', info.version ? 'ok' : 'err');
    line('安装目录', info.install_dir || '—');
    line('运行状态', info.running ? '正在运行' : '未运行（请先登录微信再导出）', info.running ? 'ok' : 'warn');
    line('数据目录', (info.roots || []).join('；') || '未找到');
    line('账号', accounts.length ? `${accounts.length} 个` : '未找到', accounts.length ? 'ok' : 'err');
    (info.warnings || []).forEach(w => line('提示', w, 'warn'));
    show('envInfo');
    $('scanMsg').textContent = accounts.length ? '检测完成，请继续下一步' : '未找到账号，请确认微信已登录过';
    if (info.low_integrity) {
      const box = $('integrityWarn');
      box.innerHTML = '⚠ 当前程序运行在受限目录（低完整性）。这会导致：无法写入自定义输出目录、' +
        '无法读取微信进程内存取密钥、点“打开输出目录”无反应。<br>' +
        '请把整个 <b>WeChatExport</b> 文件夹复制到普通目录（例如 <b>D:\\微信导出工具</b>）后重新运行。';
      box.classList.remove('hidden');
    }

    const sel = $('accountSelect');
    sel.innerHTML = '';
    accounts.forEach(a => {
      const o = document.createElement('option');
      o.value = a.folder;
      o.textContent = `${a.wxid}  （${a.message_dbs} 个消息库）`;
      sel.appendChild(o);
    });
    if (accounts.length) {
      show('step2'); show('step3');
      refreshChatsHint();
    }
  } catch (e) {
    $('scanMsg').textContent = '检测失败：' + e.message;
    $('scanMsg').className = 'hint err';
  }
};

function refreshChatsHint() {
  $('chatHint').textContent = chats.length
    ? `已加载 ${chats.length} 个会话，可勾选需要导出的会话`
    : '默认导出全部会话';
}

function applyChatFilter() {
  const q = $('chatSearch').value.trim().toLowerCase();
  const items = Array.from($('chatList').querySelectorAll('.chat-item'));
  let shown = 0;
  items.forEach(it => {
    const hit = !q || (it.dataset.name || '').includes(q);
    it.classList.toggle('hidden', !hit);
    if (hit) shown++;
  });
  $('searchHint').textContent = q ? `匹配 ${shown} / ${items.length} 个会话` : '';
}

$('chatSearch').addEventListener('input', applyChatFilter);

$('btnChats').onclick = async () => {
  const acc = $('accountSelect').value;
  $('chatHint').textContent = '正在读取会话（首次需取密钥并解密，通常 1-3 分钟）…';
  $('btnChats').disabled = true;
  const timer = setInterval(async () => {
    try {
      const p = await api('/api/progress');
      if (p.running) {
        $('chatHint').textContent =
          `正在读取会话：${p.message || p.stage || ''}（${p.percent || 0}%）`;
      }
    } catch (e) { /* 服务繁忙时忽略 */ }
  }, 900);
  try {
    const r = await api('/api/chats', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ account: acc, key: manualKey.value.trim() }) });
    chats = r.chats || [];
    const list = $('chatList');
    list.innerHTML = '';
    chats.forEach(c => {
      const d = document.createElement('label');
      d.className = 'chat-item';
      d.dataset.name = [c.display, c.username, c.alias, c.nick].filter(Boolean).join(' ').toLowerCase();
      const when = c.last_time ? new Date(c.last_time * 1000).toLocaleString() : '';
      d.innerHTML = `<input type="checkbox" value="${c.username}" ${c.has_messages ? '' : 'disabled'}>
        <span class="name">${c.display}</span><span class="meta">${c.is_group ? '群聊 · ' : ''}${when}</span>`;
      list.appendChild(d);
    });
    show('chatList');
    refreshChatsHint();
    applyChatFilter();
  } catch (e) {
    $('chatHint').textContent = '读取会话失败：' + e.message;
  } finally {
    clearInterval(timer);
    $('btnChats').disabled = false;
  }
};

$('chkAll').onchange = () => {
  const filtering = $('chatSearch').value.trim().length > 0;
  $('chatList').querySelectorAll('.chat-item').forEach(it => {
    const box = it.querySelector('input[type=checkbox]');
    if (!box || box.disabled) return;
    if (filtering && it.classList.contains('hidden')) return;
    box.checked = $('chkAll').checked;
  });
  const n = $('chatList').querySelectorAll('input[type=checkbox]:checked').length;
  $('chatHint').textContent = n ? `已选择 ${n} 个会话` : '未选择会话，可勾选“全部会话”';
};

// 手动勾选任意会话时，自动取消“全部会话”，避免“只选一个却导出全部”
$('chatList').addEventListener('change', (ev) => {
  if (ev.target && ev.target.type === 'checkbox') {
    $('chkAll').checked = false;
    const n = $('chatList').querySelectorAll('input[type=checkbox]:checked').length;
    $('chatHint').textContent = n ? `已选择 ${n} 个会话` : '未选择会话，可勾选“全部会话”';
  }
});

// ---- 自定义导出时间段 ----
let range = { from: '', to: '' };

function dayStart(s) {
  const [y, m, d] = s.split('-').map(Number);
  return Math.floor(new Date(y, m - 1, d, 0, 0, 0).getTime() / 1000);
}
function dayEnd(s) {
  const [y, m, d] = s.split('-').map(Number);
  return Math.floor(new Date(y, m - 1, d, 23, 59, 59).getTime() / 1000);
}
function updateRangeUI() {
  const has = range.from || range.to;
  $('rangeLabel').textContent = has ? `${range.from} 至 ${range.to}` : '全部时间';
  $('btnRangeClear').classList.toggle('hidden', !has);
}
$('btnRange').onclick = () => {
  $('dateFrom').value = range.from;
  $('dateTo').value = range.to;
  show('rangeModal');
};
$('btnRangeCancel').onclick = () => hide('rangeModal');
$('btnRangeOk').onclick = () => {
  const f = $('dateFrom').value;
  const t = $('dateTo').value;
  if (!f || !t) { alert('请选择起始日期和终止日期'); return; }
  if (f > t) { alert('起始日期不能晚于终止日期'); return; }
  range = { from: f, to: t };
  updateRangeUI();
  hide('rangeModal');
};
$('btnRangeClear').onclick = () => { range = { from: '', to: '' }; updateRangeUI(); };
$('rangeModal').addEventListener('click', (e) => { if (e.target === $('rangeModal')) hide('rangeModal'); });
updateRangeUI();

$('btnStart').onclick = async () => {
  const chosen = Array.from($('chatList').querySelectorAll('input[type=checkbox]:checked'))
    .filter(c => !c.disabled).map(c => c.value);
  const all = $('chkAll').checked;
  if (!all && chosen.length === 0) {
    alert('请先勾选要导出的会话，或勾选“全部会话”');
    return;
  }
  const formats = [];
  if ($('fmtHtml').checked) formats.push('html');
  if ($('fmtCsv').checked) formats.push('csv');
  if ($('fmtJson').checked) formats.push('json');
  if ($('fmtTxt').checked) formats.push('txt');
  if ($('fmtXlsx').checked) formats.push('xlsx');
  if (!formats.length) { alert('请至少选择一种导出格式'); return; }
  const body = {
    account: $('accountSelect').value,
    chats: all ? [] : chosen,
    formats,
    out_dir: $('outDir').value.trim(),
    export_media: $('chkMedia').checked,
    include_system: $('chkSystem').checked,
    since: range.from ? dayStart(range.from) : 0,
    until: range.to ? dayEnd(range.to) : 0,
    key: manualKey.value.trim(),
  };
  $('btnStart').disabled = true;
  lastOutDir = '';
  show('progressWrap');
  $('log').textContent = '';
  try {
    await api('/api/start', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body) });
    if (timer) clearInterval(timer);
    timer = setInterval(poll, 800);
  } catch (e) {
    $('stage').textContent = '启动失败：' + e.message;
    $('stage').className = 'hint err';
    $('btnStart').disabled = false;
  }
};

async function poll() {
  try {
    const p = await api('/api/progress');
    $('bar').style.width = (p.percent || 0) + '%';
    $('stage').textContent = p.error ? '失败：' + p.error
      : `${p.message || p.stage || ''}（${p.percent || 0}%）`;
    $('stage').className = 'hint' + (p.error ? ' err' : '');
    $('log').textContent = (p.log || []).slice(-40).join('\n');
    $('log').scrollTop = $('log').scrollHeight;
    if (p.done) {
      clearInterval(timer); timer = null;
      $('btnStart').disabled = false;
      if (!p.error && p.result && p.result.out_dir) {
        lastOutDir = p.result.out_dir;
        $('outDir').value = p.result.out_dir.replace(/[\\\\/][^\\\\/]+$/, '');
        $('stage').textContent = `完成：${p.result.chats.length} 个会话，输出到 ${p.result.out_dir}`;
      }
    }
  } catch (e) { /* server gone */ }
}

$('btnPick').onclick = async () => {
  const btn = $('btnPick');
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = '请在弹出的窗口中选择…';
  try {
    const r = await api('/api/pick-folder', { method: 'POST', body: '{}' });
    if (r.ok && r.path) $('outDir').value = r.path;
  } catch (e) {
    alert('打开目录选择窗口失败：' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
};

$('btnOpen').onclick = async () => {
  const path = lastOutDir || $('outDir').value.trim();
  if (!path) { alert('还没有导出目录，请先导出一次'); return; }
  try {
    await api('/api/open-folder', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path }) });
  } catch (e) {
    alert('无法打开目录：' + e.message + '\n\n路径：' + path);
  }
};

$('btnQuit').onclick = async () => {
  if (!confirm('退出会关闭本地服务窗口，确定吗？')) return;
  try { await api('/api/quit', { method: 'POST', body: '{}' }); } catch (e) {}
  window.close();
};

refreshChatsHint();
