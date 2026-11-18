#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""把历史 B 站弹幕 XML 的金额统一成「金瓜子」。

背景：早期版本把三种量纲塞进了同一个 price 属性——礼物是金瓜子、醒目留言（SC）
和上舰是「元」。同一张表里 100 / 2 / 198 放在一起，根本分不清哪个是哪个。
现在录制端已统一成金瓜子（1 元 = 1000 金瓜子），这个脚本负责把老文件补齐。

三条改动（都可单独关掉）：

1. 金额换算：sc / guard 的 price 从「元」× 1000 换成金瓜子，并加 unit="金瓜子"
2. 上舰去重：同一个人同一档舰长在 5 秒内只留一条（B 站会同时发 GUARD_BUY 和
   USER_TOAST_MSG，价格可能一条原价一条优惠价，去重时取较低的那个）
3. 补礼物总额：连击礼物的 price 以前只记了单价（10 个人气票仍是 100），
   按同一文件里同名礼物「1 个的价格」反推单价，把 xN 的金额补成 单价 × N

默认只打印计划不动文件；加 --apply 才写回，写回前自动留 .bak。

用法：
    python tools/normalize_danmaku_xml.py L:/dlrw/downloads/B站直播
    python tools/normalize_danmaku_xml.py 某文件.danmaku.xml --apply
    python tools/normalize_danmaku_xml.py <目录> --apply --no-backup --no-dedupe-guard
"""
import argparse
import os
import re
import shutil
import sys
import xml.etree.ElementTree as ET

UNIT = '金瓜子'
PER_YUAN = 1000            # 1 元 = 1000 金瓜子
GUARD_DEDUPE_SECONDS = 5.0

TAG_RE = re.compile(r'^(\s*<(?:gift|sc|guard)\b)([^>]*?)(/?>)(.*)$', re.S)
RECORD_RE = re.compile(r'^\s*<(?:d|gift|sc|guard)\b')
MARKER = f'price_unit="{UNIT}"'


# ---------------------------------------------------------------- 行内属性
def get_attr(attrs: str, name: str):
    m = re.search(rf'\b{name}="([^"]*)"', attrs)
    return m.group(1) if m else None


def set_attr(attrs: str, name: str, value) -> str:
    """替换属性值；不存在就追加到末尾（保持原有属性顺序）。"""
    text = f'{name}="{value}"'
    if re.search(rf'\b{name}="[^"]*"', attrs):
        return re.sub(rf'\b{name}="[^"]*"', text, attrs, count=1)
    return attrs.rstrip() + ' ' + text if attrs.strip() else ' ' + text


def to_number(value, default=0.0) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def number_text(value: float) -> str:
    """金额一律写成普通数字：%g 会把 1598000 缩成 1.598e+06。"""
    value = float(value)
    if value.is_integer():
        return str(int(value))
    return f'{value:.3f}'.rstrip('0').rstrip('.')


# ---------------------------------------------------------------- 主体
class Converter:
    def __init__(self, dedupe_guard=True, fix_gift=True):
        self.dedupe_guard = dedupe_guard
        self.fix_gift = fix_gift
        self.stats = dict(sc=0, guard=0, guard_dropped=0, gift_fixed=0, gift_unclear=0)

    # ---------- 单文件
    def convert(self, path: str, force: bool = False) -> dict:
        with open(path, 'r', encoding='utf-8', errors='replace', newline='') as f:
            text = f.read()
        newline = '\r\n' if '\r\n' in text else '\n'
        lines = text.split(newline)

        if not force and not self._is_bilibili(lines):
            return {'skipped': '看起来不是 B 站弹幕文件（没有 sc/guard 板块）'}

        if not force and not text.rstrip().endswith('</i>'):
            return {'skipped': '文件还没收尾（缺 </i>，可能正在录制），跳过以免跟录制进程抢写'}

        header_marked = any(MARKER in line for line in lines[:40])
        unit_prices = self._gift_unit_prices(lines) if self.fix_gift else {}

        out = []
        guard_seen = {}          # (uid, 5 秒桶) -> out 中的行号
        stats = dict(sc=0, guard=0, guard_dropped=0, gift_fixed=0, gift_unit=0,
                     gift_unclear=0, header=0, price_fmt=0)

        for line in lines:
            m = TAG_RE.match(line)
            if not m:
                out.append(line)
                continue
            head, attrs, tail, rest = m.groups()
            tag = re.match(r'\s*<(\w+)', head).group(1)
            raw_price = get_attr(attrs, 'price')
            price = to_number(raw_price)
            # 早期版本用 %g 写金额，1998000 会被写成 1.998e+06，表格里像乱码
            if raw_price and 'e' in raw_price.lower():
                attrs = set_attr(attrs, 'price', number_text(price))
                stats['price_fmt'] += 1

            if tag in ('sc', 'guard'):
                # 逐条判断有没有 unit：容忍「转换到一半又被录制进程续写」的文件
                if get_attr(attrs, 'unit') is None:
                    price = round(price * PER_YUAN)
                    attrs = set_attr(attrs, 'price', number_text(price))
                    attrs = set_attr(attrs, 'unit', UNIT)
                    stats['sc' if tag == 'sc' else 'guard'] += 1

            if tag == 'guard':
                if self.dedupe_guard:
                    keep = self._guard_key(attrs, guard_seen, out)
                    if keep is False:
                        stats['guard_dropped'] += 1
                        continue
                    if keep is not None:
                        idx, old_price = keep
                        stats['guard_dropped'] += 1
                        # 已写过的那条：价格取低的那个（原价/优惠价成对出现时更接近实付）
                        if old_price is not None and old_price != price:
                            o_head, o_attrs, o_tail, o_rest = TAG_RE.match(out[idx]).groups()
                            o_attrs = set_attr(o_attrs, 'price', number_text(min(old_price, price)))
                            out[idx] = f'{o_head}{o_attrs}{o_tail}{o_rest}'
                        continue


            if tag == 'gift':
                if get_attr(attrs, 'unit') is None:
                    attrs = set_attr(attrs, 'unit', UNIT)
                    stats['gift_unit'] += 1
                if self.fix_gift:
                    name = get_attr(attrs, 'giftname') or ''
                    count = int(to_number(get_attr(attrs, 'giftcount'), 1)) or 1
                    unit = unit_prices.get(name)
                    if count > 1 and unit and abs(price - unit) < 1e-6:
                        price = unit * count
                        attrs = set_attr(attrs, 'price', number_text(price))
                        stats['gift_fixed'] += 1
                    elif count > 1 and unit and price < unit * count:
                        stats['gift_unclear'] += 1

            out.append(f'{head}{attrs}{tail}{rest}')

        # 头部：表头补单位、RecordInfo 加 price_unit
        if not header_marked:
            out = self._mark_header(out)
            stats['header'] = 1

        self.stats = stats
        return {
            'lines': len(lines),
            'text': newline.join(out),
            'already_converted': header_marked,
            'stats': stats,
        }

    # ---------- 辅助
    @staticmethod
    def _is_bilibili(lines) -> bool:
        for line in lines:
            if line.lstrip().startswith(('<sc', '<guard')):
                return True
            if '<h2 id="sc">' in line or '<h2 id="guard">' in line:
                return True
        return False

    @staticmethod
    def _gift_unit_prices(lines) -> dict:
        """同一个文件里，同名礼物「1 个」的金额就是单价，用它反推连击礼物的总额。"""
        prices = {}
        for line in lines:
            m = TAG_RE.match(line)
            if not m or '<gift' not in m.group(1):
                continue
            attrs = m.group(2)
            count = int(to_number(get_attr(attrs, 'giftcount'), 1)) or 1
            price = to_number(get_attr(attrs, 'price'))
            if count == 1 and price > 0:
                name = get_attr(attrs, 'giftname') or ''
                prices[name] = min(prices.get(name, price), price)
        return prices

    @staticmethod
    def _guard_key(attrs: str, seen: dict, out: list):
        """返回 None=保留，False=直接丢弃，或 (行号, 已记录价格)=丢弃但可能要改价。"""
        uid = get_attr(attrs, 'uid') or ''
        level = get_attr(attrs, 'guardname') or ''
        ts = to_number(get_attr(attrs, 'ts'))
        key = (uid, level, int(ts // GUARD_DEDUPE_SECONDS))
        price = to_number(get_attr(attrs, 'price'))
        if key in seen:
            idx = seen[key]
            old = to_number(get_attr(TAG_RE.match(out[idx]).group(2), 'price'))
            return idx, old
        seen[key] = len(out)
        return None

    @staticmethod
    def _mark_header(lines):
        out = []
        for line in lines:
            if '<RecordInfo' in line and MARKER not in line:
                line = re.sub(r'(<RecordInfo\b[^>]*?)(\s*/>)',
                              lambda m: f'{m.group(1)} {MARKER}{m.group(2)}', line, count=1)
            line = line.replace('<th>金额</th>', f'<th>金额（{UNIT}）</th>')
            line = line.replace('<th>价格</th>', f'<th>价格（{UNIT}）</th>')
            out.append(line)
        return out


def collect(paths):
    files = []
    for item in paths:
        if os.path.isdir(item):
            for root, _dirs, names in os.walk(item):
                for name in sorted(names):
                    if name.endswith('.danmaku.xml'):
                        files.append(os.path.join(root, name))
        elif os.path.isfile(item):
            files.append(item)
        else:
            print(f'跳过不存在的路径: {item}')
    return files



def _ass_index(xml_path: str) -> list:
    """同名 .ass 里的弹幕行 -> [(开始秒, 文本)]，用于反查被截断那条的内容。"""
    ass_path = xml_path[:-4] + '.ass' if xml_path.lower().endswith('.xml') else xml_path + '.ass'
    if not os.path.exists(ass_path):
        return []
    rows = []
    with open(ass_path, 'r', encoding='utf-8', errors='replace') as f:
        for line in f:
            m = re.match(r'Dialogue: 0,(\d+):(\d{2}):([\d.]+),(\d+):(\d{2}):([\d.]+),Danmaku,', line)
            if not m:
                continue
            start = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
            body = line.split(',,', 1)[-1].rstrip('\r\n')
            text = body.split('}', 1)[1] if '}' in body else body
            rows.append((start, text))
    return rows


def _repair_without_bak(path: str, apply: bool = False, force: bool = False) -> dict:
    """没有 .bak 时的外科修复：只把粘连的那一行拆成两条完整记录。"""
    with open(path, 'r', encoding='utf-8', errors='replace', newline='') as f:
        cur_text = f.read()
    if not force and not cur_text.rstrip().endswith('</i>'):
        return {'error': '文件还没收尾（缺 </i>，仍在录制），等这场录完再修'}

    newline = '\r\n' if '\r\n' in cur_text else '\n'
    lines = cur_text.split(newline)
    bad_idx = next((i for i, ln in enumerate(lines) if ln.count('<d p=') > 1), None)
    if bad_idx is None:
        return {'error': '没找到被顶掉的拼接行，无需修复'}

    bad_line = lines[bad_idx]
    second = bad_line.find('<d p=', bad_line.find('<d p=') + 1)
    head, tail = bad_line[:second].rstrip(), bad_line[second:].strip()
    m = re.match(r'\s*<d p="([^"]*)', head)
    prefix = m.group(1) if m else ''
    try:
        offset = float(prefix.split(',')[0])
    except (IndexError, ValueError):
        offset = 0.0

    index = _ass_index(path)
    text = ''
    if index:
        near = min(index, key=lambda r: abs(r[0] - offset))
        if abs(near[0] - offset) <= 0.2:
            text = near[1]
    user, _, content = text.partition(':') if text else ('', '', '')

    rebuilt = ''
    if content:
        uid = phash = ''
        hit = re.search(r'<d p="([^"]*)" user="' + re.escape(user) + r'">', cur_text)
        if hit:
            fields = hit.group(1).split(',')
            if len(fields) >= 8:
                uid, phash = fields[6], fields[7]
        rebuilt = f'  <d p="{prefix},{uid or "0"},{phash}" user="{user}">{content}</d>'

    new_lines = lines[:bad_idx] + ([rebuilt] if rebuilt else []) + [tail] + lines[bad_idx + 1:]
    new_text = newline.join(new_lines)
    try:
        ET.fromstring(re.sub(r'<XmlStyle>.*?</XmlStyle>', '', new_text, flags=re.S))
    except ET.ParseError as exc:
        return {'error': f'拼出来的内容解析失败，已放弃写入：{exc}'}

    if apply:
        shutil.copy2(path, path + '.damaged')
        with open(path, 'w', encoding='utf-8', newline='') as f:
            f.write(new_text)
    return {'recovered': rebuilt.strip()[:100], 'dropped_fragment': '' if rebuilt else head.strip()[:100],
            'bak_records': 0, 'after_records': 0, 'source': 'ass' if rebuilt else 'none'}


def repair_file(path: str, conv: 'Converter', apply: bool = False, force: bool = False) -> dict:
    """修复「录制中被写坏」的文件：某一行被续写的记录顶掉，两段粘在一起。

    成因：录制进程的 XML 句柄是普通写模式（不是 O_APPEND），外部脚本改写文件后，
    它下一次写入仍按自己的旧偏移落笔，正好压在上一条记录身上。表现就是
    ``<d p="旧记录前段  <d p="新记录全文</d>`` 挤在同一行，旧记录尾部丢失。

    修复办法有两条：
      1. 有同名 .bak（被改写之前的完整快照）：以它里面的记录为准，拼上当前文件里
         损坏行之后的记录，整份重排重写；
      2. 没有 .bak：只做外科手术 —— 从同名 .ass 里取回被截断那条的文本（录制结束
         时是按内存里的完整记录渲染的，所以 .ass 里内容是全的），补成完整一行，
         再把粘连的后半段放回下一行。取不到 .ass 或对不上时间就只保留后半段，
         并在结果里标明丢了哪条。
    """
    bak = path + '.bak'
    if not os.path.exists(bak):
        return _repair_without_bak(path, apply=apply, force=force)
    with open(path, 'r', encoding='utf-8', errors='replace', newline='') as f:
        cur_text = f.read()
    if not force and not cur_text.rstrip().endswith('</i>'):
        return {'error': '文件还没收尾（缺 </i>，仍在录制），等这场录完再修'}

    newline = '\r\n' if '\r\n' in cur_text else '\n'
    lines = cur_text.split(newline)
    bad_idx = next((i for i, ln in enumerate(lines) if ln.count('<d p=') > 1), None)
    if bad_idx is None:
        return {'error': '没找到被顶掉的拼接行，无需修复'}

    bad_line = lines[bad_idx]
    second = bad_line.find('<d p=', bad_line.find('<d p=') + 1)
    recovered = bad_line[second:].strip() if second > 0 else ''

    head_end = next((i for i, ln in enumerate(lines) if '</XmlStyle>' in ln), None)
    if head_end is None:
        return {'error': '找不到文件头部（XmlStyle），不敢动'}
    header = lines[:head_end + 1]

    with open(bak, 'r', encoding='utf-8', errors='replace', newline='') as f:
        bak_text = f.read()
    bak_records = [ln for ln in bak_text.split('\n') if RECORD_RE.match(ln)]
    after = [ln for ln in lines[bad_idx + 1:] if RECORD_RE.match(ln)]

    merged = header + bak_records + ([recovered] if recovered else []) + after + ['</i>']
    text = newline.join(merged)
    try:
        ET.fromstring(text)
    except ET.ParseError as exc:
        return {'error': f'拼出来的内容解析失败，已放弃写入：{exc}'}

    if apply:
        shutil.copy2(path, path + '.damaged')
        with open(path, 'w', encoding='utf-8', newline='') as f:
            f.write(text)
        # 拼回来的旧记录可能还是「元」口径，顺手再走一遍归一化
        result = conv.convert(path, force=True)
        if 'text' in result:
            with open(path, 'w', encoding='utf-8', newline='') as f:
                f.write(result['text'])
    return {'recovered': recovered[:80], 'bak_records': len(bak_records),
            'after_records': len(after), 'text': text}


def main(argv=None):
    parser = argparse.ArgumentParser(description='把 B 站弹幕 XML 的金额统一成金瓜子')
    parser.add_argument('paths', nargs='+', help='文件或目录（目录会递归找 *.danmaku.xml）')
    parser.add_argument('--apply', action='store_true', help='真正写回文件（默认只打印计划）')
    parser.add_argument('--no-backup', action='store_true', help='写回时不生成 .bak')
    parser.add_argument('--no-dedupe-guard', action='store_true', help='不合并重复的上舰记录')
    parser.add_argument('--no-fix-gift', action='store_true', help='不修正连击礼物的金额')
    parser.add_argument('--force', action='store_true', help='非 B 站文件也强行处理')
    parser.add_argument('--repair', action='store_true',
                        help='修复「录制中被外部改写、导致两段记录粘在一行」的文件（需同名 .bak）')
    args = parser.parse_args(argv)

    files = collect(args.paths)
    if not files:
        print('没有找到 .danmaku.xml 文件')
        return 1

    conv = Converter(dedupe_guard=not args.no_dedupe_guard, fix_gift=not args.no_fix_gift)

    if args.repair:
        for path in files:
            info = repair_file(path, conv, apply=args.apply, force=args.force)
            name = os.path.basename(path)
            if 'error' in info:
                print(f'✗ {name}: {info["error"]}')
                continue
            mark = '+' if args.apply else '·'
            tail = '，已写回（原文件留为 .damaged）' if args.apply else '（未改动，加 --apply 写回）'
            if info.get('source') == 'ass':
                print(f'{mark} {name}: 从 .ass 取回被截断的那条 —— {info["recovered"]}{tail}')
            elif info.get('source') == 'none':
                print(f'{mark} {name}: 找不到同名 .ass，丢弃不可还原的片段「{info["dropped_fragment"]}」{tail}')
            else:
                print(f'{mark} {name}: '
                      f'从 .bak 取回 {info["bak_records"]} 条记录，损坏行之后 {info["after_records"]} 条{tail}')
        return 0
    total = dict(sc=0, guard=0, guard_dropped=0, gift_fixed=0, gift_unit=0,
                 gift_unclear=0, header=0, price_fmt=0)
    touched = 0

    for path in files:
        name = os.path.basename(path)
        try:
            result = conv.convert(path, force=args.force)
        except Exception as exc:
            print(f'✗ {name}: {exc}')
            continue
        if 'skipped' in result:
            print(f'- {name}: {result["skipped"]}')
            continue
        stats = result['stats']
        changed = any(stats.values())
        for k in total:
            total[k] += stats.get(k, 0)
        if not changed:
            print(f'= {name}: 已是金瓜子口径，无需处理')
            continue
        touched += 1
        flag = '部分已转换' if result['already_converted'] else '待转换'
        print(f'{"+" if args.apply else "·"} {name} [{flag}] '
              f"sc={stats['sc']} 上舰={stats['guard']} 去重={stats['guard_dropped']} "
              f"礼物补额={stats['gift_fixed']} 礼物标注={stats['gift_unit']} "
              f"待确认={stats['gift_unclear']}")
        if args.apply:
            if not args.no_backup:
                shutil.copy2(path, path + '.bak')
            with open(path, 'w', encoding='utf-8', newline='') as f:
                f.write(result['text'])

    print()
    if not touched:
        tail = '（无需改动）'
    else:
        tail = '（已写回）' if args.apply else '（未改动，加 --apply 写回）'
    print(f'文件 {len(files)} 个，需处理 {touched} 个{tail}')
    print(f"金额换算: sc {total['sc']} 条，上舰 {total['guard']} 条；"
          f"上舰去重 {total['guard_dropped']} 条；连击礼物补额 {total['gift_fixed']} 条")
    if total['gift_unclear']:
        print(f"另有 {total['gift_unclear']} 条连击礼物的单价无法判定，保持原样（多为同名礼物没有单发记录）")
    return 0


if __name__ == '__main__':
    sys.exit(main())
