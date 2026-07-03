// ========== 工具 ==========
const $ = (s, p = document) => p.querySelector(s);
const $$ = (s, p = document) => Array.from(p.querySelectorAll(s));

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  return res.json();
}

function toast(msg, type = 'success') {
  const el = $('#toast');
  el.textContent = msg;
  el.className = `toast show ${type}`;
  setTimeout(() => el.className = 'toast', 2400);
}

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function fmtPrice(p) {
  if (p == null) return '—';
  return Number(p).toFixed(2);
}

function fmtChange(c) {
  if (c == null) return '—';
  const sign = c > 0 ? '+' : '';
  return `${sign}${Number(c).toFixed(2)}%`;
}

function priceCellClass(change) {
  if (change == null) return '';
  if (change > 0) return 'quote-up';
  if (change < 0) return 'quote-down';
  return 'quote-flat';
}

// ========== Tabs ==========
$$('.tab').forEach(btn => btn.addEventListener('click', () => {
  $$('.tab').forEach(b => b.classList.remove('active'));
  $$('.tab-content').forEach(c => c.classList.remove('active'));
  btn.classList.add('active');
  $('#tab-' + btn.dataset.tab).classList.add('active');
  if (btn.dataset.tab === 'status') loadStatus();
}));

// ========== 股票 ==========
let stocksCache = [];
let quotesTimer = null;
let currentTEvent = null;

async function loadStocks() {
  stocksCache = await api('/api/stocks');
  renderStocks();
}

function renderTList(container, events) {
  if (!events || events.length === 0) {
    container.innerHTML = '<span class="muted">—</span>';
    return;
  }
  container.innerHTML = events.map(e => {
    const isS = e.type === 'S';
    const label = isS ? 'S↓' : 'B↑';
    const priceStr = fmtPrice(e.price);
    const targetStr = e.target_price != null ? `→ ${fmtPrice(e.target_price)}` : '';
    const triggerIcon = e.triggered
      ? `<span class="t-trigger triggered" data-treset="${escapeHtml(e.id)}" data-tcode="${escapeHtml(container.dataset.tlist)}"></span>`
      : `<span class="t-trigger pending"></span>`;
    return `<span class="t-event-tag${isS ? ' type-s' : ' type-b'}" title="${escapeHtml(new Date(e.created_at * 1000).toLocaleString())} @ ${e.price}${e.target_price != null ? ' → ' + e.target_price : ''}">
      ${triggerIcon}
      <span class="t-event-edit" data-tedit="${escapeHtml(e.id)}" data-tcode="${escapeHtml(container.dataset.tlist)}">${label} ${priceStr} ${targetStr}</span>
      <button class="btn-t-del" data-tdel="${escapeHtml(e.id)}" data-tcode="${escapeHtml(container.dataset.tlist)}">×</button>
    </span>`;
  }).join(' ');
}

function renderStocks() {
  const tbody = $('#stocks-table tbody');
  tbody.innerHTML = '';
  for (const s of stocksCache) {
    const q = s.quote || {};
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td><code>${escapeHtml(s.code)}</code></td>
      <td>${escapeHtml(s.name)}</td>
      <td>${s.nickname ? escapeHtml(s.nickname) : '<span class="muted">—</span>'}</td>
      <td class="${priceCellClass(q.change_percent)}">
        <div class="quote-price">${fmtPrice(q.price)}</div>
        <div class="quote-change">${fmtChange(q.change_percent)}</div>
      </td>
      <td class="${q.surge_change != null ? priceCellClass(q.surge_change) : ''}" title="${q.surge_change != null ? `基准价: ${fmtPrice(q.surge_base_price)} @ ${new Date(q.surge_base_time * 1000).toLocaleString()}` : ''}">
        ${q.surge_change != null ? fmtChange(q.surge_change) : s.speed_threshold != null ? s.speed_threshold + '%' : '—'}
      </td>
      <td>${q.as_of ? new Date(q.as_of * 1000).toLocaleString() : '—'}</td>
      <td><label class="switch"><input type="checkbox" ${s.enabled ? 'checked' : ''} data-code="${escapeHtml(s.code)}" class="toggle"><span class="slider"></span></label></td>
      <td>
        <button class="btn" data-edit="${escapeHtml(s.code)}">编辑</button>
        <button class="btn btn-danger" data-del="${escapeHtml(s.code)}">删除</button>
      </td>
      <td class="t-events-cell" data-tcode="${escapeHtml(s.code)}">
        <div class="t-btns">
          <button class="btn btn-sm btn-t-s ${s.t_s_enabled === false ? 'btn-t-disabled' : ''}" data-tadd="${escapeHtml(s.code)}" data-ttype="S" ${s.t_s_enabled === false ? 'disabled' : ''}>S</button>
          <button class="btn btn-sm btn-t-b ${s.t_b_enabled === false ? 'btn-t-disabled' : ''}" data-tadd="${escapeHtml(s.code)}" data-ttype="B" ${s.t_b_enabled === false ? 'disabled' : ''}>B</button>
        </div>
        <div class="t-list" data-tlist="${escapeHtml(s.code)}"></div>
      </td>`;
    tbody.appendChild(tr);
    const tlist = tr.querySelector(`[data-tlist="${CSS.escape(s.code)}"]`);
    if (tlist) renderTList(tlist, s.t_events || []);
  }
  $('#stocks-empty').hidden = stocksCache.length > 0;
  $('#stocks-table').hidden = stocksCache.length === 0;
}

async function updateLatency() {
  const info = $('.latency-info');
  const dot = $('#latency-dot');
  const label = $('#latency-label');
  if (!dot || !label || !info) return;
  try {
    const s = await api('/api/status');
    const lat = s.price_latency;
    if (lat == null) {
      info.className = 'latency-info latency-gray';
      dot.className = 'latency-dot latency-gray';
      label.textContent = '—';
      return;
    }
    const cls = lat < 5 ? 'green' : lat < 30 ? 'yellow' : 'red';
    info.className = `latency-info latency-${cls}`;
    dot.className = `latency-dot latency-${cls}`;
    label.textContent = lat < 1 ? `${(lat * 1000).toFixed(0)}ms` : `${lat.toFixed(1)}s`;
  } catch {
    info.className = 'latency-info latency-gray';
    dot.className = 'latency-dot latency-gray';
    label.textContent = '?';
  }
}

// 报价自动刷新（每 30s）
function startQuoteRefresh() {
  if (quotesTimer) return;
  updateLatency();
  quotesTimer = setInterval(async () => {
    try {
      stocksCache = await api('/api/stocks');
      renderStocks();
    } catch {}
    updateLatency();
  }, 30_000);
}

$('#stocks-table').addEventListener('click', async (e) => {
  // T 事件 S/B 按钮
  const tBtn = e.target.closest('[data-tadd]');
  if (tBtn) {
    const code = tBtn.dataset.tadd;
    const type = tBtn.dataset.ttype;
    const s = stocksCache.find(x => x.code === code);
    const defaultPrice = s?.quote?.price;
    currentTEvent = { action: 'add', code, type, name: s?.name };
    $('#t-event-dialog-title').textContent = `新增${type === 'S' ? '先卖后买(S)' : '先买后卖(B)'} (${s?.name})`;
    $('#t-event-price').value = defaultPrice ? fmtPrice(defaultPrice) : '';
    $('#t-event-target-price').value = '';
    $('#t-event-dialog').showModal();
    return;
  }
  // T 事件删除
  const tDel = e.target.closest('[data-tdel]');
  if (tDel) {
    const eventId = tDel.dataset.tdel;
    const code = tDel.dataset.tcode;
    if (!confirm('确认删除此做T事件?')) return;
    try {
      await api(`/api/stocks/${code}/t-events/${eventId}`, { method: 'DELETE' });
      toast('已删除'); loadStocks();
    } catch (e) { toast('删除失败: ' + e.message, 'error'); }
    return;
  }
  // T 事件重置
  const tReset = e.target.closest('.t-trigger.triggered');
  if (tReset) {
    const eventId = tReset.dataset.treset;
    const code = tReset.dataset.tcode;
    try {
      await api(`/api/stocks/${code}/t-events/${eventId}/reset`, { method: 'POST' });
      toast('T 事件已重置，今日可再次触发');
      loadStocks();
    } catch (err) { toast('重置失败: ' + err.message, 'error'); }
    return;
  }
  // T 事件编辑
  const tEdit = e.target.closest('[data-tedit]');
  if (tEdit) {
    const eventId = tEdit.dataset.tedit;
    const code = tEdit.dataset.tcode;
    const s = stocksCache.find(x => x.code === code);
    const ev = (s?.t_events || []).find(x => x.id === eventId);
    if (!ev) return;
    currentTEvent = { action: 'edit', code, type: ev.type, eventId, name: s?.name };
    $('#t-event-dialog-title').textContent = `编辑${ev.type === 'S' ? '先卖后买(S)' : '先买后卖(B)'} (${s?.name})`;
    $('#t-event-price').value = fmtPrice(ev.price);
    $('#t-event-target-price').value = ev.target_price != null ? fmtPrice(ev.target_price) : '';
    $('#t-event-dialog').showModal();
    return;
  }
  // 编辑 / 删除股票
  const code = e.target.dataset.edit;
  if (code) openDialog(stocksCache.find(s => s.code === code));
  const delCode = e.target.dataset.del;
  if (delCode) {
    if (confirm(`确认删除 ${delCode}?`)) {
      try { await api(`/api/stocks/${delCode}`, { method: 'DELETE' }); toast('已删除'); loadStocks(); }
      catch (e) { toast('删除失败: ' + e.message, 'error'); }
    }
  }
});

$('#stocks-table').addEventListener('change', async (e) => {
  if (e.target.classList.contains('toggle')) {
    const code = e.target.dataset.code;
    const enabled = e.target.checked;
    try {
      await api(`/api/stocks/${code}/enabled`, { method: 'PATCH', body: JSON.stringify({ enabled }) });
      toast(enabled ? '已启用' : '已停用');
    } catch (e) { toast('操作失败: ' + e.message, 'error'); loadStocks(); }
  }
});

const dialog = $('#stock-dialog');
$('#btn-add-stock').addEventListener('click', () => openDialog(null));
$('#btn-cancel').addEventListener('click', () => { resetSearch(); dialog.close(); });

// ========== T 事件对话框 ==========
$('#t-event-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  if (!currentTEvent) return;
  const price = parseFloat($('#t-event-price').value);
  if (isNaN(price) || price <= 0) { toast('无效价格', 'error'); return; }
  const targetInput = $('#t-event-target-price').value.trim();
  let targetPrice = null;
  if (targetInput) {
    const tp = parseFloat(targetInput);
    if (!isNaN(tp) && tp > 0) targetPrice = tp;
  }
  try {
    if (currentTEvent.action === 'add') {
      await api(`/api/stocks/${currentTEvent.code}/t-events`, {
        method: 'POST',
        body: JSON.stringify({ type: currentTEvent.type, price, target_price: targetPrice })
      });
      toast(`已添加 ${currentTEvent.type} @ ${fmtPrice(price)}`);
    } else {
      await api(`/api/stocks/${currentTEvent.code}/t-events/${currentTEvent.eventId}`, {
        method: 'PUT',
        body: JSON.stringify({ type: currentTEvent.type, price, target_price: targetPrice })
      });
      toast('已更新');
    }
    loadStocks();
  } catch (e) { toast('失败: ' + e.message, 'error'); }
  $('#t-event-dialog').close();
  currentTEvent = null;
});

$('#t-event-cancel').addEventListener('click', () => {
  $('#t-event-dialog').close();
  currentTEvent = null;
});

function openDialog(stock) {
  const form = $('#stock-form');
  form.reset();
  $('#stock-search-input').value = '';
  $('#stock-code-display').value = '';
  $('#stock-code-hidden').value = '';
  $('#stock-search-dropdown').classList.remove('active');
  $('#stock-search-dropdown').innerHTML = '';
  $('#stock-dialog-title').textContent = stock ? '编辑股票' : '新增股票';
  if (stock) {
    for (const [k, v] of Object.entries(stock)) {
      if (k === 'quote') continue;
      if (form.elements[k]) {
        if (k === 'daily_change_up' || k === 'daily_change_down') {
          form.elements[k].value = (v || []).join(', ');
        } else {
          if (k === 'code') {
            form.elements[k].value = v ?? '';
            $('#stock-code-display').value = v ?? '';
            $('#stock-search-input').value = (stock.name || '') + ' (' + (v ?? '') + ')';
          } else {
            form.elements[k].value = v ?? '';
          }
        }
      }
    }
    $('#stock-search-input').readOnly = true;
    const disabledSet = new Set(stock.disabled_alerts || []);
    $$('.alert-type-toggle', form).forEach(cb => {
      cb.checked = !disabledSet.has(cb.dataset.type);
    });
    updateInputStates();
  } else {
    $('#stock-search-input').readOnly = false;
  }
  dialog.showModal();
}

function updateInputStates() {
  const form = $('#stock-form');
  $$('.alert-type-toggle', form).forEach(cb => {
    const inputName = cb.dataset.input;
    if (!inputName) return;
    // Collect all toggles for this input
    const group = $$(`.alert-type-toggle[data-input="${inputName}"]`, form);
    const allDisabled = group.length > 0 && group.every(c => !c.checked);
    const input = form.elements[inputName];
    if (input) input.disabled = allDisabled;
  });
}

// Bind toggle changes to update input states
document.addEventListener('change', (e) => {
  if (e.target.matches('#stock-form .alert-type-toggle')) updateInputStates();
});

$('#stock-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const form = e.target;
  const code = $('#stock-code-hidden').value.trim();
  if (!code) { toast('请先搜索并选择股票', 'error'); return; }
  const stock = {
    code,
    name: form.elements.name.value.trim(),
    nickname: form.elements.nickname.value.trim(),
    price_high: numOrNull(form.elements.price_high.value),
    price_low: numOrNull(form.elements.price_low.value),
    speed_threshold: numOrNull(form.elements.speed_threshold.value),
    speed_window: Number(form.elements.speed_window.value),
    cooldown_minutes: Number(form.elements.cooldown_minutes.value),
    enabled: true,
    daily_change_up: form.elements.daily_change_up.value.split(',').map(s => Number(s.trim())).filter(n => !isNaN(n)),
    daily_change_down: form.elements.daily_change_down.value.split(',').map(s => Number(s.trim())).filter(n => !isNaN(n)),
    retracement_threshold: numOrNull(form.elements.retracement_threshold.value),
    bounce_threshold: numOrNull(form.elements.bounce_threshold.value),
    t_threshold: numOrNull(form.elements.t_threshold.value),
    t_s_enabled: form.elements.t_s_enabled.checked,
    t_b_enabled: form.elements.t_b_enabled.checked,
    disabled_alerts: $$('.alert-type-toggle', form).filter(cb => !cb.checked).map(cb => cb.dataset.type),
  };
  const isEdit = stocksCache.some(s => s.code === code);
  try {
    if (isEdit) {
      await api(`/api/stocks/${code}`, { method: 'PUT', body: JSON.stringify(stock) });
    } else {
      await api('/api/stocks', { method: 'POST', body: JSON.stringify(stock) });
    }
    toast(isEdit ? '已更新' : '已新增');
    dialog.close();
    loadStocks();
  } catch (e) { toast('保存失败: ' + e.message, 'error'); }
});

function numOrNull(v) { return v === '' ? null : Number(v); }

function resetSearch() {
  $('#stock-search-input').value = '';
  $('#stock-code-display').value = '';
  $('#stock-code-hidden').value = '';
  $('#stock-search-dropdown').classList.remove('active');
  $('#stock-search-dropdown').innerHTML = '';
  $('#stock-search-input').readOnly = false;
}

// 关闭对话框时重置搜索
document.querySelectorAll('.tab-content').forEach(() => {});
dialog.addEventListener('close', resetSearch);

// 股票搜索自动补全
let searchTimer = null;
$('#stock-search-input').addEventListener('input', () => {
  clearTimeout(searchTimer);
  const q = $('#stock-search-input').value.trim();
  if (!q) { $('#stock-search-dropdown').classList.remove('active'); return; }
  searchTimer = setTimeout(async () => {
    try {
      const items = await api(`/api/stocks/search?q=${encodeURIComponent(q)}`);
      const dd = $('#stock-search-dropdown');
      if (!items || items.length === 0) {
        dd.innerHTML = '<div class="stock-search-empty">无匹配结果</div>';
        dd.classList.add('active');
        return;
      }
      dd.innerHTML = items.map(it => `
        <div class="stock-search-item" data-code="${escapeHtml(it.code)}" data-name="${escapeHtml(it.name)}">
          <span class="sname">${escapeHtml(it.name)}</span>
          <span class="scode">${escapeHtml(it.code)}</span>
        </div>`).join('');
      dd.classList.add('active');
    } catch (e) { /* ignore */ }
  }, 300);
});

$('#stock-search-dropdown').addEventListener('click', (e) => {
  const item = e.target.closest('.stock-search-item');
  if (!item) return;
  const code = item.dataset.code;
  const name = item.dataset.name;
  $('#stock-code-hidden').value = code;
  $('#stock-code-display').value = code;
  $('#stock-name-input').value = name;
  $('#stock-search-input').value = name + ' (' + code + ')';
  $('#stock-search-dropdown').classList.remove('active');
});

// 点击其他地方关闭下拉
document.addEventListener('click', (e) => {
  if (!e.target.closest('.stock-search-wrap')) {
    $('#stock-search-dropdown').classList.remove('active');
  }
});

// 导入 / 导出
$('#btn-export').addEventListener('click', async () => {
  const data = await api('/api/export');
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `stock-monitor-config-${new Date().toISOString().slice(0, 10)}.json`;
  a.click();
});

$('#file-import').addEventListener('change', async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  if (!confirm('导入将覆盖当前所有配置，确认继续？')) { e.target.value = ''; return; }
  const fd = new FormData();
  fd.append('file', file);
  try {
    const res = await fetch('/api/import', { method: 'POST', body: fd });
    if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
    toast('导入成功');
    loadStocks();
  } catch (e) { toast('导入失败: ' + e.message, 'error'); }
  e.target.value = '';
});

// ========== 模板 ==========
async function loadTemplates() {
  const t = await api('/api/templates');
  const form = $('#templates-form');
  for (const k of Object.keys(t)) {
    if (form.elements[k]) form.elements[k].value = t[k].join('\n');
  }
}

$('#templates-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const form = e.target;
  const templates = {};
  for (const key of ['price_high', 'price_low', 'daily_up', 'daily_down', 'surge_up', 'surge_down', 'retracement', 'bounce', 't_sell', 't_buy']) {
    templates[key] = form.elements[key].value.split('\n').filter(Boolean);
  }
  try {
    await api('/api/templates', { method: 'PUT', body: JSON.stringify({ templates }) });
    toast('模板已保存');
  } catch (e) { toast('保存失败: ' + e.message, 'error'); }
});

// 模板预览
$$('.btn-preview').forEach(btn => btn.addEventListener('click', async () => {
  const type = btn.dataset.type;
  const form = $('#templates-form');
  // 多行时预览第一行
  const text = (form.elements[type].value || '').split('\n').filter(Boolean)[0] || '';
  if (!text) { toast('请先输入模板内容', 'error'); return; }
  const box = $(`#preview-${type}`);
  try {
    const r = await api('/api/templates/preview', {
      method: 'POST',
      body: JSON.stringify({ template: text, alert_type: type }),
    });
    const stockLabel = r.stock_code ? `（基于 ${r.stock_name}）` : '（示例数据）';
    box.innerHTML = `
      <div class="preview-rendered">${escapeHtml(r.rendered)}</div>
      <div class="preview-meta">${escapeHtml(stockLabel)}</div>
      <details class="preview-vars">
        <summary>占位符值</summary>
        <pre>${escapeHtml(JSON.stringify(r.sample, null, 2))}</pre>
      </details>`;
    box.hidden = false;
  } catch (e) { toast('预览失败: ' + e.message, 'error'); }
}));

// ========== Webhook ==========
async function loadWebhook() {
  const r = await api('/api/settings/webhook');
  const form = $('#webhook-form');
  form.elements.webhook.placeholder = r.set ? '已设置 (输入新 URL 覆盖)' : 'https://oapi.dingtalk.com/robot/send?access_token=...';
  form.elements.at_mobiles.value = (r.at_mobiles || []).join(', ');
  form.elements.at_user_ids.value = (r.at_user_ids || []).join(', ');
}

function parseAtList(s) {
  return s.split(',').map(x => x.trim()).filter(x => x.length > 0);
}

$('#webhook-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const form = e.target;
  const v = form.elements.webhook.value;
  try {
    await api('/api/settings/webhook', {
      method: 'PUT',
      body: JSON.stringify({
        webhook: v,
        at_mobiles: parseAtList(form.elements.at_mobiles.value),
        at_user_ids: parseAtList(form.elements.at_user_ids.value),
      }),
    });
    toast('已保存');
    form.reset();
    loadWebhook();
  } catch (err) { toast('保存失败: ' + err.message, 'error'); }
});

// 自定义测试消息
$('#btn-test-notify').addEventListener('click', async () => {
  const message = $('#test-notify-form').elements.message.value;
  try {
    await api('/api/actions/test-notify', { method: 'POST', body: JSON.stringify({ message }) });
    toast('测试消息已发送');
  } catch (err) { toast('发送失败: ' + err.message, 'error'); }
});

// ========== 状态 ==========
async function loadStatus() {
  const s = await api('/api/status');
  const grid = $('#status-grid');
  const cards = [
    ['运行中', s.running ? '✅ 是' : '❌ 否'],
    ['检查次数', s.check_count],
    ['告警次数', s.alert_count],
    ['监控股票数', s.stocks.length],
    ['最后检查', s.last_check_at ? new Date(s.last_check_at * 1000).toLocaleString() : '—'],
    ['最后告警', s.last_alert_at ? new Date(s.last_alert_at * 1000).toLocaleString() : '—'],
    ['启动时间', s.started_at ? new Date(s.started_at * 1000).toLocaleString() : '—'],
    ['轮询间隔', `${s.poll_interval_seconds}s`],
    ['配置文件', s.config_path],
  ];
  grid.innerHTML = cards.map(([label, value]) => `
    <div class="status-card">
      <div class="label">${escapeHtml(label)}</div>
      <div class="value">${escapeHtml(String(value))}</div>
    </div>`).join('');
  if (s.last_error) {
    const err = document.createElement('div');
    err.className = 'status-card';
    err.style.gridColumn = '1/-1';
    err.innerHTML = `<div class="label">最后错误</div><div class="value error">${escapeHtml(s.last_error)}</div>`;
    grid.appendChild(err);
  }
  // 轮询间隔可点击编辑
  const pollCards = grid.querySelectorAll('.status-card');
  for (const card of pollCards) {
    const labelEl = card.querySelector('.label');
    if (labelEl && labelEl.textContent === '轮询间隔') {
      card.classList.add('status-card-editable');
      card.addEventListener('click', async function onClick() {
        if (card.querySelector('input')) return; // 已经在编辑
        const current = s.poll_interval_seconds;
        const input = document.createElement('input');
        input.type = 'number';
        input.min = 5;
        input.value = current;
        input.style.width = '80px';
        const valDiv = card.querySelector('.value');
        valDiv.innerHTML = '';
        valDiv.appendChild(input);
        input.focus();
        input.select();
        const save = async () => {
          const v = parseInt(input.value, 10);
          if (isNaN(v) || v < 5) { loadStatus(); return; }
          try {
            await api('/api/settings/poll-interval', { method: 'PUT', body: JSON.stringify({ seconds: v }) });
            toast(`轮询间隔已改为 ${v}s`);
            loadStatus();
          } catch (e) { toast('保存失败: ' + e.message, 'error'); loadStatus(); }
        };
        input.addEventListener('blur', save);
        input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { input.blur(); } if (e.key === 'Escape') { loadStatus(); } });
      });
      break;
    }
  }
}

let calYear, calMonth;

function renderCalendar(year, month) {
  calYear = year; calMonth = month;
  const body = $('#calendar-body');
  const label = $('#cal-month-label');
  label.textContent = `${year} 年 ${month} 月`;
  api(`/api/trading-calendar?year=${year}&month=${month}`).then(data => {
    if (!data || !data.days) return;
    const firstDay = new Date(year, month - 1, 1).getDay(); // 0=Sun
    const now = new Date();
    let html = '<table class="cal-table"><thead><tr><th>日</th><th>一</th><th>二</th><th>三</th><th>四</th><th>五</th><th>六</th></tr></thead><tbody><tr>';
    for (let i = 0; i < firstDay; i++) html += '<td></td>';
    for (const d of data.days) {
      const isToday = year === now.getFullYear() && month === now.getMonth() + 1 && d.day === now.getDate();
      let cls = 'cal-day';
      if (d.is_trading) cls += ' cal-trading';
      else cls += ' cal-non-trading';
      if (isToday) cls += ' cal-today';
      if (d.is_weekend) cls += ' cal-weekend';
      if (d.is_holiday) cls += ' cal-holiday';
      html += `<td class="${cls}">${d.day}</td>`;
      if ((firstDay + d.day) % 7 === 0) html += '</tr><tr>';
    }
    html += '</tr></tbody></table>';
    body.innerHTML = html;
  }).catch(() => {});
}

$('#cal-prev').addEventListener('click', (e) => {
  e.stopPropagation();
  let m = calMonth - 1, y = calYear;
  if (m < 1) { m = 12; y--; }
  renderCalendar(y, m);
});

$('#cal-next').addEventListener('click', (e) => {
  e.stopPropagation();
  let m = calMonth + 1, y = calYear;
  if (m > 12) { m = 1; y++; }
  renderCalendar(y, m);
});

$('#calendar-section').addEventListener('toggle', () => {
  if ($('#calendar-section').open && !calYear) {
    const now = new Date();
    renderCalendar(now.getFullYear(), now.getMonth() + 1);
  }
});

$('#btn-sync-holidays').addEventListener('click', async () => {
  try {
    const r = await api('/api/actions/sync-holidays', { method: 'POST' });
    toast(`已同步 ${r.count} 条节假日`);
  } catch (e) { toast('同步失败: ' + e.message, 'error'); }
});

// ========== 初始化 ==========
loadStocks();
loadTemplates();
loadWebhook();
startQuoteRefresh();
