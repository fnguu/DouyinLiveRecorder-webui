# -*- encoding: utf-8 -*-
"""DouyinLiveRecorder WebUI 后端（FastAPI）。"""
import base64
import os
import secrets
from typing import Any, Dict, List, Optional

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import anchors as anchors_mod
from .config_io import read_ini
from .paths import CONFIG_FILE, STATIC_DIR, ensure_dirs, norm_url
from .runtime import disk_info, read_runtime, recent_files, tail_log, thread_map
from .schema import build_schema, save_settings

ensure_dirs()

app = FastAPI(title='DouyinLiveRecorder WebUI', docs_url='/api/docs', openapi_url='/api/openapi.json')

WEBUI_PASSWORD = os.environ.get('WEBUI_PASSWORD', '').strip()


@app.middleware('http')
async def basic_auth(request: Request, call_next):
    if not WEBUI_PASSWORD or request.url.path.startswith('/api/health'):
        return await call_next(request)
    header = request.headers.get('authorization', '')
    if header.startswith('Basic '):
        try:
            decoded = base64.b64decode(header[6:]).decode('utf-8', 'ignore')
            _user, _, pwd = decoded.partition(':')
            if secrets.compare_digest(pwd, WEBUI_PASSWORD):
                return await call_next(request)
        except Exception:
            pass
    return JSONResponse({'detail': '需要登录'}, status_code=401,
                        headers={'WWW-Authenticate': 'Basic realm="DLR WebUI"'})


def _state_of(anchor: dict) -> dict:
    """把实时状态叠加到主播信息上。"""
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


@app.get('/api/anchors')
def list_anchors():
    return {'items': [_state_of(a) for a in anchors_mod.list_anchors()]}


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
            added.append(anchors_mod.add_anchor(url, name=name, enabled=enabled, notify=notify))
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


app.mount('/static', StaticFiles(directory=STATIC_DIR), name='static')


@app.get('/')
def index():
    return FileResponse(os.path.join(STATIC_DIR, 'index.html'))
