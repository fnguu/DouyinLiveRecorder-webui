#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""把老录像的弹幕按视频分段切开，并把文件名统一成「与视频同名」。

背景：以前分段录制是按序号命名的（`主播_标题_2026-11-23_17-27-57_000.mp4`、
`_001.mp4` …），而弹幕只有一个整场文件（`…_17-27-57.danmaku.xml`，时间轴从
开播算起）。现在录制端改成「一段视频一份 xml/ass，文件名跟视频完全一致」，
这个脚本负责把已经录好的老文件补齐：

1. 视频：`…_17-27-57_001.mp4` → `…_18-27-57.mp4`（按每段实际时长推算开始时间）
2. 弹幕：整场的 xml 按同样的边界切成 N 份，每份时间轴归零，文件名 = 对应视频
3. 命名：`xxx.danmaku.xml` / `xxx_%03d.danmaku.xml` 一律改成 `xxx.xml`

默认只打印计划、不动任何文件；加 --apply 才真正执行，覆盖前自动留 .bak。

用法：
    python tools/split_danmaku_segments.py L:/dlrw/downloads
    python tools/split_danmaku_segments.py ./downloads/抖音直播/兔娘 --apply
    python tools/split_danmaku_segments.py ./downloads --apply --ass
    python tools/split_danmaku_segments.py ./downloads --apply --no-video
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

SEG_FMT = '%Y-%m-%d_%H-%M-%S'
VIDEO_EXTS = ('.mp4', '.mkv', '.ts', '.flv')
DANMAKU_EXTS = ('.xml', '.ass')

# 主播_标题 + 时间戳 + 可选的三位序号
NAME_RE = re.compile(r'^(?P<prefix>.+?)(?P<stamp>\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})(?:_(?P<idx>\d{3}))?$')
# 时间戳后面剩下的部分：''、'.danmaku'、'_000'、'_%03d'、'_000.danmaku' …
TAIL_RE = re.compile(r'^(?:_(?:\d{3}|%03d))?(?:\.danmaku)?\.(?:xml|ass)$')
RECORD_RE = re.compile(r'^\s*<(?:d|gift|sc|guard)\b')
OFFSET_RE = re.compile(r'^\s*<d[^>]*\bp="\s*([0-9.]+)', re.S)
TS_RE = re.compile(r'^\s*<(?:gift|sc|guard)[^>]*\bts="\s*([0-9.]+)', re.S)


# ---------------------------------------------------------------- 小工具
def parse_stamp(text: str):
    try:
        return datetime.strptime(text, SEG_FMT)
    except (TypeError, ValueError):
        return None


def probe_duration(path: str):
    """ffprobe 读时长（秒）；读不到返回 None。"""
    exe = shutil.which('ffprobe')
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', path],
            capture_output=True, text=True, timeout=30,
        )
        return round(float((out.stdout or '').strip()), 3)
    except Exception:
        return None


def xml_finished(path: str) -> bool:
    """文件末尾有 </i> 才能确认这一场已经录完（录制中是边写边追加的）。"""
    try:
        with open(path, 'rb') as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 512))
            return b'</i>' in f.read()
    except OSError:
        return False


def recently_touched(paths, minutes: float) -> bool:
    """最近还在写 = 可能正在录制，这种会话一律不碰。"""
    if minutes <= 0:
        return False
    now = time.time()
    for path in paths:
        try:
            if now - os.path.getmtime(path) < minutes * 60:
                return True
        except OSError:
            continue
    return False


def record_offset(line: str):
    """一行记录的时间：<d> 看 p 的第 1 段，gift/sc/guard 看 ts。"""
    m = OFFSET_RE.match(line) or TS_RE.match(line)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def read_xml(path: str):
    """拆成 (头部行, [(offset, 原始行)])。头部 = 第一条记录之前的所有行。"""
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        lines = f.readlines()
    head_end = len(lines)
    for i, line in enumerate(lines):
        if RECORD_RE.match(line):
            head_end = i
            break
    header = ''.join(lines[:head_end])
    if not header.endswith('\n'):
        header += '\n'
    records = []
    for line in lines[head_end:]:
        if not RECORD_RE.match(line):
            continue  # </i>、结尾注释之类
        off = record_offset(line)
        if off is None:
            continue
        if not line.endswith('\n'):
            line += '\n'
        records.append((off, line))
    return header, records


def with_start_time(header: str, start: datetime) -> str:
    """把 RecordInfo/@start_time 换成这一段的开始时间（原文件是整场的）。"""
    stamp = start.isoformat(timespec='seconds')
    return re.sub(r'(start_time=")[^"]*(")', lambda m: m.group(1) + stamp + m.group(2), header, count=1)


# ---------------------------------------------------------------- 扫描
def scan_sessions(root: str) -> list:
    """按 (目录, 主播_标题, 时间戳) 归组，返回每个会话的分段视频。"""
    groups = {}
    for dirpath, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        videos = {}
        others = []
        for name in names:
            if name.endswith('.bak'):
                continue
            ext = os.path.splitext(name)[1].lower()
            stem = os.path.splitext(name)[0]
            if ext in VIDEO_EXTS:
                m = NAME_RE.match(stem)
                if m:
                    key = (m.group('prefix'), m.group('stamp'))
                    idx = int(m.group('idx')) if m.group('idx') else None
                    videos.setdefault(key, []).append((idx, os.path.join(dirpath, name), ext))
            elif ext in DANMAKU_EXTS:
                others.append(os.path.join(dirpath, name))
        for (prefix, stamp), items in videos.items():
            exts = {ext for _idx, _p, ext in items}
            if len(exts) > 1:
                # 同一场里出现不同容器格式，按扩展名再分一次，避免把转码中间产物混在一起
                for ext in exts:
                    sub = [it for it in items if it[2] == ext]
                    groups[(dirpath, prefix, stamp, ext)] = sub
            else:
                groups[(dirpath, prefix, stamp, exts.pop())] = items
        # 弹幕文件按前缀+时间戳挂到对应会话上
        for key in [k for k in groups if k[0] == dirpath]:
            _d, prefix, stamp, _e = key
            base = f'{prefix}{stamp}'
            files = []
            for path in others:
                name = os.path.basename(path)
                if name.startswith(base) and TAIL_RE.match(name[len(base):]):
                    files.append(path)
            groups[key] = (groups[key], files)
    return [(k, v) for k, v in groups.items()]


def pick_xml(files: list):
    xmls = [p for p in files if p.lower().endswith('.xml')]
    if not xmls:
        return None
    # 优先「已经跟视频同名」的那个，其次 .danmaku.xml
    xmls.sort(key=lambda p: ('.danmaku' in os.path.basename(p), len(os.path.basename(p))))
    return xmls[0]


def plan_session(items, files, folder):
    """算出这一场的分段开始时间与目标文件名。

    有 ffprobe 就按每段时长累加（最准）；没有就退回「上一段的修改时间 ≈ 下一段
    开始」—— ffmpeg 一定是关掉上一段才建下一段，这个时间戳正好就是分界点。
    """
    items = sorted(items, key=lambda it: (it[0] is None, it[0] or 0))
    match = NAME_RE.match(os.path.splitext(os.path.basename(items[0][1]))[0])
    if not match:
        return None
    stamp = parse_stamp(match.group('stamp'))
    if stamp is None:
        return None
    prefix = match.group('prefix')
    sources = [p for _i, p, _e in items]

    durations = [probe_duration(p) for p in sources]
    exact = all(d and d > 0 for d in durations)
    starts = [stamp]
    if exact:
        cursor = stamp
        starts = []
        for dur in durations:
            starts.append(cursor)
            cursor = cursor + timedelta(seconds=dur)
    else:
        for prev in sources[:-1]:
            try:
                starts.append(datetime.fromtimestamp(os.path.getmtime(prev)))
            except OSError:
                return None

    # 起点必须严格递增，否则说明时间戳/修改时间不可靠，宁可不改
    for i in range(1, len(starts)):
        if starts[i] <= starts[i - 1]:
            return None

    targets = []
    for (_idx, path, ext), start in zip(items, starts):
        targets.append(os.path.join(folder, f'{prefix}{start.strftime(SEG_FMT)}{ext}'))
    return {'stamp': stamp, 'starts': starts, 'sources': sources,
            'targets': targets, 'prefix': prefix, 'exact': exact}


# ---------------------------------------------------------------- 执行
def split_danmaku(xml_path: str, targets: list, starts: list, apply: bool, backup: bool,
                  make_ass: bool) -> list:
    """把整场 XML 按分段切开。返回每个分段写出的 xml 路径。"""
    header, records = read_xml(xml_path)
    base_starts = [(s - starts[0]).total_seconds() for s in starts]
    base_starts.append(float('inf'))
    chunks = []
    for i, target in enumerate(targets):
        out = f'{os.path.splitext(target)[0]}.xml'
        lo, hi = base_starts[i], base_starts[i + 1]
        rows = []
        for off, line in records:
            if off < lo or off >= hi:
                continue
            # 时间轴归零：减掉本段起点，其余属性原样保留
            rel = f'{max(off - lo, 0):.3f}'
            line = re.sub(r'(\bp=")\s*[0-9.]+', lambda m: m.group(1) + rel, line, count=1)
            line = re.sub(r'(\bts=")\s*[0-9.]+', lambda m: m.group(1) + rel, line, count=1)
            rows.append(line)
        chunks.append((out, rows, with_start_time(header, starts[i])))
        print(f'    分段 {i + 1}: {len(rows):>6} 条 -> {os.path.basename(out)}')
    if not apply:
        return [out for out, _r, _h in chunks]

    # 原文件先挪成 .bak（内容已经在内存里了），位置留给切出来的第一段
    out_paths = [os.path.abspath(out) for out, _r, _h in chunks]
    bak = xml_path + '.bak'
    if backup:
        if os.path.exists(bak):
            print(f'    .bak 已存在，保留旧的: {os.path.basename(bak)}')
        else:
            os.replace(xml_path, bak)
    for (out, rows, head) in chunks:
        os.makedirs(os.path.dirname(out) or '.', exist_ok=True)
        with open(out, 'w', encoding='utf-8', newline='') as f:
            f.write(head)
            f.writelines(rows)
            f.write('</i>\n')
        if make_ass:
            from src.danmaku_convert import convert_file
            res = convert_file(out, overwrite=True)
            if res['status'] != 'ok':
                print(f'      ! ASS 生成失败: {res["message"]}')
    if not backup and os.path.abspath(xml_path) not in out_paths and os.path.exists(xml_path):
        os.remove(xml_path)   # --no-backup 时，整场的老文件留着只会让人误用
    return [out for out, _r, _h in chunks]


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='老录像的弹幕按视频分段切开，并把文件命名统一成与视频同名')
    parser.add_argument('paths', nargs='+', help='录像根目录（会递归扫描）')
    parser.add_argument('--apply', action='store_true', help='真正执行（默认只打印计划）')
    parser.add_argument('--no-video', action='store_true', help='不改视频文件名，只处理弹幕')
    parser.add_argument('--no-backup', action='store_true', help='覆盖/替换前不保留 .bak')
    parser.add_argument('--ass', action='store_true', help='顺手给每段生成 .ass（会慢一些）')
    parser.add_argument('--only-split', action='store_true',
                        help='只处理「一段视频多份弹幕」的会话，跳过单段的改名')
    parser.add_argument('--active-minutes', type=float, default=10.0,
                        help='最近这么多分钟还在动的文件视为「正在录制」，整场跳过（默认 10）')
    args = parser.parse_args(argv)

    sessions = []
    for root in args.paths:
        if os.path.isfile(root):
            root = os.path.dirname(root) or '.'
        sessions.extend(scan_sessions(root))
    if not sessions:
        print('没有找到符合「主播_标题_时间戳[_序号]」命名的录像')
        return 1

    total_split = total_renamed = skipped_active = 0
    for (folder, prefix, stamp, ext), (items, files) in sessions:
        title = f'{prefix}{stamp}'
        xml_path = pick_xml(files)
        # 正在录制的一律不碰：文件名和内容都还在变，改了会错位
        if recently_touched([p for _i, p, _e in items] + files, args.active_minutes):
            print(f'[跳过] {title}{ext}：最近还在写入，可能正在录制')
            skipped_active += 1
            continue
        if xml_path and not xml_finished(xml_path):
            # 没 </i> 说明上次是强杀/断电收场的，记录本身还在，照切不误
            print(f'    （{os.path.basename(xml_path)} 没有收尾标签，按已写入的记录切分）')
        plan = plan_session(items, files, folder)
        if plan is None:
            print(f'[跳过] {title}{ext}：读不到分段时长/起止时间（缺 ffprobe 且文件时间不可靠）')
            continue
        seg_count = len(plan['targets'])
        moves = [(src, dst) for src, dst in zip(plan['sources'], plan['targets']) if src != dst]
        single = seg_count == 1

        if args.only_split and single:
            continue
        if not moves and not xml_path:
            continue
        if single and not xml_path:
            continue
        if single:
            # 已经是「与视频同名」的状态，重跑时不用再动（老的 .danmaku.ass 除外）
            stem = os.path.splitext(plan['targets'][0])[0]
            old_ass = f'{os.path.splitext(xml_path)[0]}.ass'
            ass_left = os.path.exists(old_ass) and os.path.abspath(old_ass) != os.path.abspath(f'{stem}.ass')
            if not moves and not ass_left and os.path.abspath(xml_path) == os.path.abspath(f'{stem}.xml'):
                continue

        print(f'[{"执行" if args.apply else "计划"}] {title}{ext}'
              f'（{seg_count} 段，弹幕 {os.path.basename(xml_path) if xml_path else "无"}）')
        if seg_count > 1 and not plan['exact']:
            print('    提示: 本机没有 ffprobe，按文件修改时间推算分段起点'
                  '（在容器里跑会改用真实时长，更准）')

        # 1) 视频改名：按每段开始时间
        if moves and not args.no_video:
            for src, dst in moves:
                print(f'    视频: {os.path.basename(src)} -> {os.path.basename(dst)}')
            if args.apply:
                for src, dst in moves:
                    if os.path.exists(dst) and os.path.abspath(src) != os.path.abspath(dst):
                        print(f'      ! 目标已存在，跳过: {os.path.basename(dst)}')
                        continue
                    os.replace(src, dst)
                total_renamed += len(moves)

        if not xml_path:
            continue

        # 2) 弹幕：单段只改名，多段按边界切开
        if single:
            dst = f'{os.path.splitext(plan["targets"][0])[0]}.xml'
            if os.path.abspath(xml_path) != os.path.abspath(dst):
                print(f'    弹幕: {os.path.basename(xml_path)} -> {os.path.basename(dst)}')
                if args.apply:
                    if os.path.exists(dst):
                        print(f'      ! 目标已存在，跳过: {os.path.basename(dst)}')
                    else:
                        os.replace(xml_path, dst)
                        total_renamed += 1
            # 同名的整场 .ass 也一起改（老文件带 .danmaku 中缀）
            ass_src = os.path.splitext(xml_path)[0] + '.ass'
            ass_dst = f'{os.path.splitext(plan["targets"][0])[0]}.ass'
            if (os.path.exists(ass_src) and os.path.abspath(ass_src) != os.path.abspath(ass_dst)
                    and not os.path.exists(ass_dst)):
                print(f'    字幕: {os.path.basename(ass_src)} -> {os.path.basename(ass_dst)}')
                if args.apply:
                    os.replace(ass_src, ass_dst)
                    total_renamed += 1
            if args.ass and args.apply:
                from src.danmaku_convert import convert_file
                res = convert_file(dst, overwrite=False)
                if res['status'] == 'skip':
                    print(f'    字幕已存在，跳过: {os.path.basename(res["ass"])}')
                elif res['status'] == 'ok':
                    print(f'    字幕: {res["message"]}')
                else:
                    print(f'    ! ASS 生成失败: {res["message"]}')
            continue

        # 多段：切开
        split_danmaku(xml_path, plan['targets'], plan['starts'], args.apply,
                      not args.no_backup, args.ass)
        total_split += 1
        old_ass = os.path.splitext(xml_path)[0] + '.ass'
        new_ass = {os.path.abspath(f'{os.path.splitext(t)[0]}.ass') for t in plan['targets']}
        if os.path.exists(old_ass) and os.path.abspath(old_ass) not in new_ass:
            # 整场字幕切完就没用了（它对应的是「一整段」，现在没有任何视频叫这个名字）
            print(f'    整场字幕改名留档: {os.path.basename(old_ass)}.bak'
                  + ('' if args.ass else '（要重新生成每段的 .ass 就加 --ass）'))
            if args.apply and not os.path.exists(old_ass + '.bak'):
                os.replace(old_ass, old_ass + '.bak')

    print(f'\n{"完成" if args.apply else "预览"}: 处理 {total_split} 场的分段切分，'
          f'改名 {total_renamed} 个文件'
          + (f'，跳过 {skipped_active} 场正在录制的' if skipped_active else ''))
    if not args.apply:
        print('确认无误后加 --apply 执行（覆盖前自动留 .bak）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
