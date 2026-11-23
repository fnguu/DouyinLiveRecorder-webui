# -*- encoding: utf-8 -*-
"""弹幕落盘：B 站弹幕格式 XML + ASS 字幕（抖音、B站共用）。

XML 采用 mikufans录播姬（BililiveRecorder）同款结构，播放器、DanmakuFactory、
弹幕转换工具都认这个结构，且自带 XSLT 样式 —— 直接双击用浏览器打开就能看到
「用户名 / 出现时间 / 用户ID / 内容 / 参数」的可视化表格，不需要额外工具：

    <?xml version="1.0" encoding="UTF-8"?>
    <?xml-stylesheet type="text/xsl" href="#s"?>
    <i>
      <chatserver>chat.bilibili.com</chatserver>
      <chatid>房间号</chatid>
      <RecordInfo app_version=".." roomid=".." name="主播名" start_time=".." />
      <XmlStyle>…内嵌 XSLT 样式表（id=s）…</XmlStyle>
      <d p="出现时间,模式,字号,颜色,时间戳,弹幕池,用户ID,弹幕ID" user="用户名">内容</d>
      <gift ts=".." user=".." uid=".." giftname=".." giftcount=".." price=".."/>
      <sc ts=".." user=".." uid=".." price="..">内容</sc>
    </i>

p 的 8 个字段：
    0 出现时间   相对录制开始的秒数，保留 3 位小数
    1 模式       1/2/3 滚动，4 底部固定，5 顶部固定
    2 字号       B 站默认 25，这里按弹幕原始字号
    3 颜色       十进制 RGB，例如 16777215 = 白
    4 时间戳     发送时刻的 Unix 毫秒
    5 弹幕池     0 普通、1 字幕、2 特殊
    6 用户ID     有真实 uid 就写明文（录播姬同款），拿不到才退回 crc32 哈希
    7 弹幕 ID    平台给的 dmid，没有就用序号

XML 是边抓边写的：先写头部，每条弹幕立刻追加一行 `<d>`，结束（或容器被杀）
才补 `</i>`。所以中途断掉的文件只是缺收尾标签，绝大部分内容仍然可用——
这一点和 BililiveRecorder 的做法一致，长直播不会因为一次崩溃丢掉整场弹幕。

礼物/醒目留言/上舰写在独立的 <gift>/<sc>/<guard> 标签里：播放器和转换工具
只读 <d>，不会被这些消息干扰；浏览器预览时则在单独的表格里展示。
抖音没有 SC/上舰概念，生成的 XML 不带这两个板块（少两行无意义的 0 计数）。
"""
import binascii
import os
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Iterable, Optional

from .logger import logger

# 只有真正"有文字"的消息才进 <d>：进场/点赞这些是弹幕之外的附带信息，
# 写进 XML 会被播放器当成奇怪的弹幕。
DM_TYPES = ('chat', 'emoji')
# 旧常量名保留，避免外部引用失效
XML_TYPES = {'chat', 'emoji', 'sc'}

APP_NAME = 'DouyinLiveRecorder'
REPO_URL = 'https://github.com/fnguu/DouyinLiveRecorder-webui'
try:
    from webui.version import __version__ as APP_VERSION
except Exception:  # pragma: no cover - 单独分发 src 时没有 webui 包
    APP_VERSION = 'unknown'


def esc(text: str) -> str:
    """XML 文本转义。弹幕里出现 & < > 的概率不低，不转义整个文件就废了。"""
    return (str(text or '')
            .replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;')
            .replace('"', '&quot;'))


def uid_hash(uid) -> str:
    """B 站官方 XML 里放的是 uid 的 crc32，不是明文；仅在没有真实 uid 时兜底。"""
    raw = str(uid or '0').encode('utf-8')
    return f'{binascii.crc32(raw) & 0xffffffff:08x}'


def _uid_text(record: dict) -> str:
    """p[6] 与预览表格的用户ID：优先真实 uid（数字），拿不到才退回 crc32 哈希。"""
    uid = str(record.get('user_id') or '')
    return uid if uid.isdigit() else uid_hash(uid)


def xml_mode(record: dict) -> int:
    """把消息类型映射成 B 站的 8 种弹幕模式里最接近的一种。

    1/2/3 = 滚动（右→左），4 = 底部固定，5 = 顶部固定，6 = 逆向滚动（左→右）。
    DanmakuFactory 就是按这六个值分流的，所以 6 必须原样保留，不能并进 1。
    """
    mode = record.get('mode')
    try:
        mode = int(mode)
    except (TypeError, ValueError):
        mode = 0
    if mode in (1, 2, 3, 4, 5, 6):
        return mode
    return 1  # 默认滚动


def _offset_text(record: dict) -> str:
    return f'{max(float(record.get("offset") or 0), 0.0):.3f}'


def _price_text(record: dict) -> str:
    """金额写成人能读的数字。

    不能用 %g：它的默认精度是 6 位有效数字，1998000 会变成 1.998e+06，
    表格里直接显示成科学计数法。整数一律按整数输出，非整数最多保留 3 位小数。
    """
    try:
        price = float(record.get('diamond') or record.get('price') or 0)
    except (TypeError, ValueError):
        price = 0.0
    if price.is_integer():
        return str(int(price))
    return f'{price:.3f}'.rstrip('0').rstrip('.')


def _unit_attr(record: dict) -> str:
    """金额单位显式写进属性：B 站是金瓜子，抖音是抖币，省略表示沿用旧文件的约定。"""
    unit = str(record.get('price_unit') or '').strip()
    return f' unit="{esc(unit)}"' if unit else ''


# ---------------------------------------------------------------------- 内嵌样式
# 照录播姬（BililiveRecorder）的思路：把 XSLT 样式表塞在 XML 里，
# 浏览器打开时把 <d>/<gift>/<sc>/<guard> 渲染成表格。注意整个字符串必须是
# 合法 XML：JS 里不能出现裸的 < 和 &。
#
# sections 控制文件里有哪些板块：抖音没有 SC/上舰，就不生成对应表格。

_FULL_SECTIONS = ('d', 'gift', 'sc', 'guard')


def _stylesheet(sections=_FULL_SECTIONS, price_unit: str = '') -> str:
    s = set(sections or _FULL_SECTIONS)
    suffix = f'（{price_unit}）' if price_unit else ''
    money_head = f'金额{suffix}'
    price_head = f'价格{suffix}'
    rows = [f'<tr><td><a href="#d">弹幕</a></td><td>共<z:value-of select="count(/i/d)"/>条记录</td></tr>']
    if 'gift' in s:
        rows.append('<tr><td><a href="#gift">礼物</a></td><td>共<z:value-of select="count(/i/gift)"/>条记录</td></tr>')
    if 'sc' in s:
        rows.append('<tr><td><a href="#sc">SC</a></td><td>共<z:value-of select="count(/i/sc)"/>条记录</td></tr>')
    if 'guard' in s:
        rows.append('<tr><td><a href="#guard">上舰</a></td><td>共<z:value-of select="count(/i/guard)"/>条记录</td></tr>')
    meta_rows = ''.join(rows)

    extra_tables = ''
    if 'gift' in s:
        extra_tables += (
            '<h2 id="gift">礼物</h2>'
            '<div id="gf"><table>'
            '<tr><th>用户名</th><th>出现时间</th><th>用户ID</th><th>礼物</th><th>数量</th>'
            f'<th>{money_head}</th></tr>'
            '<z:for-each select="/i/gift"><tr>'
            '<td><z:value-of select="@user"/></td><td><z:value-of select="@ts"/></td>'
            '<td><z:value-of select="@uid"/></td><td><z:value-of select="@giftname"/></td>'
            '<td><z:value-of select="@giftcount"/></td><td><z:value-of select="@price"/></td>'
            '</tr></z:for-each>'
            '</table></div>'
        )
    if 'sc' in s:
        extra_tables += (
            '<h2 id="sc">SC</h2>'
            '<div id="sct"><table>'
            '<tr><th>用户名</th><th>出现时间</th><th>用户ID</th><th>内容</th>'
            f'<th>{price_head}</th></tr>'
            '<z:for-each select="/i/sc"><tr>'
            '<td><z:value-of select="@user"/></td><td><z:value-of select="@ts"/></td>'
            '<td><z:value-of select="@uid"/></td><td><z:value-of select="."/></td>'
            '<td><z:value-of select="@price"/></td>'
            '</tr></z:for-each>'
            '</table></div>'
        )
    if 'guard' in s:
        extra_tables += (
            '<h2 id="guard">上舰</h2>'
            '<div id="gd"><table>'
            '<tr><th>用户名</th><th>出现时间</th><th>用户ID</th><th>舰团</th>'
            f'<th>{money_head}</th></tr>'
            '<z:for-each select="/i/guard"><tr>'
            '<td><z:value-of select="@user"/></td><td><z:value-of select="@ts"/></td>'
            '<td><z:value-of select="@uid"/></td><td><z:value-of select="@guardname"/></td>'
            '<td><z:value-of select="@price"/></td>'
            '</tr></z:for-each>'
            '</table></div>'
        )

    return (
        '<z:stylesheet version="1.0" id="s" xml:id="s" xmlns:z="http://www.w3.org/1999/XSL/Transform">'
        '<z:output method="html"/>'
        '<z:template match="/">'
        '<html><meta name="viewport" content="width=device-width"/>'
        '<title>' + APP_NAME + ' 弹幕XML文件 - <z:value-of select="/i/RecordInfo/@name"/></title>'
        '<style>body{margin:0}h1,h2,p,table{margin-left:5px}table{border-spacing:0}'
        'td,th{border:1px solid grey;padding:1px}th{position:sticky;top:0;background:#4098de}'
        'tr:hover{background:#d9f4ff}div{overflow:auto;max-height:80vh;max-width:100vw;width:fit-content}</style>'
        '<h1><a href="' + REPO_URL + '">' + APP_NAME + '</a> 弹幕XML文件</h1>'
        '<p>本文件自带样式，推荐使用 Chrome Firefox Edge 浏览器打开。</p>'
        '<p>弹幕信息兼容 B 站直播弹幕 XML 格式，可用 DanmakuFactory 等工具转换成 ASS 字幕。</p>'
        '<table>'
        '<tr><td>版本</td><td><z:value-of select="/i/RecordInfo/@app_version"/></td></tr>'
        '<tr><td>房间号</td><td><z:value-of select="/i/RecordInfo/@roomid"/></td></tr>'
        '<tr><td>主播名</td><td><z:value-of select="/i/RecordInfo/@name"/></td></tr>'
        '<tr><td>录制开始时间</td><td><z:value-of select="/i/RecordInfo/@start_time"/></td></tr>'
        + meta_rows +
        '</table>'
        '<h2 id="d">弹幕</h2>'
        '<div id="dm"><table>'
        '<tr><th>用户名</th><th>出现时间</th><th>用户ID</th><th>弹幕</th><th>参数</th></tr>'
        '<z:for-each select="/i/d"><tr>'
        '<td><z:value-of select="@user"/></td><td></td><td></td>'
        '<td><z:value-of select="."/></td><td><z:value-of select="@p"/></td>'
        '</tr></z:for-each>'
        '</table></div>'
        + extra_tables +
        '<script>'
        'Array.from(document.querySelectorAll("#dm tr")).slice(1).map(function(t){'
        'var td=t.querySelectorAll("td");var p=td[4].textContent.split(",");'
        'var a=parseFloat(p[0]);if(isNaN(a)){return;}'
        'var h=Math.floor(a/3600);var m=Math.floor(a/60)%60;var s=a%60;'
        'td[1].textContent=(h+"").padStart(2,"0")+":"+(m+"").padStart(2,"0")+":"+s.toFixed(3).padStart(6,"0");'
        'td[2].textContent=p[6]||"";});'
        '</script>'
        '</html>'
        '</z:template>'
        '</z:stylesheet>'
    )


def xml_header(room_id, anchor_name='', start_time='', version=None, sections=None,
               price_unit: str = '') -> str:
    v = str(version or APP_VERSION)
    s = tuple(sections) if sections else _FULL_SECTIONS
    tags = 'd 为弹幕'
    if 'gift' in s:
        tags += ', gift 为礼物'
    if 'sc' in s:
        tags += ', sc 为醒目留言'
    if 'guard' in s:
        tags += ', guard 为上舰'
    if price_unit:
        tags += f'；gift/sc/guard 的 price 统一为{price_unit}'
    unit_attr = f' price_unit="{esc(price_unit)}"' if price_unit else ''
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<?xml-stylesheet type="text/xsl" href="#s"?>\n'
        '<i>\n'
        f'  <!--\n{APP_NAME} {v}\n{REPO_URL}\n'
        '本文件的弹幕信息兼容 B 站直播弹幕 XML 格式，自带浏览器预览样式。\n'
        f'{tags}。播放器与转换工具只读取 d 标签。\n'
        '-->\n'
        '  <chatserver>chat.bilibili.com</chatserver>\n'
        f'  <chatid>{esc(room_id)}</chatid>\n'
        '  <mission>0</mission>\n'
        '  <maxlimit>99999</maxlimit>\n'
        '  <state>0</state>\n'
        '  <real_name>0</real_name>\n'
        '  <source>k-v</source>\n'
        f'  <RecordInfo app_version="{esc(v)}" roomid="{esc(room_id)}"'
        f' name="{esc(anchor_name)}" start_time="{esc(start_time)}"{unit_attr} />\n'
        f'  <XmlStyle>{_stylesheet(s, price_unit)}</XmlStyle>\n'
    )


def xml_line(record: dict, index: int = 0) -> str:
    """一条弹幕 -> 一行 <d>（带 user 属性，录播姬同款）。"""
    ts = record.get('ts')
    try:
        ts = int(ts) if ts else int(time.time() * 1000)
    except (TypeError, ValueError):
        ts = int(time.time() * 1000)
    dmid = record.get('dmid') or record.get('rowid') or index
    size = record.get('size')
    try:
        size = int(size) if int(size) > 0 else 25
    except (TypeError, ValueError):
        size = 25
    color = record.get('color')
    try:
        color = int(color)
    except (TypeError, ValueError):
        color = 16777215
    p = (f'{_offset_text(record)},{xml_mode(record)},{size},{color},{ts},0,'
         f'{_uid_text(record)},{esc(dmid)}')
    return (f'  <d p="{p}" user="{esc(record.get("user"))}">'
            f'{esc(record.get("content"))}</d>\n')


def _seconds_text(record: dict) -> str:
    """消息自带的显示时长（秒）。DanmakuFactory 就是读 XML 的 time 属性决定消息框停多久。"""
    for key in ('duration', 'sc_time'):
        try:
            val = float(record.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if val > 0:
            return f'{val:g}'
    return ''


def gift_line(record: dict) -> str:
    """一条礼物 -> 一行 <gift>（自闭合，播放器不会当成弹幕）。

    time 是这条礼物该显示多久（秒）：B 站下发的 combo_stay_time，没有就不写，
    由转换端按 DanmakuFactory 的兜底值（5 秒）处理。
    """
    secs = _seconds_text(record)
    time_attr = f' time="{secs}"' if secs else ''
    return (f'  <gift ts="{_offset_text(record)}" user="{esc(record.get("user"))}"'
            f' uid="{esc(_uid_text(record))}"'
            f' giftname="{esc(record.get("gift_name") or record.get("content") or "礼物")}"'
            f' giftcount="{esc(record.get("gift_count") or 1)}"'
            f' price="{_price_text(record)}"{_unit_attr(record)}{time_attr}/>\n')


def sc_line(record: dict) -> str:
    """一条醒目留言 -> 一行 <sc>。

    time 是 B 站给的这条 SC 的显示时长（秒）。DanmakuFactory 直接拿它当消息框
    停留时长（XmlFile.c 里把 time 属性读成 gift.duration），我们保持一致。
    """
    secs = _seconds_text(record)
    time_attr = f' time="{secs}"' if secs else ''
    return (f'  <sc ts="{_offset_text(record)}" user="{esc(record.get("user"))}"'
            f' uid="{esc(_uid_text(record))}"'
            f' price="{_price_text(record)}"{_unit_attr(record)}{time_attr}>'
            f'{esc(record.get("content"))}</sc>\n')


def guard_line(record: dict) -> str:
    """一条上舰 -> 一行 <guard>（自闭合）。guardname 如 舰长/提督/总督。

    level 保留 B 站的档位（1 总督 / 2 提督 / 3 舰长），DanmakuFactory 靠它
    反推价格与停留时长；time 有就一并写上。
    """
    secs = _seconds_text(record)
    time_attr = f' time="{secs}"' if secs else ''
    try:
        level = int(record.get('guard_level') or 0)
    except (TypeError, ValueError):
        level = 0
    level_attr = f' level="{level}"' if level in (1, 2, 3) else ''
    return (f'  <guard ts="{_offset_text(record)}" user="{esc(record.get("user"))}"'
            f' uid="{esc(_uid_text(record))}"'
            f' guardname="{esc(record.get("guard_name") or record.get("gift_name") or "舰长")}"'
            f' price="{_price_text(record)}"{_unit_attr(record)}{level_attr}{time_attr}/>\n')


class XmlWriter:
    """增量写弹幕 XML。close() 之前文件始终是"未收尾"状态。

    sections 控制头部样式里生成哪些板块（抖音没有 SC/上舰，只传 d+gift）。
    price_unit 标明 gift/sc/guard 的 price 单位（B 站传「金瓜子」），会写进
    表头和 RecordInfo，省得以后又有人对着 100 / 2 / 198 猜单位。
    """

    def __init__(self, path: str, room_id='0', anchor_name='', start_time=None,
                 sync_existing: bool = False, sections=None, price_unit: str = ''):
        self.path = path
        self.room_id = room_id
        self.count = 0
        os.makedirs(os.path.dirname(os.path.abspath(path)) or '.', exist_ok=True)
        mode = 'a' if sync_existing and os.path.exists(path) else 'w'
        self._fh = open(path, mode, encoding='utf-8')
        if mode == 'w':
            if not start_time:
                start_time = datetime.now().astimezone().isoformat(timespec='seconds')
            self._fh.write(xml_header(room_id, anchor_name, start_time,
                                      sections=sections, price_unit=price_unit))
            self._fh.flush()

    def append(self, records: Iterable[dict], fsync: bool = False) -> int:
        """按类型分派：chat/emoji 走 <d>，gift 走 <gift>，sc 走 <sc>，guard 走 <guard>。
        返回本次写入的弹幕条数（礼物/SC/上舰不计入弹幕数）。"""
        rows = []
        dm_count = 0
        for r in records:
            rtype = str(r.get('type') or 'chat').lower()
            if rtype == 'gift':
                rows.append(gift_line(r))
                continue
            if rtype == 'sc':
                rows.append(sc_line(r))
                continue
            if rtype == 'guard':
                rows.append(guard_line(r))
                continue
            if rtype not in ('chat', 'emoji'):
                continue
            if not str(r.get('content') or '').strip():
                continue
            dm_count += 1
            rows.append(xml_line(r, self.count + dm_count))
        if not rows:
            return 0
        self._fh.write(''.join(rows))
        self.count += dm_count
        try:
            self._fh.flush()
            if fsync:
                os.fsync(self._fh.fileno())
        except Exception:
            pass
        return dm_count

    def close(self) -> None:
        try:
            self._fh.write('</i>\n')
            self._fh.flush()
            os.fsync(self._fh.fileno())
        except Exception:
            pass
        finally:
            try:
                self._fh.close()
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


def write_xml(records: Iterable[dict], path: str, room_id='0', anchor_name='', sections=None) -> int:
    """一次性写完整个 XML（自测、补写时用）。"""
    with XmlWriter(path, room_id, anchor_name, sections=sections) as w:
        w.append(records, fsync=True)
        return w.count


# ---------------------------------------------------------------------- 分段录制
# 分段录制时 ffmpeg 每切一段就写一个新文件，文件名里带这一段的开始时间
# （-strftime 1），例如「兔娘_2026-11-23_17-27-57.mp4」。弹幕要跟着切，
# 否则一段一小时的视频配一份整场的 xml/ass，字幕时间轴对不上。

SEGMENT_TIME_FMT = '%Y-%m-%d_%H-%M-%S'
_SEG_TIME_RE = re.compile(r'(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})')


def parse_segment_time(name) -> Optional[datetime]:
    """从文件名里取出这一段的开始时间；不是时间分段的就返回 None。"""
    m = _SEG_TIME_RE.search(os.path.basename(str(name or '')))
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), SEGMENT_TIME_FMT)
    except ValueError:
        return None


def segment_prefix(name) -> str:
    """「主播_标题」前缀：去掉时间戳和扩展名，用来找同一次录制的所有分段。"""
    base = os.path.basename(str(name or ''))
    m = _SEG_TIME_RE.search(base)
    return base[:m.start()] if m else ''


class SegmentWatcher:
    """盯录像目录，按顺序报告「又有哪一段视频开始了」。

    只要视频文件名带时间戳，就能靠扫目录拿到边界，不需要和 ffmpeg 通信：
    新文件出现的那一刻就是新的分段开始。目录里上一次录制留下的同名文件用
    session_start 过滤掉，不会串到本场来。
    """

    def __init__(self, video_hint: str, session_start=None, interval: float = 1.0,
                 slack: float = 20.0):
        self.hint = str(video_hint or '')
        self.folder = os.path.dirname(os.path.abspath(self.hint)) if self.hint else ''
        self.prefix = segment_prefix(self.hint)
        self.ext = os.path.splitext(self.hint)[1].lower()
        self.session_start = session_start
        self.interval = max(0.2, float(interval or 1.0))
        # 允许比录制开始早一点点（时钟取整到秒）；只用来挡上一场留下的同名文件
        self.slack = float(slack or 0)
        self.seen = 0

    @property
    def enabled(self) -> bool:
        return bool(self.folder and self.prefix and self.ext and os.path.isdir(self.folder))

    def scan(self) -> list:
        """属于本次录制的分段，按开始时间升序：[(stem, start_dt), ...]。

        stem 是去掉扩展名的完整路径，可以直接拼 .xml / .ass。
        """
        if not self.enabled:
            return []
        try:
            names = os.listdir(self.folder)
        except OSError:
            return []
        found = []
        for name in names:
            if not name.startswith(self.prefix) or not name.lower().endswith(self.ext):
                continue
            start_dt = parse_segment_time(name)
            if start_dt is None:
                continue
            if self.session_start is not None and start_dt < self.session_start - timedelta(seconds=self.slack):
                continue  # 上一场录制的残留
            found.append((os.path.join(self.folder, os.path.splitext(name)[0]), start_dt))
        found.sort(key=lambda item: item[1])
        return found

    def new_segments(self) -> list:
        """自上次调用以来新出现的分段（第一次调用会返回已有的全部）。"""
        items = self.scan()
        fresh = items[self.seen:]
        self.seen = len(items)
        return fresh


class SegmentWriterMixin:
    """弹幕文件跟着视频分段走：一段视频一份 xml/ass，时间轴按段归零。

    子类要提供三样东西，其余都在这里：

    * ``_lock`` / ``_buffer`` / ``_records`` / ``_xml`` / ``formats`` /
      ``base_path`` / ``ass_path`` / ``xml_path`` 这些属性
    * ``_open_xml_writer(stem)``：按 stem 开一个新的 XmlWriter（各自决定 sections）
    * ``_render_segment_ass(records, stem)``：把一段弹幕渲染成 ``<stem>.ass``

    切段的时机由 SegmentWatcher 决定，误差在一个轮询间隔（默认 1 秒）以内：
    边界前后一两秒的弹幕可能落到前一段，肉眼无感，但比整场一份字幕好用得多。
    """

    _seg_watcher: Optional[SegmentWatcher] = None
    _seg_stem = ''
    _seg_base = 0.0
    _seg_thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------ 生命周期

    def init_segment_mode(self, video_hint: str = '', session_start=None) -> None:
        """video_hint 是「第一段视频的预期路径」，只用它的目录/前缀/扩展名。

        没给（或给的不是时间分段）就保持原来的单文件行为。
        """
        watcher = SegmentWatcher(video_hint, session_start) if video_hint else None
        self._seg_watcher = watcher if (watcher and watcher.enabled) else None
        self._seg_base = 0.0
        self._seg_stem = ''
        self._seg_thread = None

    @property
    def segment_mode(self) -> bool:
        return self._seg_watcher is not None

    def start_segment_thread(self) -> None:
        if not self._seg_watcher or (self._seg_thread and self._seg_thread.is_alive()):
            return
        self._seg_thread = threading.Thread(
            target=self._segment_loop,
            name=f'segments_{getattr(self, "room_id", "")}',
            daemon=True,
        )
        self._seg_thread.start()

    # ------------------------------------------------------------------ 切段

    def _segment_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                fresh = self._seg_watcher.new_segments()
            except Exception as exc:  # 目录读不了就下一轮再试，不影响抓弹幕
                logger.debug(f'弹幕分段扫描失败: {exc}')
                fresh = []
            for stem, start_dt in fresh:
                if self._stop_event.is_set():
                    return  # 已经在收尾了，别再开新文件
                try:
                    self._switch_segment(stem, start_dt)
                except Exception as exc:
                    logger.warning(f'弹幕分段切换失败（{os.path.basename(stem)}）: {exc}')
            self._stop_event.wait(self._seg_watcher.interval)

    def _switch_segment(self, stem: str, start_dt) -> None:
        """切到新的一段：收尾上一段的 xml/ass，再开新文件。"""
        watcher = self._seg_watcher
        target_xml = f'{stem}.xml'
        if ('xml' in self.formats and os.path.abspath(target_xml) != os.path.abspath(self.xml_path)
                and os.path.exists(target_xml) and os.path.getsize(target_xml) > 0):
            # 同名前缀的旧文件（上一场、或卡死重连前那一段）不能覆盖，跳过这一段
            logger.debug(f'弹幕分段跳过（目标已存在）: {os.path.basename(target_xml)}')
            return
        base = 0.0
        if watcher is not None and watcher.session_start is not None and start_dt is not None:
            base = max((start_dt - watcher.session_start).total_seconds(), 0.0)
        with self._lock:
            prev_stem, prev_base = self._seg_stem, self._seg_base
            self._flush(force=True)          # 旧基准写完再换
            if self._xml is not None:
                self._xml.close()
                self._xml = None
            self._seg_stem = stem
            self._seg_base = base
            self.base_path = stem
            self.xml_path = f'{stem}.xml'
            self.ass_path = f'{stem}.ass'
            if 'xml' in self.formats:
                self._xml = self._open_xml_writer(stem)
            rows = self._records_between(prev_base, base) if prev_stem else []
        # 字幕渲染放到锁外：一小时的弹幕渲染要几十到几百毫秒，别卡住收包
        if prev_stem and rows:
            self._render_segment_ass(rows, prev_stem)
        if prev_stem:
            logger.debug(f'弹幕已切段：{os.path.basename(prev_stem)}（{len(rows)} 条）')
        else:
            logger.debug(f'弹幕按分段保存：{os.path.basename(stem)}')

    # ------------------------------------------------------------------ 落盘

    def _flush(self, force: bool = False) -> None:
        """把缓冲区写进当前这一段。

        分段模式下 offset 要减掉本段的起点；等第一段视频出现之前先攒着不写，
        否则会多出一个「第 0 段」的孤儿文件。
        """
        if not self._buffer:
            return
        if self._xml is not None:
            rows = self._rebased(self._buffer, self._seg_base)
            self._xml.append(rows, fsync=force)
            self._buffer.clear()
        elif not ('xml' in self.formats and self._seg_watcher and not self._seg_stem):
            self._buffer.clear()
        self._last_flush = time.time()

    @staticmethod
    def _rebased(records: Iterable[dict], base: float) -> list:
        """把一整场的绝对时间轴改成「相对本段开始」。"""
        if not base:
            return list(records)
        rows = []
        for r in records:
            try:
                off = float(r.get('offset') or 0) - base
            except (TypeError, ValueError):
                off = 0.0
            rows.append({**r, 'offset': round(max(off, 0.0), 3)})
        return rows

    def _records_between(self, lo: float, hi: float) -> list:
        """取 offset 落在 [lo, hi) 的消息，并改成相对 lo 的时间轴。"""
        rows = []
        for r in list(self._records):
            try:
                off = float(r.get('offset') or 0)
            except (TypeError, ValueError):
                off = 0.0
            if off < lo or off >= hi:
                continue
            rows.append({**r, 'offset': round(max(off - lo, 0.0), 3)})
        rows.sort(key=lambda item: item.get('offset', 0))
        return rows

    def finalize_segments(self) -> None:
        """录制结束：收尾最后一段并生成它的 ass。

        一段视频都没等到（ffmpeg 直接失败）就退回单文件，别把弹幕丢掉。
        """
        # 先等分段线程退出，避免它在我们收尾之后又开一个新文件
        thread = self._seg_thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            try:
                thread.join(timeout=5)
            except Exception:
                pass
        with self._lock:
            if self._xml is None and 'xml' in self.formats and not self._seg_stem:
                self._xml = self._open_xml_writer(self.base_path)
                self._seg_base = 0.0
            self._flush(force=True)
            if self._xml is not None:
                self._xml.close()
                self._xml = None
            stem, base = self._seg_stem, self._seg_base
        rows = self._records_between(base, float('inf'))
        if rows:
            self._render_segment_ass(rows, stem)
