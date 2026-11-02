# -*- encoding: utf-8 -*-
"""录像播放支持：路径安全校验 + 浏览器不支持的容器按需转封装。

浏览器只能直接播 mp4 / m4a / mp3 / webm，而录制默认产出 ts（推荐格式）。
这里用容器内的 ffmpeg 以 `-c copy` 无损转封装成 mp4 并缓存，不重新编码，几乎不耗 CPU。
"""
import hashlib
import os
import shutil
import subprocess
import tempfile
import threading

from .paths import DEFAULT_DOWNLOAD_DIR

PLAYABLE_DIRECT = {'.mp4', '.m4a', '.mp3', '.webm', '.mov'}
NEED_REMUX = {'.ts', '.mkv', '.flv'}

MEDIA_TYPES = {
    '.mp4': 'video/mp4', '.m4v': 'video/mp4', '.webm': 'video/webm',
    '.mov': 'video/quicktime', '.m4a': 'audio/mp4', '.mp3': 'audio/mpeg',
    '.srt': 'text/plain', '.ass': 'text/plain', '.jsonl': 'application/x-ndjson',
}

# 弹幕 / 字幕：只下载，不做转封装
DOWNLOAD_ONLY = {'.srt', '.ass', '.jsonl', '.vtt', '.txt'}

CACHE_DIR = os.path.join(tempfile.gettempdir(), 'dlr_media_cache')
MAX_CACHE_BYTES = 6 * 1024 ** 3

_LOCK = threading.Lock()
_FILE_LOCKS = {}


def resolve(rel_path: str) -> str:
    """把相对路径解析成 downloads 内的绝对路径，阻止目录穿越。"""
    if not rel_path:
        raise ValueError('缺少文件路径')
    base = os.path.realpath(DEFAULT_DOWNLOAD_DIR)
    target = os.path.realpath(os.path.join(base, rel_path.replace('\\', '/')))
    if target != base and not target.startswith(base + os.sep):
        raise ValueError('路径越界')
    return target


def ffmpeg_available() -> bool:
    return shutil.which('ffmpeg') is not None


def media_type_for(path: str) -> str:
    return MEDIA_TYPES.get(os.path.splitext(path)[1].lower(), 'application/octet-stream')


def is_download_only(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in DOWNLOAD_ONLY


def can_play_directly(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in PLAYABLE_DIRECT


def _cache_key(path: str) -> str:
    st = os.stat(path)
    raw = f'{path}|{st.st_size}|{st.st_mtime_ns}'
    return hashlib.sha1(raw.encode('utf-8')).hexdigest()[:16]


def _file_lock(key: str) -> threading.Lock:
    with _LOCK:
        return _FILE_LOCKS.setdefault(key, threading.Lock())


def _safe_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _prune() -> None:
    """缓存超过上限时按最近使用时间清理。"""
    try:
        entries = []
        for f in os.listdir(CACHE_DIR):
            p = os.path.join(CACHE_DIR, f)
            try:
                st = os.stat(p)
            except OSError:
                continue
            entries.append((st.st_mtime, st.st_size, p))
        total = sum(e[1] for e in entries)
        if total <= MAX_CACHE_BYTES:
            return
        for _mtime, size, p in sorted(entries):
            _safe_remove(p)
            total -= size
            if total <= MAX_CACHE_BYTES:
                break
    except OSError:
        pass


def remux_to_mp4(path: str) -> str:
    """返回可播放的文件路径。ts/mkv/flv 会转封装成缓存 mp4。"""
    ext = os.path.splitext(path)[1].lower()
    if ext in PLAYABLE_DIRECT:
        return path
    if ext not in NEED_REMUX:
        raise RuntimeError('该格式不支持在线播放')
    if not ffmpeg_available():
        raise RuntimeError('容器内缺少 ffmpeg，无法转封装')

    os.makedirs(CACHE_DIR, exist_ok=True)
    key = _cache_key(path)
    out = os.path.join(CACHE_DIR, key + '.mp4')
    if os.path.exists(out) and os.path.getsize(out) > 0:
        os.utime(out, None)
        return out

    with _file_lock(key):
        if os.path.exists(out) and os.path.getsize(out) > 0:
            return out
        tmp = out + '.part.mp4'
        cmd = ['ffmpeg', '-nostdin', '-y', '-loglevel', 'error',
               '-i', path, '-c', 'copy', '-movflags', '+faststart', tmp]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=900)
        except subprocess.TimeoutExpired:
            _safe_remove(tmp)
            raise RuntimeError('转封装超时，文件可能过大')
        if proc.returncode != 0 or not os.path.exists(tmp):
            _safe_remove(tmp)
            detail = (proc.stderr or b'').decode('utf-8', 'ignore').strip().splitlines()
            raise RuntimeError('转封装失败' + (': ' + detail[-1][:160] if detail else ''))
        os.replace(tmp, out)
        _prune()
        return out
