# -*- encoding: utf-8 -*-
"""设置项的元数据（分组、控件类型、说明）。

原则：以 config.ini 里实际存在的键为准，本文件的元数据只负责"怎么展示"。
文件里出现但这里没登记的键会自动推断控件类型并归入"其他"，保证不漏项。
"""
import re

from .config_io import read_ini

BOOL_VALUES = ('是', '否')

GROUPS = [
    ('record', '录制与保存', '决定录制什么、存到哪、存成什么格式'),
    ('network', '网络与代理', '访问频率、代理、直播源质量'),
    ('push', '推送通知', '开播/关播消息的推送渠道与内容'),
    ('account', '平台账号', '需要登录的平台账号与授权令牌'),
    ('cookie', 'Cookie 凭证', '抖音等平台的登录 Cookie，录制必备'),
    ('advanced', '高级', '自定义脚本、分段等进阶选项'),
    ('other', '其他', '未归类的配置项'),
]

# (section, key, label, type, group, help, options)
FIELDS = [
    # ---------- 录制设置 ----------
    ('录制设置', 'language(zh_cn/en)', '界面语言', 'select', 'record', '程序控制台输出语言', 'zh_cn,en'),
    ('录制设置', '视频保存格式ts|mkv|flv|mp4|mp3音频|m4a音频', '保存格式', 'select', 'record',
     '推荐 ts：异常中断也不会损坏文件', 'ts,mkv,flv,mp4,mp3音频,m4a音频'),
    ('录制设置', '原画|超清|高清|标清|流畅', '默认画质', 'select', 'record',
     '主播列表里可为单个主播单独指定画质', '原画,超清,高清,标清,流畅'),
    ('录制设置', '直播保存路径(不填则默认)', '保存目录', 'text', 'record',
     '留空则使用容器内 /app/downloads（映射到宿主机 downloads 目录）', ''),
    ('录制设置', '保存文件夹是否以作者区分', '按主播建文件夹', 'bool', 'record', '', ''),
    ('录制设置', '保存文件夹是否以时间区分', '按日期建文件夹', 'bool', 'record', '', ''),
    ('录制设置', '保存文件夹是否以标题区分', '按标题建文件夹', 'bool', 'record', '', ''),
    ('录制设置', '保存文件名是否包含标题', '文件名含直播标题', 'bool', 'record', '', ''),
    ('录制设置', '是否去除名称中的表情符号', '去除名称中的表情', 'bool', 'record',
     '避免部分系统文件名乱码', ''),
    ('录制设置', '分段录制是否开启', '开启分段录制', 'bool', 'advanced', '长时间直播按固定时长切片', ''),
    ('录制设置', '视频分段时间(秒)', '分段时长（秒）', 'number', 'advanced', '默认 1800 秒 = 30 分钟', ''),
    ('录制设置', '录制完成后自动转为mp4格式', '结束后转封装 MP4', 'bool', 'record',
     '录制结束自动把 ts 转为 mp4，兼容性更好', ''),
    ('录制设置', 'mp4格式重新编码为h264', '转 MP4 时重编码 H264', 'bool', 'advanced',
     '耗时且吃 CPU，仅在播放器不兼容时开启', ''),
    ('录制设置', '追加格式后删除原文件', '转码后删除原文件', 'bool', 'record', '', ''),
    ('录制设置', '生成时间字幕文件', '生成时间字幕', 'bool', 'advanced', '', ''),
    ('录制设置', '是否录制弹幕(是/否)', '录制抖音弹幕', 'bool', 'record',
     '仅支持抖音直播；与录像同时启停，需在主播条目上开启（默认开启）', ''),
    ('录制设置', '弹幕保存格式(逗号分隔)', '弹幕保存格式', 'tags', 'record',
     'jsonl 全字段原始数据，srt / ass 可直接挂到播放器当字幕', 'json,srt,ass'),
    ('录制设置', '录制空间剩余阈值(gb)', '磁盘剩余阈值（GB）', 'number', 'advanced',
     '剩余空间低于该值时停止录制，保护磁盘', ''),
    ('录制设置', '是否录制完成后执行自定义脚本', '录制完成后执行脚本', 'bool', 'advanced', '', ''),
    ('录制设置', '自定义脚本执行命令', '自定义脚本命令', 'text', 'advanced',
     '可用 {record_name} {save_file_path} 等占位由程序自动传入', ''),

    # ---------- 网络 ----------
    ('录制设置', '同一时间访问网络的线程数', '并发线程数', 'number', 'network',
     '同时请求直播间的数量，过大容易被风控', ''),
    ('录制设置', '循环时间(秒)', '轮询间隔（秒）', 'number', 'network',
     '每隔多久检查一次主播是否开播，默认 300；改小更及时但更耗流量', ''),
    ('录制设置', '排队读取网址时间(秒)', '启动排队间隔（秒）', 'number', 'network',
     '批量启动监测时的间隔，避免瞬间并发过高', ''),
    ('录制设置', '是否使用代理ip(是/否)', '启用代理', 'bool', 'network', '', ''),
    ('录制设置', '代理地址', '代理地址', 'text', 'network',
     '支持 http://user:pass@host:port ，仅对下方指定平台生效', ''),
    ('录制设置', '是否跳过代理检测(是/否)', '跳过本地代理检测', 'bool', 'network',
     '容器内没有系统代理，建议开启以免启动时等待检测', ''),
    ('录制设置', '使用代理录制的平台(逗号分隔)', '走代理的平台', 'text', 'network',
     '逗号分隔，如 tiktok, sooplive, twitch', ''),
    ('录制设置', '额外使用代理录制的平台(逗号分隔)', '额外走代理的平台', 'text', 'network', '', ''),
    ('录制设置', '是否强制启用https录制', '强制 HTTPS 拉流', 'bool', 'network', '', ''),
    ('录制设置', '是否显示循环秒数', '日志显示轮询倒计时', 'bool', 'advanced', '', ''),
    ('录制设置', '是否显示直播源地址', '日志显示直播源地址', 'bool', 'advanced', '', ''),

    # ---------- 推送 ----------
    ('推送配置', '直播状态推送渠道', '推送渠道', 'tags', 'push',
     '可多选：微信 / 钉钉 / TG / 邮箱 / BARK / NTFY / PUSHPLUS', '微信,钉钉,TG,邮箱,BARK,NTFY,PUSHPLUS'),
    ('推送配置', '开播推送开启(是/否)', '开播时推送', 'bool', 'push',
     '总开关；单个主播还可在主播列表里单独关闭通知', ''),
    ('推送配置', '关播推送开启(是/否)', '关播时推送', 'bool', 'push', '', ''),
    ('推送配置', '只推送通知不录制(是/否)', '只通知，不录制', 'bool', 'push',
     '全局开关：只监控开播并推送，不落盘录像', ''),
    ('推送配置', '直播推送检测频率(秒)', '推送检测频率（秒）', 'number', 'push', '', ''),
    ('推送配置', '自定义推送标题', '推送标题', 'text', 'push', '', ''),
    ('推送配置', '自定义开播推送内容', '开播推送内容', 'textarea', 'push',
     '支持占位：[直播间名称] [时间]，换行用 \\n', ''),
    ('推送配置', '自定义关播推送内容', '关播推送内容', 'textarea', 'push',
     '支持占位：[直播间名称] [时间]，换行用 \\n', ''),
    ('推送配置', '钉钉推送接口链接', '钉钉 Webhook', 'password', 'push',
     '钉钉群机器人地址，含 access_token', ''),
    ('推送配置', '钉钉通知@对象(填手机号)', '@ 指定手机号', 'text', 'push', '', ''),
    ('推送配置', '钉钉通知@全体(是/否)', '@ 全体成员', 'bool', 'push', '', ''),
    ('推送配置', '微信推送接口链接', '微信推送接口', 'password', 'push',
     '息知 / Server酱 等中转服务地址', ''),
    ('推送配置', 'tgapi令牌', 'Telegram Bot Token', 'password', 'push', '', ''),
    ('推送配置', 'tg聊天id(个人或者群组id)', 'Telegram Chat ID', 'text', 'push', '', ''),
    ('推送配置', 'smtp邮件服务器', 'SMTP 服务器', 'text', 'push', '例如 smtp.qq.com', ''),
    ('推送配置', 'SMTP邮件服务器端口', 'SMTP 端口', 'text', 'push', 'SSL 通常为 465', ''),
    ('推送配置', '是否使用SMTP服务SSL加密(是/否)', 'SMTP 使用 SSL', 'bool', 'push', '', ''),
    ('推送配置', '邮箱登录账号', '发件账号', 'text', 'push', '', ''),
    ('推送配置', '发件人密码(授权码)', '邮箱授权码', 'password', 'push', '', ''),
    ('推送配置', '发件人邮箱', '发件人邮箱', 'text', 'push', '', ''),
    ('推送配置', '发件人显示昵称', '发件人昵称', 'text', 'push', '', ''),
    ('推送配置', '收件人邮箱', '收件人邮箱', 'text', 'push', '多个用逗号分隔', ''),
    ('推送配置', 'bark推送接口链接', 'Bark 接口地址', 'password', 'push', '', ''),
    ('推送配置', 'bark推送中断级别', 'Bark 中断级别', 'select', 'push', '', 'active,timeSensitive,passive,critical'),
    ('推送配置', 'bark推送铃声', 'Bark 铃声', 'text', 'push', '', ''),
    ('推送配置', 'ntfy推送地址', 'NTFY 地址', 'text', 'push', '例如 https://ntfy.sh/your-topic', ''),
    ('推送配置', 'ntfy推送标签', 'NTFY 标签', 'text', 'push', '', ''),
    ('推送配置', 'ntfy推送邮箱', 'NTFY 邮箱', 'text', 'push', '', ''),
    ('推送配置', 'pushplus推送token', 'PushPlus Token', 'password', 'push', '', ''),

    # ---------- 账号 ----------
    ('Authorization', 'popkontv_token', 'PopkonTV Token', 'password', 'account', '', ''),
    ('账号密码', 'sooplive账号', 'SOOP 账号', 'text', 'account', '', ''),
    ('账号密码', 'sooplive密码', 'SOOP 密码', 'password', 'account', '', ''),
    ('账号密码', 'flextv账号', 'FlexTV 账号', 'text', 'account', '', ''),
    ('账号密码', 'flextv密码', 'FlexTV 密码', 'password', 'account', '', ''),
    ('账号密码', 'popkontv账号', 'PopkonTV 账号', 'text', 'account', '', ''),
    ('账号密码', 'popkontv密码', 'PopkonTV 密码', 'password', 'account', '', ''),
    ('账号密码', 'partner_code', 'PopkonTV 合作码', 'text', 'account', '', ''),
    ('账号密码', 'twitcasting账号类型', 'TwitCasting 账号类型', 'select', 'account', '', 'normal,premium'),
    ('账号密码', 'twitcasting账号', 'TwitCasting 账号', 'text', 'account', '', ''),
    ('账号密码', 'twitcasting密码', 'TwitCasting 密码', 'password', 'account', '', ''),
]

# Cookie 键名美化
_COOKIE_NAME = {
    'dy': '抖音', 'ks': '快手', 'hy': '虎牙', 'douyu': '斗鱼', 'yy': 'YY', 'bili': 'B站',
    'xhs': '小红书', 'bigo': 'Bigo', 'blued': 'Blued', 'sooplive': 'SOOP', 'netease': '网易CC',
    'qiandurebo': '千度热播', 'pandatv': 'PandaTV', 'maoerfm': '猫耳FM', 'winktv': 'WinkTV',
    'flextv': 'FlexTV', 'look': 'Look', 'twitcasting': 'TwitCasting', 'baidu': '百度',
    'weibo': '微博', 'kugou': '酷狗', 'twitch': 'Twitch', 'liveme': 'LiveMe', 'huajiao': '花椒',
    'liuxing': '流星', 'showroom': 'ShowRoom', 'acfun': 'Acfun', 'changliao': '畅聊',
    'yinbo': '音播', 'yingke': '映客', 'zhihu': '知乎', 'chzzk': 'CHZZK', 'haixiu': '嗨秀',
    'vvxqiu': 'VV星球', '17live': '17Live', 'langlive': '浪Live', 'pplive': '漂漂',
    '6room': '六间房', 'lehaitv': '乐嗨', 'huamao': '花猫', 'shopee': 'Shopee',
    'youtube': 'YouTube', 'taobao': '淘宝', 'jd': '京东', 'faceit': 'Faceit',
    'migu': '咪咕', 'lianjie': '连接', 'laixiu': '来秀', 'picarto': 'Picarto',
}


def _cookie_label(key: str) -> str:
    base = key[:-len('cookie')] if key.lower().endswith('cookie') else key
    base = base.strip('_').strip()
    name = _COOKIE_NAME.get(base.lower(), base)
    return f'{name} Cookie'


def _infer_type(key: str, value: str) -> str:
    low = key.lower()
    if value in BOOL_VALUES:
        return 'bool'
    if 'cookie' in low:
        return 'textarea'
    if any(w in key for w in ('密码', '授权码', 'token', 'Token')):
        return 'password'
    if re.fullmatch(r'-?\d+(\.\d+)?', value or '') and value != '':
        return 'number'
    return 'text'


def build_schema() -> dict:
    """返回 {groups:[{id,title,desc}], fields:[{...}]}"""
    ini = read_ini()
    known = {(f[0], f[1]) for f in FIELDS}
    fields = []

    for section, key, label, ftype, group, help_text, options in FIELDS:
        value = ini.get(section, {}).get(key)
        if value is None and section not in ini:
            continue
        fields.append({
            'section': section, 'key': key, 'label': label, 'type': ftype,
            'group': group, 'help': help_text,
            'options': [o for o in options.split(',') if o] if options else [],
            'value': value if value is not None else '',
        })

    # 文件里有、元数据没登记的键（例如上游新增配置）——自动补齐
    for section, kv in ini.items():
        for key, value in kv.items():
            if (section, key) in known:
                continue
            if section == 'Cookie' or 'cookie' in key.lower():
                label, group = _cookie_label(key), 'cookie'
            else:
                label, group = key, ('other' if section not in ('录制设置', '推送配置') else
                                     ('record' if section == '录制设置' else 'push'))
            fields.append({
                'section': section, 'key': key, 'label': label,
                'type': _infer_type(key, value), 'group': group, 'help': '',
                'options': [], 'value': value,
            })

    fields.sort(key=lambda f: (f['group'] == 'cookie',))
    return {
        'groups': [{'id': g, 'title': t, 'desc': d} for g, t, d in GROUPS],
        'fields': fields,
    }


def save_settings(items) -> dict:
    """items: [{section,key,value}]，校验后写回。"""
    from .config_io import update_values
    cleaned = []
    for it in items:
        section = str(it.get('section', '')).strip()
        key = str(it.get('key', '')).strip()
        if not section or not key:
            continue
        value = it.get('value', '')
        if isinstance(value, bool):
            value = '是' if value else '否'
        value = '' if value is None else str(value)
        value = value.replace('\r\n', '\n').replace('\n', '\\n')
        cleaned.append((section, key, value))
    touched = update_values(cleaned)
    return {'ok': True, 'updated': len(touched)}
