#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""把弹幕 XML 转成 ASS 字幕，参数对齐 DanmakuFactory 的默认预设。

默认值（与 DanmakuFactory 预设页一致）：
    字体大小 42、阴影 1、不透明度 180（ASS alpha 75）、描边 0
    滚动弹幕通过时间 12 秒、固定弹幕停留时间 5 秒、显示用户名

唯一的例外是分辨率：**默认按视频实际分辨率**（用 ffprobe 读同目录的录像文件），
读不到才退回 1920x1080 —— PlayRes 和视频对不上时字幕会被拉扁或拉长。

用法：
    # 转一个文件（.ass 写在 XML 旁边，已存在则跳过）
    python tools/xml_to_ass.py ./downloads/B站直播/某主播/xxx.danmaku.xml

    # 整个目录，覆盖已有 .ass
    python tools/xml_to_ass.py ./downloads/B站直播 --overwrite

    # 视频不在旁边时手动指定分辨率
    python tools/xml_to_ass.py xxx.danmaku.xml --resolution 2560x1440
"""
import argparse
import glob
import os
import re
import sys
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.danmaku import (FIXED_DURATION, SCROLL_DURATION, SUBTITLE_ALPHA,  # noqa: E402
                         SUBTITLE_FONT_SIZE, SUBTITLE_OUTLINE, SUBTITLE_SHADOW,
                         probe_playres, render_ass)

DEFAULT_RESOLUTION = (1920, 1080)


def _num(value, default=0.0) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def read_records(xml_path: str) -> list:
    """读 XML -> render_ass 需要的记录列表。

    顺序即文件顺序（写入顺序），offset 是相对录制开始的秒数。
    """
    # 内嵌的 XSLT 样式表对渲染没用，且它依赖 xmlns:z 声明；
    # 顺手摘掉再解析，能兼容样式表被编辑过、命名空间缺失的文件。
    with open(xml_path, 'r', encoding='utf-8', errors='replace') as f:
        text = re.sub(r'<XmlStyle>.*?</XmlStyle>', '', f.read(), flags=re.S)
    root = ET.fromstring(text)
    records = []

    for node in root:
        tag = node.tag
        attrs = node.attrib
        if tag == 'd':
            p = (attrs.get('p') or '').split(',')
            offset = _num(p[0]) if p else 0.0
            records.append({
                'type': 'chat', 'offset': offset, 'user': attrs.get('user') or '',
                'content': node.text or '',
            })
        elif tag == 'gift':
            name = attrs.get('giftname') or '礼物'
            count = attrs.get('giftcount') or '1'
            records.append({
                'type': 'gift', 'offset': _num(attrs.get('ts')), 'user': attrs.get('user') or '',
                'content': f'{name} x{count}', 'diamond': _num(attrs.get('price')),
            })
        elif tag == 'sc':
            records.append({
                'type': 'sc', 'offset': _num(attrs.get('ts')), 'user': attrs.get('user') or '',
                'content': node.text or '', 'diamond': _num(attrs.get('price')),
            })
        elif tag == 'guard':
            name = attrs.get('guardname') or '舰长'
            records.append({
                'type': 'guard', 'offset': _num(attrs.get('ts')), 'user': attrs.get('user') or '',
                'content': f'开通 {name}', 'diamond': _num(attrs.get('price')),
            })
    return records


def convert_one(xml_path: str, overwrite: bool, resolution, **render_kwargs) -> str:
    ass_path = re.sub(r'\.danmaku\.xml$', '.danmaku.ass', xml_path)
    if ass_path == xml_path:
        ass_path = xml_path + '.ass'
    if os.path.exists(ass_path) and not overwrite:
        return f'跳过（已存在 {os.path.basename(ass_path)}，加 --overwrite 覆盖）'

    base = re.sub(r'\.danmaku\.xml$', '', xml_path)
    if resolution:
        playres, source = resolution, '手动指定'
    else:
        probed = probe_playres(base)
        playres, source = (probed, '视频') if probed else (DEFAULT_RESOLUTION, '默认')

    records = read_records(xml_path)
    if not records:
        return '跳过（没有可渲染的弹幕）'

    written, dropped = render_ass(records, ass_path, playres=playres, **render_kwargs)
    return (f'{written} 条 -> {os.path.basename(ass_path)}'
            f'（{playres[0]}x{playres[1]}，取自{source}' + (f'，丢弃 {dropped} 条' if dropped else '') + '）')


def collect(paths):
    files = []
    for item in paths:
        if os.path.isdir(item):
            files.extend(sorted(glob.glob(os.path.join(item, '**', '*.danmaku.xml'), recursive=True)))
        elif os.path.isfile(item):
            files.append(item)
        else:
            print(f'跳过不存在的路径: {item}')
    return files


def parse_resolution(text):
    if not text:
        return None
    m = re.fullmatch(r'\s*(\d{3,5})\s*[xX*×]\s*(\d{3,5})\s*', text)
    if not m:
        raise argparse.ArgumentTypeError('分辨率要写成 1920x1080 这种形式')
    return int(m.group(1)), int(m.group(2))


def main(argv=None):
    parser = argparse.ArgumentParser(description='弹幕 XML 转 ASS（对齐 DanmakuFactory 默认预设）')
    parser.add_argument('paths', nargs='+', help='XML 文件或目录（递归找 *.danmaku.xml）')
    parser.add_argument('--overwrite', action='store_true', help='覆盖已存在的 .ass')
    parser.add_argument('--resolution', type=parse_resolution,
                        help='强制指定分辨率（默认用 ffprobe 读同目录视频，读不到用 1920x1080）')
    parser.add_argument('--font-size', type=int, default=SUBTITLE_FONT_SIZE, help='字体大小（默认 42）')
    parser.add_argument('--alpha', type=int, default=SUBTITLE_ALPHA,
                        help='ASS 透明度 0-255（默认 75，对应不透明度 180）')
    parser.add_argument('--outline', type=int, default=SUBTITLE_OUTLINE, help='描边宽度（默认 0）')
    parser.add_argument('--shadow', type=int, default=SUBTITLE_SHADOW, help='阴影距离（默认 1）')
    parser.add_argument('--scroll-seconds', type=float, default=SCROLL_DURATION,
                        help='滚动弹幕通过时间（默认 12 秒）')
    parser.add_argument('--fixed-seconds', type=float, default=FIXED_DURATION,
                        help='固定弹幕停留时间（默认 5 秒）')
    parser.add_argument('--fixed', action='store_true', help='改用顶部固定模式（默认滚动）')
    parser.add_argument('--max-lines', type=int, default=0,
                        help='同屏最多几行（默认 0 = 按画面高度自动铺满）')
    args = parser.parse_args(argv)

    files = collect(args.paths)
    if not files:
        print('没有找到 .danmaku.xml 文件')
        return 1

    kwargs = dict(duration=args.fixed_seconds, mode='fixed' if args.fixed else 'scroll',
                  font_size=args.font_size, scroll_duration=args.scroll_seconds,
                  alpha=args.alpha, outline=args.outline, shadow=args.shadow,
                  max_lines=args.max_lines)

    done = 0
    for path in files:
        try:
            msg = convert_one(path, args.overwrite, args.resolution, **kwargs)
        except Exception as exc:
            print(f'✗ {os.path.basename(path)}: {exc}')
            continue
        if msg.startswith('跳过'):
            print(f'- {os.path.basename(path)}: {msg}')
        else:
            done += 1
            print(f'+ {os.path.basename(path)}: {msg}')

    print(f'\n文件 {len(files)} 个，生成 {done} 个 .ass')
    return 0


if __name__ == '__main__':
    sys.exit(main())
