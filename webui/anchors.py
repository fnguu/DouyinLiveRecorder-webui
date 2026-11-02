# -*- encoding: utf-8 -*-
"""主播列表管理。

事实来源是 config/URL_config.ini（录制器只认这个文件），
webui_state.json 只保存文件里表达不了的元数据：每个主播的通知开关、备注、排序。
行级增量修改，避免整体重写覆盖用户手写的其它行。
"""
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime

from .config_io import _backup, _read_lines, _write_lines, _LOCK
from .paths import STATE_FILE, URL_CONFIG_FILE, build_url_line, detect_platform, norm_url, split_url_line

_STATE_LOCK = threading.RLock()
_OP_LOCK = threading.RLock()
_QUALITIES = ('原画', '超清', '高清', '标清', '流畅')


# ---------------------------------------------------------------- state 文件
def _empty_state() -> dict:
    return {'version': 1, 'anchors': [], 'deleting': [], 'updated_at': ''}


def load_state() -> dict:
    with _STATE_LOCK:
        if not os.path.exists(STATE_FILE):
            return _empty_state()
        try:
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except (OSError, ValueError):
            return _empty_state()
        data.setdefault('anchors', [])
        data.setdefault('deleting', [])
        return data


def save_state(state: dict) -> None:
    with _STATE_LOCK:
        state['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        tmp = STATE_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_FILE)


# ---------------------------------------------------------------- 文件行操作
def _read_entries() -> list:
    """读取 URL_config.ini -> [{line_index, line, commented, quality, url, name}]"""
    if not os.path.exists(URL_CONFIG_FILE):
        return []
    entries = []
    for idx, line in enumerate(_read_lines(URL_CONFIG_FILE)):
        parsed = split_url_line(line)
        if not parsed:
            continue
        commented, quality, url, name = parsed
        if not url or len(url) < 8 or url.startswith('//'):
            continue
        entries.append({
            'line_index': idx, 'line': line, 'commented': commented,
            'quality': quality, 'url': url, 'name': name,
        })
    return entries


def _write_entries_line(url: str, new_line: str) -> bool:
    """把匹配 url 的那一行替换为 new_line（保留行位置）。"""
    with _LOCK:
        lines = _read_lines(URL_CONFIG_FILE)
        changed = False
        for i, line in enumerate(lines):
            parsed = split_url_line(line)
            if not parsed:
                continue
            if norm_url(parsed[2]) == norm_url(url):
                if lines[i] != new_line:
                    lines[i] = new_line
                    changed = True
                break
        if changed:
            _backup(URL_CONFIG_FILE)
            _write_lines(URL_CONFIG_FILE, lines)
        return changed


def _remove_entry_line(url: str) -> bool:
    with _LOCK:
        lines = _read_lines(URL_CONFIG_FILE)
        keep, removed = [], False
        target = norm_url(url)
        for line in lines:
            parsed = split_url_line(line)
            if parsed and norm_url(parsed[2]) == target:
                removed = True
                continue
            keep.append(line)
        if removed:
            _backup(URL_CONFIG_FILE)
            _write_lines(URL_CONFIG_FILE, keep)
        return removed


def _append_line(line: str) -> None:
    with _LOCK:
        lines = _read_lines(URL_CONFIG_FILE)
        while lines and not lines[-1].strip():
            lines.pop()
        lines.append(line)
        _backup(URL_CONFIG_FILE)
        _write_lines(URL_CONFIG_FILE, lines)


def _locked(fn):
    """串行化"改文件 + 改 state"的组合操作，避免并发请求互相踩踏。"""
    def wrapper(*args, **kwargs):
        with _OP_LOCK:
            return fn(*args, **kwargs)
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


# ---------------------------------------------------------------- 同步
@_locked
def sync_from_file(state: dict | None = None) -> dict:
    """以 URL_config.ini 为准，补全/清理 state 中的主播。"""
    state = state or load_state()
    entries = _read_entries()
    by_norm = {}
    for a in state['anchors']:
        by_norm[norm_url(a['url'])] = a

    deleting = {norm_url(u) for u in state.get('deleting', [])}
    seen = set()
    ordered = []
    for e in entries:
        n = norm_url(e['url'])
        if n in deleting or n in seen:
            continue
        seen.add(n)
        anchor = by_norm.get(n)
        if anchor is None:
            anchor = {
                'id': uuid.uuid4().hex[:10],
                'url': e['url'],
                'name': e['name'] or '',
                'quality': e['quality'],
                'enabled': not e['commented'],
                'notify': True,
                'danmaku': True,
                'platform': detect_platform(e['url']),
                'note': '',
                'added_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            }
        else:
            anchor['enabled'] = not e['commented']
            anchor['url'] = e['url']
            if not anchor.get('name'):
                anchor['name'] = e['name'] or ''
            anchor['quality'] = e['quality'] or anchor.get('quality') or '原画'
            anchor.setdefault('platform', detect_platform(e['url']))
        ordered.append(anchor)

    # 保留 state 中的展示顺序
    order = {a['id']: i for i, a in enumerate(state['anchors'])}
    ordered.sort(key=lambda a: order.get(a['id'], 999))

    if ordered != state['anchors']:
        state['anchors'] = ordered
        save_state(state)
    return state


def _find(state: dict, anchor_id: str):
    for a in state['anchors']:
        if a['id'] == anchor_id:
            return a
    return None


# ---------------------------------------------------------------- 对外操作
def list_anchors() -> list:
    state = sync_from_file()
    return state['anchors']


@_locked
def add_anchor(url: str, name: str = '', quality: str = '', enabled: bool = True,
               notify: bool = True, note: str = '', danmaku: bool = True) -> dict:
    url = (url or '').strip()
    if not url:
        raise ValueError('直播间地址不能为空')
    if '://' not in url:
        url = 'https://' + url
    if not re.search(r'[\w-]+\.[\w.]+', url):
        raise ValueError('直播间地址格式不正确')

    state = sync_from_file()
    if any(norm_url(a['url']) == norm_url(url) for a in state['anchors']):
        raise ValueError('该直播间已在列表中')

    quality = quality if quality in _QUALITIES else ''
    anchor = {
        'id': uuid.uuid4().hex[:10],
        'url': url,
        'name': (name or '').strip(),
        'quality': quality or '',
        'enabled': bool(enabled),
        'notify': bool(notify),
        'danmaku': bool(danmaku),
        'platform': detect_platform(url),
        'note': (note or '').strip(),
        'added_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }
    default_quality = _default_quality()
    anchor['quality'] = anchor['quality'] or default_quality
    _append_line(build_url_line(anchor['quality'], anchor['url'], anchor['name'], anchor['enabled']))
    state['anchors'].append(anchor)
    save_state(state)
    return anchor


@_locked
def update_anchor(anchor_id: str, payload: dict) -> dict:
    state = sync_from_file()
    anchor = _find(state, anchor_id)
    if not anchor:
        raise ValueError('主播不存在')

    new_line = None
    old_url = anchor['url']
    old_enabled = anchor['enabled']

    if 'name' in payload:
        anchor['name'] = str(payload['name'] or '').strip()
    if 'note' in payload:
        anchor['note'] = str(payload['note'] or '').strip()
    if 'notify' in payload:
        anchor['notify'] = bool(payload['notify'])
    if 'danmaku' in payload:
        anchor['danmaku'] = bool(payload['danmaku'])
    if 'enabled' in payload:
        anchor['enabled'] = bool(payload['enabled'])
    if 'quality' in payload and payload['quality']:
        anchor['quality'] = payload['quality'] if payload['quality'] in _QUALITIES else anchor['quality']
    if 'url' in payload and payload['url']:
        anchor['url'] = str(payload['url']).strip()
        if '://' not in anchor['url']:
            anchor['url'] = 'https://' + anchor['url']
        anchor['platform'] = detect_platform(anchor['url'])

    need_rewrite = ('name' in payload or 'quality' in payload or 'url' in payload or
                    'enabled' in payload)
    if need_rewrite:
        new_line = build_url_line(anchor['quality'], anchor['url'], anchor['name'], anchor['enabled'])
        if old_url != anchor['url']:
            _remove_entry_line(old_url)
            _append_line(new_line)
        else:
            _write_entries_line(old_url, new_line)

    save_state(state)

    # 线程启动时把「画质 / 名义名称」做成了快照，改了名字或画质要让它重建线程才生效。
    # 仅在未开播时重建，避免打断正在写入的视频文件。
    if ('quality' in payload or 'name' in payload or 'url' in payload) and anchor['enabled']:
        if _thread_status(anchor['url']) not in ('recording', 'live'):
            _restart_monitor_thread(anchor)

    # 由"停用"切到"启用"时，原本的监测线程可能已经退出；
    # 这里不动文件，主循环会重新为它拉起线程并立即检测一次。
    if old_enabled != anchor['enabled'] and anchor['enabled']:
        anchor['just_enabled'] = True
    return anchor


def _thread_status(url: str) -> str:
    from .runtime import read_runtime
    try:
        for t in read_runtime().get('threads', []):
            if norm_url(t.get('url', '')) == norm_url(url):
                return t.get('status', '')
    except Exception:
        pass
    return ''


def _restart_monitor_thread(anchor: dict) -> None:
    """注释 1 秒再恢复：让录制器丢弃旧的线程快照，按新配置重建。"""
    url = anchor['url']
    try:
        _write_entries_line(url, build_url_line(anchor['quality'], url, anchor['name'], False))
        time.sleep(1.0)
        _write_entries_line(url, build_url_line(anchor['quality'], url, anchor['name'], True))
    except OSError:
        pass


@_locked
def delete_anchor(anchor_id: str) -> dict:
    state = sync_from_file()
    anchor = _find(state, anchor_id)
    if not anchor:
        raise ValueError('主播不存在')
    url = anchor['url']
    norm = norm_url(url)

    # 先注释：录制中的线程会在 1~2 秒内给 ffmpeg 发 SIGINT 安全收尾后退出；
    # 等待中的线程会在下一轮检测时退出。确认退出后再物理删除该行。
    _write_entries_line(url, build_url_line(anchor['quality'], url, anchor['name'], False))

    state['anchors'] = [a for a in state['anchors'] if a['id'] != anchor_id]
    if norm not in [norm_url(u) for u in state['deleting']]:
        state['deleting'].append(url)
    save_state(state)

    threading.Thread(target=_finalize_delete, args=(url,), daemon=True).start()
    return {'ok': True, 'id': anchor_id}


def _finalize_delete(url: str, timeout: int = 180) -> None:
    """等待录制线程退出后删除注释行，避免留下僵尸线程。"""
    from .runtime import read_runtime

    norm = norm_url(url)
    deadline = time.time() + timeout
    while time.time() < deadline:
        rt = read_runtime()
        running = {norm_url(u) for u in rt.get('running', [])}
        busy = norm in running or any(norm == norm_url(st.get('url', ''))
                                      for st in rt.get('threads', []))
        if not busy:
            break
        time.sleep(3)
    _remove_entry_line(url)
    state = load_state()
    state['deleting'] = [u for u in state.get('deleting', []) if norm_url(u) != norm]
    save_state(state)


def set_recording(anchor_id: str, enabled: bool) -> dict:
    return update_anchor(anchor_id, {'enabled': bool(enabled)})


def notify_targets() -> dict:
    """给录制进程用：{norm_url: notify}"""
    state = load_state()
    return {norm_url(a['url']): bool(a.get('notify', True)) for a in state.get('anchors', [])}


def danmaku_targets() -> dict:
    """给录制进程用：{norm_url: 是否录制弹幕}"""
    state = load_state()
    return {norm_url(a['url']): bool(a.get('danmaku', True)) for a in state.get('anchors', [])}


def _default_quality() -> str:
    try:
        from .config_io import get_value
        q = get_value('录制设置', '原画|超清|高清|标清|流畅', '原画')
        return q if q in _QUALITIES else '原画'
    except Exception:
        return '原画'
