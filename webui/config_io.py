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


def dedupe_lines(lines: list) -> tuple:
    """同一节里同一个键出现多次时只留一条：位置取第一处，值取最后一处。

    和 read_ini 的语义一致（后写的覆盖前面的），顺便把注释留在第一次出现的位置。
    返回 (新行列表, 删掉的行数)。
    """
    first = {}
    dropped = set()
    section = None
    for i, line in enumerate(lines):
        m = _SECTION_RE.match(line)
        if m:
            section = m.group(1).strip()
            continue
        if section is None:
            continue
        kv = _KV_RE.match(line)
        if not kv:
            continue
        slot = (section, kv.group(2).strip())
        if slot not in first:
            first[slot] = i
            continue
        head = _KV_RE.match(lines[first[slot]])
        if head is None:  # pragma: no cover - 理论上不会发生
            dropped.add(i)
            continue
        lines[first[slot]] = f'{head.group(1)}{head.group(2)}{head.group(3)}{kv.group(4)}'
        dropped.add(i)
    if not dropped:
        return lines, 0
    return [line for j, line in enumerate(lines) if j not in dropped], len(dropped)


def update_values(items, path: str = CONFIG_FILE, create_backup: bool = True) -> list:
    """原地更新若干键值，返回真正被改动的键。

    items: [(section, key, value), ...]
    保持原有行顺序、注释、空行、行内空格风格；缺失的键追加到所属节末尾。
    """
    with _LOCK:
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        lines = _read_lines(path)
        # 先把重复键合并成一条再改：否则「改第一条、留最后一条」会让刚保存的值被
        # 后面那条旧值顶掉（历史文件里这种重复不少，见 migrate.repair_equal_sign_keys）
        lines, merged = dedupe_lines(lines)
        if merged:
            print(f'[webui] 配置里的重复键已合并 {merged} 行', flush=True)
        pending = {}
        invalid = []
        for s, k, v in items:
            key = ('' if k is None else str(k)).strip()
            if not key or '=' in key or '[' in key or ']' in key:
                # 键名里带等号/方括号的行会被 INI 从中间切开（键变成半截、值变成
                # `自动) = 0`），写进去就是一条永远读不到、也永远改不掉的行。
                # 历史版本因此每保存一次追加一行，把配置文件撑到了 4 万多行。
                invalid.append(key)
                continue
            pending[(s, key)] = ('' if v is None else str(v))
        if invalid:
            print(f'[webui] 忽略非法配置键（键名不能含 = [ ]）: {", ".join(invalid)}', flush=True)
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

        if touched or merged:
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
