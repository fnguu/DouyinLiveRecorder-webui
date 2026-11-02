# -*- encoding: utf-8 -*-
"""B 站（bilibili）直播弹幕抓取。

协议参考 BililiveRecorder 与社区实现，与抖音那套完全不同：

1. `room_init` 把网址里的房号换成真实 room_id（有些房间用的是短号）
2. `getDanmuInfo` 拿弹幕服务器地址和 token —— 这个接口现在要 WBI 签名，
   还得带 buvid3/buvid4，否则一律返回 -352（风控）
3. 连 wss://<host>:<wss_port>/sub，发 op=7 鉴权包（16 字节头 + JSON）
4. 服务端 op=8 回 {"code":0} 表示成功，之后 op=5 是消息包
5. 消息包可能整包 zlib(ver=2) / brotli(ver=3) 压缩，解压后里面还是若干完整包，
   要递归拆；ver=0 才是明文 JSON
6. 每 30 秒发一次 op=2 心跳，服务端回 op=3（人气值）

输出与抖音一致：`.danmaku.xml`（B 站弹幕格式，边抓边写）+ `.danmaku.ass`（收尾生成）。
"""
import hashlib
import json
import os
import re
import struct
import threading
import time
import urllib.parse
import zlib
from datetime import datetime
from typing import Optional

from .danmaku_store import XmlWriter
from .logger import logger

try:
    import brotli
except ImportError:  # pragma: no cover
    brotli = None

try:
    import websocket
except ImportError:  # pragma: no cover
    websocket = None

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

API_ROOM_INIT = 'https://api.live.bilibili.com/room/v1/Room/room_init'
API_DANMU_INFO = 'https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo'
API_NAV = 'https://api.bilibili.com/x/web-interface/nav'
API_FINGER = 'https://api.bilibili.com/x/frontend/finger/spi'

WS_FALLBACK = 'wss://broadcastlv.chat.bilibili.com:443/sub'
HEARTBEAT_INTERVAL = 30
HEADER_LEN = 16

# WBI 混入表的固定置换顺序（B 站前端 JS 里写死的）
WBI_MIXIN = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61,
    26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36,
    20, 34, 44, 52,
]

# 进字幕的消息类型。进场/点赞/关注量太大，默认只进 XML 之外的统计，不刷屏。
SUBTITLE_TYPES = ('chat', 'gift', 'sc', 'emoji')

_sessions: dict = {}
_sessions_lock = threading.Lock()

_ctx_lock = threading.Lock()
_ctx = {'session': None, 'buvid3': '', 'buvid4': '', 'mixin_key': '', 'mixin_ts': 0.0}


# ---------------------------------------------------------------------- 工具函数

def _clean_base(save_file_path: str) -> str:
    """`a/b/xxx.ts` -> `a/b/xxx`，去掉扩展名和分段后缀，与视频同名前缀。"""
    p = str(save_file_path or '')
    p = re.sub(r'\.(ts|mp4|mkv|flv|m4a|mp3)$', '', p, flags=re.I)
    return p


def _ts_to_ms(value) -> Optional[int]:
    """B 站不同消息的时间戳单位不统一：弹幕是毫秒，其它多是秒。按量级归一。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    return int(v * 1000) if v < 1e11 else int(v)


def _mixin_key(img_key: str, sub_key: str) -> str:
    raw = img_key + sub_key
    return ''.join(raw[i] for i in WBI_MIXIN if i < len(raw))[:32]


def _ensure_session():
    """建一个带 buvid 的会话。buvid 是新访客标识，缺了会被风控直接拒。"""
    if requests is None:
        return None
    with _ctx_lock:
        if _ctx['session'] is not None:
            return _ctx['session']
        s = requests.Session()
        s.headers.update({
            'User-Agent': UA,
            'Referer': 'https://live.bilibili.com/',
            'Origin': 'https://live.bilibili.com',
        })
        try:
            spi = s.get(API_FINGER, timeout=10).json().get('data') or {}
            _ctx['buvid3'] = spi.get('b_3') or ''
            _ctx['buvid4'] = spi.get('b_4') or ''
            if _ctx['buvid3']:
                s.cookies.set('buvid3', _ctx['buvid3'], domain='.bilibili.com')
            if _ctx['buvid4']:
                s.cookies.set('buvid4', _ctx['buvid4'], domain='.bilibili.com')
        except Exception as exc:
            logger.debug(f'bilibili 取 buvid 失败（继续尝试）: {exc}')
        _ctx['session'] = s
        return s


def _apply_cookie(session, cookie: str) -> None:
    """把配置文件里的 B 站 Cookie 注入会话：有登录态才能拿到完整弹幕流。"""
    if not session or not cookie:
        return
    for part in str(cookie).split(';'):
        if '=' not in part:
            continue
        k, v = part.split('=', 1)
        k, v = k.strip(), v.strip()
        if k and v:
            session.cookies.set(k, v, domain='.bilibili.com')


def _wbi_key(session, force: bool = False) -> str:
    with _ctx_lock:
        if not force and _ctx['mixin_key'] and time.time() - _ctx['mixin_ts'] < 1800:
            return _ctx['mixin_key']
    try:
        nav = session.get(API_NAV, timeout=10).json()
        wbi = (nav.get('data') or {}).get('wbi_img') or {}
        img = (wbi.get('img_url') or '').rsplit('/', 1)[-1].split('.')[0]
        sub = (wbi.get('sub_url') or '').rsplit('/', 1)[-1].split('.')[0]
        key = _mixin_key(img, sub) if img and sub else ''
    except Exception as exc:
        logger.debug(f'bilibili 取 WBI 密钥失败: {exc}')
        key = ''
    with _ctx_lock:
        _ctx['mixin_key'] = key
        _ctx['mixin_ts'] = time.time()
    return key


def resolve_room_id(value: str) -> tuple:
    """把直播链接 / 短号 / 真实房号换成真实 room_id，返回 (room_id, 标题)。"""
    session = _ensure_session()
    if session is None:
        return None, ''
    text = str(value or '').strip()
    m = re.search(r'bilibili\.com/(?:blanc/)?(\d+)', text) or re.search(r'^(\d{2,})$', text)
    if not m:
        m = re.search(r'(\d{3,})', text)
    if not m:
        return None, ''
    rid = m.group(1)
    try:
        data = session.get(API_ROOM_INIT, params={'id': rid}, timeout=10).json()
        info = data.get('data') or {}
        real = info.get('room_id')
        if real:
            return str(real), ''
    except Exception as exc:
        logger.debug(f'bilibili room_init 失败（用网址里的房号继续）: {exc}')
    return rid, ''


def fetch_danmu_info(room_id: str) -> tuple:
    """返回 (token, [wss url...])。失败返回 ('', [])，调用方退回默认入口。"""
    session = _ensure_session()
    if session is None:
        return '', []
    params = {'id': str(room_id), 'type': 0, 'web_location': 1315873, 'wts': int(time.time())}
    signed = dict(sorted(params.items()))
    query = urllib.parse.urlencode(signed)
    key = _wbi_key(session)
    if key:
        query += '&w_rid=' + hashlib.md5((query + key).encode()).hexdigest()
    try:
        data = session.get(f'{API_DANMU_INFO}?{query}', timeout=10).json()
    except Exception as exc:
        logger.debug(f'bilibili getDanmuInfo 请求失败: {exc}')
        return '', []
    if data.get('code') != 0:
        logger.debug(f'bilibili getDanmuInfo 返回 {data.get("code")}: {data.get("message")}')
        return '', []
    payload = data.get('data') or {}
    token = payload.get('token') or ''
    urls = []
    for host in payload.get('host_list') or []:
        h = host.get('host')
        port = host.get('wss_port') or 443
        if h:
            url = f'wss://{h}:{port}/sub'
            if url not in urls:
                urls.append(url)
    return token, urls


# ---------------------------------------------------------------------- 抓取器

class BiliDanmakuRecorder:
    """单个直播间的 B 站弹幕抓取器。"""

    def __init__(self, room_id: str, save_file_path: str, anchor_name: str = '',
                 cookie: Optional[str] = None, formats=('xml', 'ass'),
                 sync_start: Optional[datetime] = None,
                 subtitle_max_lines: int = 4, subtitle_duration: float = 5.0,
                 subtitle_mode: str = 'scroll', playres: tuple = (1920, 1080),
                 subtitle_types=None, gift_min_diamond: float = 0.0,
                 subtitle_font_size: int = 40, uid: str = '0'):
        self.room_id = str(room_id)
        self.anchor_name = anchor_name or self.room_id
        self.cookie = cookie or ''
        self.formats = [f.strip().lower() for f in formats if f.strip()] or ['xml', 'ass']
        self.sync_start = sync_start
        self.subtitle_max_lines = max(1, int(subtitle_max_lines or 4))
        self.subtitle_duration = float(subtitle_duration or 5.0)
        self.subtitle_mode = 'fixed' if str(subtitle_mode).strip().lower() in ('fixed', '固定') else 'scroll'
        self.playres = (int(playres[0]), int(playres[1])) if playres else (1920, 1080)
        self.subtitle_types = tuple(subtitle_types or SUBTITLE_TYPES)
        self.gift_min_diamond = float(gift_min_diamond or 0)
        self.subtitle_font_size = int(subtitle_font_size or 40)
        self.uid = str(uid or '0')

        self.base_path = _clean_base(save_file_path)
        self.xml_path = f'{self.base_path}.danmaku.xml'
        self.ass_path = f'{self.base_path}.danmaku.ass'

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._beat_thread: Optional[threading.Thread] = None
        self._ws = None
        self._lock = threading.Lock()
        self._buffer: list = []
        self._records: list = []
        self._xml = None
        self._last_flush = 0.0
        self._base_time: Optional[datetime] = None
        self.count = 0

    # ------------------------------------------------------------------ 对外

    def start(self) -> bool:
        if websocket is None:
            logger.warning('B站弹幕未启动：缺少 websocket-client，请执行 pip install websocket-client')
            return False
        if requests is None:
            logger.warning('B站弹幕未启动：缺少 requests')
            return False
        os.makedirs(os.path.dirname(self.base_path) or '.', exist_ok=True)
        if 'xml' in self.formats:
            self._xml = XmlWriter(self.xml_path, self.room_id, anchor_name=self.anchor_name)
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name=f'bili_danmaku_{self.room_id}', daemon=True)
        self._thread.start()
        self._beat_thread = threading.Thread(target=self._heartbeat, name=f'bili_heartbeat_{self.room_id}',
                                             daemon=True)
        self._beat_thread.start()
        logger.info(f'{self.anchor_name} B站弹幕抓取已启动（房间 {self.room_id}），保存至 {self.xml_path}')
        return True

    def stop(self) -> None:
        self._stop_event.set()
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=8)
        self._flush(force=True)
        if self._xml:
            self._xml.close()
            self._xml = None
        self._write_ass()
        logger.info(f'{self.anchor_name} B站弹幕抓取已停止，共 {self.count} 条 -> {self.xml_path}')

    # ------------------------------------------------------------------ 连接

    def _connect(self):
        token, urls = fetch_danmu_info(self.room_id)
        session = _ensure_session()
        _apply_cookie(session, self.cookie)
        buvid = (_ctx.get('buvid3') or '')
        # 登录 Cookie 必须带给 WebSocket：B 站对匿名连接（尤其机房 IP）经常
        # 鉴权成功却不推任何消息，带登录态后才能稳定收到 DANMU_MSG。
        headers = [f'User-Agent: {UA}', 'Origin: https://live.bilibili.com',
                   f'Referer: https://live.bilibili.com/{self.room_id}']
        ws_cookie = f'buvid3={buvid}' if buvid else ''
        if self.cookie:
            ws_cookie = f'{self.cookie};{ws_cookie}' if ws_cookie else self.cookie
        if ws_cookie:
            headers.append(f'Cookie: {ws_cookie}')
        # protover=3 的消息包是 brotli 压缩，缺库时降级到 protover=2（zlib）
        protover = 3 if brotli is not None else 2
        # 登录 uid：鉴权包里的 uid 应与 Cookie 的 DedeUserID 一致
        uid = 0
        m_uid = re.search(r'DedeUserID=(\d+)', self.cookie, re.I)
        if m_uid:
            uid = int(m_uid.group(1))
        targets = urls or [WS_FALLBACK]
        last_error = None
        for url in targets:
            try:
                ws = websocket.create_connection(url, timeout=15, header=headers,
                                                 origin='https://live.bilibili.com')
            except Exception as exc:
                last_error = exc
                continue
            auth = {
                'uid': uid,
                'roomid': int(self.room_id),
                'protover': protover,
                'platform': 'web',
                'type': 2,
                'key': token,
            }
            try:
                ws.send(self._packet(7, json.dumps(auth).encode('utf-8')))
                ws.settimeout(8)
                reply = ws.recv()
                code = None
                body = self._body(reply) if reply else b''
                if body:
                    try:
                        code = json.loads(body.decode('utf-8', 'ignore')).get('code')
                    except Exception:
                        code = None
                if code not in (0, None):
                    logger.warning(f'B站弹幕鉴权被拒（{url}）code={code}，尝试下一个节点')
                    ws.close()
                    continue
                if not token:
                    logger.debug('B站弹幕未取到 token，匿名连接可能收不到消息；建议在设置里填 B 站 Cookie')
                # B 站官方流程：鉴权成功后必须立刻发一次心跳，服务端才会开始推消息，
                # 之后由心跳线程每 30 秒续上。
                try:
                    ws.send(self._packet(2))
                except Exception as exc:
                    logger.debug(f'B站弹幕首次心跳发送失败: {exc}')
                logger.debug(f'B站弹幕已连接 {url}（room {self.room_id}）')
                return ws
            except Exception as exc:
                last_error = exc
                try:
                    ws.close()
                except Exception:
                    pass
        if last_error:
            logger.debug(f'B站弹幕连接失败: {last_error}')
        return None

    def _run(self) -> None:
        backoff = 3
        while not self._stop_event.is_set():
            ws = self._connect()
            if ws is None:
                self._stop_event.wait(backoff)
                backoff = min(backoff * 2, 30)
                continue
            backoff = 3
            self._ws = ws
            try:
                ws.settimeout(8)
                while not self._stop_event.is_set():
                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    if not raw:
                        continue
                    self._handle(raw)
            except Exception as exc:
                if not self._stop_event.is_set():
                    logger.debug(f'B站弹幕连接中断，将重连: {exc}')
            finally:
                try:
                    ws.close()
                except Exception:
                    pass
                self._ws = None
            if not self._stop_event.is_set():
                self._stop_event.wait(2)

    def _heartbeat(self) -> None:
        while not self._stop_event.wait(HEARTBEAT_INTERVAL):
            ws = self._ws
            if ws is None:
                continue
            try:
                ws.send(self._packet(2))
            except Exception:
                pass

    # ------------------------------------------------------------------ 收包

    @staticmethod
    def _packet(op: int, body: bytes = b'') -> bytes:
        return struct.pack('>IHHII', HEADER_LEN + len(body), HEADER_LEN, 1, op, 1) + body

    @staticmethod
    def _body(raw: bytes) -> bytes:
        if not raw or len(raw) < HEADER_LEN:
            return b''
        plen, hlen, _ver, op, _seq = struct.unpack('>IHHII', raw[:HEADER_LEN])
        if plen < hlen or plen > len(raw):
            return b''
        return raw[hlen:plen] if op in (5, 8) else b''

    def _handle(self, raw: bytes) -> None:
        """拆包：压缩包递归展开，明文包走 JSON。"""
        if not raw or len(raw) < HEADER_LEN:
            return
        plen, hlen, ver, op, _seq = struct.unpack('>IHHII', raw[:HEADER_LEN])
        if plen < hlen or plen > len(raw) or hlen > plen:
            return
        body = raw[hlen:plen]
        rest = raw[plen:]
        if ver == 2:
            try:
                self._handle(zlib.decompress(body))
            except Exception:
                pass
        elif ver == 3:
            if brotli is None:
                logger.debug('B站弹幕收到 brotli 压缩包但缺少 brotli 库，跳过')
            else:
                try:
                    self._handle(brotli.decompress(body))
                except Exception:
                    pass
        elif op == 5:
            try:
                data = json.loads(body.decode('utf-8', 'ignore'))
            except Exception:
                data = None
            if isinstance(data, dict):
                self._on_message(data)
        if rest:
            self._handle(rest)

    def _on_message(self, msg: dict) -> None:
        cmd = str(msg.get('cmd') or '')
        base = cmd.split(':', 1)[0]
        data = msg.get('data') or {}
        rec = None
        try:
            if base == 'DANMU_MSG':
                rec = self._danmu_msg(msg)
            elif base == 'SEND_GIFT':
                rec = self._gift(data)
            elif base == 'SUPER_CHAT_MESSAGE':
                rec = self._super_chat(data)
            elif base == 'INTERACT_WORD_V2':
                rec = self._interact(data)
            elif base == 'LIKE_INFO_V3_CLICK':
                rec = self._pack('like', time.time(), data.get('uid') or '',
                                 data.get('uname') or '', '点赞')
            elif base == 'WATCHED_CHANGE':
                num = data.get('num') or 0
                rec = self._pack('stats', time.time(), '', '', f'看过 {num}')
        except Exception as exc:
            logger.debug(f'B站弹幕消息解析失败 {base}: {exc}')
        if rec:
            self._append(rec)

    def _danmu_msg(self, msg: dict) -> Optional[dict]:
        info = msg.get('info') or []
        if len(info) < 3:
            return None
        meta = info[0] or []
        text = str(info[1] or '')
        user_info = info[2] or []
        uname = str(user_info[1] or '') if len(user_info) > 1 else ''
        uid = user_info[0] if user_info else ''
        mode = meta[1] if len(meta) > 1 else 1
        size = meta[2] if len(meta) > 2 else 25
        color = meta[3] if len(meta) > 3 else 16777215
        ts_ms = meta[4] if len(meta) > 4 else None
        # 弹幕 ID 藏在 info[9] 里；没有就用时间戳兜底
        dmid = ''
        if len(info) > 9 and isinstance(info[9], dict):
            dmid = str(info[9].get('ct') or '')
        rec = self._pack('chat', ts_ms, uid, uname, text, dmid=dmid, mode=mode, size=size, color=color)
        return rec

    def _gift(self, data: dict) -> Optional[dict]:
        name = data.get('giftName') or '礼物'
        num = data.get('num') or 1
        uname = data.get('uname') or ''
        uid = data.get('uid') or ''
        try:
            diamond = float(data.get('total_coin') or 0)
        except (TypeError, ValueError):
            diamond = 0.0
        return self._pack('gift', data.get('timestamp'), uid, uname, f'{name} x{num}',
                          diamond=diamond, gift_name=name, gift_count=num)

    def _super_chat(self, data: dict) -> Optional[dict]:
        info = data.get('user_info') or {}
        uname = info.get('uname') or ''
        uid = data.get('uid') or ''
        price = data.get('price') or 0
        # price 单独存 diamond，ASS 里用【醒目留言】前缀展示，XML 的 sc 标签带 price 属性
        return self._pack('sc', data.get('start_time') or data.get('ts'), uid, uname,
                          data.get('message') or '', diamond=price)

    def _interact(self, data: dict) -> Optional[dict]:
        # INTERACT_WORD_V2 的字段是 protobuf base64，取不到明文时退回 V1 结构
        uname = data.get('uname') or ''
        uid = data.get('uid') or ''
        msg_type = data.get('msg_type')
        if msg_type is None and isinstance(data.get('pb'), str):
            return None
        mapping = {1: ('member', '进入直播间'), 2: ('social', '关注了主播'),
                   3: ('social', '分享了直播间'), 4: ('member', '特别关注'), 5: ('member', '互粉')}
        rtype, text = mapping.get(msg_type, ('member', '进入直播间'))
        if not uname:
            return None
        return self._pack(rtype, data.get('timestamp'), uid, uname, text)

    def _pack(self, rtype: str, ts, uid, user: str, content: str, **extra) -> dict:
        ts_ms = _ts_to_ms(ts) or int(time.time() * 1000)
        try:
            dt = datetime.fromtimestamp(ts_ms / 1000)
        except (OSError, ValueError, OverflowError):
            ts_ms = int(time.time() * 1000)
            dt = datetime.fromtimestamp(ts_ms / 1000)
        if self.sync_start is not None:
            base = self.sync_start
        else:
            if self._base_time is None:
                self._base_time = dt
            base = self._base_time
        offset = (dt - base).total_seconds()
        rec = {
            'type': rtype,
            'time': dt.strftime('%Y-%m-%d %H:%M:%S'),
            'ts': ts_ms,
            'offset': round(max(offset, 0.0), 3),
            'user': user or '',
            'user_id': str(uid or ''),
            'content': content or '',
        }
        if extra:
            rec.update({k: v for k, v in extra.items() if v not in (None, '')})
        return rec

    # ------------------------------------------------------------------ 落盘

    def _append(self, record: dict) -> None:
        with self._lock:
            self.count += 1
            self._buffer.append(record)
            self._records.append(record)
            now = time.time()
            if len(self._buffer) >= 20 or now - self._last_flush >= 5:
                self._flush(force=True)

    def _flush(self, force: bool = False) -> None:
        if not self._buffer:
            return
        if self._xml is not None:
            self._xml.append(self._buffer, fsync=force)
        self._buffer.clear()
        self._last_flush = time.time()

    def _write_ass(self) -> None:
        if 'ass' not in self.formats:
            return
        from .danmaku import render_ass  # 复用同一套 ASS 渲染，避免两份实现漂移
        records = [r for r in self._records if str(r.get('type') or '').lower() in self.subtitle_types]
        if not records:
            return
        records.sort(key=lambda r: r.get('offset', 0))
        kept = []
        for r in records:
            if str(r.get('type') or '').lower() == 'gift' and self.gift_min_diamond > 0:
                try:
                    if float(r.get('diamond', 0) or 0) < self.gift_min_diamond:
                        continue
                except (TypeError, ValueError):
                    pass
            kept.append(r)
        if not kept:
            return
        written, dropped = render_ass(
            kept, self.ass_path, self.subtitle_max_lines, self.subtitle_duration,
            self.subtitle_mode, self.playres,
            font_size=self.subtitle_font_size,
            types=self.subtitle_types, gift_min_diamond=self.gift_min_diamond,
        )
        logger.debug(f'B站弹幕 ASS 已生成: {self.ass_path}（写入 {written} 条'
                     + (f'，丢弃 {dropped} 条' if dropped else '') + '）')


# ---------------------------------------------------------------------- 会话管理

def start_danmaku(room_id: str, save_file_path: str, anchor_name: str = '',
                  cookie: Optional[str] = None, formats=('xml', 'ass'),
                  sync_start: Optional[datetime] = None, **kwargs) -> bool:
    key = os.path.abspath(_clean_base(save_file_path))
    with _sessions_lock:
        if key in _sessions:
            return False
        rec = BiliDanmakuRecorder(room_id, save_file_path, anchor_name, cookie, formats,
                                  sync_start, **kwargs)
        ok = rec.start()
        if ok:
            _sessions[key] = rec
        return ok


def stop_danmaku(save_file_path: str) -> None:
    key = os.path.abspath(_clean_base(save_file_path))
    with _sessions_lock:
        rec = _sessions.pop(key, None)
    if rec:
        try:
            rec.stop()
        except Exception as exc:
            logger.debug(f'停止 B站弹幕出错: {exc}')


def stop_all_danmaku() -> None:
    with _sessions_lock:
        recs = list(_sessions.values())
        _sessions.clear()
    for rec in recs:
        try:
            rec.stop()
        except Exception:
            pass


if __name__ == '__main__':  # pragma: no cover
    import argparse
    import sys

    parser = argparse.ArgumentParser(description='B站直播弹幕抓取自测')
    parser.add_argument('room', help='直播链接或房间号')
    parser.add_argument('--seconds', type=int, default=30)
    parser.add_argument('--out', default='./bili_danmaku_test.ts')
    args = parser.parse_args()

    rid, _ = resolve_room_id(args.room)
    if not rid:
        print('无法解析房间号')
        sys.exit(1)
    print(f'房间: {rid}')
    rec = BiliDanmakuRecorder(rid, args.out, rid, formats=('xml', 'ass'))
    if not rec.start():
        sys.exit(1)
    try:
        time.sleep(args.seconds)
    except KeyboardInterrupt:
        pass
    rec.stop()
    print(f'共抓取 {rec.count} 条 -> {rec.xml_path}')
