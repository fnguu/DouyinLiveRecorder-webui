# -*- encoding: utf-8 -*-
"""弹幕落盘：B 站弹幕 XML + ASS 字幕（抖音、B站共用）。

XML 采用 B 站弹幕库格式（BililiveRecorder 同款），播放器、DanmakuFactory、
弹幕转换工具都认这个结构：

    <?xml version="1.0" encoding="UTF-8"?>
    <i>
      <chatserver>chat.bilibili.com</chatserver>
      <chatid>房间号</chatid>
      ...
      <d p="出现时间,模式,字号,颜色,时间戳,弹幕池,用户哈希,弹幕ID">内容</d>
    </i>

p 的 8 个字段：
    0 出现时间   相对录制开始的秒数，保留 3 位小数
    1 模式       1/2/3 滚动，4 底部固定，5 顶部固定
    2 字号       B 站默认 25，这里按弹幕原始字号
    3 颜色       十进制 RGB，例如 16777215 = 白
    4 时间戳     发送时刻的 Unix 秒
    5 弹幕池     0 普通、1 字幕、2 特殊
    6 用户哈希   uid 的 crc32（8 位十六进制），不写真实 uid
    7 弹幕 ID    平台给的 dmid，没有就用序号

XML 是边抓边写的：先写头部，每条弹幕立刻追加一行 `<d>`，结束（或容器被杀）
才补 `</i>`。所以中途断掉的文件只是缺收尾标签，绝大部分内容仍然可用——
这一点和 BililiveRecorder 的做法一致，长直播不会因为一次崩溃丢掉整场弹幕。
"""
import binascii
import os
import time
from typing import Iterable

# 只有真正"有文字"的消息才进 XML：礼物/进场/点赞这些是弹幕之外的附带信息，
# 写进 XML 会被播放器当成奇怪的弹幕，ASS 里才适合带前缀展示。
XML_TYPES = {'chat', 'emoji', 'sc'}


def esc(text: str) -> str:
    """XML 文本转义。弹幕里出现 & < > 的概率不低，不转义整个文件就废了。"""
    return (str(text or '')
            .replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;')
            .replace('"', '&quot;')
            .replace("'", '&apos;'))


def uid_hash(uid) -> str:
    """B 站 XML 里放的是 uid 的 crc32，不是明文。"""
    raw = str(uid or '0').encode('utf-8')
    return f'{binascii.crc32(raw) & 0xffffffff:08x}'


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


def xml_header(room_id) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<i>\n'
        '  <chatserver>chat.bilibili.com</chatserver>\n'
        f'  <chatid>{esc(room_id)}</chatid>\n'
        '  <mission>0</mission>\n'
        '  <maxlimit>99999</maxlimit>\n'
        '  <state>0</state>\n'
        '  <real_name>0</real_name>\n'
        '  <source>k-v</source>\n'
    )


def xml_line(record: dict, index: int = 0) -> str:
    """一条记录 -> 一行 <d>。"""
    offset = float(record.get('offset') or 0)
    ts = record.get('ts')
    try:
        ts = int(ts) // 1000 if ts else int(time.time())
    except (TypeError, ValueError):
        ts = int(time.time())
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
    p = f'{max(offset, 0.0):.3f},{xml_mode(record)},{size},{color},{ts},0,' \
        f'{uid_hash(record.get("user_id"))},{esc(dmid)}'
    return f'  <d p="{p}">{esc(record.get("content"))}</d>\n'


class XmlWriter:
    """增量写 B 站弹幕 XML。close() 之前文件始终是"未收尾"状态。"""

    def __init__(self, path: str, room_id='0', sync_existing: bool = False):
        self.path = path
        self.room_id = room_id
        self.count = 0
        os.makedirs(os.path.dirname(os.path.abspath(path)) or '.', exist_ok=True)
        mode = 'a' if sync_existing and os.path.exists(path) else 'w'
        self._fh = open(path, mode, encoding='utf-8')
        if mode == 'w':
            self._fh.write(xml_header(room_id))
            self._fh.flush()

    def append(self, records: Iterable[dict], fsync: bool = False) -> int:
        rows = []
        for r in records:
            if str(r.get('type') or 'chat').lower() not in XML_TYPES:
                continue
            if not str(r.get('content') or '').strip():
                continue
            rows.append(xml_line(r, self.count + len(rows) + 1))
        if not rows:
            return 0
        self._fh.write(''.join(rows))
        self.count += len(rows)
        try:
            self._fh.flush()
            if fsync:
                os.fsync(self._fh.fileno())
        except Exception:
            pass
        return len(rows)

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


def write_xml(records: Iterable[dict], path: str, room_id='0') -> int:
    """一次性写完整个 XML（自测、补写时用）。"""
    with XmlWriter(path, room_id) as w:
        w.append(records, fsync=True)
        return w.count
