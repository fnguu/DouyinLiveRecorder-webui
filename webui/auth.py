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
            'updated_at': 0, 'sessions': {}}


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


# ------------------------------------------------------------------ 初始化 / 登录
def setup(user: str, password: str) -> None:
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
        _save(d)


def login(user: str, password: str, user_agent: str = '', days: int = SESSION_DAYS) -> str:
    """校验用户名密码，成功返回新会话 Token。"""
    with _LOCK:
        d = _load()
    if not (d.get('username') and d.get('password')):
        raise ValueError('尚未设置用户名密码')
    ok_user = secrets.compare_digest((d.get('username') or '').encode(), (user or '').strip().encode())
    ok_pwd = _check_password(password or '', d.get('password') or '')
    if not (ok_user and ok_pwd):
        raise ValueError('用户名或密码不正确')
    return create_session(user_agent, days)


def change_password(old: str, new: str, keep_token: str = '') -> None:
    with _LOCK:
        d = _load()
        if not _check_password(old or '', d.get('password') or ''):
            raise ValueError('原密码不正确')
        pwd = _check_new_password(new)
        keep = _tid(keep_token) if keep_token else ''
        d['password'] = _hash_password(pwd)
        d['updated_at'] = int(time.time())
        # 改密码后其它设备全部下线，只保留当前这个会话
        d['sessions'] = {k: v for k, v in (d.get('sessions') or {}).items() if k == keep}
        _save(d)


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


def create_session(user_agent: str = '', days: int = SESSION_DAYS) -> str:
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
