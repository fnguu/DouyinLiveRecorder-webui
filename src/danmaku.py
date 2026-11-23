# -*- coding: utf-8 -*-
"""
抖音直播弹幕(IM 消息)抓取模块

通过抖音 webcast IM 的 WebSocket 长连接实时拉取直播间消息，抓到的内容远不止"弹幕"：
    - WebcastChatMessage            评论/弹幕
    - WebcastGiftMessage            礼物
    - WebcastLikeMessage            点赞
    - WebcastMemberMessage          进场/关注提示
    - WebcastSocialMessage          关注/分享
    - WebcastRoomStatsMessage       在线人数
    - WebcastMatchAgainstScoreMessage   PK 比分
    - WebcastEmojiChatMessage       表情弹幕
    - WebcastControlMessage         直播间状态(关播等)

输出格式：
    xml    B 站弹幕格式（BililiveRecorder 同款），播放器与弹幕工具通用，可挂载可转换
    ass    ASS 字幕，样式可控，可用 ffmpeg 烧进画面

依赖：websocket-client、protobuf（已随 requirements 追加）
"""

import glob
import gzip
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Optional

from .danmaku_store import SegmentWriterMixin, XmlWriter
from .logger import logger

JS_SIGN_PATH = Path(__file__).resolve().parent / 'javascript' / 'douyin_ws_sign.js'

# 生成 WebSocket signature 时参与 md5 的固定字段，顺序不能变
WS_SIGN_PARAMS = ("live_id,aid,version_code,webcast_sdk_version,"
                  "room_id,sub_room_id,sub_channel_id,did_rule,"
                  "user_unique_id,device_platform,device_type,ac,"
                  "identity").split(',')

try:
    from .protobuf import douyin_pb2 as pb
except ImportError as _e:  # pragma: no cover
    pb = None
    _PB_IMPORT_ERROR = _e

try:
    import websocket
except ImportError:  # pragma: no cover
    websocket = None


DEFAULT_USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36'
)

# 兜底 ttwid：抖音的 webcast 接口没有 ttwid 会直接拒绝，
# 但任何硬编码凭证终会失效，所以下面会自动申请新的，这个值只在网络不通时兜底。
DEFAULT_COOKIE = (
    'ttwid=1%7C2iDIYVmjzMcpZ20fcaFde0VghXAA3NaNXE_SLR68IyE%7C1761045455'
    '%7Cab35197d5cfb21df6cbb2fa7ef1c9262206b062c315b9d04da746d0b37dfbc7d'
)

_TTWID_TTL = 6 * 3600  # 缓存 6 小时后重新申请
_ttwid_lock = threading.Lock()


def _cache_path() -> str:
    try:
        import tempfile
        return os.path.join(tempfile.gettempdir(), 'douyin_danmaku_ttwid.json')
    except Exception:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), '.ttwid_cache.json')


def _fetch_ttwid(user_agent: str = DEFAULT_USER_AGENT, timeout: int = 15) -> Optional[str]:
    """向抖音申请一个新的 ttwid，两种途径依次尝试，都失败返回 None。"""
    try:
        import requests
    except ImportError:
        return None
    headers = {'user-agent': user_agent}
    try:  # 官方注册接口
        resp = requests.post(
            'https://ttwid.bytedance.com/ttwid/union/register/',
            json={'region': 'cn', 'aid': '6383', 'needFid': 'false',
                  'service': 'https://live.douyin.com', 'migrate_info': '',
                  'cbUrlProtocol': 'https', 'union': 'true'},
            headers=headers, timeout=timeout)
        data = resp.json()
        value = (data.get('data') or {}).get('ttwid') or data.get('ttwid')
        if value:
            return value
    except Exception:
        pass
    try:  # 访问直播首页，从 Set-Cookie 里拿
        resp = requests.get('https://live.douyin.com/', headers=headers, timeout=timeout)
        value = resp.cookies.get('ttwid')
        if value:
            return value
    except Exception:
        pass
    return None


def get_default_cookie(force_refresh: bool = False) -> str:
    """返回可用的 ttwid cookie：优先本地缓存，过期/缺失时自动申请。"""
    path = _cache_path()
    cached = None
    with _ttwid_lock:
        if not force_refresh:
            try:
                with open(path, 'r', encoding='utf-8') as fh:
                    cached = json.load(fh)
            except Exception:
                cached = None
        if cached and cached.get('ttwid') and time.time() < cached.get('expire', 0):
            return f"ttwid={cached['ttwid']}"
        value = _fetch_ttwid()
        if value:
            try:
                with open(path, 'w', encoding='utf-8') as fh:
                    json.dump({'ttwid': value, 'expire': time.time() + _TTWID_TTL}, fh)
            except Exception:
                pass
            return f"ttwid={value}"
    if cached and cached.get('ttwid'):
        return f"ttwid={cached['ttwid']}"
    return DEFAULT_COOKIE


def resolve_room_id(value: str) -> tuple:
    """把用户给的分享链接 / web_rid 解析成 WebSocket 需要的真实 room_id。

    抖音网址里那串短数字是 web_rid，弹幕 WebSocket 要的是 enter 接口返回的
    id_str（19 位左右），两者不是一回事。

    返回 (room_id, 主播名)；解析失败时返回 (None, '')。
    """
    text = (value or '').strip()
    nums = re.findall(r'\d{8,25}', text)
    if not nums:
        return None, ''
    num = nums[0]
    if len(num) >= 15:  # 已经是真实 room_id
        return num, ''

    try:
        from .ab_sign import ab_sign
    except Exception:
        ab_sign = None

    params = {'aid': '6383', 'app_name': 'douyin_web', 'live_id': '1',
              'device_platform': 'web', 'language': 'zh-CN', 'browser_language': 'zh-CN',
              'browser_platform': 'Win32', 'browser_name': 'Chrome',
              'browser_version': '116.0.0.0', 'web_rid': num, 'msToken': ''}
    query = urllib.parse.urlencode(params)
    url = 'https://live.douyin.com/webcast/room/web/enter/?' + query
    if ab_sign:
        url += '&a_bogus=' + ab_sign(query, DEFAULT_USER_AGENT)
    try:
        import requests
        resp = requests.get(url, headers={
            'user-agent': DEFAULT_USER_AGENT,
            'referer': 'https://live.douyin.com/',
            'cookie': get_default_cookie(),
        }, timeout=15)
        payload = resp.json()
        rooms = (payload.get('data') or {}).get('data') or []
        room = rooms[0] if rooms else {}
        id_str = room.get('id_str')
        if id_str:
            anchor = (room.get('owner') or {}).get('nickname') or ''
            status = room.get('status')
            if status == 2:
                logger.info(f'「{anchor or num}」正在直播，room_id={id_str}')
            elif status == 4:
                logger.warning(f'「{anchor or num}」当前未开播(status=4)，连上也不会收到消息')
            else:
                logger.warning(f'「{anchor or num}」直播间状态 status={status}，可能已关播')
            return str(id_str), anchor
        logger.warning(f'enter 接口未返回 id_str: {payload.get("status_msg", "")}')
    except Exception as exc:
        logger.warning(f'解析直播间失败({exc})，改用原 ID 直连')
    return num, ''

WS_HOSTS = [
    'webcast100-ws-web-lq.douyin.com',
    'webcast5-ws-web-hl.douyin.com',
    'webcast3-ws-web-hl.douyin.com',
]

BROWSER_VERSION = ('5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                   '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36')

_sign_ctx = None
_sign_ctx_lock = threading.Lock()


def _random_id() -> str:
    return str(random.randint(10 ** 18, 10 ** 19 - 1))


# webmssdk 是字节官方的前端 SDK，面向浏览器编写，直接丢进 Node 会报
# "window is not defined"。这里补一层最小的浏览器环境（本项目原创代码），
# 让它可以脱离浏览器运行，省掉 jsdom 这类重依赖。
BROWSER_POLYFILL = r"""
var window = globalThis;
var self = globalThis;
globalThis.window = window;
if (!window.navigator) {
    window.navigator = {
        userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36',
        platform: 'Win32', language: 'zh-CN', languages: ['zh-CN'],
        vendor: 'Google Inc.', appVersion: '5.0', hardwareConcurrency: 8
    };
}
if (!window.screen) window.screen = { width: 1920, height: 1080, colorDepth: 24 };
if (!window.location) {
    window.location = {
        href: 'https://live.douyin.com/', hostname: 'live.douyin.com',
        protocol: 'https:', origin: 'https://live.douyin.com', pathname: '/'
    };
}
if (!window.document) {
    var _el = function () {
        return { style: {}, setAttribute: function () {}, getAttribute: function () { return ''; },
                 appendChild: function () {}, removeChild: function () {}, addEventListener: function () {},
                 getContext: function () { return null; }, toDataURL: function () { return ''; } };
    };
    window.document = {
        createElement: _el, createElementNS: _el, getElementsByTagName: function () { return []; },
        getElementById: function () { return null; }, querySelector: function () { return null; },
        addEventListener: function () {}, head: { appendChild: function () {} },
        body: { appendChild: function () {} }, cookie: '', referrer: ''
    };
}
"""


def _get_sign_ctx():
    """抖音 WS 签名走的是旧版 acrawler 的 get_sign，用 execjs 调 webmssdk 得到。"""
    global _sign_ctx
    with _sign_ctx_lock:
        if _sign_ctx is None:
            import execjs
            with open(JS_SIGN_PATH, 'r', encoding='utf-8') as f:
                _sign_ctx = execjs.compile(BROWSER_POLYFILL + '\n' + f.read())
    return _sign_ctx


def generate_ws_signature(wss_url: str) -> str:
    query = urllib.parse.urlparse(wss_url).query
    wss_maps = {}
    for item in query.split('&'):
        if '=' in item:
            key, value = item.split('=', 1)
            wss_maps[key] = value
    param = ','.join(f'{key}={wss_maps.get(key, "")}' for key in WS_SIGN_PARAMS)
    return _get_sign_ctx().call('get_sign', hashlib.md5(param.encode()).hexdigest())

# 参与字幕生成的消息类型（其余只保留在内存，不写 XML）
#
# 刻意排除了 member(进场) / like(点赞) / social(关注)：这几类量极大（实测一场里
# 进场能占到六成以上），放进字幕会把真正的评论挤掉——顶部 4 行会被「XX进入直播间」
# 刷满，观众说了什么反而看不到。这两类消息不进 XML，也不进字幕；需要的话调这个常量即可。
SUBTITLE_TYPES = ('chat', 'gift', 'emoji')

# 每类消息用什么样式、前缀什么标记。礼物和表情单独一档，画面上一眼能认出来，
# 否则它们混在普通评论里看不出区别。
# 注意：B 站弹幕的 ASS 渲染也复用 render_ass（见 danmaku_bili._write_ass），
# 所以 sc(醒目留言)/guard(上舰) 的样式和前缀虽然抖音用不到，也要留在这里。
SUBTITLE_STYLES = {'gift': 'Gift', 'emoji': 'Emoji', 'sc': 'Gift', 'guard': 'Gift'}
SUBTITLE_PREFIX = {
    'gift': '【礼物】',
    'emoji': '【表情】',
    'sc': '【醒目留言】',
    'guard': '【上舰】',
    'like': '【点赞】',
    'member': '【进场】',
    'social': '【关注】',
}


def subtitle_text(record: dict, show_user: bool = True) -> str:
    """字幕里显示的单行文本。普通消息带发言人，格式：用户名:弹幕内容；
    stats 类（在线人数）没有发言人，只显示内容本身。
    show_user=False 时省掉「用户名:」，对应 DanmakuFactory 的 --showusernames false。"""
    prefix = SUBTITLE_PREFIX.get(record.get('type'), '')
    if record.get('type') == 'stats':
        body = record.get('content', '')
    elif not show_user:
        body = record.get('content', '')
    else:
        body = f"{record.get('user', '')}:{record.get('content', '')}"
    return prefix + body


def _fmt_ass_time(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    h = int(seconds // 3600)
    m = int(seconds % 3600 // 60)
    s = int(seconds % 60)
    cs = int(round(seconds * 100) % 100)
    return f'{h:d}:{m:02d}:{s:02d}.{cs:02d}'


def ass_font_name() -> str:
    """ASS 里写死的字体必须真实存在，否则 libass 渲染不出中文。

    Docker/Linux 镜像里没有微软雅黑，直接沿用 Windows 的名字会导致字幕整体不显示；
    这里按平台挑一个确定装了的字体，并在镜像里额外配了 fontconfig 别名兜底。
    """
    # 统一写 Microsoft YaHei：这就是 DanmakuFactory 的默认字体，Windows 播放器
    # 一定有；Linux 播放器会由 fontconfig 替换成系统里的中文字体。之前按平台写
    # WenQuanYi，玩家那边字体一换，字号观感就和 DMF 的成片对不上了。
    return 'Microsoft YaHei'


# ASS 字幕的默认排版，对齐 DanmakuFactory 的默认预设：
#   字体 42 / 阴影 1 / 不透明度 180 / 描边 0 / 滚动通过 12 秒 / 固定停留 5 秒
# 这样录完直接得到的 .ass 和用 DanmakuFactory 手工转出来的观感一致。
SUBTITLE_FONT_SIZE = 42
SUBTITLE_ALPHA = 75          # 不透明度 180 → ASS 透明度 = 255-180
SUBTITLE_OUTLINE = 0
SUBTITLE_SHADOW = 1
SCROLL_DURATION = 12.0       # 滚动弹幕通过时间（秒）
FIXED_DURATION = 5.0         # 固定弹幕停留时间（秒）
_LINE_HEIGHT_RATIO = 1.45

_VIDEO_EXTS = ('.mp4', '.ts', '.mkv', '.flv')


def probe_playres(base_path: str) -> Optional[tuple]:
    """从同名的录像文件读视频分辨率，读不到返回 None。

    ASS 的 PlayRes 必须和视频一致，libass 会按两者的比例缩放，对不上字幕就被
    拉扁或拉长。所以生成的 .ass 一律以实际视频为准，1920x1080 只是兜底。
    """
    exe = shutil.which('ffprobe')
    if not exe:
        return None
    # 分段录制时文件叫 xxx_001.ts，所以先在「同名文件」里找，再退回「同前缀的片段」
    candidates = [f'{base_path}{ext}' for ext in _VIDEO_EXTS]
    candidates = [p for p in candidates if os.path.exists(p)]
    if not candidates:
        for ext in _VIDEO_EXTS:
            hits = sorted(glob.glob(f'{base_path}_*{ext}'))
            if hits:
                candidates = hits[:1]
                break
    for path in candidates:
        try:
            out = subprocess.run(
                [exe, '-v', 'error', '-select_streams', 'v:0',
                 '-show_entries', 'stream=width,height', '-of', 'csv=p=0:s=x', path],
                capture_output=True, text=True, timeout=10,
            )
            width, height = (out.stdout or '').strip().split('\n')[0].split('x')[:2]
            width, height = int(width), int(height)
            if width > 0 and height > 0:
                return width, height
        except Exception:
            continue
    return None


def _ass_colour(rgb: str, alpha: int = SUBTITLE_ALPHA) -> str:
    """ASS 颜色是 &HAABBGGRR，alpha 越大越透明（0 完全不透明）。"""
    return f'&H{max(0, min(255, int(alpha))):02X}{rgb}'


def _escape_ass_text(text: str) -> str:
    """弹幕内容可能含有 ASS 的保留字符，原样写进去会把整行结构搞坏。

    换行按 DanmakuFactory 的做法换成 ASS 的硬空格 `\\h`：直接留 \\n 会断成两行，
    换成普通空格又会把连续换行挤成一个间隔。
    """
    return (text or '').replace('\\', '\\\\').replace('{', '(').replace('}', ')').replace('\n', '\\h')


def _ass_text_width(text: str, font_size: int = SUBTITLE_FONT_SIZE) -> float:
    """粗略估算文本宽度：中日韩字符按一个字宽算，其余（拉丁字母、数字）按 0.55 字宽算。"""
    width = 0.0
    for ch in text or '':
        width += font_size if ord(ch) > 0x2E7F else font_size * 0.55
    return max(width, font_size * 2) + font_size * 0.5


PRIVILEGE_TYPES = ('gift', 'sc', 'guard')

# 消息框停留时长的兜底与分档参数。
# 金额单位一律按「厘」算（1 元 = 1000 厘，1 电池 = 100 厘），与 DanmakuFactory 一致：
#   < 2000 电池    ：每电池 0.06 秒（100 电池 = 6 秒），最短 2 秒，最长 2 分钟
#   2000~20000 电池：线性映射到 2~5 分钟
#   >= 20000 电池  ：固定 6 分钟
MSG_LOW_TIER = 2000          # 电池
MSG_HIGH_TIER = 20000        # 电池
MSG_LOW_MAX = 120.0          # 秒（2 分钟，低档封顶、中档起点）
MSG_MID_MAX = 300.0          # 秒（5 分钟，中档封顶）
MSG_HIGH_MAX = 360.0         # 秒（6 分钟，高档固定）
MSG_MIN_SECONDS = 2.0        # 秒（低档下限）
MSG_SECONDS_PER_BATTERY = 0.06


def message_seconds(record: dict, fixed: float = 0.0, cap: float = 0.0,
                    source: str = 'price') -> float:
    """礼物 / SC / 上舰的消息框停留多久。

    source='price'（默认）按金额分档，规则与自用版 DanmakuFactory 的
    getMsgDurationByPrice() 一致（battery = 金瓜子 / 100）：

        battery <  2000 : 0.06 秒/电池，钳在 [2 秒, 2 分钟]
        2000 ~ 20000    : 线性映射到 [2 分钟, 5 分钟]
        battery >= 20000: 固定 6 分钟

    source='time' 则优先用消息自带的时长（XML 的 `time` 属性：SC 的 time、
    礼物的 combo_stay_time），没有才回落到上面的分档。

    fixed > 0 时全部统一成这个值（DanmakuFactory 的 --msgboxduration 优先级最高）；
    cap > 0 再统一封顶，是本项目的额外选项（默认 0 = 不封顶）。
    """
    rtype = str(record.get('type') or '').lower()

    if fixed and fixed >= 0.01:
        seconds = float(fixed)
    else:
        own = 0.0
        if str(source).lower().startswith(('time', '自带', '消息')):
            try:
                own = float(record.get('duration') or record.get('sc_time') or 0)
            except (TypeError, ValueError):
                own = 0.0
        if own > 0:
            seconds = own
        else:
            try:
                coin = float(record.get('diamond') or 0)
            except (TypeError, ValueError):
                coin = 0.0
            battery = max(0.0, coin) / 100.0
            if battery >= MSG_HIGH_TIER:
                seconds = MSG_HIGH_MAX
            elif battery >= MSG_LOW_TIER:
                span = MSG_HIGH_TIER - MSG_LOW_TIER
                seconds = MSG_LOW_MAX + (battery - MSG_LOW_TIER) * (MSG_MID_MAX - MSG_LOW_MAX) / span
            else:
                seconds = battery * MSG_SECONDS_PER_BATTERY
                seconds = min(MSG_LOW_MAX, max(MSG_MIN_SECONDS, seconds))

    if cap and cap > 0:
        seconds = min(seconds, float(cap))
    return max(seconds, MSG_MIN_SECONDS)


def _take_slot(tracks: list, start: float, limit: int):
    """找一条空闲轨道；都占着就新增一条（不超过 limit），返回 (slot, 是否新建)。"""
    for i, busy_until in enumerate(tracks):
        if busy_until <= start:
            return i, False
    if len(tracks) < limit:
        tracks.append(0.0)
        return len(tracks) - 1, True
    return None, False


USER_NAME_COLOUR = 'BCACF7'      # DanmakuFactory 固定用这个粉色画用户名（ASS 是 BGR）
MSG_FONT_RATIO = 38.0 / 42.0     # 消息框字号 = 正文字号 × 38/42（DMF 默认 42 / 38）
MSG_CARD_H = 87                  # 上舰卡片的圆角矩形高度（字号 38 时）
MSG_CARD_W = 500                 # 卡片宽度（DMF --msgboxsize 默认 500x1080）
MSG_BOTTOM_PAD = 6


def _bgr(colour) -> str:
    """B 站弹幕颜色是 0xRRGGBB，ASS 要的是 BBGGRR。"""
    try:
        value = int(colour)
    except (TypeError, ValueError):
        return 'FFFFFF'
    value = max(0, min(0xFFFFFF, value))
    return f'{value & 0xFF:02X}{(value >> 8) & 0xFF:02X}{(value >> 16) & 0xFF:02X}'


def _msg_body(record: dict) -> str:
    """消息框里正文那一行：礼物「名称 xN」、上舰「Welcome new 舰长!」、SC 为留言内容。

    上舰的文案照 DanmakuFactory 的原样，它就是这么写的。
    """
    rtype = str(record.get('type') or '').lower()
    if rtype == 'guard':
        name = record.get('guard_name') or record.get('gift_name') or '舰长'
        return f'Welcome new {name}!'
    if rtype == 'gift':
        name = record.get('gift_name') or record.get('content') or '礼物'
        try:
            count = int(record.get('gift_count') or 1)
        except (TypeError, ValueError):
            count = 1
        return f'{name} x{count}'
    return record.get('content') or ''


def _ass_line_text(record: dict, show_user: bool = True, hard_space: bool = False,
                   message: bool = False) -> str:
    """一行字幕的 ASS 文本。

    照 DanmakuFactory 的着色方式：用户名固定粉色（消息框里加粗），正文用弹幕自己的
    颜色（B 站 XML 的 p 第 4 项）。show_user=False 时只输出正文，但仍然带颜色标签，
    否则会继承 Style 里的颜色，和 DMF 的 --showusernames false 输出不一致。
    """
    if message:
        body = _escape_ass_text(_msg_body(record))
    else:
        body = _escape_ass_text(record.get('content') or '')
    colour = _bgr(record.get('color'))

    if not show_user:
        bold = '\\b0' if message else ''
        return f'{{\\c&HFFFFFF{bold}}}{body}'

    user = _escape_ass_text(record.get('user') or '')
    if message:
        return (f'{{\\c&H{USER_NAME_COLOUR}\\b1}}{user}: '
                f'{{\\c&HFFFFFF\\b0}}{body}')
    sep = '\\h' if hard_space else ''
    return f'{{\\c&H{USER_NAME_COLOUR}}}{user}:{sep}{{\\c&H{colour}}}{body}'



def _message_events(start: float, seconds: float, blocks: list, left: int, height: int,
                    slide: float = 0.25) -> list:
    """消息块 -> 进场 / 停留 / 退场三段，照 DanmakuFactory 的 250ms 滑动 + 圆角裁剪。

    DMF 的文案不是在原地凭空出现：从画面左侧滑进来 0.25 秒、停住、再滑出去 0.25 秒，
    并用 \\clip 把超出消息框的部分裁掉，所以左边会有一段"推入"的观感。
    """
    out = []
    hold_in = start + slide
    hold_out = start + seconds - slide
    if hold_out <= hold_in:
        hold_in, hold_out = start, start + seconds
    clip = (f'\\clip(m {left} 19 b {left} 9 {left + 9} 0 {left + 19} 0 '
            f'l {left + 481} 0 b {left + 491} 0 {left + 500} 9 {left + 500} 19 '
            f'l {left + 500} {height} l {left} {height})')
    off = 20 - MSG_CARD_W          # 完全滑出画面左侧的位置（DMF 用 -480）
    for layer, x, y, text in blocks:
        out.append(f'Dialogue: {layer},{_fmt_ass_time(start)},{_fmt_ass_time(hold_in)},Gift,msg,0,0,0,,'
                   f'{{\\an7\\move({off},{y},{x},{y}){clip}}}{text}')
        out.append(f'Dialogue: {layer},{_fmt_ass_time(hold_in)},{_fmt_ass_time(hold_out)},Gift,msg,0,0,0,,'
                   f'{{\\an7\\pos({x},{y})}}{text}')
        out.append(f'Dialogue: {layer},{_fmt_ass_time(hold_out)},{_fmt_ass_time(start + seconds)},Gift,msg,0,0,0,,'
                   f'{{\\an7\\move({x},{y},{off},{y}){clip}}}{text}')
    return out


def render_ass(records: list[dict], ass_path: str, max_lines: int = 4, duration: float = FIXED_DURATION,
               mode: str = 'scroll', playres: tuple[int, int] = (1920, 1080),
               font_size: int = SUBTITLE_FONT_SIZE, types: tuple | list | None = None,
               gift_min_diamond: float = 0.0, scroll_duration: float = SCROLL_DURATION,
               alpha: int = SUBTITLE_ALPHA, outline: int = SUBTITLE_OUTLINE,
               shadow: int = SUBTITLE_SHADOW, privilege_layout: str = 'corner',
               privilege_max: int = 0, privilege_cap: float = 0.0,
               msg_duration: float = 0.0, msg_source: str = 'price',
               show_user: bool = True) -> tuple[int, int]:
    """把弹幕渲染成 ASS 字幕文件，返回 (写入条数, 丢弃条数)。

    排版与配色都按 DanmakuFactory 的实际输出对齐（src/AssFile/AssFile.c）：

      1/2/3 滚动（R2L）：一行一行从 y=0 开始，行距等于字号，一趟 scroll_duration 秒
      6     逆向滚动（L2R）：反过来飘，同样 12 秒
      5     顶部固定（TOP）：停在顶部，居中，停留 duration（fixTime）秒
      4     底部固定（BTM）：从画面底部往上按字号排列，停留 duration 秒
      礼物/SC/上舰：消息框，左下角从下往上堆，上舰还画一层半透明圆角卡片

    文字配色：用户名 `&HBCACF7` 粉色，正文用弹幕自身颜色（白弹幕就是白色）；
    消息框里用户名加粗、正文白色。`mode='fixed'` 是本项目额外开关，把普通弹幕
    一律收进顶部固定。msg_duration > 0 时消息统一停留该秒数（--msgboxduration）。

    playres 必须与最终视频的分辨率一致：libass 按 PlayRes 与实际画面的比例缩放，
    宽高比对不上时字幕会被拉扁或拉长。调用方应先用 probe_playres() 取实际分辨率。
    """
    duration = float(duration or FIXED_DURATION)
    scroll_duration = float(scroll_duration or SCROLL_DURATION)
    width, height = int(playres[0]), int(playres[1])
    font_size = max(12, int(font_size or SUBTITLE_FONT_SIZE))
    line_height = font_size                       # DMF 的行距就是字号本身
    msg_font = max(12, int(round(font_size * MSG_FONT_RATIO)))
    left_margin = max(6, int(round(width * 20.0 / 1920.0)))
    # 0 = 自动：按画面高度能放几行就放几行（对齐 DanmakuFactory 的「显示区域 1.0、密度不限」）
    limit = int(max_lines or 0)
    max_lines = limit if limit > 0 else max(1, height // line_height)
    scroll = str(mode).strip().lower() not in ('fixed', '固定', '0', 'false', 'no')
    privilege_layout = str(privilege_layout or 'corner').strip().lower()
    if privilege_layout in ('关闭', 'off', 'none', '0', 'false', 'no'):
        privilege_layout = 'off'
    elif privilege_layout in ('滚动', 'scroll'):
        privilege_layout = 'scroll'
    else:
        privilege_layout = 'corner'
    try:
        msg_duration = max(0.0, float(msg_duration or 0))
    except (TypeError, ValueError):
        msg_duration = 0.0

    font = ass_font_name()
    header = (
        '[Script Info]\n'
        'ScriptType: v4.00+\n'
        'Collisions: Normal\n'
        f'PlayResX: {width}\n'
        f'PlayResY: {height}\n'
        'Timer: 100.0000\n'
        'WrapStyle: 2\n'
        'ScaledBorderAndShadow: yes\n'
        '\n'
        '[V4+ Styles]\n'
        'Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, '
        'Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, '
        'Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n'
        # 普通弹幕：白字，颜色逐行用 \c 覆盖
        f'Style: Danmaku,{font},{font_size},{_ass_colour("FFFFFF", alpha)},&H00FFFFFF,&H00000000,'
        f'&H1E6A5149,0,0,0,0,100.00,100.00,0.00,0.00,1,{int(outline)},{int(shadow)},8,0,0,0,1\n'
        # 固定弹幕（顶部/底部）
        f'Style: Fixed,{font},{font_size},{_ass_colour("FFFFFF", alpha)},&H00FFFFFF,&H00000000,'
        f'&H1E6A5149,0,0,0,0,100.00,100.00,0.00,0.00,1,{int(outline)},{int(shadow)},8,0,0,0,1\n'
        # 消息框：字号比正文小一号，左上对齐
        f'Style: Gift,{font},{msg_font},{_ass_colour("FFFFFF", alpha)},&H00FFFFFF,&H00000000,'
        f'&H1E6A5149,0,0,0,0,100.00,100.00,0.00,0.00,1,{int(outline)},{int(shadow)},7,0,0,0,1\n'
        # 表情（抖音小红心/微笑这类动态表情的名字）：浅黄
        f'Style: Emoji,{font},{font_size},{_ass_colour("A0F0FF", alpha)},&H00FFFFFF,&H00000000,'
        f'&H1E6A5149,0,0,0,0,100.00,100.00,0.00,0.00,1,{int(outline)},{int(shadow)},8,0,0,0,1\n'
        '\n'
        '[Events]\n'
        'Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n'
    )

    events: list[str] = []
    scroll_tracks: list[float] = []    # 滚动轨道：值是该轨道可再放新弹幕的时间
    top_tracks: list[float] = []       # 顶部固定
    bottom_tracks: list[float] = []    # 底部固定
    priv_tracks: list[float] = []      # 左下角消息
    priv_heights: list[int] = []       # 每个消息块的高度，用来从下往上累加
    dropped = 0

    allowed = None
    if types:
        allowed = {str(t).strip().lower() for t in types if str(t).strip()}
    min_diamond = float(gift_min_diamond or 0)
    filtered = 0

    for r in records:
        rtype = str(r.get('type') or 'chat').lower()
        if allowed is not None and rtype not in allowed:
            continue
        if rtype == 'gift' and min_diamond > 0:
            try:
                diamond = float(r.get('diamond', 0) or 0)
            except (TypeError, ValueError):
                diamond = 0.0
            if diamond < min_diamond:
                filtered += 1
                continue

        start = float(r.get('offset', 0) or 0)
        if not str(r.get('content') or '').strip() and not str(r.get('gift_name') or '').strip():
            continue

        # ---------- 消息：左下角消息框，上舰额外画一层圆角卡片 ----------
        if rtype in PRIVILEGE_TYPES and privilege_layout != 'scroll':
            if privilege_layout == 'off':
                continue
            seconds = message_seconds(r, msg_duration, privilege_cap, msg_source)
            text = _ass_line_text(r, show_user, message=True)
            block_h = MSG_CARD_H if rtype == 'guard' else msg_font + MSG_BOTTOM_PAD

            slot, _new = _take_slot(priv_tracks, start, int(privilege_max or 0) or max_lines)
            if slot is None:
                dropped += 1
                continue
            while len(priv_heights) <= slot:
                priv_heights.append(block_h)
            priv_heights[slot] = block_h
            priv_tracks[slot] = start + seconds

            used = sum(priv_heights[:slot]) if slot else 0
            base = height - MSG_BOTTOM_PAD - used
            y = base - block_h
            blocks: list[tuple[int, int, int, str]] = []   # (layer, x, y, 文本)
            if rtype == 'guard':
                # 卡片：DMF 的 500x87 圆角矩形，浅米色 30% 不透明
                card = ('{\\c&HFCE8D8\\alpha&H4D&\\p1\\bord0\\shad0}'
                        'm 0 19 b 0 9 9 0 19 0 l 481 0 b 491 0 500 9 500 19 '
                        'l 500 68 b 500 78 491 87 481 87 l 19 87 b 9 87 0 78 0 68')
                blocks.append((0, left_margin, y, card))
                guard_fs = max(12, int(round(msg_font * 30.0 / 38.0)))
                # 卡片里两行：上面深棕色的用户名，下面深灰的欢迎语（DMF 就这两行）
                guard_user = _escape_ass_text(r.get('user') or '')
                if show_user and guard_user:
                    blocks.append((1, left_margin + 9, y + 6,
                                   f'{{\\c&H8A3619\\bord0\\shad0}}{guard_user}'))
                blocks.append((1, left_margin + 9, y + 44,
                               f'{{\\c&H313131\\fs{guard_fs}\\bord0\\shad0}}{_escape_ass_text(_msg_body(r))}'))
            else:
                blocks.append((1, left_margin, base - msg_font,
                               _ass_line_text(r, show_user, message=True)))
            events.extend(_message_events(start, seconds, blocks, left_margin, height))
            continue

        # ---------- 普通弹幕：按 DanmakuFactory 的 type 映射分流 ----------
        try:
            chat_mode = int(r.get('mode') or 1)
        except (TypeError, ValueError):
            chat_mode = 1
        if not scroll:
            kind = 'top'
        elif chat_mode == 4:
            kind = 'bottom'
        elif chat_mode == 5:
            kind = 'top'
        elif chat_mode == 6:
            kind = 'l2r'
        else:
            kind = 'scroll'

        text = None
        if kind == 'scroll':
            slot, _new = _take_slot(scroll_tracks, start, max_lines)
            if slot is None:
                dropped += 1
                continue
            text = _ass_line_text(r, show_user, hard_space=True)
            end = start + scroll_duration
            text_width = _ass_text_width(subtitle_text(r, show_user), font_size)
            # 同轨道的两条不能追尾：前一条得先把尾部挪进画面，后一条才能出发，
            # 所需时间占整趟行程的比例正好是「宽度 / 行程总长」
            scroll_tracks[slot] = start + scroll_duration * text_width / (width + text_width) + 0.25
            y = slot * line_height
            prefix = '{\\an7\\move(' + f'{width},{y},{-int(text_width)},{y}' + ')}'
        elif kind == 'l2r':
            slot, _new = _take_slot(scroll_tracks, start, max_lines)
            if slot is None:
                dropped += 1
                continue
            text = _ass_line_text(r, show_user)
            end = start + scroll_duration
            text_width = _ass_text_width(subtitle_text(r, show_user), font_size)
            scroll_tracks[slot] = start + scroll_duration * text_width / (width + text_width) + 0.25
            y = slot * line_height
            prefix = '{\\an7\\move(' + f'{-int(text_width)},{y},{width},{y}' + ')}'
        elif kind == 'top':
            slot, _new = _take_slot(top_tracks, start, max_lines)
            if slot is None:
                dropped += 1
                continue
            text = _ass_line_text(r, show_user)
            end = start + duration
            top_tracks[slot] = end
            y = slot * line_height
            prefix = '{\\an8\\pos(' + f'{width // 2},{y}' + ')}'
        else:
            slot, _new = _take_slot(bottom_tracks, start, max_lines)
            if slot is None:
                dropped += 1
                continue
            text = _ass_line_text(r, show_user)
            end = start + duration
            bottom_tracks[slot] = end
            # 底部固定：DMF 让第一行贴着画面底边，往上按字号叠
            y = height - (slot + 1) * line_height
            prefix = '{\\an8\\pos(' + f'{width // 2},{y}' + ')}'

        style_name = SUBTITLE_STYLES.get(rtype, 'Danmaku')
        if kind in ('top', 'bottom'):
            style_name = 'Fixed'
        # 底部固定压在上面一层，和 DMF 一样（否则会被后写的弹幕盖住）
        layer = 1 if kind == 'bottom' else 0
        events.append(
            f'Dialogue: {layer},{_fmt_ass_time(start)},{_fmt_ass_time(end)},{style_name},row{slot},0,0,0,,{prefix}{text}'
        )

    # DMF 写的是「带 BOM 的 UTF-8 + CRLF」，这里跟它一致，Windows 播放器最稳；
    # 显式拼 CRLF 再以 newline='' 落盘，免得 Windows/Linux 两个环境输出不一样。
    body = (header + '\n'.join(events) + '\n').replace('\n', '\r\n')
    with open(ass_path, 'w', encoding='utf-8-sig', newline='') as f:
        f.write(body)
    return len(events), dropped + filtered


def clean_base_path(save_file_path: str) -> str:
    """从 ffmpeg 输出路径推出弹幕文件的公共前缀。

    处理分段录制时的 xxx_%03d.ts 这类模板路径。
    """
    base = str(save_file_path)
    base = re.sub(r'_%0\d+d', '', base)          # 去掉 _000 这类序号占位符
    base = re.sub(r'%Y-%m-%d_%H-%M-%S', '', base)  # 去掉按时间分段的模板占位符
    base = re.sub(r'\.[A-Za-z0-9]+$', '', base)  # 去掉扩展名
    return base


class DanmakuRecorder(SegmentWriterMixin):
    """单个直播间的弹幕抓取器，运行在独立线程里。"""

    def __init__(self, room_id: str, save_file_path: str, anchor_name: str = '',
                 cookie: Optional[str] = None, formats=('xml', 'ass'),
                 proxy: Optional[str] = None, sync_start: Optional[datetime] = None,
                 subtitle_max_lines: int = 0, subtitle_duration: float = FIXED_DURATION,
                 subtitle_mode: str = 'scroll', playres: tuple[int, int] = (1920, 1080),
                 subtitle_types: tuple | list | None = None, gift_min_diamond: float = 0.0,
                 subtitle_font_size: int = SUBTITLE_FONT_SIZE,
                 subtitle_alpha: int = SUBTITLE_ALPHA, subtitle_outline: int = SUBTITLE_OUTLINE,
                 subtitle_shadow: int = SUBTITLE_SHADOW,
                 subtitle_scroll_duration: float = SCROLL_DURATION,
                 privilege_layout: str = 'corner', privilege_max: int = 0,
                 privilege_cap: float = 0.0, msg_duration: float = 0.0,
                 msg_source: str = 'price', show_user: bool = True,
                 segment_video: str = ''):
        self.room_id = str(room_id)
        self.anchor_name = anchor_name or self.room_id
        self.formats = [f.strip().lower() for f in formats if f.strip()] or ['xml', 'ass']
        self.cookie = cookie or get_default_cookie()
        self.proxy = proxy
        self.sync_start = sync_start
        # 弹幕只占顶部若干行，避免密集时铺满画面
        # 0 = 自动铺满画面，具体行数在 render_ass 里按分辨率算
        self.subtitle_max_lines = max(0, int(subtitle_max_lines or 0))
        self.subtitle_duration = float(subtitle_duration or FIXED_DURATION)
        # scroll=滚动飘屏 / fixed=顶部固定逐条替换
        self.subtitle_mode = 'fixed' if str(subtitle_mode).strip().lower() in ('fixed', '固定') else 'scroll'
        self.playres = (int(playres[0]), int(playres[1])) if playres else (1920, 1080)
        self.subtitle_types = tuple(str(t).strip().lower() for t in (subtitle_types or SUBTITLE_TYPES) if str(t).strip())
        try:
            self.gift_min_diamond = float(gift_min_diamond or 0)
        except (TypeError, ValueError):
            self.gift_min_diamond = 0.0
        try:
            self.subtitle_font_size = max(12, int(subtitle_font_size or SUBTITLE_FONT_SIZE))
        except (TypeError, ValueError):
            self.subtitle_font_size = SUBTITLE_FONT_SIZE
        # ASS 排版（对齐 DanmakuFactory 默认预设，可在 WebUI「弹幕字幕」里改）
        self.subtitle_alpha = max(0, min(255, int(subtitle_alpha if subtitle_alpha is not None else SUBTITLE_ALPHA)))
        self.subtitle_outline = max(0, int(subtitle_outline if subtitle_outline is not None else SUBTITLE_OUTLINE))
        self.subtitle_shadow = max(0, int(subtitle_shadow if subtitle_shadow is not None else SUBTITLE_SHADOW))
        self.subtitle_scroll_duration = max(1.0, float(subtitle_scroll_duration or SCROLL_DURATION))
        # 礼物/SC/上舰：corner=堆在左下角（默认） scroll=跟着飘 off=不显示
        self.privilege_layout = str(privilege_layout or 'corner').strip().lower()
        # 0 = 按消息框高度自动决定能堆几条（对应 DanmakuFactory 的密度不限）
        self.privilege_max = max(0, int(privilege_max or 0))
        try:
            self.privilege_cap = max(0.0, float(privilege_cap or 0))
        except (TypeError, ValueError):
            self.privilege_cap = 0.0
        # >0 时所有消息统一停留这么久（DanmakuFactory 的 --msgboxduration）
        try:
            self.msg_duration = max(0.0, float(msg_duration or 0))
        except (TypeError, ValueError):
            self.msg_duration = 0.0
        # 'price'（默认）= 按金额分档；'time' = 优先用消息自带时长
        self.msg_source = str(msg_source or 'price').strip().lower()
        self.show_user = bool(show_user)
        self.base_path = clean_base_path(save_file_path)

        # 弹幕文件与视频同名，只换扩展名：xxx.mp4 / xxx.xml / xxx.ass
        self.xml_path = f'{self.base_path}.xml'
        self.ass_path = f'{self.base_path}.ass'

        self._stop_event = threading.Event()
        self._ws_host = WS_HOSTS[0]
        self._lock = threading.Lock()
        self._buffer: list[dict] = []
        self._records: list[dict] = []  # 全量消息，收尾时用来渲染 ASS
        self._last_flush = 0.0
        self._xml = None
        self._ws = None
        self._internal_ext = ''
        self._base_time: Optional[datetime] = None  # 无 sync_start 时作为时间轴基准
        self._thread: Optional[threading.Thread] = None
        self._heartbeat_thread: Optional[threading.Thread] = None
        self.count = 0
        # 分段录制：video_hint 是第一段视频的预期路径，只用来盯目录切段
        self.init_segment_mode(segment_video, sync_start)

    # ------------------------------------------------------------------ 对外接口

    def start(self) -> bool:
        if websocket is None:
            logger.warning('弹幕抓取未启动：缺少 websocket-client，请执行 pip install websocket-client')
            return False
        if pb is None:
            logger.warning(f'弹幕抓取未启动：protobuf 模块未就绪 -> {_PB_IMPORT_ERROR}')
            return False
        os.makedirs(os.path.dirname(self.base_path) or '.', exist_ok=True)
        if 'xml' in self.formats and not self.segment_mode:
            # 抖音没有 SC/上舰，XML 头部样式不生成这两个板块
            self._xml = XmlWriter(self.xml_path, self.room_id, anchor_name=self.anchor_name,
                                  sections=('d', 'gift'))
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name=f'danmaku_{self.room_id}', daemon=True)
        self._thread.start()
        self.start_segment_thread()
        target = self.xml_path if 'xml' in self.formats else self.ass_path
        logger.info(f'{self.anchor_name} 弹幕抓取已启动，保存至 {target}')
        return True

    def stop(self) -> None:
        self._stop_event.set()
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)
        # 收尾：写完最后一段（分段模式下每段各有一份 xml/ass）
        self.finalize_segments()
        target = self.xml_path if 'xml' in self.formats else self.ass_path
        logger.info(f'{self.anchor_name} 弹幕抓取已停止，共 {self.count} 条 -> {target}')

    # ------------------------------------------------------------------ 连接与收包

    def _build_ws_url(self) -> tuple[str, str]:
        now_ms = int(time.time() * 1000)
        user_unique_id = _random_id()
        push_did = _random_id()
        wrds_v = _random_id()
        internal_ext = (
            f'internal_src:dim|wss_push_room_id:{self.room_id}|wss_push_did:{push_did}'
            f'|first_req_ms:{now_ms}|fetch_time:{now_ms}|seq:1|wss_info:0-{now_ms}-0-0|wrds_v:{wrds_v}'
        )
        self._internal_ext = internal_ext

        params = {
            'app_name': 'douyin_web',
            'version_code': '180800',
            'webcast_sdk_version': '1.0.14-beta.0',
            'update_version_code': '1.0.14-beta.0',
            'compress': 'gzip',
            'device_platform': 'web',
            'cookie_enabled': 'true',
            'screen_width': '1536',
            'screen_height': '864',
            'browser_language': 'zh-CN',
            'browser_platform': 'Win32',
            'browser_name': 'Mozilla',
            'browser_version': BROWSER_VERSION,
            'browser_online': 'true',
            'tz_name': 'Asia/Shanghai',
            'cursor': f'd-1_u-1_fh-{push_did}_t-{now_ms}_r-1',
            'internal_ext': internal_ext,
            'host': 'https://live.douyin.com',
            'aid': '6383',
            'live_id': '1',
            'did_rule': '3',
            'endpoint': 'live_pc',
            'support_wrds': '1',
            'user_unique_id': user_unique_id,
            'im_path': '/webcast/im/fetch/',
            'identity': 'audience',
            'need_persist_msg_count': '15',
            'insert_task_id': '',
            'live_reason': '',
            'room_id': self.room_id,
            'heartbeatDuration': '0',
        }
        query = urllib.parse.urlencode(params)
        host = self._ws_host
        wss = f'wss://{host}/webcast/im/push/v2/?{query}'
        try:
            signature = generate_ws_signature(wss)
        except Exception as e:
            logger.warning(f'{self.anchor_name} 弹幕签名生成失败，尝试切换备用方案: {e}')
            signature = ''
        return f'{wss}&signature={signature}', host

    def _proxy_kwargs(self) -> dict:
        if not self.proxy:
            return {}
        try:
            parsed = urllib.parse.urlparse(self.proxy if '://' in self.proxy else f'http://{self.proxy}')
            if not parsed.hostname:
                return {}
            kwargs = {
                'http_proxy_host': parsed.hostname,
                'http_proxy_port': parsed.port or 80,
                'proxy_type': 'https' if parsed.scheme in ('https', 'wss') else 'http',
            }
            return kwargs
        except Exception:
            return {}

    def _run(self) -> None:
        retry = 0
        while not self._stop_event.is_set():
            url, host = self._build_ws_url()
            cookie_header = f'Cookie: {self.cookie}'
            ua_header = f'User-Agent: {DEFAULT_USER_AGENT}'
            origin_header = 'Origin: https://live.douyin.com'
            referer_header = 'Referer: https://live.douyin.com/'

            try:
                self._ws = websocket.WebSocketApp(
                    url,
                    header=[cookie_header, ua_header, origin_header, referer_header],
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws.run_forever(
                    ping_interval=0,
                    reconnect=0,
                    **self._proxy_kwargs(),
                )
            except Exception as e:
                logger.debug(f'弹幕连接异常({host}): {e}')

            if self._stop_event.is_set():
                break

            retry += 1
            # 连不上就换下一个接入点试试
            if retry % 2 == 0:
                self._ws_host = WS_HOSTS[(WS_HOSTS.index(self._ws_host) + 1) % len(WS_HOSTS)]
            if retry > 10:
                logger.warning(f'{self.anchor_name} 弹幕连接连续失败 10 次，停止重试')
                break
            logger.debug(f'{self.anchor_name} 弹幕连接断开，{min(retry * 2, 30)} 秒后重连(第 {retry} 次)')
            if self._stop_event.wait(min(retry * 2, 30)):
                break

    def _on_open(self, ws) -> None:
        logger.debug(f'{self.anchor_name} 弹幕 WebSocket 已连接(room_id={self.room_id})')
        if not self._heartbeat_thread or not self._heartbeat_thread.is_alive():
            self._heartbeat_thread = threading.Thread(target=self._heartbeat, args=(ws,), daemon=True)
            self._heartbeat_thread.start()

    def _on_error(self, ws, error) -> None:
        logger.debug(f'{self.anchor_name} 弹幕连接错误: {error}')

    def _on_close(self, ws, close_status_code, close_msg) -> None:
        logger.debug(f'{self.anchor_name} 弹幕连接关闭: {close_status_code} {close_msg}')

    def _heartbeat(self, ws) -> None:
        """抖音 IM 的心跳是一个 payload_type='hb' 的空 PushFrame，必须用 PING 帧发送"""
        while not self._stop_event.is_set():
            if self._stop_event.wait(10):
                break
            try:
                frame = pb.PushFrame()
                frame.payloadType = 'hb'
                ws.send(frame.SerializeToString(), opcode=websocket.ABNF.OPCODE_PING)
            except Exception:
                break

    def _send_ack(self, ws, internal_ext: str) -> None:
        """服务端要求回执时，把 internalExt 原样回传"""
        if pb is None or ws is None:
            return
        frame = pb.PushFrame()
        frame.payloadType = 'ack'
        frame.payload = (internal_ext or '').encode('utf-8')
        try:
            ws.send(frame.SerializeToString(), opcode=websocket.ABNF.OPCODE_BINARY)
        except Exception:
            pass

    def _on_message(self, ws, message) -> None:
        if not isinstance(message, (bytes, bytearray)):
            return
        try:
            frame = pb.PushFrame()
            frame.ParseFromString(bytes(message))
            if frame.payloadType != 'msg':
                return
            # 注意：不能只看 payloadEncoding。实测抖音现在会返回 'pb'，
            # 但 payload 实际仍带 gzip 头(1f8b)，按字段判断会漏解压，
            # 直接把压缩包丢给 protobuf 会报 "Wire format was corrupt"。
            # 这里以 magic number 为准，字段值仅作补充。
            payload = frame.payload
            if payload[:2] == b'\x1f\x8b' or frame.payloadEncoding == 'gzip':
                try:
                    payload = gzip.decompress(payload)
                except Exception:
                    pass

            response = pb.Response()
            response.ParseFromString(payload)

            if response.needAck:
                self._send_ack(ws, response.internalExt)

            for msg in response.messagesList:
                record = self._parse_message(msg)
                if record:
                    self._append(record)
        except Exception as e:
            logger.debug(f'弹幕消息解析失败: {e}')

    # ------------------------------------------------------------------ 消息解析

    def _parse_message(self, msg) -> Optional[dict]:
        method = msg.method
        if not method:
            return None
        try:
            if method == 'WebcastChatMessage':
                m = pb.ChatMessage()
                m.ParseFromString(msg.payload)
                content = m.content or (m.rtfContent.defaultPattern if m.rtfContent else '')
                return self._pack('chat', m.common, m.user, content, extra={})

            if method == 'WebcastEmojiChatMessage':
                m = pb.EmojiChatMessage()
                m.ParseFromString(msg.payload)
                content = m.defaultContent or (m.emojiContent.defaultPattern if m.emojiContent else '[表情]')
                return self._pack('emoji', m.common, m.user, content)

            if method == 'WebcastGiftMessage':
                m = pb.GiftMessage()
                m.ParseFromString(msg.payload)
                gift_name = m.gift.name if m.gift else '礼物'
                count = m.totalCount or m.repeatCount or m.comboCount or 1
                diamond = (m.gift.diamondCount if m.gift else 0) * count
                return self._pack('gift', m.common, m.user, f'送出 {gift_name} x{count}', extra={
                    'gift_name': gift_name,
                    'gift_count': count,
                    'diamond': diamond,
                })

            if method == 'WebcastLikeMessage':
                m = pb.LikeMessage()
                m.ParseFromString(msg.payload)
                return self._pack('like', m.common, m.user, f'点赞 x{m.count or 1}', extra={
                    'like_count': m.count,
                    'like_total': m.total,
                })

            if method == 'WebcastMemberMessage':
                m = pb.MemberMessage()
                m.ParseFromString(msg.payload)
                action_desc = m.actionDescription or ('进入直播间' if m.action == 1 else '关注主播')
                return self._pack('member', m.common, m.user, action_desc, extra={
                    'member_count': m.memberCount,
                    'action': m.action,
                })

            if method == 'WebcastSocialMessage':
                m = pb.SocialMessage()
                m.ParseFromString(msg.payload)
                return self._pack('social', m.common, m.user, f'关注了主播(共 {m.followCount} 人)')

            if method == 'WebcastRoomStatsMessage':
                m = pb.RoomStatsMessage()
                m.ParseFromString(msg.payload)
                return self._pack('stats', m.common, None, f'在线人数 {m.displayValue or m.displayShort}',
                                  extra={'online': m.displayValue})

            if method == 'WebcastMatchAgainstScoreMessage':
                m = pb.MatchAgainstScoreMessage()
                m.ParseFromString(msg.payload)
                a = m.against
                return self._pack('pk', m.common, None,
                                  f'PK {a.leftName or "左"} {a.leftGoalInt} : {a.rightGoalInt} {a.rightName or "右"}',
                                  extra={
                                      'left_name': a.leftName,
                                      'right_name': a.rightName,
                                      'left_score': a.leftGoalInt,
                                      'right_score': a.rightGoalInt,
                                      'match_status': m.matchStatus,
                                  })

            if method == 'WebcastControlMessage':
                m = pb.ControlMessage()
                m.ParseFromString(msg.payload)
                return self._pack('control', m.common, None, f'直播间状态变化 status={m.status}',
                                  extra={'status': m.status})
        except Exception as e:
            logger.debug(f'{method} 解析失败: {e}')
            return None
        return None

    @staticmethod
    def _to_timestamp_ms(value) -> Optional[int]:
        """把时间戳统一成毫秒。

        抖音不同消息的 createTime 单位并不一致：多数是秒(1e9)，
        但 WebcastRoomStatsMessage 给的是毫秒(1e12)，个别是微秒/纳秒。
        不归一化的话 *1000 后会把毫秒放大成 1e18，
        datetime.fromtimestamp 直接抛 OSError [Errno 22] Invalid argument。
        无法识别时返回 None，由调用方用当前时间兜底。
        """
        try:
            v = int(value)
        except (TypeError, ValueError):
            return None
        if v <= 0:
            return None
        if v < 100_000_000_000:  # 秒级
            v *= 1000
        while v > 4_102_444_800_000:  # 微秒 / 纳秒级，逐级缩小
            v //= 1000
        return v

    def _pack(self, msg_type: str, common, user, content: str, extra: Optional[dict] = None) -> dict:
        ts_ms = self._to_timestamp_ms(common.createTime) if common else None
        if ts_ms is None:
            ts_ms = int(time.time() * 1000)
        try:
            dt = datetime.fromtimestamp(ts_ms / 1000)
        except (OSError, ValueError, OverflowError):
            ts_ms = int(time.time() * 1000)
            dt = datetime.fromtimestamp(ts_ms / 1000)
        # 基准时刻：正常录制时是视频开始时刻（main.py 传入 sync_start）。
        # 单独自测时没有这个值，退化成「第一条消息的时间」，
        # 否则所有 offset 都是 0，字幕会全部堆在 0 秒没法用。
        if self.sync_start is not None:
            base = self.sync_start
        else:
            if self._base_time is None:
                self._base_time = dt
            base = self._base_time
        offset = (dt - base).total_seconds()
        record = {
            'type': msg_type,
            'time': dt.strftime('%Y-%m-%d %H:%M:%S'),
            'ts': ts_ms,
            'offset': round(max(offset, 0.0), 3),
            'user': user.nickName if user else '',
            'user_id': str(user.id) if user else '',
            'content': content,
        }
        if extra:
            record.update(extra)
        return record

    # ------------------------------------------------------------------ 落盘

    def _append(self, record: dict) -> None:
        with self._lock:
            self.count += 1
            self._buffer.append(record)
            self._records.append(record)
            now = time.time()
            if len(self._buffer) >= 20 or now - self._last_flush >= 5:
                self._flush(force=True)

    def _read_records(self) -> list[dict]:
        return list(self._records)

    def _open_xml_writer(self, stem: str):
        """按分段开一个新的 XML（抖音只有 d/gift 两个板块）。"""
        return XmlWriter(f'{stem}.xml', self.room_id, anchor_name=self.anchor_name,
                         sections=('d', 'gift'))

    def _render_segment_ass(self, records: list[dict], stem: str = '') -> None:
        """生成一段的 ASS 字幕。records 的时间轴已经相对本段起点。"""
        if 'ass' not in self.formats or not records:
            return
        kept = [r for r in records if str(r.get('type') or '').lower() in self.subtitle_types]
        if not kept:
            return
        ass_path = f'{stem}.ass' if stem else self.ass_path
        # PlayRes 以实际录像分辨率为准，取不到才退回构造时的值（默认 1920x1080）
        playres = probe_playres(stem or self.base_path) or self.playres
        written, dropped = render_ass(
            kept, ass_path, self.subtitle_max_lines, self.subtitle_duration,
            self.subtitle_mode, playres,
            font_size=self.subtitle_font_size,
            types=self.subtitle_types, gift_min_diamond=self.gift_min_diamond,
            scroll_duration=self.subtitle_scroll_duration,
            alpha=self.subtitle_alpha, outline=self.subtitle_outline, shadow=self.subtitle_shadow,
            privilege_layout=self.privilege_layout, privilege_max=self.privilege_max,
            privilege_cap=self.privilege_cap, msg_duration=self.msg_duration,
            msg_source=self.msg_source, show_user=self.show_user,
        )
        logger.debug(
            f'弹幕 ASS 字幕已生成: {ass_path}'
            f'(写入 {written} 条，'
            + ('飘屏滚动' if self.subtitle_mode == 'scroll' else '顶部固定')
            + (f'，最多 {self.subtitle_max_lines} 行' if self.subtitle_max_lines else '，按画面高度铺满')
            + (f'，因轨道占满丢弃 {dropped} 条' if dropped else '')
            + ')'
        )


# ---------------------------------------------------------------------- 会话管理

_sessions: dict[str, DanmakuRecorder] = {}
_sessions_lock = threading.Lock()


def start_danmaku(room_id: str, save_file_path: str, anchor_name: str = '',
                  cookie: Optional[str] = None, formats=('json',),
                  proxy: Optional[str] = None, sync_start: Optional[datetime] = None,
                  subtitle_max_lines: int = 0, subtitle_duration: float = FIXED_DURATION,
                  subtitle_mode: str = 'scroll', playres: tuple[int, int] = (1920, 1080),
                  subtitle_types: tuple | list | None = None,
                  gift_min_diamond: float = 0.0,
                  subtitle_font_size: int = SUBTITLE_FONT_SIZE,
                  subtitle_alpha: int = SUBTITLE_ALPHA, subtitle_outline: int = SUBTITLE_OUTLINE,
                  subtitle_shadow: int = SUBTITLE_SHADOW,
                  subtitle_scroll_duration: float = SCROLL_DURATION,
                  privilege_layout: str = 'corner', privilege_max: int = 0,
                  privilege_cap: float = 0.0, msg_duration: float = 0.0,
                  msg_source: str = 'price', show_user: bool = True,
                  segment_video: str = '') -> bool:
    key = os.path.abspath(clean_base_path(save_file_path))
    with _sessions_lock:
        if key in _sessions:
            return False
        recorder = DanmakuRecorder(room_id, save_file_path, anchor_name, cookie, formats, proxy, sync_start,
                                   subtitle_max_lines, subtitle_duration, subtitle_mode, playres,
                                   subtitle_types, gift_min_diamond, subtitle_font_size,
                                   subtitle_alpha, subtitle_outline, subtitle_shadow,
                                   subtitle_scroll_duration,
                                   privilege_layout, privilege_max, privilege_cap,
                                   msg_duration, msg_source, show_user, segment_video)
        ok = recorder.start()
        if ok:
            _sessions[key] = recorder
        return ok


def stop_danmaku(save_file_path: str) -> None:
    key = os.path.abspath(clean_base_path(save_file_path))
    with _sessions_lock:
        recorder = _sessions.pop(key, None)
    if recorder:
        recorder.stop()


def stop_all_danmaku() -> None:
    with _sessions_lock:
        recorders = list(_sessions.values())
        _sessions.clear()
    for recorder in recorders:
        try:
            recorder.stop()
        except Exception:
            pass


if __name__ == '__main__':
    # 自测：python -m src.danmaku <room_id|直播链接> [--seconds 30]
    import argparse
    import sys

    parser = argparse.ArgumentParser(description='抖音直播弹幕抓取自测')
    parser.add_argument('room_id', help='直播间真实 room_id，或 https://live.douyin.com/xxx 链接')
    parser.add_argument('--seconds', type=int, default=30, help='抓取时长，默认 30 秒')
    parser.add_argument('--out', default='./danmaku_test.ts', help='输出文件路径前缀')
    args = parser.parse_args()

    room_id, anchor = resolve_room_id(args.room_id)
    if not room_id:
        logger.error('无法解析直播间，请检查链接或 room_id 是否正确')
        sys.exit(1)
    logger.info(f'直播间: {anchor or room_id} (room_id={room_id})')

    rec = DanmakuRecorder(room_id, args.out, anchor or room_id, formats=('xml', 'ass'))
    if not rec.start():
        sys.exit(1)
    try:
        import time as _time
        _time.sleep(args.seconds)
    except KeyboardInterrupt:
        pass
    rec.stop()
    print(f'共抓取 {rec.count} 条 -> {rec.xml_path}')
