# -*- encoding: utf-8 -*-
"""WebUI 版本号。

格式：``<基版本>.<修订号>``，例如 ``1.0.0.9``

- 基版本：出现不兼容改动或大改版时才调整（默认 1.0.0）
- 修订号：影响 WebUI 的改动次数，从初版 1 计

维护方式：每次改动在 REVISIONS 末尾追加一条，__version__ 自动取列表长度，
不需要手改数字。这样版本号永远和「改了几次」一致，也留下了可查阅的更新记录。
"""

BASE_VERSION = '1.0.0'

# (日期, 变更说明) —— 顺序即修订顺序，只能在末尾追加
REVISIONS = [
    ('2026-11-02', '初版：WebUI 控制台，主播增删改、单主播录制/通知开关、全部设置项、状态与日志'),
    ('2026-11-02', '直播状态推送渠道默认微信；主播录制/通知开关改为横条样式'),
    ('2026-11-02', '主播列表改为表格，新增最后直播时间、单个主播录像列表与在线播放'),
    ('2026-11-02', '修复关闭播放弹窗后仍在后台播放声音'),
    ('2026-11-02', '首页最近录像支持点击播放；手机端适配，取消横向滚动'),
    ('2026-11-02', '修复首页播放弹窗中播放器溢出边框'),
    ('2026-11-02', '修复长文件名与心跳文案撑宽首页卡片导致的横向溢出'),
    ('2026-11-02', '运行日志页改为系统状态，展示程序版本/构建时间/运行环境；README 注明 AI 构建'),
    ('2026-11-02', '版本号改为「基版本 + 修订号」，系统状态页展示修订次数'),
]

__revision__ = len(REVISIONS)
__version__ = f'{BASE_VERSION}.{__revision__}'


def history(limit: int = 8) -> list:
    """最近的修订记录，新的在前。"""
    items = []
    for idx, (date, title) in enumerate(REVISIONS, start=1):
        items.append({
            'rev': idx,
            'date': date,
            'title': title,
            'version': f'{BASE_VERSION}.{idx}',
        })
    items.reverse()
    return items[:limit] if limit else items


def info() -> dict:
    return {
        'version': __version__,
        'base': BASE_VERSION,
        'revision': __revision__,
        'history': history(),
    }
