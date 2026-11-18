# -*- encoding: utf-8 -*-
"""WebUI 的「字幕转换」：把已录好的弹幕 XML 批量转成 ASS。

调用的是 `src/danmaku_convert.py`（与命令行 `tools/xml_to_ass.py` 同一套核心），
参数取自 config.ini 的「弹幕字幕」分组，所以界面上改完设置再来转，结果一致。

转换在后台线程里跑，进度通过 `status()` 暴露给前端；同一时间只允许一个任务。
正在录制、没有 `</i>` 收尾的文件会被跳过，避免生成半截字幕，也避免跟录制进程抢写。
"""
import os
import sys
import threading
import time

from .config_io import read_ini
from .paths import BASE_DIR, DEFAULT_DOWNLOAD_DIR

if BASE_DIR not in sys.path:      # 让 webui 进程也能 import src.*
    sys.path.insert(0, BASE_DIR)

from src.danmaku_convert import ass_path_for, convert_file, find_xml_files, is_finished  # noqa: E402

_LOCK = threading.Lock()
_STATE = {
    'running': False, 'total': 0, 'done': 0, 'ok': 0, 'skipped': 0, 'failed': 0,
    'current': '', 'overwrite': False, 'started_at': 0, 'finished_at': 0,
    'errors': [], 'root': '',
}


def download_root() -> str:
    """录像目录：优先用配置里的「直播保存路径」，否则用默认 downloads。"""
    save_path = (read_ini().get('录制设置', {}).get('直播保存路径(不填则默认)', '') or '').strip()
    return save_path or DEFAULT_DOWNLOAD_DIR


def style_from_config() -> dict:
    """config.ini 的「弹幕字幕」参数 -> render_ass 的关键字参数。"""
    ini = read_ini().get('录制设置', {})

    def num(key, default):
        try:
            return float(str(ini.get(key, default)).strip())
        except (TypeError, ValueError):
            return float(default)

    opacity = int(max(0, min(255, num('弹幕字幕不透明度(0-255)', 180))))
    mode = '固定' if str(ini.get('弹幕字幕显示方式(滚动/固定)', '滚动')).strip().startswith('固定') else '滚动'
    return {
        'font_size': int(max(12, num('弹幕字幕字号', 42))),
        'alpha': 255 - opacity,
        'outline': int(max(0, num('弹幕字幕描边宽度', 0))),
        'shadow': int(max(0, num('弹幕字幕阴影大小', 1))),
        'scroll_duration': max(1.0, num('滚动弹幕通过时间(秒)', 12)),
        'duration': max(1.0, num('固定弹幕停留时间(秒)', 5)),
        'max_lines': int(max(0, num('弹幕字幕同屏行数(0=自动)', 0))),
        'mode': 'fixed' if mode == '固定' else 'scroll',
        'privilege_layout': str(ini.get('礼物弹幕显示方式(左下角/滚动/关闭)', '左下角')).strip() or '左下角',
        'privilege_max': int(max(0, num('礼物弹幕最多条数', 0))),
        'privilege_cap': max(0.0, num('礼物弹幕最长停留(秒)', 0)),
        'msg_duration': max(0.0, num('礼物消息框统一停留(秒)', 0)),
    }


def scan() -> dict:
    """只读统计：有多少 XML、缺多少 .ass、多少还没录完。"""
    root = download_root()
    if not os.path.isdir(root):
        return {'root': root, 'exists': False, 'total': 0, 'missing': 0, 'unfinished': 0, 'ready': 0}
    files = find_xml_files(root)
    missing = unfinished = 0
    for path in files:
        if not os.path.exists(ass_path_for(path)):
            missing += 1
        if not is_finished(path):
            unfinished += 1
    return {
        'root': root, 'exists': True, 'total': len(files),
        'missing': missing, 'unfinished': unfinished,
        'ready': len(files) - unfinished,
        'style': style_from_config(),
    }


def status() -> dict:
    with _LOCK:
        state = dict(_STATE)
    state['errors'] = list(state.get('errors') or [])[-5:]
    state['scan'] = scan()
    return state


def start(overwrite: bool = False) -> dict:
    """启动后台转换。已有任务在跑时直接返回当前状态。"""
    with _LOCK:
        if _STATE['running']:
            return {'ok': False, 'message': '已经有一个转换任务在跑', 'state': dict(_STATE)}
        root = download_root()
        files = find_xml_files(root) if os.path.isdir(root) else []
        if not files:
            return {'ok': False, 'message': f'录像目录里没有弹幕文件：{root}'}
        _STATE.update({
            'running': True, 'total': len(files), 'done': 0, 'ok': 0, 'skipped': 0,
            'failed': 0, 'current': '', 'overwrite': bool(overwrite),
            'started_at': int(time.time()), 'finished_at': 0, 'errors': [], 'root': root,
        })
        style = style_from_config()

    threading.Thread(target=_run, args=(files, bool(overwrite), style),
                     name='subtitle-convert', daemon=True).start()
    return {'ok': True, 'message': f'开始转换 {len(files)} 个弹幕文件', 'total': len(files)}


def _run(files: list, overwrite: bool, style: dict) -> None:
    for path in files:
        with _LOCK:
            _STATE['current'] = os.path.basename(path)
        try:
            result = convert_file(path, overwrite=overwrite, **style)
        except Exception as exc:  # 单个文件失败不该中断整批
            result = {'status': 'error', 'message': f'{type(exc).__name__}: {exc}'}
        with _LOCK:
            _STATE['done'] += 1
            if result['status'] == 'ok':
                _STATE['ok'] += 1
            elif result['status'] == 'skip':
                _STATE['skipped'] += 1
            else:
                _STATE['failed'] += 1
                _STATE['errors'].append(f"{os.path.basename(path)}: {result.get('message', '')}")
    with _LOCK:
        _STATE.update({'running': False, 'current': '', 'finished_at': int(time.time())})
