# -*- encoding: utf-8 -*-
"""WebUI 登录认证。

- 首次访问：没有凭据文件时由前端展示「设置用户名密码」页面，调用 ``setup()`` 初始化。
- 之后登录：密码用 PBKDF2-HMAC-SHA256 加盐存储；登录成功下发一个随机 Token
  （只把它的 SHA-256 存盘），Cookie 长期有效并随使用自动续期，从而「登录一次长期保持」。
- 会话可撤销：退出登录、修改密码都会让对应 Token 立即失效。

凭据文件 ``config/webui_auth.json`` 与 config.ini 同目录，随容器挂载持久化。
"""
import hashlib
import json
import os
import secrets
import threading
import time
from typing import Dict, Optional

from .paths import CONFIG_DIR

AUTH_FILE = os.environ.get('DLR_AUTH_FILE', os.path.join(CONFIG_DIR, 'webui_auth.json'))
COOKIE_NAME = 'dlr_session'

# 「长期保持登录」：Cookie 有效期（天）。使用中会自动续期，等于一直有效，
# 真正长期不访问才会过期。
SESSION_DAYS = 180
# 每次验证会话时，把到期时间往后推到这个值（最多每 6 小时写一次盘）
_SLIDE_THRESHOLD = 6 * 3600

PBKDF2_ROUNDS = 200_000
PBKDF2_ALG = 'pbkdf2_sha256'
MIN_PASSWORD = 6
MAX_PASSWORD = 128
MAX_USERNAME = 32
MAX_SESSIONS = 20

# Cookie 是否加 Secure（走 HTTPS 时可由环境变量打开）
COOKIE_SECURE = os.environ.get('DLR_COOKIE_SECURE', '').strip() in ('1', 'true', 'yes', 'on')

_LOCK = threading.RLock()
_DATA: Optional[dict] = None

# 登录失败限流（内存，重启即清零）
_FAIL_WINDOW = 600
_FAIL_LIMIT = 10
_FAILS: Dict[str, list] = {}


# ------------------------------------------------------------------ 存取
def _blank() -> dict:
    return {'version': 1, 'username': '', 'password': '', 'created_at': 0,
            'updated_at': 0, 'sessions': {}, 'events': []}


def _load() -> dict:
    global _DATA
    if _DATA is not None:
        return _DATA
    data = _blank()
    if os.path.exists(AUTH_FILE):
        try:
            with open(AUTH_FILE, 'r', encoding='utf-8') as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                data.update({k: raw.get(k, data[k]) for k in data})
                if not isinstance(data.get('sessions'), dict):
                    data['sessions'] = {}
                if not isinstance(data.get('events'), list):
                    data['events'] = []
        except (OSError, ValueError) as exc:
            # 文件损坏：当成未初始化，让用户重新设置，而不是被永久锁在门外
            print(f'[webui] 登录凭据文件无法读取，将重新初始化: {exc}', flush=True)
            data = _blank()
    _DATA = data
    return data


def _save(data: dict) -> None:
    global _DATA
    _DATA = data
    os.makedirs(os.path.dirname(AUTH_FILE) or '.', exist_ok=True)
    tmp = f'{AUTH_FILE}.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, AUTH_FILE)


# ------------------------------------------------------------------ 密码
def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, PBKDF2_ROUNDS)
    return f'{PBKDF2_ALG}${PBKDF2_ROUNDS}${salt.hex()}${dk.hex()}'


def _check_password(password: str, stored: str) -> bool:
    try:
        algo, rounds, salt_hex, dk_hex = stored.split('$')
        if algo != PBKDF2_ALG:
            return False
        dk = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'),
                                 bytes.fromhex(salt_hex), int(rounds))
        return secrets.compare_digest(dk.hex(), dk_hex)
    except (ValueError, AttributeError):
        return False


def _clean_username(username: str) -> str:
    name = (username or '').strip()
    if not name:
        raise ValueError('用户名不能为空')
    if len(name) > MAX_USERNAME:
        raise ValueError(f'用户名不能超过 {MAX_USERNAME} 个字符')
    if any(ch in name for ch in '\r\n\t'):
        raise ValueError('用户名不能包含换行或制表符')
    return name


def _check_new_password(password: str) -> str:
    if not isinstance(password, str) or len(password) < MIN_PASSWORD:
        raise ValueError(f'密码至少 {MIN_PASSWORD} 位')
    if len(password) > MAX_PASSWORD:
        raise ValueError(f'密码不能超过 {MAX_PASSWORD} 位')
    return password


# ------------------------------------------------------------------ 状态
def is_configured() -> bool:
    with _LOCK:
        d = _load()
    return bool(d.get('username') and d.get('password'))


def username() -> str:
    with _LOCK:
        return _load().get('username') or ''


def public_info() -> dict:
    with _LOCK:
        d = _load()
    return {
        'configured': bool(d.get('username') and d.get('password')),
        'username': d.get('username') or '',
        'session_days': SESSION_DAYS,
        'created_at': d.get('created_at') or 0,
    }


# ------------------------------------------------------------------ 操作日志
EVENT_LIMIT = 200        # 文件里最多保留多少条
EVENT_RETURN = 40        # 接口一次返回多少条


def _note(d: dict, kind: str, text: str, ip: str = '') -> None:
    """记录一条账号操作日志（时间 + 事件 + IP），供「全局设置 → 登录账号」展示。"""
    events = d.get('events')
    if not isinstance(events, list):
        events = []
    events.append({'t': int(time.time()), 'kind': kind, 'text': text, 'ip': ip or ''})
    d['events'] = events[-EVENT_LIMIT:]


def _events(d: dict) -> list:
    items = d.get('events')
    if not isinstance(items, list):
        return []
    return list(reversed(items[-EVENT_RETURN:]))


# ------------------------------------------------------------------ 初始化 / 登录
def setup(user: str, password: str, ip: str = '') -> None:
    """首次设置用户名密码。已初始化则抛 ValueError（防止被覆盖）。"""
    with _LOCK:
        if is_configured():
            raise ValueError('已设置过用户名密码，请直接登录')
        name = _clean_username(user)
        pwd = _check_new_password(password)
        d = _load()
        now = int(time.time())
        d.update({'version': 1, 'username': name, 'password': _hash_password(pwd),
                  'created_at': now, 'updated_at': now, 'sessions': {}})
        _note(d, 'setup', f'创建账号 {name}', ip)
        _save(d)


def login(user: str, password: str, user_agent: str = '', days: int = SESSION_DAYS,
          ip: str = '') -> str:
    """校验用户名密码，成功返回新会话 Token。"""
    with _LOCK:
        d = _load()
    if not (d.get('username') and d.get('password')):
        raise ValueError('尚未设置用户名密码')
    ok_user = secrets.compare_digest((d.get('username') or '').encode(), (user or '').strip().encode())
    ok_pwd = _check_password(password or '', d.get('password') or '')
    if not (ok_user and ok_pwd):
        raise ValueError('用户名或密码不正确')
    token = create_session(user_agent, days, ip=ip)
    with _LOCK:
        d = _load()
        _note(d, 'login', f'登录成功（{(user_agent or "未知设备")[:60]}）', ip)
        _save(d)
    return token


def change_password(old: str, new: str, keep_token: str = '', ip: str = '') -> None:
    change_account(current_password=old, new_password=new, keep_token=keep_token, ip=ip)


def verify_password(password: str) -> bool:
    with _LOCK:
        stored = _load().get('password') or ''
    return _check_password(password or '', stored)


def change_account(current_password: str, username: Optional[str] = None,
                   new_password: Optional[str] = None, keep_token: str = '',
                   ip: str = '') -> dict:
    """修改用户名和/或密码。

    - 必须提供当前密码用于确认身份；
    - ``username`` 为空表示不改用户名，``new_password`` 为空表示不改密码；
    - 改密码后其它设备的会话全部失效，只保留 ``keep_token`` 对应的当前会话。
    """
    with _LOCK:
        d = _load()
        if not d.get('password'):
            raise ValueError('尚未设置用户名密码')
        if not _check_password(current_password or '', d.get('password') or ''):
            raise ValueError('当前密码不正确')
        name = d.get('username') or ''
        target_name = name
        if username is not None and str(username).strip():
            target_name = _clean_username(username)
        new_pwd = _check_new_password(new_password) if new_password else ''
        name_changed = target_name != name
        if not name_changed and not new_pwd:
            raise ValueError('用户名和新密码都没有变化')
        if name_changed:
            d['username'] = target_name
        if new_pwd:
            d['password'] = _hash_password(new_pwd)
            keep = _tid(keep_token) if keep_token else ''
            d['sessions'] = {k: v for k, v in (d.get('sessions') or {}).items() if k == keep}
        d['updated_at'] = int(time.time())
        if name_changed and new_pwd:
            _note(d, 'account', f'修改用户名（{name} → {target_name}）与密码', ip)
        elif name_changed:
            _note(d, 'account', f'修改用户名（{name} → {target_name}）', ip)
        else:
            _note(d, 'account', '修改登录密码', ip)
        _save(d)
    return {'username': target_name, 'name_changed': name_changed,
            'password_changed': bool(new_pwd)}


def account_info(token: str = '') -> dict:
    """账号、登录设备与操作日志（不含任何哈希）。"""
    now = int(time.time())
    tid = _tid(token) if token else ''
    with _LOCK:
        d = _load()
        sessions = d.get('sessions') or {}
        _prune(d, now)
        sessions = d.get('sessions') or {}
        devices = []
        for key, sess in sorted(sessions.items(),
                                key=lambda kv: int(kv[1].get('last_seen') or 0), reverse=True):
            devices.append({
                'id': key[:10],
                'current': key == tid,
                'created': int(sess.get('created') or 0),
                'last_seen': int(sess.get('last_seen') or 0),
                'expires': int(sess.get('expires') or 0),
                'days': int(sess.get('days') or SESSION_DAYS),
                'ua': sess.get('ua') or '',
                'ip': sess.get('ip') or '',
            })
        current = sessions.get(tid) if tid else None
        return {
            'username': d.get('username') or '',
            'created_at': int(d.get('created_at') or 0),
            'updated_at': int(d.get('updated_at') or 0),
            'sessions': len(sessions),
            'other_sessions': len([k for k in sessions if k != tid]),
            'last_seen': max([int(v.get('last_seen') or 0) for v in sessions.values()] or [0]),
            'current_expires': int((current or {}).get('expires') or 0),
            'devices': devices,
            'events': _events(d),
        }


def revoke_others(keep_token: str, ip: str = '') -> int:
    """退出除当前设备外的所有会话，返回被清掉的会话数。"""
    keep = _tid(keep_token) if keep_token else ''
    with _LOCK:
        d = _load()
        sessions = d.get('sessions') or {}
        dead = [k for k in sessions if k != keep]
        if dead:
            d['sessions'] = {k: v for k, v in sessions.items() if k == keep}
            _note(d, 'device', f'退出其它 {len(dead)} 处登录', ip)
            _save(d)
    return len(dead)


def reset_credentials(ip: str = '') -> None:
    """清空用户名密码（回到首次设置流程），所有会话同时失效。

    操作日志会保留，方便重置后仍能看到之前的账号使用记录。
    """
    with _LOCK:
        old = _load()
        now = int(time.time())
        blank = _blank()
        blank['created_at'] = now
        blank['updated_at'] = now
        blank['events'] = list(old.get('events') or [])
        _note(blank, 'reset', '重置登录凭据（清空用户名与密码）', ip)
        _save(blank)


# ------------------------------------------------------------------ 会话
def _tid(token: str) -> str:
    return hashlib.sha256((token or '').encode('utf-8')).hexdigest()


def _prune(d: dict, now: int) -> bool:
    sessions: dict = d.get('sessions') or {}
    dead = [k for k, v in sessions.items() if int(v.get('expires') or 0) <= now]
    for k in dead:
        sessions.pop(k, None)
    if len(sessions) > MAX_SESSIONS:
        for k, _ in sorted(sessions.items(), key=lambda kv: kv[1].get('created') or 0)[:-MAX_SESSIONS]:
            sessions.pop(k, None)
    d['sessions'] = sessions
    return bool(dead)


def create_session(user_agent: str = '', days: int = SESSION_DAYS, ip: str = '') -> str:
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    days = max(1, int(days or SESSION_DAYS))
    with _LOCK:
        d = _load()
        _prune(d, now)
        d.setdefault('sessions', {})[_tid(token)] = {
            'created': now,
            'last_seen': now,
            'expires': now + days * 86400,
            'days': days,
            'ua': (user_agent or '')[:160],
            'ip': (ip or '')[:46],
        }
        _save(d)
    return token


def verify_session(token: str) -> bool:
    """Token 是否有效；有效则顺延到期时间（滑动续期）。"""
    if not token:
        return False
    now = int(time.time())
    tid = _tid(token)
    with _LOCK:
        d = _load()
        sess = (d.get('sessions') or {}).get(tid)
        if not sess:
            return False
        if int(sess.get('expires') or 0) <= now:
            d['sessions'].pop(tid, None)
            _save(d)
            return False
        # 续期：剩余不足完整周期才写盘，避免每个请求都落盘
        days = max(1, int(sess.get('days') or SESSION_DAYS))
        if now - int(sess.get('last_seen') or 0) > 600:
            sess['last_seen'] = now
            if int(sess.get('expires') or 0) - now < days * 86400 - _SLIDE_THRESHOLD:
                sess['expires'] = now + days * 86400
                d['sessions'][tid] = sess
                _save(d)
        return True


def expires_at(token: str) -> int:
    with _LOCK:
        sess = (_load().get('sessions') or {}).get(_tid(token)) or {}
    return int(sess.get('expires') or 0)


def revoke(token: str) -> None:
    if not token:
        return
    with _LOCK:
        d = _load()
        if (d.get('sessions') or {}).pop(_tid(token), None) is not None:
            _save(d)


def logout(token: str, ip: str = '') -> None:
    """退出登录：撤销当前会话并记录一条日志。"""
    if not token:
        return
    with _LOCK:
        d = _load()
        if (d.get('sessions') or {}).pop(_tid(token), None) is not None:
            _note(d, 'logout', '退出登录', ip)
            _save(d)


def revoke_all() -> None:
    with _LOCK:
        d = _load()
        d['sessions'] = {}
        _save(d)


# ------------------------------------------------------------------ 失败限流
def note_failure(key: str) -> None:
    now = time.time()
    with _LOCK:
        items = [t for t in _FAILS.get(key, []) if now - t < _FAIL_WINDOW]
        items.append(now)
        _FAILS[key] = items


def clear_failures(key: str) -> None:
    with _LOCK:
        _FAILS.pop(key, None)


def locked_for(key: str) -> int:
    """还有多少秒解锁；0 表示未锁定。"""
    now = time.time()
    with _LOCK:
        items = [t for t in _FAILS.get(key, []) if now - t < _FAIL_WINDOW]
        _FAILS[key] = items
        if len(items) < _FAIL_LIMIT:
            return 0
        return int(_FAIL_WINDOW - (now - items[-_FAIL_LIMIT]))
