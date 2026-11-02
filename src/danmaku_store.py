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

礼物和醒目留言写在独立的 <gift>/<sc> 标签里：播放器和转换工具只读 <d>，
不会被礼物消息干扰；浏览器预览时则在单独的表格里展示。
"""
import binascii
import os
import time
from datetime import datetime
from typing import Iterable

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
    """把消息类型映射成 B 站的 8 种弹幕模式里最接近的一种。"""
    mode = record.get('mode')
    try:
        mode = int(mode)
    except (TypeError, ValueError):
        mode = 0
    if mode in (1, 2, 3, 4, 5):
        return mode
    return 1  # 默认滚动


def _offset_text(record: dict) -> str:
    return f'{max(float(record.get("offset") or 0), 0.0):.3f}'


def _price_text(record: dict) -> str:
    try:
        price = float(record.get('diamond') or record.get('price') or 0)
    except (TypeError, ValueError):
        price = 0.0
    return f'{price:g}'


# ---------------------------------------------------------------------- 内嵌样式
# 照录播姬（BililiveRecorder）的思路：把 XSLT 样式表塞在 XML 里，
# 浏览器打开时把 <d>/<gift>/<sc> 渲染成表格。注意整个字符串必须是合法 XML：
# JS 里不能出现裸的 < 和 &。

_STYLESHEET = (
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
    '<tr><td><a href="#d">弹幕</a></td><td>共<z:value-of select="count(/i/d)"/>条记录</td></tr>'
    '<tr><td><a href="#gift">礼物</a></td><td>共<z:value-of select="count(/i/gift)"/>条记录</td></tr>'
    '<tr><td><a href="#sc">SC</a></td><td>共<z:value-of select="count(/i/sc)"/>条记录</td></tr>'
    '</table>'
    '<h2 id="d">弹幕</h2>'
    '<div id="dm"><table>'
    '<tr><th>用户名</th><th>出现时间</th><th>用户ID</th><th>弹幕</th><th>参数</th></tr>'
    '<z:for-each select="/i/d"><tr>'
    '<td><z:value-of select="@user"/></td><td></td><td></td>'
    '<td><z:value-of select="."/></td><td><z:value-of select="@p"/></td>'
    '</tr></z:for-each>'
    '</table></div>'
    '<h2 id="gift">礼物</h2>'
    '<div id="gf"><table>'
    '<tr><th>用户名</th><th>出现时间</th><th>用户ID</th><th>礼物</th><th>数量</th><th>金额</th></tr>'
    '<z:for-each select="/i/gift"><tr>'
    '<td><z:value-of select="@user"/></td><td><z:value-of select="@ts"/></td>'
    '<td><z:value-of select="@uid"/></td><td><z:value-of select="@giftname"/></td>'
    '<td><z:value-of select="@giftcount"/></td><td><z:value-of select="@price"/></td>'
    '</tr></z:for-each>'
    '</table></div>'
    '<h2 id="sc">SC</h2>'
    '<div id="sct"><table>'
    '<tr><th>用户名</th><th>出现时间</th><th>用户ID</th><th>内容</th><th>价格</th></tr>'
    '<z:for-each select="/i/sc"><tr>'
    '<td><z:value-of select="@user"/></td><td><z:value-of select="@ts"/></td>'
    '<td><z:value-of select="@uid"/></td><td><z:value-of select="."/></td>'
    '<td><z:value-of select="@price"/></td>'
    '</tr></z:for-each>'
    '</table></div>'
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


def xml_header(room_id, anchor_name='', start_time='', version=None) -> str:
    v = str(version or APP_VERSION)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<?xml-stylesheet type="text/xsl" href="#s"?>\n'
        '<i>\n'
        f'  <!--\n{APP_NAME} {v}\n{REPO_URL}\n'
        '本文件的弹幕信息兼容 B 站直播弹幕 XML 格式，自带浏览器预览样式。\n'
        'd 为弹幕, gift 为礼物, sc 为醒目留言。播放器与转换工具只读取 d 标签。\n'
        '-->\n'
        '  <chatserver>chat.bilibili.com</chatserver>\n'
        f'  <chatid>{esc(room_id)}</chatid>\n'
        '  <mission>0</mission>\n'
        '  <maxlimit>99999</maxlimit>\n'
        '  <state>0</state>\n'
        '  <real_name>0</real_name>\n'
        '  <source>k-v</source>\n'
        f'  <RecordInfo app_version="{esc(v)}" roomid="{esc(room_id)}"'
        f' name="{esc(anchor_name)}" start_time="{esc(start_time)}" />\n'
        f'  <XmlStyle>{_STYLESHEET}</XmlStyle>\n'
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


def gift_line(record: dict) -> str:
    """一条礼物 -> 一行 <gift>（自闭合，播放器不会当成弹幕）。"""
    return (f'  <gift ts="{_offset_text(record)}" user="{esc(record.get("user"))}"'
            f' uid="{esc(_uid_text(record))}"'
            f' giftname="{esc(record.get("gift_name") or record.get("content") or "礼物")}"'
            f' giftcount="{esc(record.get("gift_count") or 1)}"'
            f' price="{_price_text(record)}"/>\n')


def sc_line(record: dict) -> str:
    """一条醒目留言 -> 一行 <sc>。"""
    return (f'  <sc ts="{_offset_text(record)}" user="{esc(record.get("user"))}"'
            f' uid="{esc(_uid_text(record))}"'
            f' price="{_price_text(record)}">{esc(record.get("content"))}</sc>\n')


class XmlWriter:
    """增量写弹幕 XML。close() 之前文件始终是"未收尾"状态。"""

    def __init__(self, path: str, room_id='0', anchor_name='', start_time=None,
                 sync_existing: bool = False):
        self.path = path
        self.room_id = room_id
        self.count = 0
        os.makedirs(os.path.dirname(os.path.abspath(path)) or '.', exist_ok=True)
        mode = 'a' if sync_existing and os.path.exists(path) else 'w'
        self._fh = open(path, mode, encoding='utf-8')
        if mode == 'w':
            if not start_time:
                start_time = datetime.now().astimezone().isoformat(timespec='seconds')
            self._fh.write(xml_header(room_id, anchor_name, start_time))
            self._fh.flush()

    def append(self, records: Iterable[dict], fsync: bool = False) -> int:
        """按类型分派：chat/emoji 走 <d>，gift 走 <gift>，sc 走 <sc>。
        返回本次写入的弹幕条数（礼物/SC 不计入弹幕数）。"""
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


def write_xml(records: Iterable[dict], path: str, room_id='0', anchor_name='') -> int:
    """一次性写完整个 XML（自测、补写时用）。"""
    with XmlWriter(path, room_id, anchor_name) as w:
        w.append(records, fsync=True)
        return w.count
