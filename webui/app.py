# -*- encoding: utf-8 -*-
"""DouyinLiveRecorder WebUI 后端（FastAPI）。"""
import base64
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import anchors as anchors_mod
from . import auth
from . import cookie_health
from . import version as webui_version
from . import media
from .config_io import read_ini
from .migrate import repair_case_keys
from .paths import BASE_DIR, CONFIG_FILE, STATIC_DIR, ensure_dirs, norm_url
from .runtime import (assets_for_names, disk_info, files_for_names, read_runtime,
                       recent_files, tail_log, thread_map)
from .schema import build_schema, save_settings

ensure_dirs()

# 修一次被 configparser.write() 降级过的键名（B站 -> b站 这种），
# 否则界面上会出现同一项显示两次
try:
    _fixed = repair_case_keys()
    if _fixed:
        logger_text = ', '.join(
            f"{a.get('section')}: {a.get('removed') or a.get('renamed')} -> {a.get('kept') or a.get('to')}"
            for a in _fixed)
        print(f'[webui] 已修复配置文件中的键名大小写重复: {logger_text}', flush=True)
except Exception as _exc:  # pragma: no cover
    print(f'[webui] 配置文件键名修复跳过: {_exc}', flush=True)

app = FastAPI(title='DouyinLiveRecorder WebUI', docs_url='/api/docs', openapi_url='/api/openapi.json')

# 兼容旧的单密码模式：设置了该环境变量时，HTTP Basic 仍可直接访问 API（供脚本/旧集成使用），
# 日常浏览器访问走下面的用户名密码 + 长期会话。
WEBUI_PASSWORD = os.environ.get('WEBUI_PASSWORD', '').strip()

# 不需要登录就能访问的路径：页面本身、静态资源、健康检查、登录相关接口。
# 页面放行是为了让前端能渲染「首次设置 / 登录」遮罩层，真正的数据接口仍然受保护。
_PUBLIC_PATHS = {'/', '/index.html', '/favicon.ico', '/api/health'}
_PUBLIC_PREFIXES = ('/static/', '/api/auth/')


def _client_key(request: Request) -> str:
    """限流用的客户端标识，优先取反向代理传来的真实 IP。"""
    fwd = request.headers.get('x-forwarded-for', '')
    ip = fwd.split(',')[0].strip() if fwd else ''
    return ip or (request.client.host if request.client else 'unknown')


def _basic_ok(request: Request) -> bool:
    if not WEBUI_PASSWORD:
        return False
    header = request.headers.get('authorization', '')
    if not header.startswith('Basic '):
        return False
    try:
        decoded = base64.b64decode(header[6:]).decode('utf-8', 'ignore')
        _user, _, pwd = decoded.partition(':')
        return secrets.compare_digest(pwd, WEBUI_PASSWORD)
    except Exception:
        return False


def _cookie_token(request: Request) -> str:
    """会话 Token：优先请求头，其次 Cookie。

    某些场景（例如被 NAS 桌面以 iframe 嵌入、浏览器禁用第三方 Cookie）下 Cookie
    会整体失效，前端会同时把 Token 放在 ``X-DLR-Token`` 头里；显式请求头优先，
    避免浏览器里残留的旧 Cookie 盖掉前端刚拿到的 Token。
    """
    token = request.headers.get('x-dlr-token', '') or ''
    if token:
        return token
    header = request.headers.get('authorization', '')
    if header.lower().startswith('bearer '):
        return header[7:].strip()
    return request.cookies.get(auth.COOKIE_NAME, '') or ''


@app.middleware('http')
async def auth_guard(request: Request, call_next):
    path = request.url.path
    if path in _PUBLIC_PATHS or path.startswith(_PUBLIC_PREFIXES):
        return await call_next(request)
    if _basic_ok(request) or auth.verify_session(_cookie_token(request)):
        return await call_next(request)
    if path.startswith('/api/'):
        return JSONResponse({'detail': '需要登录', 'auth_required': True}, status_code=401)
    return await call_next(request)


def _with_session(payload: dict, token: str, remember: bool = True) -> JSONResponse:
    """返回 JSON 并下发会话 Cookie。

    remember=True：持久 Cookie，180 天内免登录（使用中自动续期）；
    remember=False：浏览器会话 Cookie，关掉浏览器即失效。

    响应体里同时带上 token，前端会存进 localStorage 作为 Cookie 失效时的兜底
    （例如页面被 iframe 嵌入导致浏览器不发送 Cookie）。
    """
    payload = dict(payload, token=token, remember=bool(remember))
    resp = JSONResponse(payload)
    kwargs = {'httponly': True, 'samesite': 'lax', 'path': '/', 'secure': auth.COOKIE_SECURE}
    if remember:
        kwargs['max_age'] = auth.SESSION_DAYS * 86400
    resp.set_cookie(auth.COOKIE_NAME, token, **kwargs)
    resp.headers['X-DLR-Token'] = token
    return resp


@app.get('/api/auth/status')
def auth_status(request: Request):
    info = auth.public_info()
    token = _cookie_token(request)
    logged_in = bool(token) and auth.verify_session(token)
    info.update({
        'logged_in': logged_in,
        'expires_at': auth.expires_at(token) if logged_in else 0,
        'password_env': bool(WEBUI_PASSWORD),
    })
    if logged_in:
        account = auth.account_info(token)
        info.update({
            'sessions': account['sessions'],
            'other_sessions': account['other_sessions'],
            'account_updated_at': account['updated_at'],
            'account_created_at': account['created_at'],
            'devices': account['devices'],
            'events': account['events'],
        })
    return info


@app.post('/api/auth/setup')
def auth_setup(request: Request, payload: Dict[str, Any] = Body(...)):
    """首次设置用户名密码，成功后直接登录。"""
    if auth.is_configured():
        raise HTTPException(status_code=409, detail='已设置过用户名密码，请直接登录')
    user = str(payload.get('username') or '')
    pwd = str(payload.get('password') or '')
    ip = _client_key(request)
    ua = request.headers.get('user-agent', '')
    try:
        auth.setup(user, pwd, ip=ip)
        token = auth.login(user, pwd, ua, ip=ip)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f'无法写入账号文件，请检查 config 目录权限: {exc}')
    return _with_session({'ok': True, 'username': auth.username()}, token)


@app.post('/api/auth/login')
def auth_login(request: Request, payload: Dict[str, Any] = Body(...)):
    key = _client_key(request)
    wait = auth.locked_for(key)
    if wait:
        raise HTTPException(status_code=429, detail=f'密码错误次数过多，请 {wait} 秒后再试')
    if not auth.is_configured():
        raise HTTPException(status_code=409, detail='尚未设置用户名密码，请先完成初始化')
    try:
        token = auth.login(str(payload.get('username') or ''), str(payload.get('password') or ''),
                           request.headers.get('user-agent', ''),
                           days=auth.SESSION_DAYS if payload.get('remember', True) else 1,
                           ip=key)
    except ValueError as exc:
        auth.note_failure(key)
        raise HTTPException(status_code=401, detail=str(exc))
    auth.clear_failures(key)
    return _with_session({'ok': True, 'username': auth.username()}, token,
                         remember=bool(payload.get('remember', True)))


@app.post('/api/auth/logout')
def auth_logout(request: Request):
    auth.logout(_cookie_token(request), ip=_client_key(request))
    resp = JSONResponse({'ok': True})
    resp.delete_cookie(auth.COOKIE_NAME, path='/')
    return resp


def _require_session(request: Request) -> str:
    """这些接口本身放行（公开前缀），所以自己校验一次会话。"""
    token = _cookie_token(request)
    if not auth.verify_session(token):
        raise HTTPException(status_code=401, detail='需要登录')
    return token


@app.post('/api/auth/password')
def auth_password(request: Request, payload: Dict[str, Any] = Body(...)):
    """修改密码：成功后其它设备的会话全部失效，当前会话保留。"""
    token = _require_session(request)
    try:
        auth.change_password(str(payload.get('old_password') or ''),
                             str(payload.get('new_password') or ''), keep_token=token,
                             ip=_client_key(request))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {'ok': True, 'username': auth.username()}


@app.post('/api/auth/account')
def auth_account(request: Request, payload: Dict[str, Any] = Body(...)):
    """修改用户名 / 密码（全局设置 → 登录账号）。当前密码用于确认身份。"""
    token = _require_session(request)
    key = _client_key(request)
    wait = auth.locked_for(key)
    if wait:
        raise HTTPException(status_code=429, detail=f'密码错误次数过多，请 {wait} 秒后再试')
    username = payload.get('username')
    try:
        result = auth.change_account(
            str(payload.get('current_password') or ''),
            username=str(username) if username is not None else None,
            new_password=str(payload.get('new_password') or ''),
            keep_token=token,
            ip=key,
        )
    except ValueError as exc:
        auth.note_failure(key)
        raise HTTPException(status_code=400, detail=str(exc))
    auth.clear_failures(key)
    account = auth.account_info(token)
    account.update({'ok': True, **result})
    return account


@app.post('/api/auth/logout-others')
def auth_logout_others(request: Request):
    """只保留当前设备，其它设备全部下线。"""
    token = _require_session(request)
    removed = auth.revoke_others(token, ip=_client_key(request))
    account = auth.account_info(token)
    account.update({'ok': True, 'removed': removed})
    return account


@app.post('/api/auth/reset')
def auth_reset(request: Request, payload: Dict[str, Any] = Body(...)):
    """清空用户名密码，回到首次设置流程。需要当前密码 + 手动输入确认词。"""
    token = _require_session(request)
    if str(payload.get('confirm') or '').strip() not in ('重置', 'reset', 'RESET', 'Reset'):
        raise HTTPException(status_code=400, detail='请输入「重置」确认操作')
    if not auth.verify_password(str(payload.get('current_password') or '')):
        raise HTTPException(status_code=400, detail='当前密码不正确')
    auth.reset_credentials(ip=_client_key(request))
    resp = JSONResponse({'ok': True, 'reset': True})
    resp.delete_cookie(auth.COOKIE_NAME, path='/')
    return resp



def _state_of(anchor: dict, tm: dict | None = None) -> dict:
    """把实时状态叠加到主播信息上。"""
    if tm is None:
        tm = thread_map()
    rt = read_runtime()
    item = dict(anchor)
    thread = tm.get(norm_url(anchor['url']))
    if not anchor.get('enabled'):
        item['state'] = 'disabled'
        item['state_text'] = '已停止'
    elif thread:
        status = thread.get('status', 'waiting')
        mapping = {
            'recording': ('recording', '录制中'),
            'live': ('live', '直播中'),
            'idle': ('idle', '等待开播'),
            'error': ('error', '获取失败'),
            'exited': ('idle', '已退出'),
            'waiting': ('idle', '排队中'),
        }
        item['state'], item['state_text'] = mapping.get(status, ('idle', '等待开播'))
        item['current_name'] = thread.get('name', '')
        item['last_file'] = thread.get('file', '')
        item['since'] = thread.get('started')
    elif not rt.get('alive'):
        item['state'], item['state_text'] = 'unknown', '录制进程未运行'
    else:
        item['state'], item['state_text'] = 'pending', '即将开始'
    if not anchor.get('name') and item.get('current_name'):
        item['display_name'] = item['current_name']
    else:
        tail = (anchor.get('url') or '').rstrip('/').split('/')[-1][:14]
        item['display_name'] = (anchor.get('name')
                                or (f"{anchor.get('platform')} · {tail}" if tail else anchor.get('platform'))
                                or '未命名')
    item['last_live_at'] = anchor.get('last_live_at') or ''
    return item


@app.get('/api/health')
def health():
    return {'ok': True}


@app.get('/api/summary')
def summary():
    rt = read_runtime()
    state = anchors_mod.sync_from_file()
    anchors = state['anchors']
    ini = read_ini()
    save_path = ini.get('录制设置', {}).get('直播保存路径(不填则默认)', '') or ''
    disk = disk_info(save_path)
    recording = len([t for t in rt.get('threads', []) if t.get('status') == 'recording'])
    return {
        'process': {
            'alive': rt.get('alive', False),
            'version': rt.get('version', ''),
            'pid': rt.get('pid', 0),
            'updated_at': rt.get('updated_at', ''),
            'monitoring': rt.get('monitoring', 0),
            'error_count': rt.get('error_count', 0),
            'recording': recording,
        },
        'counts': {
            'total': len(anchors),
            'enabled': len([a for a in anchors if a.get('enabled')]),
            'notify': len([a for a in anchors if a.get('notify')]),
        },
        'disk': disk,
        'save_path': save_path,
        'language': ini.get('录制设置', {}).get('language(zh_cn/en)', 'zh_cn'),
    }


_LIVE_TOUCH: Dict[str, float] = {}


def _touch_last_live(state: dict, tm: dict) -> None:
    """主播正在直播/录制时记录时间，节流 120 秒，避免频繁写盘。"""
    now = time.time()
    changed = False
    for a in state['anchors']:
        t = tm.get(norm_url(a['url']))
        if not t or t.get('status') not in ('live', 'recording'):
            continue
        if now - _LIVE_TOUCH.get(a['id'], 0) < 120:
            continue
        a['last_live_at'] = time.strftime('%Y-%m-%d %H:%M', time.localtime())
        _LIVE_TOUCH[a['id']] = now
        changed = True
    if changed:
        anchors_mod.save_state(state)


_LIVE_STATES = ('live', 'recording')


def _display_order(items: List[dict]) -> List[dict]:
    """主播列表的展示顺序：正在直播的最前，其余按最后直播时间由近到远，
    从未直播过的（没有最后直播时间）按添加时间由新到旧兜底。

    时间字段都是 ``YYYY-MM-DD HH:MM`` 这种可直接比较的字符串，空串按「最旧」处理，
    所以连做三次稳定排序就能表达三级优先级，不需要解析时间。只在返回给界面的
    副本上排，不动 state 与 URL_config.ini 里的顺序。
    """
    items.sort(key=lambda it: it.get('added_at') or '', reverse=True)
    items.sort(key=lambda it: it.get('last_live_at') or '', reverse=True)
    items.sort(key=lambda it: 0 if it.get('state') in _LIVE_STATES else 1)
    return items


@app.get('/api/anchors')
def list_anchors():
    state = anchors_mod.sync_from_file()
    tm = thread_map()
    _touch_last_live(state, tm)
    return {'items': _display_order([_state_of(a, tm) for a in state['anchors']])}


@app.get('/api/anchors/{anchor_id}/files')
def anchor_files(anchor_id: str):
    """某个主播名下的录像文件列表。"""
    state = anchors_mod.sync_from_file()
    anchor = next((a for a in state['anchors'] if a['id'] == anchor_id), None)
    if not anchor:
        raise HTTPException(status_code=404, detail='主播不存在')
    tm = thread_map()
    thread = tm.get(norm_url(anchor['url'])) or {}

    names = []
    if anchor.get('name'):
        names.append(anchor['name'])
    # 录制器实际使用抓取到的昵称（形如「序号1 小美」），去掉序号参与匹配
    current = re.sub(r'^序号\d+\s*', '', (thread.get('name') or '').strip())
    if current and current not in names:
        names.append(current)

    items = files_for_names(names)
    assets = assets_for_names(names)
    return {
        'anchor': _state_of(anchor, tm),
        'names': names,
        'items': items,
        'count': len(items),
        'total_size': sum(i['size'] for i in items),
        'assets': assets,
        'asset_count': len(assets),
        'remux': media.ffmpeg_available(),
    }


@app.get('/api/media')
def get_media(path: str = Query(..., description='相对 downloads 目录的文件路径')):
    """播放/下载录像。ts、mkv、flv 会先无损转封装成 mp4 再返回。"""
    try:
        full = media.resolve(path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not os.path.isfile(full):
        raise HTTPException(status_code=404, detail='文件不存在')
    if media.is_download_only(full):
        # 弹幕 / 字幕：不转封装，直接作为附件下载
        return FileResponse(full, media_type=media.media_type_for(full),
                            filename=os.path.basename(full))
    try:
        playable = full if media.can_play_directly(full) else media.remux_to_mp4(full)
    except RuntimeError as exc:
        raise HTTPException(status_code=415, detail=str(exc))
    return FileResponse(playable, media_type=media.media_type_for(playable))


@app.post('/api/anchors')
def create_anchor(payload: Dict[str, Any] = Body(...)):
    try:
        anchor = anchors_mod.add_anchor(
            url=payload.get('url', ''),
            name=payload.get('name', ''),
            quality=payload.get('quality', ''),
            enabled=bool(payload.get('enabled', True)),
            notify=bool(payload.get('notify', True)),
            note=payload.get('note', ''),
            danmaku=bool(payload.get('danmaku', True)),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {'ok': True, 'item': _state_of(anchor)}


@app.patch('/api/anchors/{anchor_id}')
def patch_anchor(anchor_id: str, payload: Dict[str, Any] = Body(...)):
    try:
        anchor = anchors_mod.update_anchor(anchor_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {'ok': True, 'item': _state_of(anchor)}


@app.delete('/api/anchors/{anchor_id}')
def remove_anchor(anchor_id: str):
    try:
        anchors_mod.delete_anchor(anchor_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {'ok': True}


@app.post('/api/anchors/batch')
def batch(payload: Dict[str, Any] = Body(...)):
    """批量粘贴多行地址添加。支持每行 "名称,地址" 或纯地址。"""
    text = str(payload.get('text', ''))
    enabled = bool(payload.get('enabled', True))
    notify = bool(payload.get('notify', True))
    danmaku = bool(payload.get('danmaku', True))
    added, failed = [], []
    for raw in text.replace('\r', '\n').split('\n'):
        line = raw.strip().lstrip('#').strip()
        if not line:
            continue
        if '://' not in line and '.' not in line:
            continue  # 纯文字说明行，跳过
        parts = [p.strip() for p in line.replace('，', ',').split(',')]
        url = next((p for p in parts if '://' in p or '.' in p and '/' in p), parts[-1])
        name = ''
        for p in parts:
            if p != url and p:
                name = p
                break
        try:
            added.append(anchors_mod.add_anchor(url, name=name, enabled=enabled, notify=notify, danmaku=danmaku))
        except ValueError as exc:
            failed.append({'line': raw.strip(), 'error': str(exc)})
    return {'ok': True, 'added': len(added), 'failed': failed}


@app.get('/api/settings')
def get_settings():
    return build_schema()


@app.post('/api/settings')
def post_settings(payload: Dict[str, Any] = Body(...)):
    items: List[dict] = payload.get('items') or []
    if not isinstance(items, list):
        raise HTTPException(status_code=400, detail='items 必须是数组')
    result = save_settings(items)
    result['schema'] = build_schema()
    return result


_VER_CACHE: Dict[str, str] = {}


def _tool_version(name: str, args: List[str]) -> str:
    """取 ffmpeg / node 之类的版本号，结果缓存，避免每次请求都起子进程。"""
    if name in _VER_CACHE:
        return _VER_CACHE[name]
    version = ''
    exe = shutil.which(name)
    if exe:
        try:
            out = subprocess.run([exe] + args, capture_output=True, text=True, timeout=5)
            lines = (out.stdout or out.stderr).strip().splitlines()
            if lines:
                version = lines[0].strip()[:80]
        except Exception:
            version = ''
    _VER_CACHE[name] = version
    return version


def _build_info() -> dict:
    path = os.path.join(BASE_DIR, 'build_info.json')
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _human_uptime(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    if d:
        return f'{d} 天 {h} 小时'
    if h:
        return f'{h} 小时 {m} 分'
    return f'{m} 分'


@app.get('/api/system')
def system_info():
    """系统状态页用：版本、构建信息、运行环境、进程与磁盘。"""
    rt = read_runtime()
    ini = read_ini()
    save_path = ini.get('录制设置', {}).get('直播保存路径(不填则默认)', '') or ''
    build = _build_info()

    started = float(rt.get('started_at') or 0)
    uptime = time.time() - started if started else 0
    threads = rt.get('threads', [])
    recording = len([t for t in threads if t.get('status') == 'recording'])

    wv = webui_version.info()
    return {
        'webui_version': wv['version'],
        'webui_revision': wv['revision'],
        'webui_history': wv['history'],
        'app_version': rt.get('version', '') or '未运行',
        'build': {
            'time': build.get('build_time', ''),
            'sha': build.get('git_sha', ''),
        },
        'process': {
            'alive': rt.get('alive', False),
            'pid': rt.get('pid', 0),
            'updated_at': rt.get('updated_at', ''),
            'monitoring': rt.get('monitoring', 0),
            'recording': recording,
            'error_count': rt.get('error_count', 0),
            'uptime': _human_uptime(uptime) if started else '',
            'uptime_seconds': int(uptime) if started else 0,
        },
        'env': {
            'python': platform.python_version(),
            'platform': platform.platform(terse=True),
            'machine': platform.machine(),
            'hostname': platform.node(),
            'cpu': os.cpu_count() or 0,
            'tz': time.tzname[0],
            'ffmpeg': _tool_version('ffmpeg', ['-version']),
            'node': _tool_version('node', ['-v']),
        },
        'disk': disk_info(save_path),
        'save_path': save_path,
    }


@app.get('/api/cookie/status')
def cookie_status():
    """Cookie 状态卡片用：只读上次结论与当前配置，不联网。"""
    return cookie_health.snapshot()


@app.post('/api/cookie/check')
def cookie_check():
    """立即检测一次（会按需推送提醒）。"""
    try:
        return cookie_health.run_check(notify=True)
    except Exception as exc:  # 单次检测异常不该把接口打挂
        raise HTTPException(status_code=500, detail=f'Cookie 检测失败：{exc}')


@app.get('/api/logs')
def get_logs(name: str = Query('console'), lines: int = Query(300)):
    return tail_log(name, lines)


@app.get('/api/files')
def get_files():
    rt = read_runtime()
    return {'items': recent_files(10), 'recording': [t for t in rt.get('threads', [])
                                                     if t.get('status') == 'recording']}


@app.get('/api/config/raw')
def raw_config():
    if not os.path.exists(CONFIG_FILE):
        raise HTTPException(status_code=404, detail='配置文件不存在')
    with open(CONFIG_FILE, 'r', encoding='utf-8-sig', errors='replace') as f:
        return {'text': f.read()}


@app.post('/api/anchor/test')
def test_anchor(payload: Optional[Dict[str, Any]] = Body(None)):
    """检查地址是否被支持（仅做平台识别，不做网络请求）。"""
    from .paths import detect_platform
    url = (payload or {}).get('url', '').strip()
    if not url:
        raise HTTPException(status_code=400, detail='地址为空')
    platform = detect_platform(url)
    return {'platform': platform, 'supported': platform != '其他'}


# Cookie 失效检查跑在 WebUI 进程里：只要容器活着就会按「Cookie 检查间隔」自己跑，
# 不依赖是否有人打开页面。
try:
    cookie_health.start_background()
except Exception as _exc:  # pragma: no cover
    print(f'[webui] Cookie 定时检查未启动: {_exc}', flush=True)

app.mount('/static', StaticFiles(directory=STATIC_DIR), name='static')


@app.get('/')
def index():
    return FileResponse(os.path.join(STATIC_DIR, 'index.html'))
