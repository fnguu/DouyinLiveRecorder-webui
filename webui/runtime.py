# -*- encoding: utf-8 -*-
"""运行时状态读取：录制进程心跳、日志、磁盘。"""
import json
import os
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


def recent_files(limit: int = 8) -> list:
    """最近生成的录像文件，用于首页概览。"""
    files = []
    if not os.path.isdir(DEFAULT_DOWNLOAD_DIR):
        return files
    try:
        for root, _dirs, names in os.walk(DEFAULT_DOWNLOAD_DIR):
            depth = root[len(DEFAULT_DOWNLOAD_DIR):].count(os.sep)
            if depth > 3:
                continue
            for n in names:
                if n.lower().endswith(('.ts', '.mp4', '.mkv', '.flv', '.m4a', '.mp3')):
                    p = os.path.join(root, n)
                    try:
                        st = os.stat(p)
                    except OSError:
                        continue
                    files.append({
                        'name': n,
                        'path': os.path.relpath(p, DEFAULT_DOWNLOAD_DIR).replace('\\', '/'),
                        'size': st.st_size,
                        'mtime': time.strftime('%m-%d %H:%M', time.localtime(st.st_mtime)),
                        'mtime_ts': st.st_mtime,
                    })
    except OSError:
        return files
    files.sort(key=lambda x: x['mtime_ts'], reverse=True)
    return files[:limit]
