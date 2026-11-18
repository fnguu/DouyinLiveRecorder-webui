# -*- encoding: utf-8 -*-
"""Cookie 凭证健康检查与失效推送。

做三件事：

1. 定时（或手动）检查 config.ini 里 ``[Cookie]`` 各平台凭证还能不能用；
2. 结论落到 ``config/cookie_status.json``，供「系统状态 → Cookie 状态」卡片读取；
3. 失效或即将过期时，走 ``[推送配置]`` 里已经配好的渠道发一条提醒。

判定原则是「宁可说无法判定，也不误报」：

- 抖音：Cookie 里的 ``sid_guard`` 带过期时间戳，能算出准确到期日，权威且离线可判；
  没有该字段时只做关键字段完整性检查，不下失效结论。
- B 站：调 ``x/web-interface/nav`` 问一次登录态，这是官方接口，权威。
- 其它平台：只报「已配置 / 未配置」。这些平台没有稳定的登录态查询接口，
  靠猜得到的「失效」只会变成狼来了。
- 网络异常一律记 unknown：不覆盖上一次的结论、也不触发推送，避免断网就讹人。

推送去重：状态变化才提醒；持续失效时按「失效重复提醒(小时)」再提醒，0 表示只提醒一次。
状态文件由后台线程和「立即检测」按钮共用，不会重复轰炸。
"""
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from email.utils import parsedate_to_datetime

from .config_io import read_ini
from .paths import BASE_DIR, CONFIG_DIR
from .schema import _COOKIE_NAME

STATUS_FILE = os.path.join(CONFIG_DIR, 'cookie_status.json')

EXPIRING_DAYS = 7                     # 距离过期多少天开始提醒
DEFAULT_INTERVAL_HOURS = 6            # 默认多久检查一次
DEFAULT_REPEAT_HOURS = 24             # 持续失效时默认多久再提醒
CHECK_TIMEOUT = 8                     # 单次探测超时（秒）
WATCH_TICK = 300                      # 后台线程多久醒一次看是否到点
WATCH_FIRST_DELAY = 25                # 启动后多久做第一次检查

BILI_NAV = 'https://api.bilibili.com/x/web-interface/nav'
BILI_REFERER = 'https://www.bilibili.com/'
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36')

# 顺序即展示顺序；(展示名, 配置键, 探测方式, 是否必填)
PLATFORMS = [
    ('抖音', '抖音cookie', 'douyin', True),
    ('B站', 'B站cookie', 'bilibili', False),
]

# 只要能出现其中一个，就说明 Cookie 至少是"粘到了东西"，不该判成粘贴不完整
DOUYIN_MARKERS = ('ttwid', 'sessionid', 'sid_guard', 'sid_tt', 'msToken',
                  'odin_tt', 'uid_tt', 'passport_csrf_token', 'UIFID')

STATUS_RANK = {'invalid': 0, 'expiring': 1, 'missing': 2, 'unknown': 3, 'valid': 4}

_LOCK = threading.Lock()
_START_LOCK = threading.Lock()
_WATCHER = None


# ---------------------------------------------------------------- 小工具
def _bool(value, default=False):
    if value is None:
        return default
    text = str(value).strip()
    if text in ('是', 'yes', 'YES', 'true', 'True', '1', 'on'):
        return True
    if text in ('否', 'no', 'NO', 'false', 'False', '0', 'off', ''):
        return False
    return default


def _int(value, default):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _fmt_ts(ts: float) -> str:
    return time.strftime('%Y-%m-%d %H:%M', time.localtime(ts))


def _platform_label(key: str) -> str:
    base = key[:-len('cookie')] if key.lower().endswith('cookie') else key
    base = base.strip('_ ').strip()
    return _COOKIE_NAME.get(base.lower(), base) or key


# ---------------------------------------------------------------- 配置与状态
def load_config() -> dict:
    """从 config.ini 读本次检查需要的开关与文案。"""
    push = read_ini().get('推送配置', {})
    return {
        'enabled': _bool(push.get('cookie失效推送(是/否)'), True),
        'interval_hours': max(1, _int(push.get('cookie检查间隔(小时)'), DEFAULT_INTERVAL_HOURS)),
        'repeat_hours': max(0, _int(push.get('cookie失效重复提醒(小时)'), DEFAULT_REPEAT_HOURS)),
        'template': (push.get('cookie失效推送内容') or '').replace('\\n', '\n'),
        'channels': push.get('直播状态推送渠道') or '',
    }


def _load_state() -> dict:
    try:
        with open(STATUS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get('items'), dict):
            return data
    except (OSError, ValueError):
        pass
    return {'items': {}, 'checked_at': 0}


def _save_state(state: dict) -> None:
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        tmp = f'{STATUS_FILE}.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
        os.replace(tmp, STATUS_FILE)
    except OSError:
        pass


def _rows():
    """把 [Cookie] 里配置了的平台展开成待检查列表。

    抖音、B 站无论有没有值都保留（缺了要能在界面上看到「未配置」）；
    其它平台只有填了才出现，免得界面被 40 个空行占满。
    """
    section = read_ini().get('Cookie', {})
    by_lower = {k.strip().lower(): (k.strip(), (v or '').strip()) for k, v in section.items()}

    rows = []
    for name, key, kind, required in PLATFORMS:
        hit = by_lower.get(key.lower())
        rows.append({
            'platform': name,
            'key': hit[0] if hit else key,
            'cookie': hit[1] if hit else '',
            'kind': kind,
            'required': required,
        })

    reserved = {k.lower() for _, k, _, _ in PLATFORMS}
    for low, (key, value) in by_lower.items():
        if low in reserved or not value:
            continue
        rows.append({
            'platform': _platform_label(key),
            'key': key,
            'cookie': value,
            'kind': 'generic',
            'required': False,
        })
    return rows


# ---------------------------------------------------------------- 各平台探测
def douyin_expiry(cookie: str) -> int:
    """从抖音 Cookie 的 sid_guard 里取过期时间戳，取不到返回 0。

    sid_guard 形如 ``<sessionid>|<ts>|<ttl>|<日期 GMT>``，坑在于第二段的含义两边不一样：
      B 站：<ts> 就是过期时间；
      抖音：<ts> 是**登录时间**（和 login_time 完全一样），真正过期 = <ts> + <ttl>。
    所以优先读第四段的日期字符串 —— 它两边写的都是真正的过期时刻；日期读不出来时，
    再按「ts 早于现在、ts+ttl 晚于现在就是登录时间」推算，都不成立才当过期时间用。
    """
    match = re.search(r'(?:^|;\s*)sid_guard=([^;]*)', cookie or '')
    if not match:
        return 0
    raw = urllib.parse.unquote(match.group(1).strip())
    parts = [p.strip() for p in raw.split('|')]
    if len(parts) < 2:
        return 0

    def plausible(ts: int) -> bool:
        # 合法范围留在 2001 ~ 2096 年之间，挡住把用户 id 当时间戳的误判
        return 1_000_000_000 < ts < 4_000_000_000

    # 1) 第四段的 GMT 日期最可靠（两边平台写的都是真正的过期时刻）
    if len(parts) >= 4 and parts[3]:
        try:
            stamp = int(parsedate_to_datetime(parts[3].replace('+', ' ')).timestamp())
            if plausible(stamp):
                return stamp
        except (TypeError, ValueError, OverflowError):
            pass

    try:
        ts = int(parts[1])
    except (IndexError, ValueError):
        return 0
    if not plausible(ts):
        return 0
    try:
        ttl = int(parts[2])
    except (IndexError, ValueError):
        ttl = 0
    now = time.time()
    if ttl > 0 and ts <= now < ts + ttl:
        # 抖音口径：第二段是登录时间，加有效期才是过期时刻
        return ts + ttl
    return ts


def _get_json(url: str, cookie: str, referer: str = '') -> dict:
    headers = {
        'User-Agent': UA,
        'Accept': 'application/json, text/plain, */*',
        'Cookie': cookie,
    }
    if referer:
        headers['Referer'] = referer
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=CHECK_TIMEOUT) as resp:
            raw = resp.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as exc:
        # 风控时接口会用 412 之类的状态码带回 JSON，尽量把内容读出来再判断
        raw = exc.read().decode('utf-8', 'replace')
        if not raw.strip():
            raise
    return json.loads(raw)


def _check_douyin(row: dict, base: dict) -> dict:
    cookie = row['cookie']
    expiry = douyin_expiry(cookie)
    now = time.time()
    if expiry:
        stamp = _fmt_ts(expiry)
        left = expiry - now
        if left <= 0:
            return dict(base, status='invalid', detail=f'登录凭证（sid_guard）已于 {stamp} 过期',
                        expires_at=expiry)
        if left <= EXPIRING_DAYS * 86400:
            days = max(1, -(-int(left) // 86400))  # 向上取整，别把 2.9 天说成 2 天
            return dict(base, status='expiring', detail=f'登录凭证将在 {days} 天后过期（{stamp}）',
                        expires_at=expiry)
        return dict(base, status='valid', detail=f'登录凭证有效期至 {stamp}', expires_at=expiry)

    if not any(marker in cookie for marker in DOUYIN_MARKERS):
        return dict(base, status='invalid',
                    detail='Cookie 里找不到 ttwid / sessionid 等关键字段，可能没粘完整')
    return dict(base, status='unknown',
                detail='Cookie 未包含 sid_guard，无法判断有效期（关键字段已就位）')


def _check_bilibili(row: dict, base: dict) -> dict:
    try:
        data = _get_json(BILI_NAV, row['cookie'], BILI_REFERER)
    except Exception as exc:  # 网络/解析异常都不能算失效
        return dict(base, status='unknown', network=True,
                    detail=f'检测请求失败（网络异常），本次不做结论：{exc}')

    code = data.get('code')
    inner = data.get('data') or {}
    if code == 0 and inner.get('isLogin'):
        who = inner.get('uname') or inner.get('mid') or ''
        return dict(base, status='valid', detail=f'登录态正常{f"：{who}" if who else ""}')
    if code == -101:
        if 'SESSDATA' not in row['cookie']:
            return dict(base, status='invalid',
                        detail='Cookie 里没有 SESSDATA（登录态字段），B 站会按未登录处理')
        return dict(base, status='invalid',
                    detail='B 站返回「账号未登录」，SESSDATA 已失效，需要重新登录获取 Cookie')
    if code in (-352, -400, -412, -509):
        return dict(base, status='unknown',
                    detail=f'B 站接口拒绝访问（code={code}），可能被风控，稍后重试')
    message = data.get('message') or ''
    return dict(base, status='unknown', detail=f'B 站返回 code={code} {message}'.strip())


def _check(row: dict) -> dict:
    base = {
        'platform': row['platform'],
        'key': row['key'],
        'checked_at': int(time.time()),
        'expires_at': 0,
        'notified_at': 0,
    }
    if not row['cookie']:
        hint = '（抖音录制必填）' if row['required'] else ''
        return dict(base, status='missing', detail=f'未配置{hint}')
    if row['kind'] == 'douyin':
        return _check_douyin(row, base)
    if row['kind'] == 'bilibili':
        return _check_bilibili(row, base)
    return dict(base, status='unknown', detail='已配置；该平台暂不支持自动校验')


# ---------------------------------------------------------------- 推送
def build_message(items, template: str, now: float):
    title = 'Cookie 失效提醒'
    stamp = _fmt_ts(now)
    detail = '\n'.join(f"{it['platform']}：{it['detail']}" for it in items)
    tpl = (template or '').strip()
    if tpl:
        return title, tpl.replace('[明细]', detail).replace('[时间]', stamp)
    content = (f'以下平台的 Cookie 需要处理：\n{detail}\n\n'
               f'请到 WebUI → 全局设置 → Cookie 凭证 里更新后保存。\n时间：{stamp}')
    return title, content


def _send(title: str, content: str) -> list:
    """按 [推送配置] 里勾选的渠道发送，返回成功的渠道名。"""
    push = read_ini().get('推送配置', {})
    channels = (push.get('直播状态推送渠道') or '').upper()
    if not channels.strip():
        return []

    if BASE_DIR not in sys.path:
        sys.path.insert(0, BASE_DIR)
    try:
        from msg_push import bark, dingtalk, ntfy, pushplus, send_email, tg_bot, xizhi
    except Exception as exc:
        print(f'[cookie] 推送模块不可用: {exc}', flush=True)
        return []

    def val(key, default=''):
        return push.get(key) or default

    senders = {
        '微信': lambda: xizhi(val('微信推送接口链接'), title, content),
        '钉钉': lambda: dingtalk(val('钉钉推送接口链接'), content,
                               val('钉钉通知@对象(填手机号)'),
                               _bool(push.get('钉钉通知@全体(是/否)'), False)),
        '邮箱': lambda: send_email(
            val('smtp邮件服务器'), val('邮箱登录账号'), val('发件人密码(授权码)'),
            val('发件人邮箱'), val('发件人显示昵称'), val('收件人邮箱'),
            title, content, val('SMTP邮件服务器端口'),
            _bool(push.get('是否使用SMTP服务SSL加密(是/否)'), True)),
        'TG': lambda: tg_bot(val('tg聊天id(个人或者群组id)'), val('tgapi令牌'), content),
        'BARK': lambda: bark(val('bark推送接口链接'), title=title, content=content,
                             level=val('bark推送中断级别', 'active'),
                             sound=val('bark推送铃声', '')),
        'NTFY': lambda: ntfy(val('ntfy推送地址'), title=title, content=content,
                             tags=val('ntfy推送标签', 'tada'), email=val('ntfy推送邮箱')),
        'PUSHPLUS': lambda: pushplus(val('pushplus推送token'), title, content),
    }

    sent = []
    for name, sender in senders.items():
        if name not in channels:
            continue
        try:
            result = sender() or {}
            if result.get('success'):
                sent.append(name)
            else:
                print(f'[cookie] 推送至{name}失败', flush=True)
        except Exception as exc:
            print(f'[cookie] 推送至{name}异常: {exc}', flush=True)
    return sent


# ---------------------------------------------------------------- 主流程
def run_check(notify: bool = True) -> dict:
    """检查全部凭证，必要时推送，并把结论写进状态文件。"""
    with _LOCK:
        cfg = load_config()
        prev = _load_state().get('items') or {}
        now = time.time()

        results = []
        for row in _rows():
            item = _check(row)
            old = prev.get(item['platform']) or {}
            old_status = old.get('status')

            # 网络抖一下不该把「已失效」洗白，也不该让「已失效」重新提醒一遍
            if (item['status'] == 'unknown' and item.get('network')
                    and old_status in ('invalid', 'expiring')):
                item.update(status=old_status,
                            detail='最近一次检测失败（网络异常），沿用上次结论：'
                                   + (old.get('detail') or ''),
                            expires_at=old.get('expires_at', 0))
                old_status = item['status']

            notified = _int(old.get('notified_at'), 0)
            if item['status'] in ('valid', 'missing'):
                notified = 0                     # 恢复正常，下次失效要重新提醒
            elif item['status'] == 'unknown' and old_status not in ('invalid', 'expiring'):
                notified = 0
            item['notified_at'] = notified
            results.append(item)

        due = []
        for item in results:
            if item['status'] not in ('invalid', 'expiring'):
                continue
            if not item['notified_at']:
                due.append(item)
            elif cfg['repeat_hours'] > 0 and now - item['notified_at'] >= cfg['repeat_hours'] * 3600:
                due.append(item)

        sent_channels = []
        if notify and due and cfg['enabled'] and cfg['channels'].strip():
            title, content = build_message(due, cfg['template'], now)
            sent_channels = _send(title, content)
            if sent_channels:
                for item in due:
                    item['notified_at'] = int(now)

        _save_state({'checked_at': int(now),
                     'items': {it['platform']: it for it in results}})

        return {
            'ok': True,
            'enabled': cfg['enabled'],
            'interval_hours': cfg['interval_hours'],
            'checked_at': int(now),
            'items': _sorted(results),
            'alarming': [it['platform'] for it in results
                         if it['status'] in ('invalid', 'expiring')],
            'pushed': bool(sent_channels),
            'channels': sent_channels,
            'notified': [it['platform'] for it in due] if sent_channels else [],
        }


def _sorted(items) -> list:
    order = {name: idx for idx, (name, *_rest) in enumerate(PLATFORMS)}
    return sorted(items, key=lambda it: (order.get(it.get('platform'), 99),
                                         STATUS_RANK.get(it.get('status'), 9),
                                         it.get('platform', '')))


def snapshot() -> dict:
    """给界面用：不联网，只读上次结论与当前配置。"""
    cfg = load_config()
    state = _load_state()
    # 用户改了 [Cookie] 之后，状态文件里的旧结论不该继续显示：
    # 平台被删掉就不再出现，Cookie 被清空则直接显示「未配置」
    has_cookie = {row['platform']: bool(row['cookie']) for row in _rows()}
    items = []
    for item in (state.get('items') or {}).values():
        platform = item.get('platform')
        if platform not in has_cookie:
            continue
        if not has_cookie[platform] and item.get('status') != 'missing':
            item = dict(item, status='missing', detail='未配置', expires_at=0)
        items.append(item)
    items = _sorted(items)
    checked_at = _int(state.get('checked_at'), 0)
    return {
        'enabled': cfg['enabled'],
        'interval_hours': cfg['interval_hours'],
        'repeat_hours': cfg['repeat_hours'],
        'expiring_days': EXPIRING_DAYS,
        'channels': [c.strip() for c in re.split(r'[,，]', cfg['channels']) if c.strip()],
        'checked_at': checked_at,
        'next_check_at': checked_at + cfg['interval_hours'] * 3600 if checked_at else 0,
        'items': items,
        'alarming': [it['platform'] for it in items
                     if it.get('status') in ('invalid', 'expiring')],
    }


# ---------------------------------------------------------------- 后台定时
def _watch_loop() -> None:
    time.sleep(WATCH_FIRST_DELAY)
    while True:
        try:
            cfg = load_config()
            if cfg['enabled']:
                last = _int(_load_state().get('checked_at'), 0)
                if not last or time.time() - last >= cfg['interval_hours'] * 3600:
                    run_check(notify=True)
        except Exception as exc:  # 后台线程绝不能死
            print(f'[cookie] 定时检查异常: {exc}', flush=True)
        time.sleep(WATCH_TICK)


def start_background() -> None:
    global _WATCHER
    with _START_LOCK:
        if _WATCHER is not None and _WATCHER.is_alive():
            return
        _WATCHER = threading.Thread(target=_watch_loop, name='cookie-health', daemon=True)
        _WATCHER.start()
