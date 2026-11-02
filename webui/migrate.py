# -*- encoding: utf-8 -*-
"""配置修复：清理被 configparser.write() 降级过的键名。

背景：`main.py` 早期版本在「配置项缺失 → 补默认值」时用了
`configparser.write()` 回写整个文件，它有两个副作用：

1. 注释、空行、原有顺序全部丢失（文件被重排）；
2. `optionxform` 默认把键名转小写，`是否录制B站弹幕(是/否)` 变成
   `是否录制b站弹幕(是/否)`。

第 2 条会让 WebUI 按原名找不到这个键 → 界面上把它当成「未知配置项」显示；
用户在界面里再改一次，又按原名写回一条大写键，于是同一项出现两次
（截图里「录制B站弹幕」和「是否录制b站弹幕(是/否)」并存就是这么来的）。

这里以 schema 登记的键为规范名做一次修复：同一节内大小写不同的变体，
规范名缺失则改名，规范名已存在则删掉多余的那条。只处理 schema 认识的键，
其余自定义项一律不动。
"""
import os

from .paths import CONFIG_FILE


def _read_lines(path: str):
    with open(path, 'r', encoding='utf-8-sig', newline='') as f:
        raw = f.read()
    nl = '\r\n' if '\r\n' in raw else '\n'
    lines = raw.split(nl)
    trailing = bool(lines) and lines[-1] == ''
    if trailing:
        lines.pop()
    return lines, nl, trailing


def _write_lines(path: str, lines, nl: str, trailing: bool) -> None:
    text = nl.join(lines) + (nl if trailing else '')
    tmp = f'{path}.tmp'
    with open(tmp, 'w', encoding='utf-8-sig', newline='') as f:
        f.write(text)
    os.replace(tmp, path)


def repair_case_keys(path: str | None = None) -> list:
    """修复大小写变体键，返回执行过的动作列表。"""
    from .schema import FIELDS

    target = path or CONFIG_FILE
    if not os.path.isfile(target):
        return []

    canonical = {}
    for section, key, *_rest in FIELDS:
        canonical.setdefault(section, {})[key.casefold()] = key

    try:
        lines, nl, trailing = _read_lines(target)
    except OSError:
        return []

    entries = []
    section = None
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith('[') and stripped.endswith(']'):
            section = stripped[1:-1]
            continue
        if not section or not stripped or stripped.startswith('#') or '=' not in stripped:
            continue
        key, value = stripped.split('=', 1)
        entries.append({'section': section, 'key': key.strip(), 'value': value.strip(), 'idx': idx})

    groups = {}
    for e in entries:
        groups.setdefault((e['section'], e['key'].casefold()), []).append(e)

    drop = set()
    actions = []
    for (sec, folded), items in groups.items():
        want = (canonical.get(sec) or {}).get(folded)
        if not want:
            continue  # schema 不认识的键，保持原样
        spellings = {e['key'] for e in items}
        if len(spellings) == 1 and next(iter(spellings)) == want:
            continue  # 已经是规范名，且没有重复
        exact = [e for e in items if e['key'] == want]
        keep = exact[0] if exact else items[0]
        for e in items:
            if e is keep:
                continue
            drop.add(e['idx'])
            actions.append({'section': sec, 'removed': e['key'], 'kept': want})
        if keep['key'] != want:
            # 只有旧写法存在：把键名改回规范名，值保持不变
            lines[keep['idx']] = f"{want} = {keep['value']}"
            actions.append({'section': sec, 'renamed': keep['key'], 'to': want})

    if not drop and not actions:
        return []

    lines = [line for i, line in enumerate(lines) if i not in drop]
    try:
        _write_lines(target, lines, nl, trailing)
    except OSError:
        return []
    return actions
