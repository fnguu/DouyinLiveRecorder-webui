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

UNIT = '金瓜子'
PER_YUAN = 1000            # 1 元 = 1000 金瓜子
GUARD_DEDUPE_SECONDS = 5.0

TAG_RE = re.compile(r'^(\s*<(?:gift|sc|guard)\b)([^>]*?)(/?>)(.*)$', re.S)
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
    return f'{value:g}'


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

        already = any(MARKER in line for line in lines[:40])
        unit_prices = self._gift_unit_prices(lines) if self.fix_gift else {}

        out = []
        guard_seen = {}          # (uid, 5 秒桶) -> out 中的行号
        stats = dict(sc=0, guard=0, guard_dropped=0, gift_fixed=0, gift_unclear=0)

        for line in lines:
            m = TAG_RE.match(line)
            if not m:
                out.append(line)
                continue
            head, attrs, tail, rest = m.groups()
            tag = re.match(r'\s*<(\w+)', head).group(1)
            price = to_number(get_attr(attrs, 'price'))

            if tag in ('sc', 'guard'):
                if not already:
                    price = round(price * PER_YUAN)
                    attrs = set_attr(attrs, 'price', number_text(price))
                    stats['sc' if tag == 'sc' else 'guard'] += 1
                if get_attr(attrs, 'unit') is None:
                    attrs = set_attr(attrs, 'unit', UNIT)

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
        if not already:
            out = self._mark_header(out)

        self.stats = stats
        return {
            'lines': len(lines),
            'text': newline.join(out),
            'already_converted': already,
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


def main(argv=None):
    parser = argparse.ArgumentParser(description='把 B 站弹幕 XML 的金额统一成金瓜子')
    parser.add_argument('paths', nargs='+', help='文件或目录（目录会递归找 *.danmaku.xml）')
    parser.add_argument('--apply', action='store_true', help='真正写回文件（默认只打印计划）')
    parser.add_argument('--no-backup', action='store_true', help='写回时不生成 .bak')
    parser.add_argument('--no-dedupe-guard', action='store_true', help='不合并重复的上舰记录')
    parser.add_argument('--no-fix-gift', action='store_true', help='不修正连击礼物的金额')
    parser.add_argument('--force', action='store_true', help='非 B 站文件也强行处理')
    args = parser.parse_args(argv)

    files = collect(args.paths)
    if not files:
        print('没有找到 .danmaku.xml 文件')
        return 1

    conv = Converter(dedupe_guard=not args.no_dedupe_guard, fix_gift=not args.no_fix_gift)
    total = dict(sc=0, guard=0, guard_dropped=0, gift_fixed=0, gift_unclear=0)
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
        changed = any(stats[k] for k in ('sc', 'guard_dropped', 'gift_fixed')) or not result['already_converted']
        for k in total:
            total[k] += stats.get(k, 0)
        if not changed:
            print(f'= {name}: 已是金瓜子口径，无需处理')
            continue
        touched += 1
        flag = '已转换' if result['already_converted'] else '待转换'
        print(f'{"+" if args.apply else "·"} {name} [{flag}] '
              f"sc={stats['sc']} guard={stats['guard']} 去重={stats['guard_dropped']} "
              f"礼物补额={stats['gift_fixed']} 待确认={stats['gift_unclear']}")
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
