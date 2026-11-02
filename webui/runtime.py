# -*- encoding: utf-8 -*-
"""运行时状态读取：录制进程心跳、日志、磁盘。"""
import json
import os
import re
import time
from collections import deque

from .paths import DEFAULT_DOWNLOAD_DIR, LOG_DIR, RUNTIME_FILE

_cache = {'ts': 0.0, 'data': None}
LOG_FILES = {
    'console': ('recorder.log', '运行输出'),
    'play': ('PlayURL.log', '直播状态'),
    'debug': ('streamget.log', '调试日志'),
}


def read_runtime(force: bool = False) -> dict:
    now = time.time()
    if not force and _cache['data'] is not None and now - _cache['ts'] < 1.0:
        return _cache['data']
    data = {'alive': False, 'running': [], 'threads': [], 'monitoring': 0,
            'error_count': 0, 'updated_at': '', 'pid': 0, 'version': ''}
    try:
        if os.path.exists(RUNTIME_FILE):
            with open(RUNTIME_FILE, 'r', encoding='utf-8') as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                data.update(raw)
            beat = raw.get('beat', 0) if isinstance(raw, dict) else 0
            data['alive'] = bool(beat) and (now - float(beat) < 25)
    except (OSError, ValueError):
        pass
    _cache.update(ts=now, data=data)
    return data


def thread_map() -> dict:
    """{norm_url: {status,name,file}}，供主播列表叠加实时状态。"""
    from .paths import norm_url
    rt = read_runtime()
    result = {}
    for t in rt.get('threads', []):
        url = t.get('url') or ''
        if url:
            result[norm_url(url)] = t
    return result


def disk_info(path: str = '') -> dict:
    target = path or DEFAULT_DOWNLOAD_DIR
    try:
        usage = os.statvfs(target) if hasattr(os, 'statvfs') else None
    except OSError:
        usage = None
    try:
        if usage is not None:
            total = usage.f_blocks * usage.f_frsize
            free = usage.f_bavail * usage.f_frsize
        else:  # Windows / 不支持 statvfs
            import shutil
            total, _, free = shutil.disk_usage(target)
        used = total - free
        return {
            'path': target,
            'total': total, 'free': free, 'used': used,
            'percent': round(used / total * 100, 1) if total else 0,
        }
    except OSError:
        return {'path': target, 'total': 0, 'free': 0, 'used': 0, 'percent': 0}


def _log_path(name: str) -> str:
    fname = LOG_FILES.get(name, LOG_FILES['console'])[0]
    return os.path.join(LOG_DIR, fname)


def tail_log(name: str = 'console', lines: int = 300) -> dict:
    path = _log_path(name)
    lines = max(20, min(int(lines or 300), 3000))
    if not os.path.exists(path):
        return {'name': name, 'path': path, 'lines': [], 'size': 0, 'exists': False}
    size = os.path.getsize(path)
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        buf = deque(f, maxlen=lines)
    return {'name': name, 'path': path, 'lines': [l.rstrip('\n') for l in buf],
            'size': size, 'exists': True, 'files': list(LOG_FILES.keys())}


MEDIA_EXTS = ('.ts', '.mp4', '.mkv', '.flv', '.m4a', '.mp3', '.mov', '.webm')
# 弹幕与字幕产物（不能播放，只提供下载）
SUBTITLE_EXTS = ('.xml', '.srt', '.ass', '.jsonl')
_NAME_STRIP = re.compile(r'[\\/:*?"<>|&#.。,， ~！·]')


def clean_anchor_name(name: str) -> str:
    """按录制器的文件名规则清洗主播名，用于和磁盘上的文件比对。"""
    if not name:
        return ''
    return _NAME_STRIP.sub('_', name.strip()).strip('_')


def _iter_media(max_depth: int = 3):
    if not os.path.isdir(DEFAULT_DOWNLOAD_DIR):
        return
    for root, _dirs, names in os.walk(DEFAULT_DOWNLOAD_DIR):
        depth = root[len(DEFAULT_DOWNLOAD_DIR):].count(os.sep)
        if depth > max_depth:
            continue
        for n in names:
            if not n.lower().endswith(MEDIA_EXTS):
                continue
            p = os.path.join(root, n)
            try:
                st = os.stat(p)
            except OSError:
                continue
            yield p, n, st


def recent_files(limit: int = 8) -> list:
    """最近生成的录像文件，用于首页概览。"""
    files = []
    for p, n, st in _iter_media():
        files.append({
            'name': n,
            'path': os.path.relpath(p, DEFAULT_DOWNLOAD_DIR).replace('\\', '/'),
            'size': st.st_size,
            'mtime': time.strftime('%m-%d %H:%M', time.localtime(st.st_mtime)),
            'mtime_ts': st.st_mtime,
        })
    files.sort(key=lambda x: x['mtime_ts'], reverse=True)
    return files[:limit]


def _iter_assets(max_depth: int = 3):
    """弹幕与字幕产物：与录像同目录、同名前缀。"""
    if not os.path.isdir(DEFAULT_DOWNLOAD_DIR):
        return
    for root, _dirs, names in os.walk(DEFAULT_DOWNLOAD_DIR):
        depth = root[len(DEFAULT_DOWNLOAD_DIR):].count(os.sep)
        if depth > max_depth:
            continue
        for n in names:
            if not n.lower().endswith(SUBTITLE_EXTS):
                continue
            p = os.path.join(root, n)
            try:
                st = os.stat(p)
            except OSError:
                continue
            yield p, n, st


def assets_for_names(names, limit: int = 200) -> list:
    """某个主播的弹幕/字幕文件（srt / ass / jsonl）。"""
    keys = []
    for n in names or []:
        k = clean_anchor_name(n)
        if k and k not in keys:
            keys.append(k)
    if not keys:
        return []

    matched = []
    for p, n, st in _iter_assets():
        dir_name = clean_anchor_name(os.path.basename(os.path.dirname(p)) or '')
        base = n
        for ext in SUBTITLE_EXTS:
            if base.lower().endswith(ext):
                base = base[: -len(ext)]
                break
        base = base.replace('.danmaku', '')
        stem = clean_anchor_name(base)
        hit = dir_name in keys or any(
            stem == k or stem.startswith(k + '_') or stem.startswith(k + '-') for k in keys
        )
        if not hit:
            continue
        matched.append({
            'name': n,
            'path': os.path.relpath(p, DEFAULT_DOWNLOAD_DIR).replace('\\', '/'),
            'size': st.st_size,
            'mtime': time.strftime('%Y-%m-%d %H:%M', time.localtime(st.st_mtime)),
            'mtime_ts': st.st_mtime,
            'ext': os.path.splitext(n)[1].lower().lstrip('.'),
            'kind': 'danmaku' if '.danmaku' in n.lower() else 'subtitle',
        })
    matched.sort(key=lambda x: x['mtime_ts'], reverse=True)
    return matched[:limit]


def files_for_names(names, limit: int = 400) -> list:
    """找出属于某个主播的录像：目录名匹配，或文件名以「主播名_」开头。"""
    keys = []
    for n in names or []:
        k = clean_anchor_name(n)
        if k and k not in keys:
            keys.append(k)
    if not keys:
        return []

    matched = []
    for p, n, st in _iter_media():
        dir_name = clean_anchor_name(os.path.basename(os.path.dirname(p)) or '')
        stem = clean_anchor_name(os.path.splitext(n)[0])
        hit = dir_name in keys or any(
            stem == k or stem.startswith(k + '_') or stem.startswith(k + '-') for k in keys
        )
        if not hit:
            continue
        matched.append({
            'name': n,
            'path': os.path.relpath(p, DEFAULT_DOWNLOAD_DIR).replace('\\', '/'),
            'size': st.st_size,
            'mtime': time.strftime('%Y-%m-%d %H:%M', time.localtime(st.st_mtime)),
            'mtime_ts': st.st_mtime,
            'ext': os.path.splitext(n)[1].lower().lstrip('.'),
        })
    matched.sort(key=lambda x: x['mtime_ts'], reverse=True)
    return matched[:limit]
