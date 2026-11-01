# -*- encoding: utf-8 -*-
"""config.ini 的无损读写。

原版程序用 configparser 读写配置，一旦触发写回就会丢注释、丢顺序、丢空行。
WebUI 必须原地修改单个键值，保留用户文件的一切排版，因此这里按行处理。
"""
import os
import re
import threading
import shutil
import time
from collections import OrderedDict

from .paths import CONFIG_FILE, TEXT_ENCODING, BACKUP_DIR

_LOCK = threading.RLock()

_SECTION_RE = re.compile(r'^\s*\[(.+?)\]\s*$')
_KV_RE = re.compile(r'^(\s*)([^=;#\[][^=]*?)(\s*=\s*)(.*)$')


def _read_lines(path: str) -> list:
    with open(path, 'r', encoding=TEXT_ENCODING, errors='replace') as f:
        return f.read().splitlines()


def _write_lines(path: str, lines: list) -> None:
    tmp = path + '.webui.tmp'
    with open(tmp, 'w', encoding=TEXT_ENCODING, newline='\n') as f:
        f.write('\n'.join(lines).lstrip('\ufeff') + '\n')
    os.replace(tmp, path)


def read_ini(path: str = CONFIG_FILE) -> 'OrderedDict[str, OrderedDict[str, str]]':
    """读出 {section: {key: value}}，忽略注释与空行。"""
    data = OrderedDict()
    if not os.path.exists(path):
        return data
    section = None
    for line in _read_lines(path):
        s = line.strip()
        if not s or s.startswith(';'):
            continue
        m = _SECTION_RE.match(line)
        if m:
            section = m.group(1).strip()
            data.setdefault(section, OrderedDict())
            continue
        if s.startswith('#'):
            continue
        m = _KV_RE.match(line)
        if m and section is not None:
            key = m.group(2).strip()
            data[section][key] = m.group(4).strip()
    return data


def get_value(section: str, key: str, default: str = '', path: str = CONFIG_FILE) -> str:
    with _LOCK:
        data = read_ini(path)
    return data.get(section, {}).get(key, default)


def update_values(items, path: str = CONFIG_FILE, create_backup: bool = True) -> list:
    """原地更新若干键值，返回真正被改动的键。

    items: [(section, key, value), ...]
    保持原有行顺序、注释、空行、行内空格风格；缺失的键追加到所属节末尾。
    """
    with _LOCK:
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        lines = _read_lines(path)
        pending = {(s, k): ('' if v is None else str(v)) for s, k, v in items}
        touched = []

        section = None
        for i, line in enumerate(lines):
            m = _SECTION_RE.match(line)
            if m:
                section = m.group(1).strip()
                continue
            if section is None:
                continue
            m = _KV_RE.match(line)
            if not m:
                continue
            key = m.group(2).strip()
            hit = (section, key)
            if hit in pending:
                new_val = pending.pop(hit)
                if new_val != m.group(4).strip():
                    lines[i] = f'{m.group(1)}{m.group(2)}{m.group(3)}{new_val}'
                    touched.append(hit)
                else:
                    touched.append(hit)

        # 有些键在文件里不存在（首次运行/旧版配置），追加到对应节的末尾
        if pending:
            by_section = {}
            for (sec, key), val in pending.items():
                by_section.setdefault(sec, []).append((key, val))
            section = None
            last_index = {}
            for i, line in enumerate(lines):
                m = _SECTION_RE.match(line)
                if m:
                    section = m.group(1).strip()
                    last_index[section] = i
            inserts = {}
            for sec, kvs in by_section.items():
                block = [f'{k} = {v}' for k, v in kvs]
                touched.extend((sec, k) for k, _ in kvs)
                if sec in last_index:
                    idx = last_index[sec]
                    # 跳过该节末尾的空行，插在最后一个非空行之后
                    j = idx + 1
                    last_nonempty = idx
                    while j < len(lines) and not _SECTION_RE.match(lines[j]):
                        if lines[j].strip():
                            last_nonempty = j
                        j += 1
                    inserts.setdefault(last_nonempty + 1, []).extend(block)
                else:
                    lines.extend(['', f'[{sec}]'] + block)

            for idx in sorted(inserts, reverse=True):
                lines[idx:idx] = inserts[idx]

        if touched:
            if create_backup:
                _backup(path)
            _write_lines(path, lines)
        return touched


def _backup(path: str, keep: int = 30) -> None:
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        name = os.path.basename(path)
        dst = os.path.join(BACKUP_DIR, f'{name}.{time.strftime("%Y%m%d_%H%M%S")}.bak')
        shutil.copy2(path, dst)
        files = sorted(
            (os.path.join(BACKUP_DIR, f) for f in os.listdir(BACKUP_DIR)),
            key=lambda p: os.path.getmtime(p), reverse=True
        )
        for old in files[keep:]:
            try:
                os.remove(old)
            except OSError:
                pass
    except OSError:
        pass


def raw_text(path: str = CONFIG_FILE) -> str:
    if not os.path.exists(path):
        return ''
    with open(path, 'r', encoding=TEXT_ENCODING, errors='replace') as f:
        return f.read()
