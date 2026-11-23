#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""把弹幕 XML 转成 ASS 字幕（命令行版）。

转换参数默认对齐 DanmakuFactory 预设：字号 42、阴影 1、不透明度 180、描边 0、
滚动通过 12 秒、固定停留 5 秒、显示用户名、行数按画面高度自动铺满。
分辨率默认取视频实际大小（ffprobe 读同目录录像），读不到才用 1920x1080。

WebUI 的「系统状态 → 字幕转换」用的是同一套核心（src/danmaku_convert.py），
参数也能在「全局设置 → 弹幕字幕」里改。

用法：
    python tools/xml_to_ass.py ./downloads/B站直播/某主播/xxx.xml
    python tools/xml_to_ass.py ./downloads/B站直播 --overwrite
    python tools/xml_to_ass.py xxx.xml --resolution 2560x1440
"""
import argparse
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.danmaku import (FIXED_DURATION, SCROLL_DURATION, SUBTITLE_ALPHA,  # noqa: E402
                         SUBTITLE_FONT_SIZE, SUBTITLE_OUTLINE, SUBTITLE_SHADOW)
from src.danmaku_convert import convert_file, find_xml_files  # noqa: E402


def parse_resolution(text):
    if not text:
        return None
    m = re.fullmatch(r'\s*(\d{3,5})\s*[xX*×]\s*(\d{3,5})\s*', text)
    if not m:
        raise argparse.ArgumentTypeError('分辨率要写成 1920x1080 这种形式')
    return int(m.group(1)), int(m.group(2))


def main(argv=None):
    parser = argparse.ArgumentParser(description='弹幕 XML 转 ASS（对齐 DanmakuFactory 默认预设）')
    parser.add_argument('paths', nargs='+', help='XML 文件或目录（递归找 *.xml，兼容 *.danmaku.xml）')
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
    parser.add_argument('--msgbox-duration', type=float, default=0.0,
                        help='礼物/SC/上舰 统一停留秒数（默认 0 = 用消息自带时长，同 DanmakuFactory --msgboxduration）')
    parser.add_argument('--msgbox-lines', type=int, default=0,
                        help='消息框同时最多几条（默认 0 = 按消息框高度自动）')
    parser.add_argument('--msgbox-cap', type=float, default=0.0,
                        help='单条消息最长停留秒数（默认 0 = 不封顶）')
    parser.add_argument('--no-msgbox', action='store_true', help='不显示礼物/SC/上舰（同 --showmsgbox false）')
    parser.add_argument('--msgbox-source', choices=('price', 'time'), default='price',
                        help='消息框时长来源：price=按金额分档（默认），time=用消息自带的 time')
    parser.add_argument('--no-usernames', action='store_true',
                        help='不显示用户名（同 DanmakuFactory 的 --showusernames false）')
    args = parser.parse_args(argv)

    files = []
    for item in args.paths:
        if os.path.isdir(item):
            files.extend(find_xml_files(item))
        elif os.path.isfile(item):
            files.append(item)
        else:
            print(f'跳过不存在的路径: {item}')
    if not files:
        print('没有找到弹幕 XML 文件')
        return 1

    kwargs = dict(duration=args.fixed_seconds, mode='fixed' if args.fixed else 'scroll',
                  font_size=args.font_size, scroll_duration=args.scroll_seconds,
                  alpha=args.alpha, outline=args.outline, shadow=args.shadow,
                  max_lines=args.max_lines,
                  privilege_layout='off' if args.no_msgbox else 'corner',
                  privilege_max=args.msgbox_lines, privilege_cap=args.msgbox_cap,
                  msg_duration=args.msgbox_duration, msg_source=args.msgbox_source,
                  show_user=not args.no_usernames)

    ok = 0
    for path in files:
        name = os.path.basename(path)
        result = convert_file(path, overwrite=args.overwrite,
                              resolution=args.resolution, **kwargs)
        if result['status'] == 'ok':
            ok += 1
            print(f"+ {name}: {result['message']}")
        elif result['status'] == 'skip':
            print(f'- {name}: {result["message"]}')
        else:
            print(f"✗ {name}: {result['message']}")

    print(f'\n文件 {len(files)} 个，生成 {ok} 个 .ass')
    return 0


if __name__ == '__main__':
    sys.exit(main())
