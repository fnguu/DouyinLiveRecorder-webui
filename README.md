![video_spider](https://socialify.git.ci/ihmily/DouyinLiveRecorder/image?font=Inter&forks=1&language=1&owner=1&pattern=Circuit%20Board&stargazers=1&theme=Light)

> ## 🖥️ 本仓库是带 WebUI 控制台 + 弹幕录制的容器版（当前版本 v4.1.0）
>
> 在上游 [v4.0.7](https://github.com/ihmily/DouyinLiveRecorder/releases/tag/v4.0.7) 基础上增加两块能力：
>
> **① WebUI 控制台** —— 不改配置文件格式，浏览器里就能增删主播、单独控制每个主播是否录制 /
> 是否推送 / 是否录弹幕，以及编辑全部设置项（录制、推送、Cookie、账号），可查看实时状态与录像回放。
>
> **② 弹幕录制** —— 抖音、B 站直播可在录像的同时抓取弹幕，输出 **B 站弹幕格式 XML**
> （内嵌样式，浏览器双击即可预览）与 **ASS 字幕**，可直接挂载进播放器或烧进画面；
> B 站额外支持礼物、醒目留言、上舰（舰长/提督/总督）记录。
>
> **③ 登录保护** —— 首次打开网页时设置用户名和密码，之后凭长期会话 Cookie 免登录
> （180 天，使用中自动续期）；「全局设置 → 登录账号」里可改用户名、改密码、退出其它设备、
> 重置凭据，并查看登录设备与操作时间日志，密码以 PBKDF2 加盐哈希保存在本机。
>
> 本仓库只提供容器运行方式，需要直接跑源码请参考[上游仓库](https://github.com/ihmily/DouyinLiveRecorder)。
>
> ```bash
> docker compose up -d          # 首次会自动构建镜像
> # 打开 http://localhost:8000
> ```
>
> 详细说明见 **[WEBUI.md](WEBUI.md)**，容器参数详解见下方「容器运行」章节。
> 当前 WebUI 版本 **1.0.0.22**（版本号末位为修订次数，见「系统状态」页的更新记录）。
>
> ---
>
> ### ⚠️ 关于本仓库的产出方式
>
> 本仓库的 **WebUI 前后端、容器化改造、部署配置与全部文档，均由 AI（WorkBuddy）生成**，
> 人工只负责提出需求与验收。代码未经专业团队评审，请自行评估后使用。
>
> 录制核心逻辑来自上游 [ihmily/DouyinLiveRecorder](https://github.com/ihmily/DouyinLiveRecorder)，
> 本仓库未改动其平台接口与流解析实现。

## 💡简介
[![Version](https://img.shields.io/badge/version-v4.1.0-blue.svg)](https://github.com/fnguu/DouyinLiveRecorder-webui/releases/latest)
[![Base](https://img.shields.io/badge/base-v4.0.7-lightgrey.svg)](https://github.com/ihmily/DouyinLiveRecorder/releases/tag/v4.0.7)
[![Danmaku](https://img.shields.io/badge/danmaku-%E6%8A%96%E9%9F%B3%20%7C%20B%E7%AB%99-ff69b4.svg)](#-弹幕录制)
[![Python Version](https://img.shields.io/badge/python-3.11.6-blue.svg)](https://www.python.org/downloads/release/python-3116/)
[![GitHub issues](https://img.shields.io/github/issues/fnguu/DouyinLiveRecorder-webui.svg)](https://github.com/fnguu/DouyinLiveRecorder-webui/issues)
[![Downloads](https://img.shields.io/github/downloads/fnguu/DouyinLiveRecorder-webui/total)](https://github.com/fnguu/DouyinLiveRecorder-webui/releases/latest)

一款**简易**的可循环值守的直播录制工具，基于 FFmpeg 实现多平台直播源录制，支持自定义配置录制以及直播状态推送。

本仓库在此基础上做了两项增强：

- 🖥️ **WebUI 控制台**：浏览器管理主播列表与全部设置项，单主播级别的录制/推送/弹幕开关，实时运行状态、系统信息与录像在线播放，手机端自适应。`docker compose up -d` 后访问 `http://localhost:8000`，首次进入设置用户名密码即可。
- 🎬 **弹幕录制**：抖音、B 站直播在录像的同时抓取弹幕，与视频同名输出 `.danmaku.xml` 与 `.ass` 字幕；XML 兼容 B 站标准弹幕格式（DanmakuFactory / 播放器可直接用），并内嵌样式表，双击文件就能在浏览器里查看完整弹幕列表。B 站支持礼物、醒目留言（SC）、上舰记录，ASS 字幕形如 `用户名:弹幕内容`。
- 🔐 **登录保护**：首次打开网页设置用户名与密码，保存在本机 `config/webui_auth.json`（PBKDF2 加盐哈希，不存明文），所有数据接口都需要登录后才能访问。登录后凭 Cookie **长期保持登录**（180 天，使用中自动续期）。「全局设置 → 登录账号」可修改用户名与密码、退出其它登录设备、重置登录凭据，并查看**登录设备列表**（浏览器/系统、IP、登录与到期时间）和**操作时间日志**（登录、退出、改密、重置记录）。
- 🍪 **Cookie 失效提醒**：定时校验 `[Cookie]` 里的凭证——抖音直接读 `sid_guard` 里的过期时间，B 站调登录态接口实测，失效或 7 天内将过期时用你已配好的推送渠道（微信/钉钉/TG/邮箱/Bark/ntfy/PushPlus）提醒一次，不用等录制失败才发现。系统状态页有「Cookie 状态」卡片与「立即检测」按钮，详见[Cookie 失效提醒](#-cookie-失效提醒)。

> 弹幕录制为可选功能，默认关闭，可在 WebUI「设置」页全局开启，或用主播列表里的单主播开关精细控制。

</div>

## 😺已支持平台

- [x] 抖音
- [x] TikTok
- [x] 快手
- [x] 虎牙
- [x] 斗鱼
- [x] YY
- [x] B站
- [x] 小红书
- [x] bigo 
- [x] blued
- [x] SOOP(原AfreecaTV)
- [x] 网易cc
- [x] 千度热播
- [x] PandaTV
- [x] 猫耳FM
- [x] Look直播
- [x] WinkTV
- [x] TTingLive(原Flextv)
- [x] PopkonTV
- [x] TwitCasting
- [x] 百度直播
- [x] 微博直播
- [x] 酷狗直播
- [x] TwitchTV
- [x] LiveMe
- [x] 花椒直播
- [x] 流星直播
- [x] ShowRoom
- [x] Acfun
- [x] 映客直播
- [x] 音播直播
- [x] 知乎直播
- [x] CHZZK
- [x] 嗨秀直播
- [x] vv星球直播
- [x] 17Live
- [x] 浪Live
- [x] 畅聊直播
- [x] 飘飘直播
- [x] 六间房直播
- [x] 乐嗨直播
- [x] 花猫直播
- [x] Shopee
- [x] Youtube
- [x] 淘宝
- [x] 京东
- [x] Faceit
- [x] 咪咕
- [x] 连接直播
- [x] 来秀直播
- [x] Picarto
- [ ] 更多平台正在更新中

</div>

## 🎬 弹幕录制

录制视频的同时把弹幕一并存下来，与视频同目录、同文件名：

```
downloads/抖音直播/某主播/某主播_2026-11-14_20-30-00.mp4
downloads/抖音直播/某主播/某主播_2026-11-14_20-30-00.danmaku.xml
downloads/抖音直播/某主播/某主播_2026-11-14_20-30-00.danmaku.ass
```

| 项目 | 说明 |
|---|---|
| 支持平台 | 抖音、B 站（其余平台暂不支持） |
| XML | B 站标准弹幕格式（`<d p="...">`），兼容 DanmakuFactory、PotPlayer 等工具；内嵌 XSLT 样式，**浏览器双击即可预览**用户名/时间/用户ID/内容表格 |
| ASS | 可直接挂载进播放器或烧进画面，文本格式为 `用户名:弹幕内容`，礼物/醒目留言/上舰带【礼物】【醒目留言】【上舰】前缀 |
| 内容类型 | 弹幕、表情；B 站另含礼物、醒目留言（SC）、上舰（舰长/提督/总督）；抖音无 SC/上舰 |
| 写入方式 | 边录边追加，进程异常退出也只丢收尾标签，已写入的弹幕不会丢 |
| 开关 | 全局开关在「设置」页，单主播开关在主播列表；可在录像弹窗里直接下载 |

使用提示：

- **B 站建议填 Cookie**（设置页 → B 站 Cookie，至少含 `SESSDATA`）。不登录时 B 站只给超清画质（原画需登录态），送礼人昵称也会被打码成 `浪***`。
- 抖音建议同样填 Cookie，能显著降低接口风控导致的「已开播但监控不到」以及礼物消息缺失。
- 礼物金额字段为金瓜子数量（B 站 1000 金瓜子 = 1 元），XML 里以 `price` 属性保存。
- 弹幕转换示例：`DanmakuFactory -i xxx.danmaku.xml -o xxx.ass`。

&emsp;

## 🔐 登录与账号

首次打开 WebUI 需要设置一个用户名和密码（至少 6 位），之后凭长期会话免登录。账号信息只保存在容器内 `config/webui_auth.json`，密码以 **PBKDF2-SHA256（20 万轮）加盐哈希**存储，不存明文。

| 项目 | 说明 |
|---|---|
| 首次设置 | 打开网页即进入「设置登录账号」，填用户名 + 密码 + 确认密码 |
| 长期登录 | 勾选「保持登录状态」后 Cookie 180 天有效，使用中自动续期，等于一直免登录 |
| 账号管理 | 「全局设置 → 登录账号」（顶栏钥匙图标可直达）：改用户名、改密码，均需当前密码确认 |
| 登录设备 | 列出所有已登录设备的浏览器/系统、IP、登录时间、最近活跃与到期时间，可一键让其它设备下线 |
| 操作时间日志 | 创建账号、登录、退出、改用户名/密码、退出设备、重置等操作，带时间与来源 IP（最近 40 条） |
| 密码规则 | 至少 6 位、最多 128 位；同一 IP 连续输错 10 次锁定 10 分钟 |
| 重置 | 「重置…」需当前密码 + 手动输入「重置」确认；重置后回到首次设置流程，主播列表与录制配置不受影响 |

> 改密码后其它设备会被踢下线，当前设备保持登录；「退出其它设备」只保留当前这台。
> 若 WebUI 被 NAS 桌面以 iframe 嵌入、或浏览器禁用了 Cookie，页面会用本地保存的会话令牌维持登录，不用每次重登。

&emsp;

## 🍪 Cookie 失效提醒

抖音、B 站等平台要登录态才能拿到原画/弹幕，Cookie 过期后表现为「明明开播却录不到」或画质悄悄降级，很容易隔几天才发现。这个功能把「凭证还能不能用」变成一条推送。

| 平台 | 判定方式 | 能说什么 |
|---|---|---|
| 抖音 | 解析 Cookie 里 `sid_guard` 的过期时间戳（离线可判，不需要联网） | 有效期至某日 / 还剩几天 / 已于某日过期 |
| B 站 | 请求 `x/web-interface/nav` 问登录态 | 登录态正常（带用户名）/ SESSDATA 已失效 |
| 其它平台 | 只看有没有填 | 已配置 / 未配置；不猜失效 |

设计上刻意**不猜**：没有稳定登录态接口的平台一律标「无法判定」，网络抖动也只记「本次不做结论」并沿用上次结果，避免半夜被误报吵醒。

**怎么用**

1. 「全局设置 → 推送通知 → Cookie 失效时提醒」保持开启（默认开），检查间隔默认 6 小时；
2. 推送渠道直接用上面的「直播状态推送渠道」，配好哪个就用哪个；
3. 「系统状态 → Cookie 状态」随时看结论，点「立即检测」手动跑一次。

**提醒规则**：失效或 7 天内将过期时推一次，内容形如

```
以下平台的 Cookie 需要处理：
抖音：登录凭证（sid_guard）已于 2026-11-10 08:20 过期

请到 WebUI → 全局设置 → Cookie 凭证 里更新后保存。
时间：2026-11-16 18:00
```

持续失效时按「失效重复提醒(小时)」再提醒（默认 24 小时，填 0 只提醒一次）；换上新 Cookie 后状态恢复「有效」，下次再失效会立刻提醒。

**相关配置项**（都在 `[推送配置]`，也能在设置页里改）

```ini
cookie失效推送(是/否) = 是
cookie检查间隔(小时) = 6
cookie失效重复提醒(小时) = 24
cookie失效推送内容 =            ; 留空用内置文案，可用占位 [明细] [时间]
```

> 检查在 WebUI 进程里跑，容器活着就按点执行，不需要打开页面；结论写在 `config/cookie_status.json`，重启不丢。
> 抖音的 Cookie 若不含 `sid_guard`（只填了 `ttwid` 之类），会显示「无法判定有效期」而不是硬报失效。

&emsp;

## 🎈项目结构

```
.
├── main.py                      # 录制主程序（轮询、调度、调用 ffmpeg）
├── src/                         # 直播数据获取与流解析
│   ├── spider.py                # 抓取直播间信息与直播源
│   ├── stream.py                # 解析直播流地址
│   ├── room.py                  # 房间信息
│   ├── proxy.py                 # 代理检测
│   ├── javascript/              # 部分平台的签名 / 解密脚本
│   └── ...
├── webui/                       # WebUI（本仓库新增）
│   ├── app.py                   # FastAPI 接口
│   ├── anchors.py               # 主播列表增删改与录制开关
│   ├── schema.py                # 设置项元数据
│   ├── config_io.py             # config.ini 无损读写
│   ├── auth.py                  # 登录认证：密码哈希、长期会话、设备与操作日志
│   ├── cookie_health.py         # Cookie 健康检查与失效推送
│   ├── runtime.py / bridge.py   # 运行时状态与进程间桥接
│   └── static/                  # 前端单页 index.html / app.js / style.css
├── docker/
│   ├── entrypoint.sh            # 容器入口：初始化目录并启动 supervisor
│   └── supervisord.conf         # 托管 recorder 与 webui 两个进程
├── config/                      # config.ini、URL_config.ini、webui_state.json、cookie_status.json
├── downloads/                   # 录像输出
├── logs/                        # 运行日志
├── backup_config/               # 配置自动备份
├── msg_push.py                  # 开播 / 关播 / Cookie 失效消息推送
├── Dockerfile
├── docker-compose.yml           # 通用容器编排
├── docker-compose.ugreen.yml    # NAS 部署配置
├── WEBUI.md                     # WebUI 功能与部署文档
└── requirements.txt / requirements-webui.txt
```


## 🌱使用说明

- **本仓库推荐用容器运行**：`docker compose up -d` 后访问 `http://localhost:8000` 即可，详细步骤见下方「容器运行」与 [WEBUI.md](WEBUI.md)。[Releases](https://github.com/fnguu/DouyinLiveRecorder-webui/releases) 里提供源码包与镜像标签，不含 Windows 打包版。
- 需要 Windows 直接运行的打包版，请到[上游 Releases](https://github.com/ihmily/DouyinLiveRecorder/releases) 下载 zip 压缩包（无 WebUI、无弹幕录制）。（有些电脑可能会报毒，直接忽略即可，如果下载时被浏览器屏蔽，请更换浏览器下载）

- 压缩包解压后，在 `config` 文件夹内的 `URL_config.ini` 中添加录制直播间地址，一行一个直播间地址。如果要自定义配置录制，可以修改`config.ini` 文件，推荐将录制格式修改为`ts`。
- 以上步骤都做好后，就可以运行`DouyinLiveRecorder.exe` 程序进行录制了。录制的视频文件保存在同目录下的 `downloads` 文件夹内。

- 另外，如果需要录制TikTok、AfreecaTV等海外平台，请在配置文件中设置开启代理并添加proxy_addr链接 如：`127.0.0.1:7890` （这只是示例地址，具体根据实际填写）。

- 假如`URL_config.ini`文件中添加的直播间地址，有个别直播间暂时不想录制又不想移除链接，可以在对应直播间的链接开头加上`#`，那么将停止该直播间的监测以及录制。

- 软件默认录制清晰度为 `原画` ，如果要单独设置某个直播间的录制画质，可以在添加直播间地址时前面加上画质即可，如`超清，https://live.douyin.com/745964462470` 记得中间要有`,` 分隔。

- 如果要长时间挂着软件循环监测直播，最好循环时间设置长一点（咱也不差没录制到的那几分钟），避免因请求频繁导致被官方封禁IP 。

- 要停止直播录制，Windows平台可执行StopRecording.vbs脚本文件，或者在录制界面使用 `Ctrl+C ` 组合键中断录制，若要停止其中某个直播间的录制，可在`URL_config.ini`文件中的地址前加#，会自动停止对应直播间的录制并正常保存已录制的视频。
- 最后，欢迎右上角给本项目一个star，同时也非常乐意大家提交pr。

&emsp;

直播间链接示例：

```
抖音:
https://live.douyin.com/745964462470
https://v.douyin.com/iQFeBnt/
https://live.douyin.com/yall1102  （链接+抖音号）
https://v.douyin.com/CeiU5cbX  （主播主页地址）

TikTok:
https://www.tiktok.com/@pearlgaga88/live

快手:
https://live.kuaishou.com/u/yall1102

虎牙:
https://www.huya.com/52333

斗鱼:
https://www.douyu.com/3637778?dyshid=
https://www.douyu.com/topic/wzDBLS6?rid=4921614&dyshid=

YY:
https://www.yy.com/22490906/22490906

B站:
https://live.bilibili.com/320

小红书（直播间分享地址):
http://xhslink.com/xpJpfM

bigo直播:
https://www.bigo.tv/cn/716418802

buled直播:
https://app.blued.cn/live?id=Mp6G2R

SOOP:
https://play.sooplive.co.kr/sw7love

网易cc:
https://cc.163.com/583946984

千度热播:
https://qiandurebo.com/web/video.php?roomnumber=33333

PandaTV:
https://www.pandalive.co.kr/live/play/bara0109

猫耳FM:
https://fm.missevan.com/live/868895007

Look直播:
https://look.163.com/live?id=65108820&position=3

WinkTV:
https://www.winktv.co.kr/live/play/anjer1004

FlexTV(TTinglive)::
https://www.flextv.co.kr/channels/593127/live

PopkonTV:
https://www.popkontv.com/live/view?castId=wjfal007&partnerCode=P-00117
https://www.popkontv.com/channel/notices?mcid=wjfal007&mcPartnerCode=P-00117

TwitCasting:
https://twitcasting.tv/c:uonq

百度直播:
https://live.baidu.com/m/media/pclive/pchome/live.html?room_id=9175031377&tab_category

微博直播:
https://weibo.com/l/wblive/p/show/1022:2321325026370190442592

酷狗直播:
https://fanxing2.kugou.com/50428671?refer=2177&sourceFrom=

TwitchTV:
https://www.twitch.tv/gamerbee

LiveMe:
https://www.liveme.com/zh/v/17141543493018047815/index.html

花椒直播:
https://www.huajiao.com/l/345096174

流星直播:
https://www.7u66.com/100960

ShowRoom:
https://www.showroom-live.com/room/profile?room_id=480206  （主播主页地址）

Acfun:
https://live.acfun.cn/live/179922

映客直播:
https://www.inke.cn/liveroom/index.html?uid=22954469&id=1720860391070904

音播直播:
https://live.ybw1666.com/800002949

知乎直播:
https://www.zhihu.com/people/ac3a467005c5d20381a82230101308e9 (主播主页地址)

CHZZK:
https://chzzk.naver.com/live/458f6ec20b034f49e0fc6d03921646d2

嗨秀直播:
https://www.haixiutv.com/6095106

VV星球直播:
https://h5webcdn-pro.vvxqiu.com//activity/videoShare/videoShare.html?h5Server=https://h5p.vvxqiu.com&roomId=LP115924473&platformId=vvstar

17Live:
https://17.live/en/live/6302408

浪Live:
https://www.lang.live/en-US/room/3349463

畅聊直播:
https://live.tlclw.com/106188

飘飘直播:
https://m.pp.weimipopo.com/live/preview.html?uid=91648673&anchorUid=91625862&app=plpl

六间房直播:
https://v.6.cn/634435

乐嗨直播:
https://www.lehaitv.com/8059096

花猫直播:
https://h.catshow168.com/live/preview.html?uid=19066357&anchorUid=18895331

Shopee:
https://sg.shp.ee/GmpXeuf?uid=1006401066&session=802458

Youtube:
https://www.youtube.com/watch?v=cS6zS5hi1w0

淘宝(需cookie):
https://tbzb.taobao.com/live?liveId=532359023188
https://m.tb.cn/h.TWp0HTd

京东:
https://3.cn/28MLBy-E

Faceit:
https://www.faceit.com/zh/players/Compl1/stream

连接直播:
https://show.lailianjie.com/10000258

咪咕直播:
https://www.miguvideo.com/p/live/120000541321

来秀直播:
https://www.imkktv.com/h5/share/video.html?uid=1845195&roomId=1710496

Picarto:
https://www.picarto.tv/cuteavalanche
```

&emsp;

## 🐋容器运行

运行前请先安装 [Docker](https://docs.docker.com/get-docker/) 与 [Docker Compose](https://docs.docker.com/compose/install/)，终端执行 `docker compose version` 能输出版本号即可。

### 快速开始

```bash
git clone https://github.com/fnguu/DouyinLiveRecorder-webui.git
cd DouyinLiveRecorder-webui
docker compose up -d
```

浏览器打开 `http://localhost:8000`，**首次进入会要求设置用户名和密码**（保存在 `config/webui_auth.json`，之后凭 Cookie 长期免登录）。登录后粘贴直播间地址就能开始值守。账号密码随时可在「全局设置 → 登录账号」里修改或重置，详见[登录与账号](#-登录与账号)。首次执行会先构建镜像（约 5~10 分钟）。

也可以直接使用已构建好的镜像，省去构建：

```bash
docker pull ghcr.io/fnguu/douyin-live-recorder-webui:latest
docker tag ghcr.io/fnguu/douyin-live-recorder-webui:latest douyin-live-recorder-webui:latest
docker compose up -d
```

### compose 详解

`docker-compose.yml` 全文：

```yaml
services:
  app:
    build: .
    image: douyin-live-recorder-webui:latest
    container_name: douyin-live-recorder
    ports:
      - "8000:8000"
    environment:
      - TZ=Asia/Shanghai
      - WEBUI_PORT=8000
    volumes:
      - ./config:/app/config
      - ./logs:/app/logs
      - ./downloads:/app/downloads
      - ./backup_config:/app/backup_config
    restart: unless-stopped
    stop_grace_period: 60s
```

**服务构成**：只有一个容器 `app`，容器内由 supervisor 托管两个进程 —— `recorder`（`python main.py`，负责轮询与录制）和 `webui`（uvicorn，提供网页管理端）。任一进程崩溃会被自动拉起，容器整体重启策略为 `unless-stopped`，即宿主机重启后仍会自动运行。

**端口**

| 映射 | 说明 |
| --- | --- |
| `8000:8000` | 左边是宿主机端口，右边是容器内 WebUI 端口。只想换访问端口就改左边，例如 `8080:8000` |

容器内监听端口由 `WEBUI_PORT` 决定，修改时需与 ports 右侧保持一致。

**卷（必须持久化，否则重建容器即丢配置和录像）**

| 宿主机目录 | 容器内路径 | 存放内容 |
| --- | --- | --- |
| `./config` | `/app/config` | `config.ini`（全部设置）、`URL_config.ini`（主播列表）、`webui_state.json`（单个主播的通知开关等） |
| `./downloads` | `/app/downloads` | 录像文件 |
| `./logs` | `/app/logs` | 运行日志，网页「运行日志」页读取这里 |
| `./backup_config` | `/app/backup_config` | 每次改配置前的自动备份 |

`config` 目录首次挂载为空时，入口脚本会用镜像内置模板补全，不会因缺配置而启动失败。

**环境变量**

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `TZ` | `Asia/Shanghai` | 时区，影响录像文件名里的时间戳 |
| `WEBUI_PORT` | `8000` | 容器内 WebUI 监听端口，需与 ports 右侧一致 |
| `WEBUI_PASSWORD` | 空 | 设置后启用 HTTP Basic 认证，远程访问或公网暴露时务必设置 |
| `HTTP_PROXY` / `HTTPS_PROXY` | 空 | 录制海外平台时使用的代理；代理跑在宿主机上时填 `http://host.docker.internal:7890` |

**停止行为**：`stop_grace_period: 60s` 配合容器内的 SIGTERM 处理，`docker compose down` 时会先通知 ffmpeg 完成当前文件封装再退出，不会留下损坏的录像。

### 常用命令

```bash
docker compose ps                              # 查看运行状态
docker compose logs -f                         # 跟踪日志
docker compose restart                          # 重启
docker compose pull && docker compose up -d     # 拉取新镜像并重建容器
docker compose down                             # 停止并移除容器（卷与录像保留）
```

容器内的实时输出也可以直接查看：

```bash
docker exec -it douyin-live-recorder tail -f /app/logs/recorder.log
```

### 本地构建镜像

修改代码或想用最新提交时可自行构建：

```bash
docker build -t douyin-live-recorder-webui:latest .

# 国内网络建议指定 pip 镜像源
docker build \
  --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
  -t douyin-live-recorder-webui:latest .

docker compose up -d
```

本仓库也配置了 GitHub Actions，向 `main` 分支推送会自动构建并发布镜像到 `ghcr.io/fnguu/douyin-live-recorder-webui`。

### NAS 部署

群晖、绿联等 x86 架构（Intel / AMD 处理器）NAS 可直接使用 [docker-compose.ugreen.yml](docker-compose.ugreen.yml)，该配置直接拉取现成镜像、不在 NAS 上构建，更适合算力有限的设备。详细步骤见 [WEBUI.md](WEBUI.md) 的 NAS 部署章节。

### 注意事项

1. 抖音等平台需要有效 Cookie，在网页「全局设置 → Cookie 凭证」里填写，否则会提示获取失败。
2. 建议使用 `ts` 格式保存，异常中断时文件仍可修复；容器停止已做优雅处理，但请避免 `docker kill`。
3. WebUI 默认没有认证，不要把 8000 端口直接暴露到公网，需要远程访问请设置 `WEBUI_PASSWORD`。
4. 删除容器不会删除录像，但要删除 `./downloads` 目录才会真正释放磁盘。

&emsp;

## 🤖相关项目

- StreamCap: https://github.com/ihmily/StreamCap
- streamget: https://github.com/ihmily/streamget

&emsp;

## ❤️贡献者

&ensp;&ensp; [![Hmily](https://github.com/ihmily.png?size=50)](https://github.com/ihmily)
[![iridescentGray](https://github.com/iridescentGray.png?size=50)](https://github.com/iridescentGray)
[![annidy](https://github.com/annidy.png?size=50)](https://github.com/annidy)
[![wwkk2580](https://github.com/wwkk2580.png?size=50)](https://github.com/wwkk2580)
[![missuo](https://github.com/missuo.png?size=50)](https://github.com/missuo)
<a href="https://github.com/xueli12" target="_blank"><img src="https://github.com/xueli12.png?size=50" alt="xueli12" style="width:53px; height:51px;" /></a>
<a href="https://github.com/kaine1973" target="_blank"><img src="https://github.com/kaine1973.png?size=50" alt="kaine1973" style="width:53px; height:51px;" /></a>
<a href="https://github.com/yinruiqing" target="_blank"><img src="https://github.com/yinruiqing.png?size=50" alt="yinruiqing" style="width:53px; height:51px;" /></a>
<a href="https://github.com/Max-Tortoise" target="_blank"><img src="https://github.com/Max-Tortoise.png?size=50" alt="Max-Tortoise" style="width:53px; height:51px;" /></a>
[![justdoiting](https://github.com/justdoiting.png?size=50)](https://github.com/justdoiting)
[![dhbxs](https://github.com/dhbxs.png?size=50)](https://github.com/dhbxs)
[![wujiyu115](https://github.com/wujiyu115.png?size=50)](https://github.com/wujiyu115)
[![zhanghao333](https://github.com/zhanghao333.png?size=50)](https://github.com/zhanghao333)
<a href="https://github.com/gyc0123" target="_blank"><img src="https://github.com/gyc0123.png?size=50" alt="gyc0123" style="width:53px; height:51px;" /></a>

&ensp;&ensp; [![HoratioShaw](https://github.com/HoratioShaw.png?size=50)](https://github.com/HoratioShaw)
[![nov30th](https://github.com/nov30th.png?size=50)](https://github.com/nov30th)
[![727155455](https://github.com/727155455.png?size=50)](https://github.com/727155455)
[![nixingshiguang](https://github.com/nixingshiguang.png?size=50)](https://github.com/nixingshiguang)
[![1411430556](https://github.com/1411430556.png?size=50)](https://github.com/1411430556)
[![Ovear](https://github.com/Ovear.png?size=50)](https://github.com/Ovear)
&emsp;

## ⏳提交日志

- 20251024
  - 修复抖音风控无法获取数据问题
  
  - 新增soop.com录制支持
  
  - 修复bigo录制
  
- 20250127
  - 新增淘宝、京东、faceit直播录制
  - 修复小红书直播流录制以及转码问题
  - 修复畅聊、VV星球、flexTV直播录制
  - 修复批量微信直播推送
  - 新增email发送ssl和port配置
  - 新增强制转h264配置
  - 更新ffmpeg版本
  - 重构包为异步函数！

- 20241130
  - 新增shopee、youtube直播录制
  - 新增支持自定义m3u8、flv地址录制
  - 新增自定义执行脚本，支持python、bat、bash等
  - 修复YY直播、花椒直播和小红书直播录制
  - 修复b站标题获取错误
  - 修复log日志错误
- 20241030
  - 新增嗨秀直播、vv星球直播、17Live、浪Live、SOOP、畅聊直播(原时光直播)、飘飘直播、六间房直播、乐嗨直播、花猫直播等10个平台直播录制
  - 修复小红书直播录制，支持小红书作者主页地址录制直播
  - 新增支持ntfy消息推送，以及新增支持批量推送多个地址（逗号分隔多个推送地址)
  - 修复Liveme直播录制、twitch直播录制
  - 新增Windows平台一键停止录制VB脚本程序
- 20241005
  - 新增邮箱和Bark推送
  - 新增直播注释停止录制
  - 优化分段录制
  - 重构部分代码
- 20240928
  - 新增知乎直播、CHZZK直播录制
  - 修复音播直播录制
- 20240903
  - 新增抖音双屏录制、音播直播录制
  - 修复PandaTV、bigo直播录制
- 20240713
  - 新增映客直播录制
- 20240705
  - 新增时光直播录制
- 20240701
  - 修复虎牙直播录制2分钟断流问题
  - 新增自定义直播推送内容
- 20240621
  - 新增Acfun、ShowRoom直播录制
  - 修复微博录制、新增直播源线路
  - 修复斗鱼直播60帧录制
  - 修复酷狗直播录制
  - 修复TikTok部分无法解析直播源
  - 修复抖音无法录制连麦直播
- 20240510
  - 修复部分虎牙直播间录制错误
- 20240508
  - 修复花椒直播录制
  - 更改文件路径解析方式 [@kaine1973](https://github.com/kaine1973)
- 20240506
  - 修复抖音录制画质解析bug
  - 修复虎牙录制 60帧最高画质问题
  - 新增流星直播录制
- 20240427
  - 新增LiveMe、花椒直播录制
- 20240425
  - 新增TwitchTV直播录制
- 20240424
  - 新增酷狗直播录制、优化PopkonTV直播录制
- 20240423
  - 新增百度直播录制、微博直播录制
  - 修复斗鱼录制直播回放的问题
  - 新增直播源地址显示以及输出到日志文件设置
- 20240311
  - 修复海外平台录制bug，增加画质选择，增强录制稳定性
  - 修复虎牙录制bug (虎牙`一起看`频道 有特殊限制，有时无法录制)
- 20240309
  - 修复虎牙直播、小红书直播和B站直播录制
  - 新增5个直播平台录制，包括winktv、flextv、look、popkontv、twitcasting
  - 新增部分海外平台账号密码配置，实现自动登录并更新配置文件中的cookie
  - 新增自定义配置需要使用代理录制的平台
  - 新增只推送开播消息不进行录制设置
  - 修复了一些bug
- 20240209
  - 优化AfreecaTV录制，新增账号密码登录获取cookie以及持久保存
  - 修复了小红书直播因官方更新直播域名，导致无法录制直播的问题
  - 修复了更新URL配置文件的bug
  - 最后，祝大家新年快乐！

<details><summary>点击展开更多提交日志</summary>

- 20240129
  - 新增猫耳FM直播录制
- 20240127
  - 新增千度热播直播录制、新增pandaTV(韩国)直播录制
  - 新增telegram直播状态消息推送，修复了某些bug
  - 新增自定义设置不同直播间的录制画质(即每个直播间录制画质可不同)
  - 修改录制视频保存路径为 `downloads` 文件夹，并且分平台进行保存。
- 20240114
  - 新增网易cc直播录制，优化ffmpeg参数，修改AfreecaTV输入直播地址格式
  - 修改日志记录器 @[iridescentGray](https://github.com/iridescentGray)
- 20240102
  - 修复Linux上运行，新增docker配置文件
- 20231210
  - 修复录制分段bug，修复bigo录制检测bug
  - 新增自定义修改录制主播名
  - 新增AfreecaTV直播录制，修复某些可能会发生的bug
- 20231207
  - 新增blued直播录制，修复YY直播录制，新增直播结束消息推送
- 20231206
  - 新增bigo直播录制
- 20231203
  - 新增小红书直播录制（全网首发），目前小红书官方没有切换清晰度功能，因此直播录制也只有默认画质
  - 小红书录制暂时无法循环监测，每次主播开启直播，都要重新获取一次链接
  - 获取链接的方式为 将直播间转发到微信，在微信中打开后，复制页面的链接。
- 20231030
  - 本次更新只是进行修复，没时间新增功能。
  - 欢迎各位大佬提pr 帮忙更新维护
- 20230930
  - 新增抖音从接口获取直播流，增强稳定性
  - 修改快手获取直播流的方式，改用从官方接口获取
  - 祝大家中秋节快乐！
- 20230919
  - 修复了快手版本更新后录制出错的问题，增加了其自动获取cookie(~~稳定性未知~~)
  - 修复了TikTok显示正在直播但不进行录制的问题
- 20230907
  - 修复了因抖音官方更新了版本导致的录制出错以及短链接转换出错
  - 修复B站无法录制原画视频的bug
  - 修改了配置文件字段，新增各平台自定义设置Cookie
- 20230903
  - 修复了TikTok录制时报644无法录制的问题
  - 新增直播状态推送到钉钉和微信的功能，如有需要请看 [设置推送教程](https://d04vqdiqwr3.feishu.cn/docx/XFPwdDDvfobbzlxhmMYcvouynDh?from=from_copylink)
  - 最近比较忙，其他问题有时间再更新
- 20230816
  - 修复斗鱼直播（官方更新了字段）和快手直播录制出错的问题
- 20230814
  - 新增B站直播录制
  - 写了一个在线播放M3U8和FLV视频的网页源码，打开即可食用
- 20230812
  - 新增YY直播录制
- 20230808
  - 修复主播重新开播无法再次录制的问题
- 20230807
  - 新增了斗鱼直播录制
  - 修复显示录制完成之后会重新开始录制的问题
- 20230805
  - 新增了虎牙直播录制，其暂时只能用flv视频流进行录制
  - Web API 新增了快手和虎牙这两个平台的直播流解析（TikTok要代理）
- 20230804
  - 新增了快手直播录制，优化了部分代码
  - 上传了一个自动化获取抖音直播间页面Cookie的代码，可以用于录制
- 20230803
  - 通宵更新 
  - 新增了国际版抖音TikTok的直播录制，去除冗余 简化了部分代码
- 20230724	
  - 新增了一个通过抖音直播间地址获取直播视频流链接的API接口，上传即可用
  </details>
  &emsp;

## 有问题可以提issue, 我会在这里持续添加更多直播平台的录制 欢迎Star
#### 
