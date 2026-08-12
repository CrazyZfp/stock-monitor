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

function fmtPriceP(p, precision) {
  if (p == null) return '—';
  return Number(p).toFixed(precision != null ? precision : 2);
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

function limitCellClass(status) {
  if (status === 'limit_up') return 'limit-up';
  if (status === 'limit_down') return 'limit-down';
  return '';
}

function fmtLots(n) {
  if (n == null) return '—';
  return Number(n).toLocaleString();
}

function fmtAmountWan(v) {
  if (v == null || v === 0) return '0.00';
  const w = Number(v);
  if (w >= 10000) return `${(w / 10000).toFixed(2)}亿`;
  return w.toFixed(2);
}

function profitLossHtml(cost, price, direction, leverage) {
  if (cost == null || cost <= 0 || price == null) return '<span class="muted">—</span>';
  const sign = direction === 'short' ? -1 : 1;
  const lev = leverage || 1;
  const pct = (price - cost) / cost * 100 * sign * lev;
  const cls = pct > 0 ? 'profit-up' : pct < 0 ? 'profit-down' : 'profit-flat';
  const s = pct > 0 ? '+' : '';
  return `<div class="${cls}">${s}${pct.toFixed(2)}%</div>`;
}

function renderLimit(q) {
  const s = q.limit_status;
  if (s == null || s === 'unknown') return '<span class="muted">—</span>';
  if (s === 'normal') return '<span class="muted">正常</span>';
  const label = s === 'limit_up' ? '涨停' : '跌停';
  const lots = q.sealed_lots || 0;
  const amt = q.sealed_amount || 0;
  return `<span class="limit-tag">${label}</span><span class="limit-seal">${fmtLots(lots)}手 / ${fmtAmountWan(amt)}万</span>`;
}

function limitTitle(q) {
  const s = q.limit_status;
  if (s === 'limit_up' || s === 'limit_down') {
    return `封板价 ${fmtPrice(q.limit_price)}　封单 ${fmtLots(q.sealed_lots)}手`;
  }
  return '';
}

// ========== Tabs ==========
$$('.tab').forEach(btn => btn.addEventListener('click', () => {
  $$('.tab').forEach(b => b.classList.remove('active'));
  $$('.tab-content').forEach(c => c.classList.remove('active'));
  btn.classList.add('active');
  $('#tab-' + btn.dataset.tab).classList.add('active');
  if (btn.dataset.tab === 'status') loadStatus();
  if (btn.dataset.tab === 'funds') loadFunds();
  if (btn.dataset.tab === 'cryptos') loadCryptos();
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
    const qtyStr = e.quantity != null ? ` ${e.quantity}手` : '';
    const triggerIcon = e.triggered
      ? `<span class="t-trigger triggered" data-treset="${escapeHtml(e.id)}" data-tcode="${escapeHtml(container.dataset.tlist)}"></span>`
      : `<span class="t-trigger pending"></span>`;
    return `<span class="t-event-tag${isS ? ' type-s' : ' type-b'}" title="${escapeHtml(new Date(e.created_at * 1000).toLocaleString())} @ ${e.price}${e.target_price != null ? ' → ' + e.target_price : ''}${e.quantity != null ? ' ' + e.quantity + '手' : ''}">
      ${triggerIcon}
      <span class="t-event-edit" data-tedit="${escapeHtml(e.id)}" data-tcode="${escapeHtml(container.dataset.tlist)}">${label} ${priceStr} ${targetStr}${qtyStr}</span>
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
      <td data-label="代码"><code>${escapeHtml(s.code)}</code></td>
      <td data-label="名称">${escapeHtml(s.name)}</td>
      <td data-label="昵称">${s.nickname ? escapeHtml(s.nickname) : '<span class="muted">—</span>'}</td>
      <td data-label="报价" class="${priceCellClass(q.change_percent)}">
        <div class="quote-price">${fmtPrice(q.price)}</div>
        <div class="quote-change">${fmtChange(q.change_percent)}</div>
      </td>
      <td data-label="盈亏">${profitLossHtml(s.position_cost, q.price)}</td>
      <td data-label="当前涨速" class="${q.surge_change != null ? priceCellClass(q.surge_change) : ''}" title="${q.surge_change != null ? `基准价: ${fmtPrice(q.surge_base_price)} @ ${new Date(q.surge_base_time * 1000).toLocaleString()}` : ''}">
        ${q.surge_change != null ? fmtChange(q.surge_change) : '—'}
      </td>
      <td data-label="涨跌停" class="limit-cell ${limitCellClass(q.limit_status)}" title="${limitTitle(q)}">
        ${renderLimit(q)}
      </td>
      <td data-label="时间">${q.as_of ? new Date(q.as_of * 1000).toLocaleString() : '—'}</td>
      <td data-label="启用"><label class="switch"><input type="checkbox" ${s.enabled ? 'checked' : ''} data-code="${escapeHtml(s.code)}" class="toggle"><span class="slider"></span></label></td>
      <td data-label="操作">
        <button class="btn" data-edit="${escapeHtml(s.code)}">编辑</button>
        <button class="btn btn-danger" data-del="${escapeHtml(s.code)}">删除</button>
      </td>
      <td data-label="做T" class="t-events-cell" data-tcode="${escapeHtml(s.code)}">
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

// 报价自动刷新（跟随后台轮询间隔）
async function startQuoteRefresh() {
  if (quotesTimer) {
    clearInterval(quotesTimer);
    quotesTimer = null;
  }
  updateLatency();
  let interval = 30_000;
  try {
    const s = await api('/api/status');
    interval = (s.poll_interval_seconds || 30) * 1000;
  } catch {}
  quotesTimer = setInterval(async () => {
    try {
      stocksCache = await api('/api/stocks');
      renderStocks();
    } catch {}
    try {
      fundsCache = await api('/api/funds');
      renderFunds();
    } catch {}
    try {
      cryptosCache = await api('/api/cryptos');
      renderCryptos();
    } catch {}
    updateLatency();
  }, interval);
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
    $('#t-event-quantity').value = '';
    $('#t-event-quantity-unit').textContent = '股票以手为单位';
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
    $('#t-event-quantity').value = ev.quantity != null ? ev.quantity : '';
    $('#t-event-quantity-unit').textContent = '股票以手为单位';
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

// ========== 基金 ==========
let fundsCache = [];

async function loadFunds() {
  fundsCache = await api('/api/funds');
  renderFunds();
}

function renderFunds() {
  const tbody = $('#funds-table tbody');
  tbody.innerHTML = '';
  for (const f of fundsCache) {
    const q = f.quote || {};
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td data-label="代码"><code>${escapeHtml(f.code)}</code></td>
      <td data-label="名称">${escapeHtml(f.name)}</td>
      <td data-label="昵称">${escapeHtml(f.nickname || '—')}</td>
      <td data-label="净值" class="${priceCellClass(q.change_percent)}">
        ${q.estimated_nav == null
          ? '<div class="quote-price muted">暂无盘中估值</div><div class="quote-change">—</div>'
          : `<div class="quote-price">${fmtPrice(q.estimated_nav)}</div><div class="quote-change">${fmtChange(q.change_percent)}</div>`}
      </td>
      <td data-label="盈亏">${profitLossHtml(f.position_cost, q.estimated_nav)}</td>
      <td data-label="时间">${q.as_of ? new Date(q.as_of * 1000).toLocaleString() : (q.nav != null ? '净值 ' + fmtPrice(q.nav) : '—')}</td>
      <td data-label="启用"><label class="switch"><input type="checkbox" ${f.enabled ? 'checked' : ''} data-code="${escapeHtml(f.code)}" class="toggle-fund"><span class="slider"></span></label></td>
      <td data-label="操作">
        <button class="btn" data-fedit="${escapeHtml(f.code)}">编辑</button>
        <button class="btn btn-danger" data-fdel="${escapeHtml(f.code)}">删除</button>
      </td>`;
    tbody.appendChild(tr);
  }
  $('#funds-empty').hidden = fundsCache.length > 0;
  $('#funds-table').hidden = fundsCache.length === 0;
}

$('#funds-table').addEventListener('click', async (e) => {
  const editCode = e.target.dataset.fedit;
  if (editCode) openFundDialog(fundsCache.find(f => f.code === editCode));
  const delCode = e.target.dataset.fdel;
  if (delCode) {
    if (confirm(`确认删除基金 ${delCode}?`)) {
      try { await api(`/api/funds/${delCode}`, { method: 'DELETE' }); toast('已删除'); loadFunds(); }
      catch (e) { toast('删除失败: ' + e.message, 'error'); }
    }
  }
});

$('#funds-table').addEventListener('change', async (e) => {
  if (e.target.classList.contains('toggle-fund')) {
    const code = e.target.dataset.code;
    const enabled = e.target.checked;
    try {
      await api(`/api/funds/${code}/enabled`, { method: 'PATCH', body: JSON.stringify({ enabled }) });
      toast(enabled ? '已启用' : '已停用');
    } catch (e) { toast('操作失败: ' + e.message, 'error'); loadFunds(); }
  }
});

// ========== 合约 ==========
let cryptosCache = [];

async function loadCryptos() {
  cryptosCache = await api('/api/cryptos');
  renderCryptos();
}

function renderCryptos() {
  const tbody = $('#cryptos-table tbody');
  tbody.innerHTML = '';
  for (const c of cryptosCache) {
    const q = c.quote || {};
    const prec = q.price_precision != null ? q.price_precision : 2;
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td data-label="代码"><code>${escapeHtml(c.code)}</code></td>
      <td data-label="名称">${escapeHtml(c.name)}</td>
      <td data-label="昵称">${c.nickname ? escapeHtml(c.nickname) : '<span class="muted">—</span>'}</td>
      <td data-label="报价" class="${priceCellClass(q.change_percent)}">
        <div class="quote-price">${fmtPriceP(q.price, prec)}</div>
        <div class="quote-change">${fmtChange(q.change_percent)}</div>
      </td>
      <td data-label="盈亏">${profitLossHtml(c.position_cost, q.price, c.direction, c.leverage)}</td>
      <td data-label="时间">${q.as_of ? new Date(q.as_of * 1000).toLocaleString() : '—'}</td>
      <td data-label="启用"><label class="switch"><input type="checkbox" ${c.enabled ? 'checked' : ''} data-code="${escapeHtml(c.code)}" class="toggle-crypto"><span class="slider"></span></label></td>
      <td data-label="操作">
        <button class="btn" data-cedit="${escapeHtml(c.code)}">编辑</button>
        <button class="btn btn-danger" data-cdel="${escapeHtml(c.code)}">删除</button>
      </td>
      <td data-label="做T" class="t-events-cell" data-tcode="${escapeHtml(c.code)}">
        <div class="t-btns">
          <button class="btn btn-sm btn-t-s ${c.t_s_enabled === false ? 'btn-t-disabled' : ''}" data-tadd="${escapeHtml(c.code)}" data-ttype="S" data-tsource="crypto" ${c.t_s_enabled === false ? 'disabled' : ''}>S</button>
          <button class="btn btn-sm btn-t-b ${c.t_b_enabled === false ? 'btn-t-disabled' : ''}" data-tadd="${escapeHtml(c.code)}" data-ttype="B" data-tsource="crypto" ${c.t_b_enabled === false ? 'disabled' : ''}>B</button>
        </div>
        <div class="t-list" data-tlist="${escapeHtml(c.code)}"></div>
      </td>`;
    tbody.appendChild(tr);
    const tlist = tr.querySelector(`[data-tlist="${CSS.escape(c.code)}"]`);
    if (tlist) renderTList(tlist, c.t_events || []);
  }
  $('#cryptos-empty').hidden = cryptosCache.length > 0;
  $('#cryptos-table').hidden = cryptosCache.length === 0;
}

$('#cryptos-table').addEventListener('click', async (e) => {
  // T 事件 S/B 按钮
  const tBtn = e.target.closest('[data-tadd]');
  if (tBtn) {
    const code = tBtn.dataset.tadd;
    const type = tBtn.dataset.ttype;
    const c = cryptosCache.find(x => x.code === code);
    const defaultPrice = c?.quote?.price;
    const prec = c?.quote?.price_precision != null ? c.quote.price_precision : 2;
    currentTEvent = { action: 'add', code, type, name: c?.name, source: 'crypto' };
    $('#t-event-dialog-title').textContent = `新增${type === 'S' ? '先卖后买(S)' : '先买后卖(B)'} (${c?.name})`;
    $('#t-event-price').value = defaultPrice ? fmtPriceP(defaultPrice, prec) : '';
    $('#t-event-target-price').value = '';
    $('#t-event-quantity').value = '';
    $('#t-event-quantity-unit').textContent = '合约以张为单位';
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
      await api(`/api/cryptos/${code}/t-events/${eventId}`, { method: 'DELETE' });
      toast('已删除'); loadCryptos();
    } catch (e) { toast('删除失败: ' + e.message, 'error'); }
    return;
  }
  // T 事件重置
  const tReset = e.target.closest('.t-trigger.triggered');
  if (tReset) {
    const eventId = tReset.dataset.treset;
    const code = tReset.dataset.tcode;
    try {
      await api(`/api/cryptos/${code}/t-events/${eventId}/reset`, { method: 'POST' });
      toast('T 事件已重置，今日可再次触发');
      loadCryptos();
    } catch (err) { toast('重置失败: ' + err.message, 'error'); }
    return;
  }
  // T 事件编辑
  const tEdit = e.target.closest('[data-tedit]');
  if (tEdit) {
    const eventId = tEdit.dataset.tedit;
    const code = tEdit.dataset.tcode;
    const c = cryptosCache.find(x => x.code === code);
    const ev = (c?.t_events || []).find(x => x.id === eventId);
    if (!ev) return;
    const prec = c?.quote?.price_precision != null ? c.quote.price_precision : 2;
    currentTEvent = { action: 'edit', code, type: ev.type, eventId, name: c?.name, source: 'crypto' };
    $('#t-event-dialog-title').textContent = `编辑${ev.type === 'S' ? '先卖后买(S)' : '先买后卖(B)'} (${c?.name})`;
    $('#t-event-price').value = fmtPriceP(ev.price, prec);
    $('#t-event-target-price').value = ev.target_price != null ? fmtPriceP(ev.target_price, prec) : '';
    $('#t-event-quantity').value = ev.quantity != null ? ev.quantity : '';
    $('#t-event-quantity-unit').textContent = '合约以张为单位';
    $('#t-event-dialog').showModal();
    return;
  }
  // 编辑 / 删除合约
  const editCode = e.target.dataset.cedit;
  if (editCode) openCryptoDialog(cryptosCache.find(c => c.code === editCode));
  const delCode = e.target.dataset.cdel;
  if (delCode) {
    if (confirm(`确认删除 ${delCode}?`)) {
      try { await api(`/api/cryptos/${delCode}`, { method: 'DELETE' }); toast('已删除'); loadCryptos(); }
      catch (e) { toast('删除失败: ' + e.message, 'error'); }
    }
  }
});

$('#cryptos-table').addEventListener('change', async (e) => {
  if (e.target.classList.contains('toggle-crypto')) {
    const code = e.target.dataset.code;
    const enabled = e.target.checked;
    try {
      await api(`/api/cryptos/${code}/enabled`, { method: 'PATCH', body: JSON.stringify({ enabled }) });
      toast(enabled ? '已启用' : '已停用');
    } catch (e) { toast('操作失败: ' + e.message, 'error'); loadCryptos(); }
  }
});

const cryptoDialog = $('#crypto-dialog');
$('#btn-add-crypto').addEventListener('click', () => openCryptoDialog(null));
$('#btn-crypto-cancel').addEventListener('click', () => { resetCryptoSearch(); cryptoDialog.close(); });

function openCryptoDialog(crypto) {
  const form = $('#crypto-form');
  form.reset();
  resetCryptoSearch();
  $('#crypto-dialog-title').textContent = crypto ? '编辑合约' : '新增合约';
  if (crypto) {
    for (const [k, v] of Object.entries(crypto)) {
      if (k === 'quote') continue;
      if (form.elements[k]) {
        if (k === 'daily_change_up' || k === 'daily_change_down') {
          form.elements[k].value = (v || []).join(', ');
        } else if (k === 'code') {
          form.elements[k].value = v ?? '';
          $('#crypto-code-display').value = v ?? '';
          $('#crypto-search-input').value = (crypto.name || '') + ' (' + (v ?? '') + ')';
        } else {
          form.elements[k].value = v ?? '';
        }
      }
    }
    $('#crypto-search-input').readOnly = true;
    const disabledSet = new Set(crypto.disabled_alerts || []);
    $$('.alert-type-toggle', form).forEach(cb => {
      cb.checked = !disabledSet.has(cb.dataset.type);
    });
    updateCryptoInputStates();
  } else {
    $('#crypto-search-input').readOnly = false;
  }
  cryptoDialog.showModal();
}

function updateCryptoInputStates() {
  const form = $('#crypto-form');
  $$('.alert-type-toggle', form).forEach(cb => {
    const inputName = cb.dataset.input;
    if (!inputName) return;
    const group = $$(`.alert-type-toggle[data-input="${inputName}"]`, form);
    const allDisabled = group.length > 0 && group.every(c => !c.checked);
    const input = form.elements[inputName];
    if (input) input.disabled = allDisabled;
  });
}

document.addEventListener('change', (e) => {
  if (e.target.matches('#crypto-form .alert-type-toggle')) updateCryptoInputStates();
});

function resetCryptoSearch() {
  $('#crypto-search-input').value = '';
  $('#crypto-code-display').value = '';
  $('#crypto-code-hidden').value = '';
  $('#crypto-search-dropdown').classList.remove('active');
  $('#crypto-search-dropdown').innerHTML = '';
  $('#crypto-search-input').readOnly = false;
}

cryptoDialog.addEventListener('close', resetCryptoSearch);

// 合约搜索自动补全
let cryptoSearchTimer = null;
$('#crypto-search-input').addEventListener('input', () => {
  clearTimeout(cryptoSearchTimer);
  const q = $('#crypto-search-input').value.trim();
  if (!q) { $('#crypto-search-dropdown').classList.remove('active'); return; }
  cryptoSearchTimer = setTimeout(async () => {
    try {
      const items = await api(`/api/cryptos/search?q=${encodeURIComponent(q)}`);
      const dd = $('#crypto-search-dropdown');
      if (!items || items.length === 0) {
        dd.innerHTML = '<div class="stock-search-empty">无匹配结果</div>';
        dd.classList.add('active');
        return;
      }
      dd.innerHTML = items.map(it => `
        <div class="stock-search-item" data-code="${escapeHtml(it.code)}" data-name="${escapeHtml(it.name)}" data-precision="${it.price_precision ?? 2}">
          <span class="sname">${escapeHtml(it.name)}</span>
          <span class="scode">${escapeHtml(it.market)} ${escapeHtml(it.code)}</span>
        </div>`).join('');
      dd.classList.add('active');
    } catch (e) { /* ignore */ }
  }, 400);
});

$('#crypto-search-dropdown').addEventListener('click', (e) => {
  const item = e.target.closest('.stock-search-item');
  if (!item) return;
  const code = item.dataset.code;
  const name = item.dataset.name;
  $('#crypto-code-hidden').value = code;
  $('#crypto-code-display').value = code;
  $('#crypto-name-input').value = name;
  $('#crypto-search-input').value = name + ' (' + code + ')';
  $('#crypto-search-dropdown').classList.remove('active');
});

document.addEventListener('click', (e) => {
  if (!e.target.closest('#crypto-dialog .stock-search-wrap')) {
    $('#crypto-search-dropdown').classList.remove('active');
  }
});

$('#crypto-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const form = e.target;
  const code = $('#crypto-code-hidden').value.trim();
  if (!code) { toast('请先搜索并选择合约', 'error'); return; }
  const crypto = {
    code,
    name: form.elements.name.value.trim(),
    nickname: form.elements.nickname.value.trim(),
    position_cost: numOrNull(form.elements.position_cost.value),
    direction: form.elements.direction.value,
    leverage: numOrNull(form.elements.leverage.value),
    price_high: numOrNull(form.elements.price_high.value),
    price_low: numOrNull(form.elements.price_low.value),
    daily_change_up: form.elements.daily_change_up.value.split(',').map(s => Number(s.trim())).filter(n => !isNaN(n)),
    daily_change_down: form.elements.daily_change_down.value.split(',').map(s => Number(s.trim())).filter(n => !isNaN(n)),
    cooldown_minutes: Number(form.elements.cooldown_minutes.value),
    enabled: true,
    t_threshold: numOrNull(form.elements.t_threshold.value),
    t_s_enabled: form.elements.t_s_enabled.checked,
    t_b_enabled: form.elements.t_b_enabled.checked,
    disabled_alerts: $$('.alert-type-toggle', form).filter(cb => !cb.checked).map(cb => cb.dataset.type),
  };
  const isEdit = cryptosCache.some(c => c.code === code);
  try {
    if (isEdit) {
      await api(`/api/cryptos/${code}`, { method: 'PUT', body: JSON.stringify(crypto) });
    } else {
      await api('/api/cryptos', { method: 'POST', body: JSON.stringify(crypto) });
    }
    toast(isEdit ? '已更新' : '已新增');
    cryptoDialog.close();
    loadCryptos();
  } catch (e) { toast('保存失败: ' + e.message, 'error'); }
});

const fundDialog = $('#fund-dialog');
$('#btn-add-fund').addEventListener('click', () => openFundDialog(null));
$('#btn-fund-cancel').addEventListener('click', () => { resetFundSearch(); fundDialog.close(); });

function openFundDialog(fund) {
  const form = $('#fund-form');
  form.reset();
  $('#fund-search-input').value = '';
  $('#fund-code-display').value = '';
  $('#fund-code-hidden').value = '';
  $('#fund-search-dropdown').classList.remove('active');
  $('#fund-search-dropdown').innerHTML = '';
  $('#fund-dialog-title').textContent = fund ? '编辑基金' : '新增基金';
  if (fund) {
    for (const [k, v] of Object.entries(fund)) {
      if (k === 'quote') continue;
      if (form.elements[k]) {
        if (k === 'daily_change_up' || k === 'daily_change_down') {
          form.elements[k].value = (v || []).join(', ');
        } else {
          if (k === 'code') {
            form.elements[k].value = v ?? '';
            $('#fund-code-display').value = v ?? '';
            $('#fund-search-input').value = (fund.name || '') + ' (' + (v ?? '') + ')';
          } else {
            form.elements[k].value = v ?? '';
          }
        }
      }
    }
    $('#fund-search-input').readOnly = true;
    const disabledSet = new Set(fund.disabled_alerts || []);
    $$('.alert-type-toggle', form).forEach(cb => {
      cb.checked = !disabledSet.has(cb.dataset.type);
    });
  } else {
    $('#fund-search-input').readOnly = false;
  }
  fundDialog.showModal();
}

function resetFundSearch() {
  $('#fund-search-input').value = '';
  $('#fund-code-display').value = '';
  $('#fund-code-hidden').value = '';
  $('#fund-search-dropdown').classList.remove('active');
  $('#fund-search-dropdown').innerHTML = '';
  $('#fund-search-input').readOnly = false;
}

fundDialog.addEventListener('close', resetFundSearch);

// 基金搜索自动补全
let fundSearchTimer = null;
$('#fund-search-input').addEventListener('input', () => {
  clearTimeout(fundSearchTimer);
  const q = $('#fund-search-input').value.trim();
  if (!q) { $('#fund-search-dropdown').classList.remove('active'); return; }
  fundSearchTimer = setTimeout(async () => {
    try {
      const items = await api(`/api/funds/search?q=${encodeURIComponent(q)}`);
      const dd = $('#fund-search-dropdown');
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

$('#fund-search-dropdown').addEventListener('click', (e) => {
  const item = e.target.closest('.stock-search-item');
  if (!item) return;
  const code = item.dataset.code;
  const name = item.dataset.name;
  $('#fund-code-hidden').value = code;
  $('#fund-code-display').value = code;
  $('#fund-name-input').value = name;
  $('#fund-search-input').value = name + ' (' + code + ')';
  $('#fund-search-dropdown').classList.remove('active');
});

// 点击其他地方关闭下拉（复用 stock-search-wrap 类）
document.addEventListener('click', (e) => {
  if (!e.target.closest('#fund-dialog .stock-search-wrap')) {
    $('#fund-search-dropdown').classList.remove('active');
  }
});

$('#fund-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const form = e.target;
  const code = $('#fund-code-hidden').value.trim();
  if (!code) { toast('请先搜索并选择基金', 'error'); return; }
  const fund = {
    code,
    name: form.elements.name.value.trim(),
    nickname: form.elements.nickname.value.trim(),
    position_cost: numOrNull(form.elements.position_cost.value),
    cooldown_minutes: Number(form.elements.cooldown_minutes.value),
    enabled: true,
    daily_change_up: form.elements.daily_change_up.value.split(',').map(s => Number(s.trim())).filter(n => !isNaN(n)),
    daily_change_down: form.elements.daily_change_down.value.split(',').map(s => Number(s.trim())).filter(n => !isNaN(n)),
    retracement_threshold: numOrNull(form.elements.retracement_threshold.value),
    bounce_threshold: numOrNull(form.elements.bounce_threshold.value),
    disabled_alerts: $$('.alert-type-toggle', form).filter(cb => !cb.checked).map(cb => cb.dataset.type),
  };
  const isEdit = fundsCache.some(f => f.code === code);
  try {
    if (isEdit) {
      await api(`/api/funds/${code}`, { method: 'PUT', body: JSON.stringify(fund) });
    } else {
      await api('/api/funds', { method: 'POST', body: JSON.stringify(fund) });
    }
    toast(isEdit ? '已更新' : '已新增');
    fundDialog.close();
    loadFunds();
  } catch (e) { toast('保存失败: ' + e.message, 'error'); }
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
  const qtyInput = $('#t-event-quantity').value.trim();
  let quantity = null;
  if (qtyInput) {
    const q = parseInt(qtyInput, 10);
    if (!isNaN(q) && q > 0) quantity = q;
  }
  try {
    const base = currentTEvent.source === 'crypto' ? 'cryptos' : 'stocks';
    if (currentTEvent.action === 'add') {
      await api(`/api/${base}/${currentTEvent.code}/t-events`, {
        method: 'POST',
        body: JSON.stringify({ type: currentTEvent.type, price, target_price: targetPrice, quantity })
      });
      toast(`已添加 ${currentTEvent.type} @ ${fmtPrice(price)}`);
    } else {
      await api(`/api/${base}/${currentTEvent.code}/t-events/${currentTEvent.eventId}`, {
        method: 'PUT',
        body: JSON.stringify({ type: currentTEvent.type, price, target_price: targetPrice, quantity })
      });
      toast('已更新');
    }
    if (currentTEvent.source === 'crypto') loadCryptos(); else loadStocks();
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

function updateFundInputStates() {
  const form = $('#fund-form');
  $$('.alert-type-toggle', form).forEach(cb => {
    const inputName = cb.dataset.input;
    if (!inputName) return;
    const group = $$(`.alert-type-toggle[data-input="${inputName}"]`, form);
    const allDisabled = group.length > 0 && group.every(c => !c.checked);
    const input = form.elements[inputName];
    if (input) input.disabled = allDisabled;
  });
}

// Bind toggle changes to update input states
document.addEventListener('change', (e) => {
  if (e.target.matches('#stock-form .alert-type-toggle')) updateInputStates();
  if (e.target.matches('#fund-form .alert-type-toggle')) updateFundInputStates();
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
    position_cost: numOrNull(form.elements.position_cost.value),
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
    limit_seal_min_lots: intOrNull(form.elements.limit_seal_min_lots.value),
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
function intOrNull(v) { return v === '' ? null : parseInt(v, 10); }

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

// 导入 / 导出（带范围选择）
$('#btn-export').addEventListener('click', () => {
  $('#export-dialog').showModal();
});

$('#btn-export-cancel').addEventListener('click', () => $('#export-dialog').close());

$('#export-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const checked = $$('#export-form input[name="scope"]:checked').map(cb => cb.value);
  if (checked.length === 0) { toast('请至少选择一项', 'error'); return; }
  try {
    const scope = checked.join(',');
    const data = await api(`/api/export?scope=${encodeURIComponent(scope)}`);
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `stock-monitor-config-${new Date().toISOString().slice(0, 10)}.json`;
    a.click();
    $('#export-dialog').close();
    toast('已导出');
  } catch (e) { toast('导出失败: ' + e.message, 'error'); }
});

let importFileData = null;

$('#file-import').addEventListener('change', async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  try {
    importFileData = JSON.parse(await file.text());
    $('#import-dialog').showModal();
  } catch (err) {
    toast('文件解析失败: ' + err.message, 'error');
  }
  e.target.value = '';
});

$('#btn-import-cancel').addEventListener('click', () => $('#import-dialog').close());

$('#import-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  if (!importFileData) { toast('请先选择文件', 'error'); return; }
  const checked = $$('#import-form input[name="scope"]:checked').map(cb => cb.value);
  if (checked.length === 0) { toast('请至少选择一项', 'error'); return; }
  try {
    const scope = checked.join(',');
    const fd = new FormData();
    fd.append('file', new Blob([JSON.stringify(importFileData)], { type: 'application/json' }));
    const res = await fetch(`/api/import?scope=${encodeURIComponent(scope)}`, { method: 'POST', body: fd });
    if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
    toast('导入成功');
    $('#import-dialog').close();
    importFileData = null;
    loadStocks();
    loadFunds();
    loadCryptos();
    loadTemplates();
    loadWebhook();
    loadNotifySettings();
    loadEmailConfig();
    loadStatus();
  } catch (e) { toast('导入失败: ' + e.message, 'error'); }
});

// ========== 模板 ==========
const TEMPLATE_TYPES = ['price_high', 'price_low', 'daily_up', 'daily_down', 'surge_up', 'surge_down', 'retracement', 'bounce', 't_sell', 't_buy', 'limit_up', 'limit_up_broken', 'limit_up_low_seal', 'limit_up_exhaust', 'limit_down', 'limit_down_broken', 'limit_down_low_seal', 'limit_down_exhaust', 'profit', 'loss'];
const TEMPLATE_GROUPS = [
  ['价格与当日涨跌', ['price_high', 'price_low', 'daily_up', 'daily_down']],
  ['涨速与回撤', ['surge_up', 'surge_down', 'retracement', 'bounce']],
  ['做T与盈亏', ['t_sell', 't_buy', 'profit', 'loss']],
  ['涨跌停', ['limit_up', 'limit_up_broken', 'limit_up_low_seal', 'limit_up_exhaust', 'limit_down', 'limit_down_broken', 'limit_down_low_seal', 'limit_down_exhaust']],
];
const MARKET_TEMPLATE_TYPES = {
  stock: TEMPLATE_TYPES,
  fund: ['daily_up', 'daily_down', 'retracement', 'bounce', 'profit', 'loss'],
  crypto: ['price_high', 'price_low', 'daily_up', 'daily_down', 't_sell', 't_buy', 'profit', 'loss'],
};
const MARKETS = ['stock', 'fund', 'crypto'];
let currentMarket = 'stock';
let marketTemplatesData = { stock: {}, fund: {}, crypto: {} };

function buildMarketFields() {
  const container = $('#market-templates-fields');
  container.innerHTML = MARKETS.map(mkt => {
    const supported = new Set(MARKET_TEMPLATE_TYPES[mkt] || TEMPLATE_TYPES);
    const groups = TEMPLATE_GROUPS
      .map(([gname, types]) => [gname, types.filter(t => supported.has(t))])
      .filter(([, types]) => types.length > 0);
    return `
    <div class="market-fields" data-market="${mkt}" hidden>
      ${groups.map(([gname, types]) => `
        <section class="cfg-group">
          <h4 class="group-title">${gname}</h4>
          <div class="template-grid">
            ${types.map(type => `
              <div class="template-block">
                <div class="template-label">
                  <label>${type} <span class="muted">留空继承全局</span></label>
                  <button type="button" class="btn btn-preview btn-preview-market" data-type="${type}" data-market="${mkt}">预览</button>
                </div>
                <textarea name="${type}" rows="2" placeholder="留空 → 使用全局基础模板"></textarea>
                <div class="preview-box" id="preview-${mkt}-${type}" hidden></div>
              </div>`).join('')}
          </div>
        </section>`).join('')}
    </div>`;
  }).join('');
}

async function loadTemplates() {
  const t = await api('/api/templates');
  // 全局表单
  const gform = $('#templates-form');
  const globalT = t.global || {};
  for (const k of TEMPLATE_TYPES) {
    if (gform.elements[k]) gform.elements[k].value = (globalT[k] || []).join('\n');
  }
  // 市场表单
  marketTemplatesData = t.market || { stock: {}, fund: {}, crypto: {} };
  buildMarketFields();
  fillMarketForm();
}

function fillMarketForm() {
  const mktData = marketTemplatesData[currentMarket] || {};
  const active = $(`#market-templates-fields .market-fields[data-market="${currentMarket}"]`);
  if (!active) return;
  for (const type of TEMPLATE_TYPES) {
    const el = active.querySelector(`textarea[name="${type}"]`);
    if (el) el.value = (mktData[type] || []).join('\n');
  }
}

$('#templates-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const form = e.target;
  const templates = {};
  for (const key of TEMPLATE_TYPES) {
    templates[key] = form.elements[key].value.split('\n').filter(Boolean);
  }
  try {
    await api('/api/templates', { method: 'PUT', body: JSON.stringify({ global_templates: templates }) });
    toast('全局模板已保存');
  } catch (err) { toast('保存失败: ' + err.message, 'error'); }
});

// 模板二级 tab（全局 / 股票 / 基金 / 合约）
$('#market-tabs').addEventListener('click', (e) => {
  const btn = e.target.closest('.market-tab');
  if (!btn) return;
  const mkt = btn.dataset.market;
  currentMarket = mkt;
  $$('#market-tabs .market-tab').forEach(b => b.classList.toggle('active', b.dataset.market === mkt));
  const isGlobal = mkt === 'global';
  $('#global-template-panel').hidden = !isGlobal;
  $('#market-templates-form').hidden = isGlobal;
  $$('#market-templates-fields .market-fields').forEach(div => {
    div.hidden = div.dataset.market !== mkt;
  });
  if (!isGlobal) fillMarketForm();
});

$('#market-templates-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  if (currentMarket === 'global') return;
  const active = $(`#market-templates-fields .market-fields[data-market="${currentMarket}"]`);
  const templates = {};
  for (const type of TEMPLATE_TYPES) {
    const el = active.querySelector(`textarea[name="${type}"]`);
    const lines = (el ? el.value : '').split('\n').filter(Boolean);
    if (lines.length) templates[type] = lines;
  }
  const payload = { market_templates: { [currentMarket]: templates } };
  try {
    await api('/api/templates', { method: 'PUT', body: JSON.stringify(payload) });
    marketTemplatesData[currentMarket] = templates;
    toast(`${currentMarket} 市场模板已保存`);
  } catch (err) { toast('保存失败: ' + err.message, 'error'); }
});

// 预览逻辑（共用）
async function doPreview(type, mkt) {
  let textArea;
  if (mkt) {
    textArea = $(`#market-templates-fields .market-fields[data-market="${mkt}"] textarea[name="${type}"]`);
  } else {
    textArea = $('#templates-form').elements[type];
  }
  const text = ((textArea && textArea.value) || '').split('\n').filter(Boolean)[0] || '';
  if (!text) { toast('请先输入模板内容', 'error'); return; }
  const boxId = mkt ? `preview-${mkt}-${type}` : `preview-${type}`;
  const box = $(`#${boxId}`);
  try {
    const body = { template: text, alert_type: type };
    if (mkt) body.market = mkt;
    const r = await api('/api/templates/preview', {
      method: 'POST',
      body: JSON.stringify(body),
    });
    const stockLabel = r.stock_code ? `（基于 ${r.stock_name}）` : '（示例数据）';
    const marketLabel = mkt ? `<span class="muted">[${mkt}]</span> ` : '';
    box.innerHTML = `
      <div class="preview-rendered">${marketLabel}${escapeHtml(r.rendered)}</div>
      <div class="preview-meta">${escapeHtml(stockLabel)}</div>
      <details class="preview-vars">
        <summary>占位符值</summary>
        <pre>${escapeHtml(JSON.stringify(r.sample, null, 2))}</pre>
      </details>`;
    box.hidden = false;
  } catch (e) { toast('预览失败: ' + e.message, 'error'); }
}

// 全局模板的预览按钮（静态）
$$('.btn-preview').forEach(btn => {
  if (!btn.classList.contains('btn-preview-market')) {
    btn.addEventListener('click', () => doPreview(btn.dataset.type, btn.dataset.market));
  }
});

// 市场模板的预览按钮（动态生成，事件委托）
$('#market-templates-fields').addEventListener('click', (e) => {
  const btn = e.target.closest('.btn-preview-market');
  if (btn) doPreview(btn.dataset.type, btn.dataset.market);
});

// ========== 通知设置 / Webhook / 邮箱 ==========
const CHANNEL_LABELS = { dingding: '钉钉', email: '邮箱' };

async function loadNotifySettings() {
  const r = await api('/api/settings/notify');
  // 模式
  document.querySelectorAll('input[name="notify_mode"]').forEach(rd => rd.checked = rd.value === r.mode);
  // 通道开关
  const ch = r.channels || {};
  document.querySelector('input[name="ch_dingding"]').checked = !!ch.dingding;
  document.querySelector('input[name="ch_email"]').checked = !!ch.email;
  // 优先级
  renderPriorityList(r.priority || ['dingding', 'email']);
}

function renderPriorityList(priority) {
  const list = $('#priority-list');
  list.innerHTML = priority.map(ch => `
    <div class="priority-item" data-channel="${ch}">
      <span class="priority-handle">⋮⋮</span>
      <span>${CHANNEL_LABELS[ch] || ch}</span>
      <button type="button" class="btn btn-sm btn-priority-up">↑</button>
      <button type="button" class="btn btn-sm btn-priority-down">↓</button>
    </div>`).join('');
}

function getPriorityOrder() {
  return Array.from($$('#priority-list .priority-item')).map(el => el.dataset.channel);
}

// 优先级上移/下移
$('#priority-list').addEventListener('click', (e) => {
  const btn = e.target.closest('.btn-priority-up, .btn-priority-down');
  if (!btn) return;
  const item = btn.closest('.priority-item');
  const up = btn.classList.contains('btn-priority-up');
  if (up && item.previousElementSibling) {
    item.parentNode.insertBefore(item, item.previousElementSibling);
  } else if (!up && item.nextElementSibling) {
    item.parentNode.insertBefore(item.nextElementSibling, item);
  }
});

// 保存通知设置
$('#btn-save-notify-settings').addEventListener('click', async () => {
  const mode = document.querySelector('input[name="notify_mode"]:checked')?.value;
  const channels = {
    dingding: document.querySelector('input[name="ch_dingding"]').checked,
    email: document.querySelector('input[name="ch_email"]').checked,
  };
  const priority = getPriorityOrder();
  try {
    await api('/api/settings/notify', {
      method: 'PUT',
      body: JSON.stringify({ mode, channels, priority }),
    });
    toast('通知设置已保存');
  } catch (err) { toast('保存失败: ' + err.message, 'error'); }
});

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
    toast('钉钉配置已保存（已开启钉钉通道）');
    form.reset();
    loadWebhook();
    loadNotifySettings();
  } catch (err) { toast('保存失败: ' + err.message, 'error'); }
});

// 邮箱配置
async function loadEmailConfig() {
  const r = await api('/api/settings/email');
  const form = $('#email-form');
  form.elements.smtp_host.value = r.smtp_host || '';
  form.elements.smtp_port.value = r.smtp_port || 465;
  form.elements.username.value = r.username || '';
  form.elements.password.value = '';
  form.elements.password.placeholder = r.password_set ? '已设置 (输入新授权码覆盖)' : 'SMTP 授权码';
  form.elements.to_addrs.value = (r.to_addrs || []).join(', ');
  form.elements.use_ssl.checked = r.use_ssl !== false;
}

$('#email-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const form = e.target;
  const pw = form.elements.password.value;
  if (!pw) { toast('请输入 SMTP 授权码', 'error'); return; }
  try {
    await api('/api/settings/email', {
      method: 'PUT',
      body: JSON.stringify({
        smtp_host: form.elements.smtp_host.value,
        smtp_port: parseInt(form.elements.smtp_port.value, 10) || 465,
        username: form.elements.username.value,
        password: pw,
        to_addrs: parseAtList(form.elements.to_addrs.value),
        use_ssl: form.elements.use_ssl.checked,
      }),
    });
    toast('邮箱配置已保存（已开启邮箱通道）');
    loadEmailConfig();
    loadNotifySettings();
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
    ['运行中', s.running ? '是' : '否', s.running ? 'ok' : 'bad'],
    ['检查次数', s.check_count],
    ['告警次数', s.alert_count],
    ['监控股票数', s.stocks.length],
    ['监控基金数', s.funds ? s.funds.length : 0],
    ['监控合约数', s.cryptos ? s.cryptos.length : 0],
    ['最后检查', s.last_check_at ? new Date(s.last_check_at * 1000).toLocaleString() : '—'],
    ['最后告警', s.last_alert_at ? new Date(s.last_alert_at * 1000).toLocaleString() : '—'],
    ['启动时间', s.started_at ? new Date(s.started_at * 1000).toLocaleString() : '—'],
    ['轮询间隔', `${s.poll_interval_seconds}s`],
    ['封单将尽阈值', `${s.limit_seal_exhaust_seconds}s · ${s.limit_seal_exhaust_samples}轮询`],
    ['配置文件', s.config_path],
  ];
  grid.innerHTML = cards.map(([label, value, cls]) => `
    <div class="status-card">
      <div class="label">${escapeHtml(label)}</div>
      <div class="value ${cls || ''}">${escapeHtml(String(value))}</div>
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
            startQuoteRefresh();
            loadStatus();
          } catch (e) { toast('保存失败: ' + e.message, 'error'); loadStatus(); }
        };
        input.addEventListener('blur', save);
        input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { input.blur(); } if (e.key === 'Escape') { loadStatus(); } });
      });
    }
    // 封单将尽阈值可点击编辑（两个 input：秒数 + 采样周期数）
    if (labelEl && labelEl.textContent === '封单将尽阈值') {
      card.classList.add('status-card-editable');
      card.addEventListener('click', function onClick() {
        if (card.querySelector('input')) return;
        const curSec = s.limit_seal_exhaust_seconds;
        const curSmp = s.limit_seal_exhaust_samples;
        const valDiv = card.querySelector('.value');
        valDiv.innerHTML = '';
        const wrap = document.createElement('div');
        wrap.style.display = 'flex';
        wrap.style.gap = '4px';
        wrap.style.alignItems = 'center';
        const iSec = document.createElement('input');
        iSec.type = 'number'; iSec.min = 1; iSec.value = curSec; iSec.style.width = '64px';
        const iSmp = document.createElement('input');
        iSmp.type = 'number'; iSmp.min = 2; iSmp.value = curSmp; iSmp.style.width = '64px';
        wrap.appendChild(iSec);
        wrap.appendChild(document.createTextNode('s·'));
        wrap.appendChild(iSmp);
        wrap.appendChild(document.createTextNode('轮询'));
        valDiv.appendChild(wrap);
        iSec.focus(); iSec.select();
        let saved = false;
        const save = async () => {
          if (saved) return; saved = true;
          const sec = parseInt(iSec.value, 10);
          const smp = parseInt(iSmp.value, 10);
          if (isNaN(sec) || sec < 1 || isNaN(smp) || smp < 2) { loadStatus(); return; }
          try {
            await api('/api/settings/limit-exhaust', { method: 'PUT', body: JSON.stringify({ seconds: sec, samples: smp }) });
            toast(`封单将尽阈值已改为 ${sec}s · ${smp}轮询`);
            loadStatus();
          } catch (e) { toast('保存失败: ' + e.message, 'error'); loadStatus(); }
        };
        // 仅当两个 input 都失焦且焦点未在另一个 input 上时才保存（避免在两个框间切换误触发）
        const maybeSave = (e) => {
          if (saved) return;
          const other = e.relatedTarget;
          if (other === iSec || other === iSmp) return; // 焦点转到另一个 input，不保存
          save();
        };
        [iSec, iSmp].forEach(inp => {
          inp.addEventListener('blur', maybeSave);
          inp.addEventListener('keydown', (e) => { if (e.key === 'Enter') { inp.blur(); } if (e.key === 'Escape') { loadStatus(); } });
        });
      });
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
loadFunds();
loadCryptos();
loadTemplates();
loadWebhook();
loadNotifySettings();
loadEmailConfig();
startQuoteRefresh();
