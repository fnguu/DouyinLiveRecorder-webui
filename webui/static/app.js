/* DouyinLiveRecorder WebUI 前端 */
(() => {
  'use strict';

  const state = {
    view: 'overview',
    summary: null,
    anchors: [],
    settings: null,
    dirty: new Map(),
    filter: 'all',
    search: '',
    logName: 'console',
    logAuto: true,
    settingsGroup: '',
    lastAnchorKey: '',
  };

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  // ------------------------------------------------------------ 基础工具
  const esc = (s) => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

  async function api(path, opts = {}) {
    const res = await fetch(path, {
      headers: { 'Content-Type': 'application/json' },
      ...opts,
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
    if (res.status === 401) {
      toast('需要访问密码，请刷新页面后输入', 'err');
      throw new Error('unauthorized');
    }
    let data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    if (!res.ok) {
      const msg = (data && (data.detail || data.message)) || `请求失败 (${res.status})`;
      throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
    }
    return data;
  }

  function toast(msg, type = 'ok') {
    const el = document.createElement('div');
    el.className = `toast ${type}`;
    el.textContent = msg;
    $('#toast-stack').appendChild(el);
    setTimeout(() => {
      el.style.transition = 'opacity .2s, transform .2s';
      el.style.opacity = '0';
      el.style.transform = 'translateX(16px)';
      setTimeout(() => el.remove(), 220);
    }, 2600);
  }

  const fmtSize = (n) => {
    if (!n && n !== 0) return '-';
    const gb = n / 1024 / 1024 / 1024;
    if (gb >= 1) return gb.toFixed(1) + ' GB';
    const mb = n / 1024 / 1024;
    if (mb >= 1) return mb.toFixed(0) + ' MB';
    return (n / 1024).toFixed(0) + ' KB';
  };

  const PLATFORM_COLOR = {
    '抖音': '#161823', '快手': '#ff5b00', '虎牙': '#ff7d00', '斗鱼': '#ff5d23',
    'B站': '#00a1d6', '小红书': '#ff2442', 'YY': '#1ee0ac', 'TikTok': '#010101',
    'Twitch': '#9146ff', 'YouTube': '#ff0000', '网易CC': '#00c2ff', '微博': '#e6162d',
    '淘宝': '#ff4400', '京东': '#e1251b', '酷狗': '#2a9ff5', '知乎': '#0084ff',
    'CHZZK': '#00e57a', 'Shopee': '#ee4d2d', 'Bigo': '#0f9cf4',
  };
  const colorOf = (name) => PLATFORM_COLOR[name] || '#5b6b82';
  const initialOf = (name) => (name || '?').trim().slice(0, 1).toUpperCase();

  const BADGE = {
    recording: '录制中', live: '直播中', idle: '等待开播',
    disabled: '已停止', unknown: '未知', error: '获取失败', pending: '即将开始',
  };

  // ------------------------------------------------------------ 视图切换
  const VIEW_META = {
    overview: ['概览', '录制进程与主播整体状态'],
    anchors: ['主播管理', '增删主播，单独控制录制与通知'],
    settings: ['全局设置', '对应 config/config.ini，保存后立即生效'],
    logs: ['运行日志', '容器内录制进程的实时输出'],
  };

  function switchView(view) {
    state.view = view;
    $$('.nav-item').forEach((b) => b.classList.toggle('active', b.dataset.view === view));
    $$('.view').forEach((v) => v.classList.toggle('active', v.id === `view-${view}`));
    const [title, sub] = VIEW_META[view] || ['', ''];
    $('#page-title').textContent = title;
    $('#page-sub').textContent = sub;
    if (view === 'settings') { loadSettings(); }
    if (view === 'logs') { loadLogs(); }
    if (view !== 'settings') { $('#save-bar').classList.remove('show'); }
    else if (state.dirty.size) { $('#save-bar').classList.add('show'); }
  }

  // ------------------------------------------------------------ 概览
  function renderSummary() {
    const s = state.summary;
    if (!s) return;
    const p = s.process || {};
    const c = s.counts || {};
    const d = s.disk || {};

    $('#top-pills').innerHTML = `
      <span class="pill pill-process"><i class="dot ${p.alive ? 'on' : 'off'}"></i>录制进程 <b>${p.alive ? '运行中' : '未运行'}</b></span>
      <span class="pill pill-monitor">监测 <b>${p.monitoring || 0}</b></span>
      <span class="pill pill-record">录制 <b>${p.recording || 0}</b></span>
      <span class="pill pill-disk">磁盘剩余 <b>${fmtSize(d.free)}</b></span>
    `;
    $('#ver-text').textContent = p.version || 'v4.0.7';
    $('#nav-anchor-count').textContent = c.total || 0;

    const cards = [
      { label: '主播总数', value: c.total || 0, sub: `其中 ${c.enabled || 0} 个已开启录制`, unit: '位' },
      { label: '正在录制', value: p.recording || 0, sub: p.recording ? 'ffmpeg 正在写入文件' : '当前没有进行中的录制', unit: '路', accent: p.recording ? 'var(--red)' : null },
      { label: '等待开播', value: Math.max((c.enabled || 0) - (p.recording || 0), 0), sub: `轮询间隔见全局设置`, unit: '位' },
      {
        label: '存储空间', value: d.total ? `${(100 - d.percent).toFixed(0)}` : '-', unit: '%',
        sub: d.total ? `已用 ${fmtSize(d.used)} / ${fmtSize(d.total)}` : '未知', bar: d.percent || 0,
      },
    ];
    $('#stat-grid').innerHTML = cards.map((k) => `
      <div class="stat">
        <div class="stat-label">${esc(k.label)}</div>
        <div class="stat-value" ${k.accent ? `style="color:${k.accent}"` : ''}>${esc(k.value)}<small>${esc(k.unit || '')}</small></div>
        <div class="stat-sub">${esc(k.sub || '')}</div>
        ${k.bar != null ? `<div class="stat-bar"><i style="width:${Math.min(k.bar, 100)}%"></i></div>` : ''}
      </div>
    `).join('');

    $('#recording-hint').textContent = p.alive
      ? (p.updated_at ? `心跳 ${p.updated_at}` : '') : '录制进程未运行';
  }

  async function loadFiles() {
    try {
      const data = await api('/api/files');
      const rec = (data.recording || []);
      const box = $('#recording-list');
      if (!rec.length) {
        box.innerHTML = '<div class="mini-empty">当前没有正在录制的直播</div>';
      } else {
        box.innerHTML = rec.map((t) => `
          <div class="mini-item">
            <i class="dot live"></i>
            <span class="name">${esc(t.name || '未命名')}</span>
            <span class="meta">${esc((t.platform || ''))}</span>
          </div>
        `).join('');
      }
      const files = data.items || [];
      $('#file-list').innerHTML = files.length ? files.map((f) => `
        <div class="mini-item playable" data-path="${esc(f.path)}" data-name="${esc(f.name)}" title="点击播放">
          <svg viewBox="0 0 24 24" width="15" height="15" style="fill:var(--muted);flex:0 0 15px"><path d="M8 5v14l11-7z"/></svg>
          <span class="name">${esc(f.name)}</span>
          <span class="meta">${fmtSize(f.size)} · ${esc(f.mtime)}</span>
        </div>
      `).join('') : '<div class="mini-empty">还没有录制文件</div>';
    } catch (e) { /* 忽略轮询错误 */ }
  }

  // ------------------------------------------------------------ 主播列表
  function anchorMatches(a) {
    if (state.filter !== 'all') {
      if (state.filter === 'recording' && a.state !== 'recording') return false;
      if (state.filter === 'live' && a.state !== 'live' && a.state !== 'recording') return false;
      if (state.filter === 'idle' && a.state !== 'idle' && a.state !== 'pending') return false;
      if (state.filter === 'disabled' && a.state !== 'disabled') return false;
    }
    const q = state.search.trim().toLowerCase();
    if (!q) return true;
    return `${a.display_name || ''} ${a.name || ''} ${a.url} ${a.platform}`.toLowerCase().includes(q);
  }

  function anchorRow(a) {
    const on = !!a.enabled;
    const badge = a.state_text || BADGE[a.state] || '未知';
    const live = a.state === 'recording' || a.state === 'live';
    const lastLive = a.last_live_at || '—';
    return `
      <div class="anchor ${on ? '' : 'is-off'}" data-id="${a.id}">
        <div class="col-anchor">
          <div class="avatar" style="background:${colorOf(a.platform)}">${esc(initialOf(a.display_name))}</div>
          <div class="anchor-main">
            <button class="anchor-name" data-act="files" title="查看录制文件">${esc(a.display_name)}</button>
            <div class="anchor-url" title="${esc(a.url)}">${esc(a.url)}</div>
          </div>
        </div>
        <div class="col-platform"><span class="tag">${esc(a.platform || '其他')}</span></div>
        <div class="col-state"><span class="badge ${esc(a.state)}">${live ? '<i class="dot live"></i>' : ''}${esc(badge)}</span></div>
        <div class="col-time">${esc(lastLive)}</div>
        <div class="anchor-controls">
          <label class="toggle-bar ${on ? 'on' : ''}">
            <input type="checkbox" data-act="record" ${on ? 'checked' : ''}>
            <span class="bar-label">录制</span>
            <span class="bar-track"><span class="bar-thumb"></span></span>
          </label>
          <label class="toggle-bar notify ${a.notify ? 'on' : ''}">
            <input type="checkbox" data-act="notify" ${a.notify ? 'checked' : ''}>
            <span class="bar-label">通知</span>
            <span class="bar-track"><span class="bar-thumb"></span></span>
          </label>
          <div class="anchor-actions">
            <button class="icon-btn" data-act="edit" title="修改">
              <svg viewBox="0 0 24 24"><path d="M3 17.25V21h3.75L17.8 9.94l-3.75-3.75L3 17.25zM20.7 7.04a1 1 0 0 0 0-1.41l-2.34-2.34a1 1 0 0 0-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z"/></svg>
            </button>
            <button class="icon-btn danger" data-act="delete" title="删除">
              <svg viewBox="0 0 24 24"><path d="M6 19a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"/></svg>
            </button>
          </div>
        </div>
      </div>`;
  }

  function renderAnchors() {
    const list = state.anchors.filter(anchorMatches);
    const box = $('#anchor-list');
    box.innerHTML = list.map(anchorRow).join('');
    $('#anchor-empty').hidden = state.anchors.length > 0;
    box.hidden = state.anchors.length === 0;
    $('#nav-anchor-count').textContent = state.anchors.length;
    if (state.anchors.length && !list.length) {
      box.innerHTML = '<div class="mini-empty" style="padding:32px;text-align:center">没有符合条件的主播</div>';
    }
  }

  async function loadAnchors(silent = false) {
    try {
      const data = await api('/api/anchors');
      const items = data.items || [];
      state.anchors = items;
      const key = items.map((a) => `${a.id}:${a.enabled}:${a.notify}`).join('|');
      if (silent && key === state.lastAnchorKey) {
        // 结构未变，只更新状态徽章，避免整页重绘打断交互
        items.forEach((a) => {
          const row = $(`.anchor[data-id="${a.id}"]`);
          if (!row) return;
          const b = $('.badge', row);
          if (b) {
            b.className = `badge ${a.state}`;
            b.innerHTML = (a.state === 'recording' || a.state === 'live' ? '<i class="dot live"></i>' : '') +
              esc(a.state_text || BADGE[a.state] || '');
          }
          row.classList.toggle('is-off', !a.enabled);
        });
        return;
      }
      state.lastAnchorKey = key;
      renderAnchors();
    } catch (e) { if (!silent) toast(e.message, 'err'); }
  }

  // ------------------------------------------------------------ 主播操作
  async function toggleAnchor(id, act, value) {
    try {
      await api(`/api/anchors/${id}`, { method: 'PATCH', body: { [act]: value } });
      const a = state.anchors.find((x) => x.id === id);
      if (a) {
        if (act === 'record') { a.enabled = value; a.state = value ? 'pending' : 'disabled'; }
        if (act === 'notify') a.notify = value;
      }
      state.lastAnchorKey = '';
      renderAnchors();
      toast(value ? '已开启' : '已关闭');
      setTimeout(() => refresh(true), 1200);
    } catch (e) {
      toast(e.message, 'err');
      loadAnchors();
    }
  }

  // ------------------------------------------------------------ 弹窗
  function openModal({ title, body, footer, width = 480 }) {
    stopPlayer();
    $('#modal-title').textContent = title;
    $('#modal-body').innerHTML = body;
    $('#modal-foot').innerHTML = footer;
    $('#modal').style.maxWidth = width + 'px';
    $('#modal-mask').hidden = false;
  }
  // 关闭弹窗/切换文件时彻底停止播放：只隐藏容器的话视频会在后台继续出声
  function stopPlayer() {
    const video = document.getElementById('player');
    if (!video) return;
    video.onerror = null;
    try { video.pause(); } catch (e) { /* ignore */ }
    video.removeAttribute('src');
    try { video.load(); } catch (e) { /* ignore */ }
  }

  function closeModal() {
    stopPlayer();
    $('#modal-mask').hidden = true;
  }

  const QUALITY_OPTIONS = ['原画', '超清', '高清', '标清', '流畅'];

  function qualitySelect(id, value) {
    return `<select class="select" id="${id}">${QUALITY_OPTIONS
      .map((q) => `<option value="${q}" ${q === value ? 'selected' : ''}>${q}</option>`).join('')}</select>`;
  }

  function openAddModal() {
    openModal({
      title: '添加主播',
      body: `
        <div class="form-row">
          <label>直播间地址</label>
          <input class="input" id="f-url" placeholder="https://live.douyin.com/123456789" autocomplete="off">
          <div class="form-tip">直接粘贴浏览器里的直播间链接，支持 40+ 平台。</div>
        </div>
        <div class="form-2col">
          <div class="form-row">
            <label>备注名称（可留空）</label>
            <input class="input" id="f-name" placeholder="留空则自动识别昵称" autocomplete="off">
          </div>
          <div class="form-row">
            <label>画质</label>
            ${qualitySelect('f-quality', '原画')}
          </div>
        </div>
        <div class="switch-row">
          <span>添加后立即开始监测录制</span>
          <label class="switch"><input type="checkbox" id="f-enabled" checked><i></i></label>
        </div>
        <div class="switch-row">
          <span>开播时推送通知</span>
          <label class="switch"><input type="checkbox" id="f-notify" checked><i></i></label>
        </div>`,
      footer: `
        <button class="btn ghost" onclick="__closeModal()">取消</button>
        <button class="btn primary" id="f-submit">添加</button>`,
    });
    setTimeout(() => $('#f-url').focus(), 60);
    $('#f-submit').onclick = async () => {
      const url = $('#f-url').value.trim();
      if (!url) { toast('请填写直播间地址', 'err'); return; }
      const btn = $('#f-submit');
      btn.disabled = true; btn.textContent = '添加中…';
      try {
        await api('/api/anchors', {
          method: 'POST',
          body: {
            url,
            name: $('#f-name').value.trim(),
            quality: $('#f-quality').value,
            enabled: $('#f-enabled').checked,
            notify: $('#f-notify').checked,
          },
        });
        closeModal();
        toast('已添加，正在排队监测');
        state.lastAnchorKey = '';
        await loadAnchors();
        refresh(true);
      } catch (e) {
        toast(e.message, 'err');
        btn.disabled = false; btn.textContent = '添加';
      }
    };
  }

  function openBatchModal() {
    openModal({
      title: '批量添加',
      width: 560,
      body: `
        <div class="form-row">
          <label>每行一个直播间，可写成「备注,地址」</label>
          <textarea class="textarea" id="f-lines" style="min-height:150px" placeholder="https://live.douyin.com/123456
小美,https://live.kuaishou.com/u/abc"></textarea>
        </div>
        <div class="switch-row">
          <span>添加后立即开始监测录制</span>
          <label class="switch"><input type="checkbox" id="f-b-enabled" checked><i></i></label>
        </div>
        <div class="switch-row">
          <span>开播时推送通知</span>
          <label class="switch"><input type="checkbox" id="f-b-notify" checked><i></i></label>
        </div>`,
      footer: `
        <button class="btn ghost" onclick="__closeModal()">取消</button>
        <button class="btn primary" id="f-b-submit">批量添加</button>`,
    });
    $('#f-b-submit').onclick = async () => {
      const text = $('#f-lines').value.trim();
      if (!text) { toast('请粘贴地址', 'err'); return; }
      try {
        const r = await api('/api/anchors/batch', {
          method: 'POST',
          body: { text, enabled: $('#f-b-enabled').checked, notify: $('#f-b-notify').checked },
        });
        closeModal();
        toast(`成功添加 ${r.added} 个${r.failed && r.failed.length ? `，${r.failed.length} 个失败` : ''}`);
        state.lastAnchorKey = '';
        await loadAnchors();
      } catch (e) { toast(e.message, 'err'); }
    };
  }

  function openEditModal(a) {
    openModal({
      title: '编辑主播',
      body: `
        <div class="form-row">
          <label>备注名称</label>
          <input class="input" id="e-name" value="${esc(a.name || '')}" placeholder="留空则使用平台昵称">
        </div>
        <div class="form-row">
          <label>直播间地址</label>
          <input class="input" id="e-url" value="${esc(a.url)}">
        </div>
        <div class="form-2col">
          <div class="form-row">
            <label>画质</label>
            ${qualitySelect('e-quality', a.quality || '原画')}
          </div>
          <div class="form-row">
            <label>平台</label>
            <input class="input" value="${esc(a.platform)}" disabled>
          </div>
        </div>`,
      footer: `
        <button class="btn ghost" onclick="__closeModal()">取消</button>
        <button class="btn primary" id="e-submit">保存</button>`,
    });
    $('#e-submit').onclick = async () => {
      try {
        await api(`/api/anchors/${a.id}`, {
          method: 'PATCH',
          body: { name: $('#e-name').value.trim(), url: $('#e-url').value.trim(), quality: $('#e-quality').value },
        });
        closeModal();
        toast('已保存');
        state.lastAnchorKey = '';
        await loadAnchors();
      } catch (e) { toast(e.message, 'err'); }
    };
  }

  // ------------------------------------------------------------ 主播录像
  async function openFilesModal(a) {
    stopPlayer();
    openModal({
      title: `${a.display_name} · 录制文件`,
      width: 960,
      body: '<div id="files-body"><div class="mini-empty" style="padding:24px;text-align:center">正在扫描录像目录…</div></div>',
      footer: '<button class="btn ghost" onclick="__closeModal()">关闭</button>',
    });
    try {
      const data = await api(`/api/anchors/${a.id}/files`);
      renderFiles(data);
    } catch (e) {
      $('#files-body').innerHTML = `<div class="mini-empty" style="padding:24px;text-align:center">${esc(e.message)}</div>`;
    }
  }

  function openPlayerModal(file) {
    const name = file.name || '播放';
    const ext = (name.split('.').pop() || '').toLowerCase();
    const needRemux = ['ts', 'mkv', 'flv'].includes(ext);
    const url = `/api/media?path=${encodeURIComponent(file.path)}`;
    openModal({
      title: name,
      width: 960,
      body: `<div class="player-wrap">
          <video id="player" controls playsinline preload="metadata"></video>
          <div class="player-meta">
            <span class="player-file">${esc(name)}</span>
            <span class="player-tip" id="player-tip"${needRemux ? '' : ' hidden'}>首次播放 ts / mkv 需要转封装，大文件请稍等几秒</span>
          </div>
        </div>`,
      footer: `<a class="btn ghost" href="${url}" download="${esc(name)}">下载</a>
               <button class="btn primary" onclick="__closeModal()">关闭</button>`,
    });
    const video = $('#player');
    if (!video) return;
    video.onerror = () => toast('这个文件无法在线播放，可以点「下载」后用本地播放器打开', 'err');
    video.src = url;
    video.load();
    const p = video.play();
    if (p && p.catch) p.catch(() => {});
  }

  const EXT_LABEL = { ts: 'TS', mp4: 'MP4', mkv: 'MKV', flv: 'FLV', m4a: 'M4A', mp3: 'MP3', webm: 'WEBM', mov: 'MOV' };

  function renderFiles(data) {
    const items = data.items || [];
    const names = data.names || [];
    let list = '';
    if (!items.length) {
      const reason = names.length
        ? `按昵称「${names.map((n) => esc(n)).join(' / ')}」在 downloads 目录里没找到录像`
        : '还没拿到主播昵称，等录制器抓到名字后再来看';
      list = `<div class="mini-empty" style="padding:28px;text-align:center">
        还没有这个主播的录像文件
        <div style="font-size:12px;margin-top:6px">${reason}</div>
      </div>`;
    } else {
      list = `<div class="files-summary">共 ${items.length} 个文件 · ${fmtSize(data.total_size)}</div>
        <div class="file-items">${items.map((f) => `
          <div class="file-item" data-path="${esc(f.path)}" data-ext="${esc(f.ext)}" data-name="${esc(f.name)}">
            <span class="file-ext">${esc(EXT_LABEL[f.ext] || f.ext.toUpperCase())}</span>
            <span class="file-name" title="${esc(f.path)}">${esc(f.name)}</span>
            <span class="file-meta">${fmtSize(f.size)} · ${esc(f.mtime)}</span>
            <button class="btn primary file-play" data-act="play">播放</button>
            <a class="icon-btn file-dl" href="/api/media?path=${encodeURIComponent(f.path)}" download="${esc(f.name)}" title="下载">
              <svg viewBox="0 0 24 24"><path d="M12 16l-5-5h3V4h4v7h3l-5 5zM5 18h14v2H5z"/></svg>
            </a>
          </div>`).join('')}</div>`;
    }

    const body = $('#files-body');
    body.innerHTML = `<div class="player-wrap" id="player-wrap" hidden>
        <video id="player" controls playsinline preload="metadata"></video>
        <div class="player-meta">
          <span id="player-name"></span>
          <span class="player-tip" id="player-tip" hidden>首次播放 ts / mkv 需要转封装，大文件请稍等几秒</span>
        </div>
      </div>` + list;

    body.onclick = (e) => {
      const item = e.target.closest('.file-item');
      if (!item) return;
      if (e.target.closest('[data-act="play"]') || e.target.closest('.file-name')) {
        playFile(item.dataset.path, item.dataset.ext, item.dataset.name);
      }
    };
  }

  function playFile(path, ext, name) {
    const wrap = $('#player-wrap');
    const video = $('#player');
    if (!wrap || !video) return;
    wrap.hidden = false;
    $('#player-name').textContent = name;
    $('#player-tip').hidden = !['ts', 'mkv', 'flv'].includes(ext);
    video.onerror = () => toast('这个文件无法在线播放，可以点右侧下载后用本地播放器打开', 'err');
    try { video.pause(); } catch (e) { /* ignore */ }
    video.src = `/api/media?path=${encodeURIComponent(path)}`;
    video.load();
    const p = video.play();
    if (p && p.catch) p.catch(() => {});
    if (typeof wrap.scrollIntoView === 'function') {
      wrap.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
  }

  function confirmDelete(a) {
    openModal({
      title: '删除主播',
      body: `<p style="margin:0;color:var(--text-2);font-size:13px">
        将删除 <b style="color:var(--text)">${esc(a.display_name)}</b> 并停止监测。
        如果它正在录制，会先安全收尾当前视频再移除。</p>`,
      footer: `
        <button class="btn ghost" onclick="__closeModal()">取消</button>
        <button class="btn danger" id="d-submit">确认删除</button>`,
    });
    $('#d-submit').onclick = async () => {
      try {
        await api(`/api/anchors/${a.id}`, { method: 'DELETE' });
        closeModal();
        toast('已删除');
        state.lastAnchorKey = '';
        await loadAnchors();
      } catch (e) { toast(e.message, 'err'); }
    };
  }

  // ------------------------------------------------------------ 设置
  const GROUP_ICON = { record: '', network: '', push: '', account: '', cookie: '', advanced: '', other: '' };

  function fieldControl(f) {
    const id = `f-${encodeURIComponent(f.section)}-${encodeURIComponent(f.key)}`.replace(/%/g, '_');
    const v = f.value == null ? '' : String(f.value);
    const wide = f.type === 'textarea';
    let control = '';
    if (f.type === 'bool') {
      control = `<div class="switch-row" style="justify-content:flex-end">
        <label class="switch"><input type="checkbox" data-field="${esc(id)}" ${v === '是' ? 'checked' : ''}><i></i></label>
      </div>`;
    } else if (f.type === 'tags') {
      // 多选字段必须排在 select 之前，否则会被 options 条件截获渲染成下拉框
      const cur = v.split(/[,，]/).map((s) => s.trim()).filter(Boolean);
      control = `<div class="chips">${(f.options || []).map((o) => `
        <label><input type="checkbox" data-tags="${esc(id)}" value="${esc(o)}" ${cur.includes(o) ? 'checked' : ''}><span>${esc(o)}</span></label>`).join('')}</div>`;
    } else if (f.type === 'select' || (f.options && f.options.length)) {
      const opts = f.options.length ? f.options : [v];
      const placeholder = opts.includes(v) ? '' : '<option value="" selected>（未设置）</option>';
      control = `<select class="select" data-field="${esc(id)}">${placeholder}${opts.map((o) => `<option value="${esc(o)}" ${o === v ? 'selected' : ''}>${esc(o)}</option>`).join('')}</select>`;
    } else if (f.type === 'textarea') {
      control = `<textarea class="textarea" data-field="${esc(id)}" spellcheck="false">${esc(v)}</textarea>`;
    } else if (f.type === 'number') {
      control = `<input class="input" type="number" step="any" data-field="${esc(id)}" value="${esc(v)}">`;
    } else if (f.type === 'password') {
      control = `<input class="input" type="password" data-field="${esc(id)}" value="${esc(v)}" autocomplete="new-password">`;
    } else {
      control = `<input class="input" type="text" data-field="${esc(id)}" value="${esc(v)}">`;
    }
    return { id, control, wide };
  }

  function renderSettings() {
    const s = state.settings;
    if (!s) return;
    const groups = s.groups.filter((g) => s.fields.some((f) => f.group === g.id));
    if (!state.settingsGroup || !groups.some((g) => g.id === state.settingsGroup)) {
      state.settingsGroup = groups[0] ? groups[0].id : '';
    }

    $('#settings-nav').innerHTML = groups.map((g) => {
      const n = s.fields.filter((f) => f.group === g.id).length;
      return `<button data-group="${g.id}" class="${g.id === state.settingsGroup ? 'active' : ''}">
        ${esc(g.title)}<small>${n}</small></button>`;
    }).join('');

    $('#settings-groups').innerHTML = groups.map((g) => {
      const fields = s.fields.filter((f) => f.group === g.id);
      return `<div class="settings-group ${g.id === state.settingsGroup ? 'active' : ''}" data-group="${g.id}">
        <div class="group-head"><h2>${esc(g.title)}</h2><p>${esc(g.desc)}</p></div>
        ${fields.map((f) => {
          const { id, control, wide } = fieldControl(f);
          return `<div class="field ${wide ? 'wide' : ''}" data-fid="${esc(id)}" data-section="${esc(f.section)}" data-key="${esc(f.key)}" data-type="${esc(f.type)}">
            <div class="field-label">
              <strong>${esc(f.label)}</strong>
              ${f.help ? `<p>${esc(f.help)}</p>` : ''}
              <span class="field-key">${esc(f.section)} · ${esc(f.key)}</span>
            </div>
            <div class="field-control">${control}</div>
          </div>`;
        }).join('')}
      </div>`;
    }).join('');

    state.dirty.clear();
    updateSaveBar();
  }

  function fieldValueFromControl(el) {
    if (el.type === 'checkbox' && el.dataset.field) return el.checked ? '是' : '否';
    return el.value;
  }

  function markDirty(el) {
    const field = el.closest('.field');
    if (!field) return;
    const section = field.dataset.section;
    const key = field.dataset.key;
    let value;
    if (el.dataset.tags) {
      const checked = $$(`[data-tags="${el.dataset.tags}"]:checked`, field).map((c) => c.value);
      value = checked.join(',');
    } else {
      value = fieldValueFromControl(el);
    }
    state.dirty.set(`${section}||${key}`, { section, key, value });
    field.classList.add('dirty');
    updateSaveBar();
  }

  function updateSaveBar() {
    const n = state.dirty.size;
    const bar = $('#save-bar');
    const btn = $('#btn-save');
    btn.disabled = n === 0;
    btn.textContent = n ? `保存设置（${n}）` : '保存设置';
    $('#save-hint').textContent = n
      ? `有 ${n} 项修改待保存，保存后录制器会在下一轮检测时生效`
      : '所有修改保存在容器内 config/config.ini';
    if (state.view === 'settings') bar.classList.toggle('show', n > 0);
  }

  async function loadSettings(force = false) {
    if (state.settings && !force && !state.dirty.size) { renderSettings(); return; }
    if (state.dirty.size && !force) return;
    try {
      state.settings = await api('/api/settings');
      renderSettings();
    } catch (e) { toast(e.message, 'err'); }
  }

  async function saveSettings() {
    if (!state.dirty.size) return;
    const items = Array.from(state.dirty.values());
    const btn = $('#btn-save');
    btn.disabled = true; btn.textContent = '保存中…';
    try {
      const r = await api('/api/settings', { method: 'POST', body: { items } });
      toast(`已保存 ${r.updated || items.length} 项设置`);
      state.dirty.clear();
      state.settings = r.schema || state.settings;
      renderSettings();
    } catch (e) {
      toast(e.message, 'err');
      updateSaveBar();
    }
  }

  // ------------------------------------------------------------ 日志
  async function loadLogs() {
    try {
      const lines = $('#log-lines').value || 500;
      const data = await api(`/api/logs?name=${encodeURIComponent(state.logName)}&lines=${lines}`);
      const box = $('#log-content');
      if (!data.exists) {
        box.textContent = `日志文件暂不存在：${data.path}\n\n录制进程启动并产生输出后即可在此查看。`;
        return;
      }
      const nearBottom = isNearBottom($('#log-box'));
      box.textContent = (data.lines || []).join('\n') || '(空)';
      if (nearBottom) {
        const el = $('#log-box');
        el.scrollTop = el.scrollHeight;
      }
    } catch (e) { /* 静默 */ }
  }

  function isNearBottom(el) {
    return el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  }

  // ------------------------------------------------------------ 刷新
  async function refresh(silent = false) {
    try {
      state.summary = await api('/api/summary');
      renderSummary();
    } catch (e) { if (!silent) toast(e.message, 'err'); }
    if (state.view === 'overview') loadFiles();
    await loadAnchors(silent);
  }

  // ------------------------------------------------------------ 主题
  function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem('dlr-theme', theme);
  }

  // ------------------------------------------------------------ 事件绑定
  function bind() {
    $('#nav').addEventListener('click', (e) => {
      const btn = e.target.closest('.nav-item');
      if (btn) switchView(btn.dataset.view);
    });

    $('#theme-toggle').addEventListener('click', () => {
      const cur = document.documentElement.dataset.theme;
      applyTheme(cur === 'dark' ? 'light' : 'dark');
    });

    $('#btn-add').addEventListener('click', openAddModal);
    $('#btn-add-empty') && $('#btn-add-empty').addEventListener('click', openAddModal);
    $('#btn-batch').addEventListener('click', openBatchModal);
    $('#modal-close').addEventListener('click', closeModal);
    $('#modal-mask').addEventListener('click', (e) => { if (e.target.id === 'modal-mask') closeModal(); });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeModal(); });
    window.__closeModal = closeModal;

    const fileList = $('#file-list');
    if (fileList) {
      fileList.addEventListener('click', (e) => {
        const item = e.target.closest('.mini-item.playable');
        if (!item) return;
        openPlayerModal({ name: item.dataset.name, path: item.dataset.path });
      });
    }

    $('#anchor-search').addEventListener('input', (e) => {
      state.search = e.target.value;
      renderAnchors();
    });

    $('#filter-row').addEventListener('click', (e) => {
      const chip = e.target.closest('.chip');
      if (!chip) return;
      state.filter = chip.dataset.filter;
      $$('.chip').forEach((c) => c.classList.toggle('active', c === chip));
      renderAnchors();
    });

    $('#anchor-list').addEventListener('click', (e) => {
      const row = e.target.closest('.anchor');
      if (!row) return;
      const id = row.dataset.id;
      const anchor = state.anchors.find((a) => a.id === id);
      const act = e.target.closest('[data-act]');
      if (!act) return;
      if (act.dataset.act === 'files') openFilesModal(anchor);
      if (act.dataset.act === 'edit') openEditModal(anchor);
      if (act.dataset.act === 'delete') confirmDelete(anchor);
    });

    $('#anchor-list').addEventListener('change', (e) => {
      const input = e.target.closest('input[data-act]');
      if (!input) return;
      const row = input.closest('.anchor');
      const act = input.dataset.act;
      const value = input.checked;
      if (act === 'record') toggleAnchor(row.dataset.id, 'enabled', value);
      if (act === 'notify') toggleAnchor(row.dataset.id, 'notify', value);
    });

    $('#settings-nav').addEventListener('click', (e) => {
      const btn = e.target.closest('button[data-group]');
      if (!btn) return;
      state.settingsGroup = btn.dataset.group;
      $$('#settings-nav button').forEach((b) => b.classList.toggle('active', b === btn));
      $$('.settings-group').forEach((g) => g.classList.toggle('active', g.dataset.group === state.settingsGroup));
    });

    $('#settings-groups').addEventListener('input', (e) => {
      if (e.target.matches('[data-field],[data-tags]')) markDirty(e.target);
    });
    $('#settings-groups').addEventListener('change', (e) => {
      if (e.target.matches('[data-field],[data-tags]')) markDirty(e.target);
    });

    $('#btn-save').addEventListener('click', saveSettings);
    $('#btn-reset').addEventListener('click', () => {
      if (!state.dirty.size) return;
      state.dirty.clear();
      loadSettings(true);
      toast('已撤销未保存的修改');
    });

    $('#log-tabs').addEventListener('click', (e) => {
      const seg = e.target.closest('.seg');
      if (!seg) return;
      state.logName = seg.dataset.log;
      $$('.seg').forEach((s) => s.classList.toggle('active', s === seg));
      loadLogs();
    });
    $('#log-lines').addEventListener('change', loadLogs);
    $('#btn-log-refresh').addEventListener('click', loadLogs);
    $('#log-auto').addEventListener('change', (e) => { state.logAuto = e.target.checked; });
  }

  // ------------------------------------------------------------ 启动
  function boot() {
    const saved = localStorage.getItem('dlr-theme');
    applyTheme(saved || (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'));
    bind();
    refresh();

    setInterval(() => {
      if (document.hidden) return;
      refresh(true);
      if (state.view === 'logs' && state.logAuto) loadLogs();
    }, 4000);

    setInterval(() => {
      if (state.view === 'overview' && !document.hidden) loadFiles();
    }, 10000);
  }

  document.addEventListener('DOMContentLoaded', boot);
})();
