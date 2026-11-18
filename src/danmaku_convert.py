# -*- encoding: utf-8 -*-
"""弹幕 XML -> ASS 的转换核心（命令行工具与 WebUI 共用）。

把「读 XML 里的记录」和「调用 render_ass 渲染」这两件事收在一处，
`tools/xml_to_ass.py` 与 WebUI 的「字幕转换」都走这里，避免两份实现漂移。

转换参数默认对齐 DanmakuFactory 预设，唯一的例外是分辨率：优先用 ffprobe 读
同目录的录像文件，读不到才退回 1920x1080 —— PlayRes 和视频对不上字幕会被拉伸。
"""
import os
import re
import xml.etree.ElementTree as ET

from .danmaku import (FIXED_DURATION, SCROLL_DURATION, SUBTITLE_ALPHA, SUBTITLE_FONT_SIZE,
                      SUBTITLE_OUTLINE, SUBTITLE_SHADOW, probe_playres, render_ass)

DEFAULT_RESOLUTION = (1920, 1080)
XML_SUFFIX = '.danmaku.xml'
ASS_SUFFIX = '.danmaku.ass'


def ass_path_for(xml_path: str) -> str:
    if xml_path.endswith(XML_SUFFIX):
        return xml_path[:-len(XML_SUFFIX)] + ASS_SUFFIX
    return xml_path + '.ass'


def is_finished(xml_path: str) -> bool:
    """录制中的文件没有 </i> 收尾，这时转 ASS 只会得到半截字幕。"""
    try:
        with open(xml_path, 'rb') as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 512))
            return b'</i>' in f.read()
    except OSError:
        return False


def _num(value, default=0.0) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def read_records(xml_path: str) -> list:
    """读 XML -> render_ass 需要的记录列表（顺序即写入顺序）。"""
    # 内嵌的 XSLT 对渲染没用，且依赖 xmlns:z 声明；先摘掉再解析，
    # 这样样式表被编辑过、命名空间缺失的文件也能转。
    with open(xml_path, 'r', encoding='utf-8', errors='replace') as f:
        text = re.sub(r'<XmlStyle>.*?</XmlStyle>', '', f.read(), flags=re.S)
    root = ET.fromstring(text)
    records = []

    for node in root:
        tag = node.tag
        attrs = node.attrib
        if tag == 'd':
            fields = (attrs.get('p') or '').split(',')
            # p: offset,mode,size,color,ts,...
            # mode 与 DanmakuFactory 一致：1/2/3 滚动，4 底部固定，5 顶部固定，6 逆向滚动
            try:
                mode = int(fields[1]) if len(fields) > 1 else 1
            except (TypeError, ValueError):
                mode = 1
            # 第 4 项是弹幕颜色（0xRRGGBB），ASS 里要用它给正文上色
            try:
                color = int(fields[3]) if len(fields) > 3 else 0xFFFFFF
            except (TypeError, ValueError):
                color = 0xFFFFFF
            records.append({
                'type': 'chat', 'offset': _num(fields[0]) if fields else 0.0,
                'user': attrs.get('user') or '', 'content': node.text or '',
                'mode': mode, 'color': color,
            })
        elif tag == 'gift':
            name = attrs.get('giftname') or '礼物'
            count = attrs.get('giftcount') or '1'
            try:
                count_num = max(1, int(float(count)))
            except (TypeError, ValueError):
                count_num = 1
            records.append({
                'type': 'gift', 'offset': _num(attrs.get('ts')), 'user': attrs.get('user') or '',
                'content': f'{name} x{count}', 'diamond': _num(attrs.get('price')),
                'duration': _num(attrs.get('time')),
                'gift_name': name, 'gift_count': count_num,
            })
        elif tag == 'sc':
            records.append({
                'type': 'sc', 'offset': _num(attrs.get('ts')), 'user': attrs.get('user') or '',
                'content': node.text or '', 'diamond': _num(attrs.get('price')),
                'sc_time': _num(attrs.get('time')),
            })
        elif tag == 'guard':
            name = attrs.get('guardname') or '舰长'
            records.append({
                'type': 'guard', 'offset': _num(attrs.get('ts')), 'user': attrs.get('user') or '',
                'content': f'开通 {name}', 'diamond': _num(attrs.get('price')),
                'duration': _num(attrs.get('time')), 'guard_level': int(_num(attrs.get('level'))),
                'guard_name': name,
            })
    return records


def resolve_playres(xml_path: str, forced=None) -> tuple:
    """返回 ((宽, 高), 来源说明)。"""
    if forced:
        return tuple(forced), '手动指定'
    base = xml_path[:-len(XML_SUFFIX)] if xml_path.endswith(XML_SUFFIX) else xml_path
    probed = probe_playres(base)
    if probed:
        return probed, '视频'
    return DEFAULT_RESOLUTION, '默认'


def style_defaults() -> dict:
    """DanmakuFactory 默认预设（分辨率另由 resolve_playres 决定）。"""
    return dict(duration=FIXED_DURATION, mode='scroll', max_lines=0,
                font_size=SUBTITLE_FONT_SIZE, scroll_duration=SCROLL_DURATION,
                alpha=SUBTITLE_ALPHA, outline=SUBTITLE_OUTLINE, shadow=SUBTITLE_SHADOW,
                privilege_layout='corner', privilege_max=0, privilege_cap=0.0,
                msg_duration=0.0, msg_source='price', show_user=True)


def convert_file(xml_path: str, overwrite: bool = False, resolution=None, **render_kwargs) -> dict:
    """把一个 XML 转成 ASS。

    返回 {'status': 'ok'|'skip'|'error', 'message': str, ...}，
    调用方（命令行/WebUI）自己决定怎么汇总。
    """
    ass_path = ass_path_for(xml_path)
    if os.path.exists(ass_path) and not overwrite:
        return {'status': 'skip', 'message': '已有 .ass（需覆盖请显式指定）', 'ass': ass_path}
    if not is_finished(xml_path):
        return {'status': 'skip', 'message': '还没收尾（可能正在录制）', 'ass': ass_path}

    style = style_defaults()
    style.update({k: v for k, v in render_kwargs.items() if v is not None})
    playres, source = resolve_playres(xml_path, resolution)

    try:
        records = read_records(xml_path)
        if not records:
            return {'status': 'skip', 'message': '没有可渲染的弹幕', 'ass': ass_path}
        written, dropped = render_ass(records, ass_path, playres=playres, **style)
    except Exception as exc:
        msg = f'{type(exc).__name__}: {exc}'
        # 被录制进程顶掉记录的文件会长这样：两段 <d> 粘在同一行，解析器报 invalid token。
        # 这里给出可执行的下一步，别让人对着 ParseError 发呆。
        try:
            with open(xml_path, 'r', encoding='utf-8', errors='replace') as f:
                if any(ln.count('<d p=') > 1 for ln in f):
                    msg += '（文件里有被顶掉后粘在一起的记录，用 tools/normalize_danmaku_xml.py --repair --apply 修复）'
        except OSError:
            pass
        return {'status': 'error', 'message': msg, 'ass': ass_path}

    return {
        'status': 'ok', 'ass': ass_path, 'written': written, 'dropped': dropped,
        'playres': list(playres), 'source': source,
        'message': f'{written} 条 -> {os.path.basename(ass_path)}（{playres[0]}x{playres[1]}，取自{source}）',
    }


def find_xml_files(root: str) -> list:
    found = []
    for dirpath, _dirs, names in os.walk(root):
        for name in sorted(names):
            if name.endswith(XML_SUFFIX):
                found.append(os.path.join(dirpath, name))
    return found
