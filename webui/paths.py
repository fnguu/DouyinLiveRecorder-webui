# -*- encoding: utf-8 -*-
"""统一的路径与小工具。WebUI 与录制器共享同一份配置目录。"""
import os
import re

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(BASE_DIR, 'config')
CONFIG_FILE = os.path.join(CONFIG_DIR, 'config.ini')
URL_CONFIG_FILE = os.path.join(CONFIG_DIR, 'URL_config.ini')
STATE_FILE = os.path.join(CONFIG_DIR, 'webui_state.json')
BACKUP_DIR = os.path.join(BASE_DIR, 'backup_config')
LOG_DIR = os.path.join(BASE_DIR, 'logs')
DEFAULT_DOWNLOAD_DIR = os.path.join(BASE_DIR, 'downloads')
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')

# 录制进程把运行时状态写在这里，供 UI 读取（tmpfs，不落盘持久化）
RUNTIME_FILE = os.environ.get('DLR_RUNTIME_FILE', '/tmp/dlr_runtime.json')
TEXT_ENCODING = 'utf-8-sig'


def ensure_dirs() -> None:
    for d in (CONFIG_DIR, BACKUP_DIR, LOG_DIR, DEFAULT_DOWNLOAD_DIR):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass


def norm_url(url: str) -> str:
    """把直播间地址规范化，用于跨文件匹配。

    录制器会自行改写地址（补 https、去掉 query、去重），所以比较时必须先归一。
    """
    if not url:
        return ''
    u = url.strip()
    u = re.sub(r'^[a-zA-Z]+://', '', u)
    u = u.split('?')[0].split('#')[0]
    u = u.rstrip('/')
    u = re.sub(r'^www\.', '', u, flags=re.I)
    return u.lower()


def split_url_line(line: str):
    """解析 URL_config.ini 的一行 -> (commented, quality, url, name)。"""
    raw = line.rstrip('\n').rstrip('\r')
    s = raw.strip()
    if not s:
        return None
    commented = s.startswith('#')
    if commented:
        s = s.lstrip('#').strip()
    parts = [p.strip() for p in re.split(r'[,，]', s)]
    quality, url, name = '', '', ''
    if len(parts) == 1:
        url = parts[0]
    elif len(parts) == 2:
        if re.search(r'https?://|[\w-]+\.[\w.]+/', parts[0]):
            url, name = parts[0], parts[1]
        else:
            quality, url = parts[0], parts[1]
    else:
        quality, url, name = parts[0], parts[1], ','.join(parts[2:])
    if name.lower().startswith('主播:'):
        name = name[3:].strip()
    quality = quality if quality in ('原画', '蓝光', '超清', '高清', '标清', '流畅') else '原画'
    return commented, quality, url, name


def build_url_line(quality: str, url: str, name: str, enabled: bool) -> str:
    body = f'{quality or "原画"},{url}'
    if name:
        body += f',主播: {name}'
    return ('' if enabled else '#') + body


PLATFORM_MAP = {
    'live.douyin.com': '抖音', 'v.douyin.com': '抖音', 'www.douyin.com': '抖音', 'douyin.com': '抖音',
    'live.kuaishou.com': '快手', 'kuaishou.com': '快手',
    'www.huya.com': '虎牙', 'huya.com': '虎牙',
    'www.douyu.com': '斗鱼', 'douyu.com': '斗鱼',
    'www.yy.com': 'YY', 'yy.com': 'YY',
    'live.bilibili.com': 'B站', 'bilibili.com': 'B站',
    'www.redelight.cn': '小红书', 'www.xiaohongshu.com': '小红书', 'xhslink.com': '小红书',
    'www.bigo.tv': 'Bigo', 'slink.bigovideo.tv': 'Bigo',
    'app.blued.cn': 'Blued',
    'cc.163.com': '网易CC',
    'qiandurebo.com': '千度热播',
    'fm.missevan.com': '猫耳FM',
    'look.163.com': 'Look',
    'twitcasting.tv': 'TwitCasting',
    'live.baidu.com': '百度',
    'weibo.com': '微博',
    'kugou.com': '酷狗',
    'www.huajiao.com': '花椒',
    '7u66.com': '流星',
    'acfun.cn': 'Acfun',
    'tlclw.com': '畅聊',
    'ybw1666.com': '音播',
    'www.inke.cn': '映客',
    'zhihu.com': '知乎',
    'haixiutv.com': '嗨秀',
    'vvxqiu.com': 'VV星球',
    '17.live': '17Live',
    'lang.live': '浪Live',
    'weimipopo.com': '漂漂',
    '6.cn': '六间房',
    'lehaitv.com': '乐嗨',
    'catshow168.com': '花猫',
    'taobao.com': '淘宝', 'tb.cn': '淘宝',
    'jd.com': '京东', '3.cn': '京东',
    'miguvideo.com': '咪咕',
    'lailianjie.com': '连接',
    'imkktv.com': '来秀',
    'picarto.tv': 'Picarto',
    'www.tiktok.com': 'TikTok', 'tiktok.com': 'TikTok',
    'sooplive.co.kr': 'SOOP', 'sooplive.com': 'SOOP',
    'pandalive.co.kr': 'PandaTV',
    'winktv.co.kr': 'WinkTV',
    'flextv.co.kr': 'FlexTV',
    'ttinglive.com': 'PopkonTV',
    'www.twitch.tv': 'Twitch', 'twitch.tv': 'Twitch',
    'liveme.com': 'LiveMe',
    'showroom-live.com': 'ShowRoom',
    'chzzk.naver.com': 'CHZZK',
    'shopee.': 'Shopee', 'shp.ee': 'Shopee',
    'youtube.com': 'YouTube', 'youtu.be': 'YouTube',
    'faceit.com': 'Faceit',
}


def detect_platform(url: str) -> str:
    host = norm_url(url).split('/')[0]
    for key, name in PLATFORM_MAP.items():
        k = key.replace('www.', '')
        if host == k or host.endswith('.' + k) or (k.endswith('.') and k in host):
            return name
    return '其他'
