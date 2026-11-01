# -*- encoding: utf-8 -*-
"""录制进程 <-> WebUI 的桥。

这个模块被 main.py 直接导入，因此**只允许依赖标准库**，
不能拖入 FastAPI 等 Web 依赖，否则会拖慢/影响录制主进程启动。
"""
import json
import os
import re
import threading
import time

_RUNTIME_FILE = os.environ.get('DLR_RUNTIME_FILE', '/tmp/dlr_runtime.json')
_STATE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           'config', 'webui_state.json')

_LOCK = threading.Lock()
_THREADS = {}
_STATE_CACHE = {'mtime': 0.0, 'map': None}


def _norm(url: str) -> str:
    if not url:
        return ''
    u = re.sub(r'^[a-zA-Z]+://', '', url.strip())
    u = u.split('?')[0].split('#')[0].rstrip('/')
    u = re.sub(r'^www\.', '', u, flags=re.I)
    return u.lower()


def _notify_map() -> dict:
    try:
        mtime = os.path.getmtime(_STATE_FILE)
    except OSError:
        return {}
    if _STATE_CACHE['map'] is not None and _STATE_CACHE['mtime'] == mtime:
        return _STATE_CACHE['map']
    result = {}
    try:
        with open(_STATE_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        for a in data.get('anchors', []):
            result[_norm(a.get('url', ''))] = bool(a.get('notify', True))
    except (OSError, ValueError):
        result = {}
    _STATE_CACHE.update(mtime=mtime, map=result)
    return result


def is_notify_enabled(url: str) -> bool:
    """单个主播是否允许推送。列表里没登记的一律放行。"""
    m = _notify_map()
    if not m:
        return True
    return m.get(_norm(url), True)


def is_anchor_known(url: str) -> bool:
    m = _notify_map()
    return (not m) or (_norm(url) in m)


def mark(url: str, **kwargs) -> None:
    """记录某个监测线程的实时状态。"""
    if not url:
        return
    with _LOCK:
        item = _THREADS.get(url)
        if item is None:
            item = {'url': url, 'status': 'waiting', 'name': '', 'started': time.time()}
            _THREADS[url] = item
        item.update(kwargs)
        item['updated'] = time.time()


def unmark(url: str) -> None:
    with _LOCK:
        _THREADS.pop(url, None)


def flush(running=None, monitoring: int = 0, error_count: int = 0,
          version: str = '', recording=None) -> None:
    """由 display_info 周期调用，把状态落到 json 供 WebUI 读取。"""
    with _LOCK:
        threads = sorted(_THREADS.values(), key=lambda t: t.get('started', 0))
    payload = {
        'beat': time.time(),
        'pid': os.getpid(),
        'version': version,
        'monitoring': monitoring,
        'error_count': error_count,
        'running': list(running or []),
        'recording': sorted(recording or []),
        'threads': threads,
        'updated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
    }
    try:
        tmp = f'{_RUNTIME_FILE}.{os.getpid()}.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, _RUNTIME_FILE)
    except OSError:
        pass
