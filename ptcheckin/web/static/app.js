/* PT 自动签到 · 前端逻辑（无构建、无框架） */
'use strict';

const state = {
  overview: null,
  settings: null,
  recPage: 1,
  recSize: 20,
  calMonth: null,
  calAccountId: null,
};

/* ------------------------------------------------------------------ utils */
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function token() { return localStorage.getItem('pt_token') || ''; }

async function api(path, options = {}) {
  const url = new URL(path, location.origin);
  if (token()) url.searchParams.set('token', token());
  const opts = { headers: {}, ...options };
  if (opts.body && typeof opts.body !== 'string') {
    opts.body = JSON.stringify(opts.body);
    opts.headers['Content-Type'] = 'application/json';
  }
  if (token()) opts.headers['X-Auth-Token'] = token();

  const resp = await fetch(url, opts);
  const text = await resp.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (e) { data = { error: text }; }

  if (resp.status === 401) {
    const input = prompt('该服务已启用访问令牌，请输入令牌：');
    if (input) { localStorage.setItem('pt_token', input.trim()); return api(path, options); }
    throw new Error('未授权');
  }
  if (!resp.ok) throw new Error((data && data.error) || `HTTP ${resp.status}`);
  return data;
}

function toast(message, type = 'info', ms = 3600) {
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.innerHTML = esc(message).replace(/\n/g, '<br>');
  $('#toast-root').appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; setTimeout(() => el.remove(), 250); }, ms);
}

function fmtTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d)) return String(iso);
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function fmtRelative(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d)) return '—';
  const diff = (d.getTime() - Date.now()) / 1000;
  const abs = Math.abs(diff);
  const unit = abs < 60 ? `${Math.round(abs)} 秒` : abs < 3600 ? `${Math.round(abs / 60)} 分钟`
    : abs < 86400 ? `${(abs / 3600).toFixed(1)} 小时` : `${(abs / 86400).toFixed(1)} 天`;
  return diff >= 0 ? `${unit}后` : `${unit}前`;
}

/* -------------------------------------------------------------- navigation */
$$('.tab').forEach((tab) => tab.addEventListener('click', () => switchTab(tab.dataset.tab)));

function switchTab(name) {
  $$('.tab').forEach((t) => t.classList.toggle('active', t.dataset.tab === name));
  $$('.view').forEach((v) => v.classList.toggle('active', v.id === `view-${name}`));
  if (name === 'records') loadRecords();
  if (name === 'calendar') initCalendar();
  if (name === 'settings') loadSettings();
}

/* ------------------------------------------------------------------ clock */
setInterval(() => {
  const now = new Date();
  const p = (n) => String(n).padStart(2, '0');
  $('#clock').textContent = `${p(now.getHours())}:${p(now.getMinutes())}:${p(now.getSeconds())}`;
}, 1000);

/* --------------------------------------------------------------- overview */
async function loadOverview(silent = false) {
  try {
    state.overview = await api('/api/overview');
    renderOverview();
    if (!silent) return;
  } catch (err) {
    toast(`加载失败：${err.message}`, 'err');
  }
}

function renderOverview() {
  const ov = state.overview;
  if (!ov) return;
  const s = ov.stats;
  const siteNames = (ov.sites || []).map((x) => x.name).join(' / ');
  $('#site-label').textContent = `${siteNames || '—'} · ${ov.timezone}`;
  $('#stat-grid').innerHTML = [
    stat('账号 / 启用', `${s.accounts_total} / ${s.accounts_enabled}`, ''),
    stat('今日已签到', s.today_signed, 'ok'),
    stat('今日待签到', s.today_pending, 'warn'),
    stat('今日失败', s.today_failed, s.today_failed ? 'bad' : ''),
    stat('累计尝试记录', s.attempts_total, ''),
  ].join('');

  const sch = ov.scheduler;
  const parts = [];
  parts.push(sch.running ? '🟢 调度器运行中' : '🔴 调度器已停止');
  parts.push(`下次运行：${sch.next_run_at ? `${fmtTime(sch.next_run_at)}（${fmtRelative(sch.next_run_at)}）` : '—'}`);
  if (sch.running_accounts && sch.running_accounts.length) parts.push(`正在执行：#${sch.running_accounts.join(', #')}`);
  $('#scheduler-line').textContent = parts.join(' · ');

  const grid = $('#account-grid');
  if (!ov.accounts.length) {
    grid.innerHTML = '<div class="empty">还没有账号，点击右上角「+ 添加账号」开始吧。</div>';
  } else {
    grid.innerHTML = ov.accounts.map(renderAccountCard).join('');
  }
  refreshAccountOptions();
}

function stat(k, v, cls) {
  return `<div class="stat ${cls}"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div></div>`;
}

function renderAccountCard(a) {
  const last = a.last_attempt;
  const plan = a.plan || {};
  const stateText = {
    done: '今日已完成', pending: '等待定时执行', due: '正在执行', waiting_retry: '等待重试',
    exhausted: '今日重试已用尽', disabled: '已停用', invalid_schedule: '时间配置错误',
  }[plan.state] || plan.state;

  const lastLine = last
    ? `上次 ${fmtTime(last.started_at)} <span class="badge ${last.view_status}" style="margin:0">${esc(last.view_label)}</span>`
    : '暂无执行记录';

  return `
  <div class="account-card ${a.view_status}">
    <div class="ac-head">
      <div>
        <div class="ac-name">${esc(a.name)} <span class="site-tag">${esc(a.site_name || a.site)}</span></div>
        <div class="ac-url">${esc(a.base_url)}</div>
      </div>
      <div class="badge ${a.view_status}">${esc(a.view_label)}</div>
    </div>
    <div class="ac-metrics">
      <div class="metric"><div class="k">今日获得${a.points_unit ? '（' + esc(a.points_unit) + '）' : ''}</div><div class="v">${a.today_points ?? '—'}</div></div>
      <div class="metric"><div class="k">连续签到</div><div class="v">${a.streak ?? 0} 天</div></div>
      <div class="metric"><div class="k">累计签到</div><div class="v">${a.total_signed ?? 0} 次</div></div>
      <div class="metric"><div class="k">失败次数</div><div class="v">${plan.failures ?? 0}</div></div>
    </div>
    <div class="ac-line">
      <span>签到时间 <b>${esc(plan.schedule_time || '默认')}</b></span>
      <span>状态 <b>${esc(stateText)}</b></span>
      <span>下次 <b>${a.next_run_at ? `${fmtTime(a.next_run_at)}（${fmtRelative(a.next_run_at)}）` : '—'}</b></span>
    </div>
    <div class="ac-line">${lastLine}</div>
    ${last && last.error ? `<div class="err-text">⚠ ${esc(last.error)}</div>` : ''}
    <div class="ac-actions">
      <button class="btn primary tiny" data-act="checkin" data-id="${a.id}">立即签到</button>
      <button class="btn ghost tiny" data-act="refresh" data-id="${a.id}">刷新状态</button>
      <button class="btn ghost tiny" data-act="calendar" data-id="${a.id}">日历</button>
      <button class="btn ghost tiny" data-act="edit" data-id="${a.id}">编辑</button>
      <button class="btn danger tiny" data-act="delete" data-id="${a.id}">删除</button>
    </div>
  </div>`;
}

$('#account-grid').addEventListener('click', async (ev) => {
  const btn = ev.target.closest('button[data-act]');
  if (!btn) return;
  const id = Number(btn.dataset.id);
  const act = btn.dataset.act;
  const account = (state.overview?.accounts || []).find((a) => a.id === id);
  if (act === 'checkin' || act === 'refresh') {
    btn.disabled = true;
    const old = btn.textContent;
    btn.innerHTML = '<span class="spin"></span> 执行中';
    try {
      const res = await api(`/api/accounts/${id}/${act}`, { method: 'POST', body: {} });
      const r = res.result;
      toast(`${account ? account.name : id}：${r.label}${r.error ? ' · ' + r.error : ''}${r.points != null ? ' · 获得 ' + r.points : ''}`,
        r.ok ? 'ok' : 'err', 6000);
      await loadOverview(true);
    } catch (err) {
      toast(`执行失败：${err.message}`, 'err');
    } finally {
      btn.disabled = false;
      btn.textContent = old;
    }
  } else if (act === 'edit') {
    openAccountModal(account);
  } else if (act === 'delete') {
    if (!confirm(`确定删除账号「${account?.name}」及其全部记录？`)) return;
    try {
      await api(`/api/accounts/${id}`, { method: 'DELETE' });
      toast('已删除', 'ok');
      await loadOverview(true);
    } catch (err) { toast(err.message, 'err'); }
  } else if (act === 'calendar') {
    switchTab('calendar');
    state.calAccountId = id;
    $('#cal-account').value = String(id);
    loadCalendar();
  }
});

$('#btn-refresh').addEventListener('click', () => loadOverview());
$('#btn-checkin-all').addEventListener('click', async () => {
  if (!confirm('立即对所有已启用账号执行签到？')) return;
  try {
    const res = await api('/api/checkin-all', { method: 'POST', body: {} });
    toast(`已提交 ${res.queued} 个签到任务，稍后自动刷新…`, 'ok');
    setTimeout(() => loadOverview(true), 3000);
    setTimeout(() => loadOverview(true), 8000);
  } catch (err) { toast(err.message, 'err'); }
});
$('#btn-add').addEventListener('click', () => openAccountModal(null));

/* ---------------------------------------------------------- account modal */
const CURL_PLACEHOLDER = `在此粘贴浏览器「Copy as cURL」的内容，例如：
curl 'https://hhanclub.net/attendance.php' \\
  -H 'accept: text/html,...' \\
  -b 'c_secure_uid=...; c_secure_pass=...; c_secure_login=...' \\
  -H 'user-agent: Mozilla/5.0 ...'`;

function openModal({ title, body, footer, width }) {
  const backdrop = document.createElement('div');
  backdrop.className = 'modal-backdrop';
  backdrop.innerHTML = `
    <div class="modal" style="${width ? `width:${width}` : ''}">
      <div class="modal-head"><h3>${esc(title)}</h3><button class="close">×</button></div>
      <div class="modal-body">${body}</div>
      <div class="modal-foot">${footer || ''}</div>
    </div>`;
  const close = () => backdrop.remove();
  backdrop.querySelector('.close').addEventListener('click', close);
  backdrop.addEventListener('click', (e) => { if (e.target === backdrop) close(); });
  $('#modal-root').appendChild(backdrop);
  return { backdrop, close };
}

function openAccountModal(account) {
  const isEdit = !!account;
  const body = `
    <div class="field">
      <label>① 粘贴 cURL（推荐，可自动识别站点与 Cookie）</label>
      <textarea id="ac-curl" placeholder="${esc(CURL_PLACEHOLDER)}"></textarea>
      <div class="row-actions" style="margin-top:8px">
        <button class="btn ghost tiny" id="ac-parse">解析并填入</button>
        <span class="hint" id="ac-parse-msg"></span>
      </div>
      <div id="ac-preview"></div>
    </div>
    <hr style="border:0;border-top:1px solid var(--border);margin:18px 0"/>
    <div class="grid-2">
      <div class="field"><label>站点</label><select id="ac-site"></select></div>
      <div class="field"><label>账号名称</label><input id="ac-name" value="${esc(account?.name || '')}" placeholder="例如 HHClub 主号"/></div>
    </div>
    <div class="field">
      <label>站点地址</label>
      <input id="ac-url" value="${esc(account?.base_url || '')}"/>
      <div class="hint" id="ac-site-hint"></div>
    </div>
    <div class="field">
      <label>Cookie${isEdit ? '（留空表示不修改）' : ''}</label>
      <textarea id="ac-cookie" placeholder="c_secure_uid=...; c_secure_pass=...; c_secure_ssl=...; c_secure_tracker_ssl=...; c_secure_login=...">${esc(account?.cookie_masked && isEdit ? '' : '')}</textarea>
      ${isEdit && account?.cookie_masked ? `<div class="hint">当前：${esc(account.cookie_masked)}</div>` : ''}
    </div>
    <div class="grid-2">
      <div class="field"><label>签到时间（留空用全局默认）</label><input type="time" id="ac-schedule" value="${esc(account?.schedule_time || '')}"/></div>
      <div class="field"><label>随机延迟上限（秒，留空用默认）</label><input type="number" min="0" id="ac-jitter" value="${account?.jitter_seconds ?? ''}"/></div>
    </div>
    <div class="grid-2">
      <div class="field"><label>时区（留空用全局默认）</label><input id="ac-tz" value="${esc(account?.timezone || '')}" placeholder="Asia/Shanghai"/></div>
      <div class="field"><label>User-Agent（可留空）</label><input id="ac-ua" value="${esc(account?.user_agent || '')}"/></div>
    </div>
    <div class="field"><label>备注</label><input id="ac-note" value="${esc(account?.note || '')}"/></div>
    <div class="field"><label><input type="checkbox" id="ac-enabled" ${account?.enabled !== false ? 'checked' : ''}/> 启用该账号的自动签到</label></div>
    <p class="hint">提示：站点签到接口对同一天是幂等的 —— 已签到时会直接返回当日结果，不会重复签到。</p>
  `;
  const footer = `
    <button class="btn ghost" id="ac-cancel">取消</button>
    <button class="btn primary" id="ac-save">${isEdit ? '保存' : '保存并校验'}</button>`;
  const { close } = openModal({ title: isEdit ? `编辑账号 · ${account.name}` : '添加 PT 账号', body, footer });

  $('#ac-cancel').addEventListener('click', close);

  /* ---- 站点选择：切换时同步默认地址与积分单位提示 ---- */
  const sites = state.overview?.sites || [];
  const siteSelect = $('#ac-site');
  siteSelect.innerHTML = sites
    .map((x) => `<option value="${esc(x.key)}">${esc(x.name)}</option>`)
    .join('');
  if (!sites.length) siteSelect.innerHTML = '<option value="hhanclub">HHClub</option>';

  function syncSiteHint(prefillUrl) {
    const chosen = sites.find((x) => x.key === siteSelect.value);
    if (prefillUrl && chosen?.default_base_url) $('#ac-url').value = chosen.default_base_url;
    $('#ac-site-hint').textContent = chosen
      ? `签到接口：${chosen.attendance_path} · 积分单位：${chosen.points_unit}`
      : '';
  }

  // 编辑既有账号时用其站点；新增时按已填地址推断，否则用第一个站点
  if (account?.site) {
    siteSelect.value = account.site;
  } else if (sites.length) {
    const guessed = sites.find((x) => x.default_base_url === $('#ac-url').value.trim());
    siteSelect.value = (guessed || sites[0]).key;
  }
  syncSiteHint(!isEdit && !$('#ac-url').value.trim());
  siteSelect.addEventListener('change', () => syncSiteHint(true));

  $('#ac-parse').addEventListener('click', async () => {
    const curl = $('#ac-curl').value.trim();
    if (!curl) { toast('请先粘贴 cURL', 'warn'); return; }
    try {
      const res = await api('/api/curl/preview', {
        method: 'POST',
        body: { curl, include_cookie: true },
      });
      const p = res.parsed || {};
      if (p.base_url) $('#ac-url').value = p.base_url;
      if (p.user_agent) $('#ac-ua').value = p.user_agent;
      if (p.cookie_full) $('#ac-cookie').value = p.cookie_full;
      // 按识别出的地址切换站点
      if (p.site) {
        const matched = sites.find((x) => x.key === p.site);
        if (matched) { siteSelect.value = matched.key; syncSiteHint(false); }
        if (!$('#ac-name').value) $('#ac-name').value = (matched || {}).name || p.site.toUpperCase();
      }

      $('#ac-preview').innerHTML = `<div class="preview ${p.cookie_valid ? 'ok' : 'bad'}">` +
        `${p.cookie_valid ? '✅ Cookie 格式正常' : '❌ ' + esc(p.cookie_check)}\n` +
        `站点：${esc(p.base_url || '(未识别)')}\n` +
        `Cookie 字段：${esc((p.cookie_keys || []).join(', ') || '(无)')}\n` +
        `Cookie：${esc(p.cookie_preview || '(未识别到)')}</div>`;
      $('#ac-parse-msg').textContent = (p.warnings || []).join('；');
      toast(p.cookie_valid ? '解析成功，已自动填入表单' : `解析完成：${p.cookie_check}`,
        p.cookie_valid ? 'ok' : 'warn');
    } catch (err) {
      toast(err.message, 'err');
    }
  });

  $('#ac-save').addEventListener('click', async () => {
    const payload = {
      name: $('#ac-name').value.trim(),
      base_url: $('#ac-url').value.trim(),
      site: $('#ac-site').value,
      cookie: $('#ac-cookie').value.trim(),
      schedule_time: $('#ac-schedule').value,
      jitter_seconds: $('#ac-jitter').value === '' ? null : Number($('#ac-jitter').value),
      timezone: $('#ac-tz').value.trim(),
      user_agent: $('#ac-ua').value.trim(),
      note: $('#ac-note').value.trim(),
      enabled: $('#ac-enabled').checked,
    };
    if (!payload.cookie) delete payload.cookie;
    if (!isEdit) {
      const curl = $('#ac-curl').value.trim();
      if (curl && !payload.cookie) payload.curl = curl;
      payload.verify = true;
    }
    const btn = $('#ac-save');
    btn.disabled = true; btn.innerHTML = '<span class="spin"></span> 保存中';
    try {
      if (isEdit) {
        await api(`/api/accounts/${account.id}`, { method: 'PUT', body: payload });
        toast('已保存', 'ok');
      } else {
        const res = await api('/api/accounts', { method: 'POST', body: payload });
        const v = res.verify;
        if (v) {
          toast(`添加成功：${v.label}${v.error ? ' · ' + v.error : ''}${v.points != null ? ' · 获得 ' + v.points : ''}`,
            v.ok ? 'ok' : 'warn', 7000);
        } else {
          toast('添加成功', 'ok');
        }
      }
      close();
      await loadOverview(true);
    } catch (err) {
      toast(`保存失败：${err.message}`, 'err', 7000);
    } finally {
      btn.disabled = false; btn.textContent = isEdit ? '保存' : '保存并校验';
    }
  });
}

/* ---------------------------------------------------------------- records */
function refreshAccountOptions() {
  const accounts = state.overview?.accounts || [];
  ['#rec-account', '#cal-account'].forEach((sel) => {
    const el = $(sel);
    if (!el) return;
    const current = el.value;
    const withAll = sel === '#rec-account';
    el.innerHTML = (withAll ? '<option value="">全部账号</option>' : '') +
      accounts.map((a) => `<option value="${a.id}">${esc(a.name)}</option>`).join('');
    if (current) el.value = current;
  });
}

async function loadRecords() {
  const params = new URLSearchParams();
  const acct = $('#rec-account').value;
  if (acct) params.set('account_id', acct);
  if ($('#rec-status').value) params.set('status', $('#rec-status').value);
  if ($('#rec-from').value) params.set('date_from', $('#rec-from').value);
  if ($('#rec-to').value) params.set('date_to', $('#rec-to').value);
  state.recSize = Number($('#rec-size').value) || 20;
  params.set('page', state.recPage);
  params.set('page_size', state.recSize);

  try {
    const data = await api(`/api/records?${params}`);
    const body = $('#rec-body');
    if (!data.items.length) {
      body.innerHTML = '<tr><td colspan="10" class="empty">没有符合条件的记录</td></tr>';
    } else {
      body.innerHTML = data.items.map((it) => `
        <tr>
          <td class="nowrap">${fmtTime(it.started_at)}</td>
          <td>${esc(it.account_name || '#' + it.account_id)}</td>
          <td class="nowrap">${esc(it.check_date || '—')}</td>
          <td><span class="status-pill ${it.view_status}">${esc(it.view_label)}</span></td>
          <td class="nowrap">${it.points ?? '—'}</td>
          <td class="nowrap">${it.streak != null ? it.streak + ' 天' : '—'}</td>
          <td class="nowrap">${it.rank != null ? `${it.rank}/${it.rank_total ?? '?'}` : '—'}</td>
          <td>${esc(it.trigger || '—')}</td>
          <td class="nowrap">${it.duration_ms != null ? it.duration_ms + ' ms' : '—'}</td>
          <td>${(it.message || it.error) ? `<button class="btn ghost tiny" data-detail="${it.id}">查看</button>` : '—'}</td>
        </tr>`).join('');
      body.querySelectorAll('button[data-detail]').forEach((btn) => {
        btn.addEventListener('click', () => {
          const item = data.items.find((x) => String(x.id) === btn.dataset.detail);
          showRecordDetail(item);
        });
      });
    }
    const pages = data.pages;
    $('#rec-pager').innerHTML = `
      <span class="muted">共 ${data.total} 条 · 第 ${data.page}/${pages} 页</span>
      <button class="btn ghost tiny" id="pg-first" ${data.page <= 1 ? 'disabled' : ''}>首页</button>
      <button class="btn ghost tiny" id="pg-prev" ${data.page <= 1 ? 'disabled' : ''}>上一页</button>
      <button class="btn ghost tiny" id="pg-next" ${data.page >= pages ? 'disabled' : ''}>下一页</button>
      <button class="btn ghost tiny" id="pg-last" ${data.page >= pages ? 'disabled' : ''}>末页</button>`;
    $('#pg-first')?.addEventListener('click', () => { state.recPage = 1; loadRecords(); });
    $('#pg-prev')?.addEventListener('click', () => { state.recPage = Math.max(1, data.page - 1); loadRecords(); });
    $('#pg-next')?.addEventListener('click', () => { state.recPage = Math.min(pages, data.page + 1); loadRecords(); });
    $('#pg-last')?.addEventListener('click', () => { state.recPage = pages; loadRecords(); });
  } catch (err) {
    toast(err.message, 'err');
  }
}

function showRecordDetail(item) {
  if (!item) return;
  const detail = item.detail || {};
  const page = detail.page || {};
  const body = `
    <div class="field"><label>状态</label><div>${esc(item.view_label)}（${esc(item.status)}）</div></div>
    <div class="field"><label>时间</label><div>${fmtTime(item.started_at)} → ${fmtTime(item.finished_at)}，耗时 ${item.duration_ms ?? '—'} ms，HTTP ${item.http_status ?? '—'}</div></div>
    <div class="field"><label>站点日期</label><div>${esc(item.site_date || '—')}（来源：${esc(page.site_date_source || '—')}）</div></div>
    <div class="field"><label>站点提示</label><div>${esc(item.message || '—')}</div></div>
    ${item.error ? `<div class="field"><label>错误</label><div class="err-text">${esc(item.error)}</div></div>` : ''}
    <div class="field"><label>解析结果</label><pre class="preview">${esc(JSON.stringify(page, null, 2))}</pre></div>`;
  openModal({ title: `执行详情 #${item.id}`, body, footer: '<button class="btn ghost" id="dt-close">关闭</button>' });
  $('#dt-close').addEventListener('click', () => $('.modal-backdrop').remove());
}

$('#rec-search').addEventListener('click', () => { state.recPage = 1; loadRecords(); });
$('#rec-reset').addEventListener('click', () => {
  ['#rec-account', '#rec-status', '#rec-from', '#rec-to'].forEach((s) => { $(s).value = ''; });
  state.recPage = 1; loadRecords();
});

/* --------------------------------------------------------------- calendar */
function initCalendar() {
  if (!state.calAccountId) {
    const first = state.overview?.accounts?.[0];
    if (first) { state.calAccountId = first.id; $('#cal-account').value = String(first.id); }
  }
  if (!state.calMonth) {
    const acct = (state.overview?.accounts || []).find((a) => a.id === state.calAccountId);
    state.calMonth = (acct?.today || new Date().toISOString().slice(0, 10)).slice(0, 7);
  }
  loadCalendar();
}

$('#cal-account').addEventListener('change', () => {
  state.calAccountId = Number($('#cal-account').value);
  loadCalendar();
});
$('#cal-prev').addEventListener('click', () => shiftMonth(-1));
$('#cal-next').addEventListener('click', () => shiftMonth(1));
$('#cal-today').addEventListener('click', () => {
  const acct = (state.overview?.accounts || []).find((a) => a.id === state.calAccountId);
  state.calMonth = (acct?.today || new Date().toISOString().slice(0, 10)).slice(0, 7);
  loadCalendar();
});

function shiftMonth(delta) {
  const [y, m] = state.calMonth.split('-').map(Number);
  const d = new Date(y, m - 1 + delta, 1);
  state.calMonth = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
  loadCalendar();
}

async function loadCalendar() {
  if (!state.calAccountId) { $('#calendar').innerHTML = '<div class="empty">请先添加账号</div>'; return; }
  try {
    const data = await api(`/api/accounts/${state.calAccountId}/calendar?month=${state.calMonth}`);
    renderCalendar(data);
  } catch (err) { toast(err.message, 'err'); }
}

function renderCalendar(data) {
  const [year, month] = data.month.split('-').map(Number);
  const first = new Date(year, month - 1, 1);
  const daysInMonth = new Date(year, month, 0).getDate();
  const offset = first.getDay();
  const acct = (state.overview?.accounts || []).find((a) => a.id === state.calAccountId);
  const todayStr = acct?.today || '';
  const weekday = ['日', '一', '二', '三', '四', '五', '六'];

  $('#cal-month').textContent = data.month;
  $('#cal-summary').textContent =
    `本月已签到 ${data.summary.signed} 天 · 共 ${data.summary.points} 憨豆` +
    (data.summary.retroactive ? ` · 含补签 ${data.summary.retroactive} 天` : '');

  const cells = weekday.map((w) => `<div class="cal-cell head">周${w}</div>`);
  for (let i = 0; i < offset; i++) cells.push('<div class="cal-cell blank"></div>');
  for (let day = 1; day <= daysInMonth; day++) {
    const dateStr = `${data.month}-${String(day).padStart(2, '0')}`;
    const rec = data.days[dateStr];
    const isToday = dateStr === todayStr;
    let cls = '';
    if (rec && rec.signed) cls = rec.is_retroactive ? 'retro' : 'signed';
    else if (todayStr && dateStr < todayStr) cls = 'missed';
    if (isToday) cls += ' today';
    const pts = rec && rec.signed ? (rec.points ?? '✓') : (cls.includes('missed') ? '✗' : '');
    const ptsCls = rec && rec.signed ? (rec.is_retroactive ? 'blue' : 'green') : '';
    cells.push(`
      <div class="cal-cell ${cls}" title="${esc(dateStr)}${rec && rec.site_created_at ? ' · ' + esc(rec.site_created_at) : ''}">
        <div class="cal-day">${day}${isToday ? ' · 今天' : ''}</div>
        <div class="cal-pts ${ptsCls}">${esc(pts)}</div>
        ${rec && rec.signed && rec.site_created_at ? `<div class="cal-note">${esc(String(rec.site_created_at).slice(11, 16))}</div>` : ''}
      </div>`);
  }
  $('#calendar').innerHTML = cells.join('');
}

/* --------------------------------------------------------------- settings */
const CHANNEL_TYPES = {
  webhook: { label: '通用 Webhook', fields: [{ k: 'url', label: 'URL', ph: 'https://example.com/hook' }] },
  serverchan: { label: 'Server酱', fields: [{ k: 'sendkey', label: 'SendKey', ph: 'SCT...' }] },
  bark: {
    label: 'Bark',
    fields: [{ k: 'url', label: '服务器', ph: 'https://api.day.app' }, { k: 'key', label: 'Device Key', ph: '可选' }],
  },
  telegram: {
    label: 'Telegram',
    fields: [{ k: 'bot_token', label: 'Bot Token', ph: '123456:ABC-DEF' }, { k: 'chat_id', label: 'Chat ID', ph: '123456789' }],
  },
  ntfy: {
    label: 'ntfy',
    fields: [{ k: 'url', label: 'Topic URL', ph: 'https://ntfy.sh/my-topic' }, { k: 'token', label: 'Token', ph: '可选' }],
  },
};

async function loadSettings() {
  try {
    const data = await api('/api/settings');
    state.settings = data.settings;
    fillSettings(data.settings);
  } catch (err) { toast(err.message, 'err'); }
}

function fillSettings(s) {
  $('#set-schedule').value = s.default_schedule_time || '00:30';
  $('#set-jitter').value = s.default_jitter_seconds ?? 600;
  $('#set-tz').value = s.timezone || 'Asia/Shanghai';
  $('#set-enabled').checked = !!s.scheduler_enabled;
  $('#set-tick').value = s.scheduler_tick_seconds ?? 20;
  $('#set-retries').value = s.max_retries_per_day ?? 5;
  $('#set-retry-interval').value = s.retry_interval_seconds ?? 900;
  $('#set-timeout').value = s.request_timeout ?? 30;
  $('#set-verify').checked = !!s.verify_ssl;
  $('#set-proxy').value = s.proxy || '';
  $('#set-host').value = s.web?.host || '127.0.0.1';
  $('#set-port').value = s.web?.port ?? 8787;
  $('#set-token').value = s.web?.auth_token || '';
  $('#set-keep').value = s.keep_raw_days ?? 30;

  const n = s.notify || {};
  $('#notify-enabled').checked = !!n.enabled;
  $('#notify-success').checked = n.on_success !== false;
  $('#notify-already').checked = !!n.on_already;
  $('#notify-failure').checked = n.on_failure !== false;
  $('#notify-recovered').checked = n.on_recovered !== false;
  renderChannels(n.channels || []);

  $('#settings-meta').textContent =
    `数据目录：${s.data_dir || '—'} · Cookie 加密：` +
    (s.encryption === 'fernet' ? 'Fernet (AES-256)' : '兼容模式（建议安装 cryptography）');
}

function renderChannels(channels) {
  const list = $('#channel-list');
  if (!channels.length) {
    list.innerHTML = '<div class="muted small">尚未配置通知渠道。</div>';
    return;
  }
  list.innerHTML = channels.map((ch, idx) => {
    const def = CHANNEL_TYPES[ch.type] || { label: ch.type, fields: [] };
    const fields = def.fields.map((f) => `
      <label>${esc(f.label)}
        <input data-ch="${idx}" data-key="${esc(f.k)}" value="${esc(ch[f.k] || '')}" placeholder="${esc(f.ph || '')}"/>
      </label>`).join('');
    return `
      <div class="channel-row" data-idx="${idx}">
        <div class="ch-head">
          <label class="muted small"><input type="checkbox" data-ch="${idx}" data-key="enabled" ${ch.enabled !== false ? 'checked' : ''}/> 启用</label>
          <strong>${esc(def.label)}</strong>
          <input data-ch="${idx}" data-key="name" value="${esc(ch.name || '')}" placeholder="备注名称" style="max-width:180px"/>
          <button class="btn danger tiny" data-remove="${idx}" style="margin-left:auto">删除</button>
        </div>
        <div class="ch-fields">${fields}</div>
      </div>`;
  }).join('');

  list.querySelectorAll('button[data-remove]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const chs = collectChannels();
      chs.splice(Number(btn.dataset.remove), 1);
      renderChannels(chs);
    });
  });
}

function collectChannels() {
  const channels = state.settings?.notify?.channels ? JSON.parse(JSON.stringify(state.settings.notify.channels)) : [];
  $$('#channel-list [data-ch]').forEach((input) => {
    const idx = Number(input.dataset.ch);
    if (!channels[idx]) return;
    const key = input.dataset.key;
    channels[idx][key] = input.type === 'checkbox' ? input.checked : input.value.trim();
  });
  return channels;
}

$('#btn-add-channel').addEventListener('click', () => {
  const channels = collectChannels();
  const type = $('#channel-type').value;
  const blank = { type, enabled: true, name: (CHANNEL_TYPES[type] || {}).label || type };
  (CHANNEL_TYPES[type]?.fields || []).forEach((f) => { blank[f.k] = ''; });
  channels.push(blank);
  if (state.settings?.notify) state.settings.notify.channels = channels;
  renderChannels(channels);
});

$('#btn-test-notify').addEventListener('click', async () => {
  const btn = $('#btn-test-notify');
  btn.disabled = true; btn.innerHTML = '<span class="spin"></span> 发送中';
  try {
    await saveSettings(true);
    const res = await api('/api/notify/test', { method: 'POST', body: {} });
    const ok = res.results.filter((r) => r.ok).length;
    toast(`测试通知：成功 ${ok}/${res.results.length}\n` +
      res.results.map((r) => `${r.ok ? '✅' : '❌'} ${r.name}${r.ok ? '' : '：' + r.detail}`).join('\n'),
      ok === res.results.length ? 'ok' : 'warn', 8000);
  } catch (err) { toast(err.message, 'err'); }
  finally { btn.disabled = false; btn.textContent = '发送测试通知'; }
});

function settingsPayload() {
  return {
    default_schedule_time: $('#set-schedule').value || '00:30',
    default_jitter_seconds: Number($('#set-jitter').value || 0),
    timezone: $('#set-tz').value.trim() || 'Asia/Shanghai',
    scheduler_enabled: $('#set-enabled').checked,
    scheduler_tick_seconds: Number($('#set-tick').value || 20),
    max_retries_per_day: Number($('#set-retries').value || 0),
    retry_interval_seconds: Number($('#set-retry-interval').value || 0),
    request_timeout: Number($('#set-timeout').value || 30),
    verify_ssl: $('#set-verify').checked,
    proxy: $('#set-proxy').value.trim(),
    keep_raw_days: Number($('#set-keep').value || 0),
    web: {
      host: $('#set-host').value.trim(),
      port: Number($('#set-port').value || 8787),
      auth_token: $('#set-token').value.trim(),
    },
    notify: {
      enabled: $('#notify-enabled').checked,
      on_success: $('#notify-success').checked,
      on_already: $('#notify-already').checked,
      on_failure: $('#notify-failure').checked,
      on_recovered: $('#notify-recovered').checked,
      channels: collectChannels(),
    },
  };
}

async function saveSettings(silent = false) {
  const res = await api('/api/settings', { method: 'PUT', body: settingsPayload() });
  state.settings = res.settings;
  if (!silent) {
    fillSettings(res.settings);
    toast('设置已保存', 'ok');
    await loadOverview(true);
  }
  return res;
}

$('#btn-save-settings').addEventListener('click', async () => {
  const btn = $('#btn-save-settings');
  btn.disabled = true; btn.innerHTML = '<span class="spin"></span> 保存中';
  try { await saveSettings(); } catch (err) { toast(err.message, 'err'); }
  finally { btn.disabled = false; btn.textContent = '保存设置'; }
});

/* ------------------------------------------------------------------ boot */
(async function boot() {
  await loadOverview();
  refreshAccountOptions();
  setInterval(() => {
    if ($('#view-overview').classList.contains('active')) loadOverview(true);
  }, 30000);
  if (location.hash) switchTab(location.hash.slice(1));
})();
