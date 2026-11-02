# WebUI 控制台使用说明

在原版 [DouyinLiveRecorder](https://github.com/ihmily/DouyinLiveRecorder) 上增加的容器版管理界面。
不改录制核心逻辑、不改配置文件格式，网页里即可完成日常运维。

> **产出说明**：本项目的 WebUI 前后端、容器化配置与文档**均由 AI（WorkBuddy）生成**，
> 人工只负责需求描述与验收。录制核心沿用上游实现，未改动其平台接口与流解析逻辑。

![架构](https://img.shields.io/badge/base-v4.0.7-blue) ![python](https://img.shields.io/badge/python-3.11-blue) ![ui](https://img.shields.io/badge/WebUI-FastAPI%2B%E5%8E%9F%E7%94%9FJS-green)

---

## 1. 它能做什么

| 能力 | 说明 |
| --- | --- |
| 主播管理 | 网页添加 / 删除 / 批量粘贴导入，自动识别平台，备注名称、单独设置画质 |
| 单主播录制开关 | 每个主播一个开关，关闭后**正在录制的视频会安全收尾**（ffmpeg 正常结束，不产生损坏文件） |
| 单主播通知开关 | 单独控制该主播的开播 / 关播推送，不影响其它主播 |
| 全部设置项 | 录制与保存、网络与代理、推送通知、平台账号、Cookie 凭证（50 个平台）共 119 个配置项，自动生成的表单，保存即写回 `config/config.ini` |
| 实时状态 | 概览页显示录制进程心跳、监测数、正在录制数、磁盘剩余、最近录像文件 |
| 主播列表 | 表格展示：主播 / 直播平台 / 状态 / 最后直播时间 / 操作 |
| 录像回放 | 点主播名查看该主播的全部录像并在线播放，ts/mkv/flv 自动无损转封装，支持拖动进度条与下载 |
| 运行日志 | 网页直接看容器内运行输出 / 直播状态 / 调试日志，可自动刷新 |
| 零停机 | 改设置、加主播都不需要重启容器 |

界面为单页应用，浅色 / 深色主题自适应，无外部 CDN 依赖（容器断网也能正常显示）。

---

## 2. 快速开始

### 2.1 用 Docker Compose（推荐）

```bash
cd DouyinLiveRecorder-WebUI
docker compose up -d          # 首次会自动构建镜像
```

打开 <http://localhost:8000>，粘贴直播间地址即可开始。

查看状态 / 日志：

```bash
docker compose ps
docker compose logs -f
docker exec -it douyin-live-recorder tail -f /app/logs/recorder.log
```

停止（会等待正在录制的视频安全收尾）：

```bash
docker compose down
```

### 2.2 直接构建镜像

```bash
# 默认源
docker build -t douyin-live-recorder-webui:latest .

# 国内网络（pip 走清华源）
docker build \
  --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
  -t douyin-live-recorder-webui:latest .

# 只跑镜像
docker run -d --name dlr-webui -p 8000:8000 \
  -v "$PWD/config:/app/config" \
  -v "$PWD/downloads:/app/downloads" \
  -v "$PWD/logs:/app/logs" \
  --restart unless-stopped \
  douyin-live-recorder-webui:latest
```

### 2.3 由 GitHub Actions 自动出镜像

把本目录推到自己的 GitHub 仓库，`.github/workflows/build-image.yml` 会自动构建并推送到
`ghcr.io/<用户名>/<仓库名>:latest`（amd64 + arm64），也可在 Actions 页面手动触发。

拉取：

```bash
docker pull ghcr.io/<用户名>/<仓库名>:latest
```

> GHCR 的包默认私有，需在仓库 Packages 设置里改为 Public，或先 `docker login ghcr.io`。

### 2.4 不用 Docker 本地跑

```bash
pip install -r requirements.txt -r requirements-webui.txt
# 终端 1：录制器
DLR_NO_CLEAR=1 DLR_NONINTERACTIVE=1 python main.py
# 终端 2：WebUI
python -m uvicorn webui.app:app --host 0.0.0.0 --port 8000
```

需要本机已安装 `ffmpeg` 与 `node`（部分平台签名脚本依赖 Node）。

### 2.5 绿联 NAS（UGOS Pro）

绿联 NAS 的 x86 机型（Intel / AMD 处理器）与 CI 构建出的 `linux/amd64` 镜像架构一致，不需要交叉编译或额外参数。

**第一步：把镜像弄到 NAS 上**

GHCR 在国内经常超时，按下面顺序选一种：

**A. 本地电脑导出再导入（最稳，推荐）**

```bash
# 在有代理的电脑上
docker login ghcr.io -u fnguu          # 密码用 PAT（勾 read:packages）
docker pull ghcr.io/fnguu/douyin-live-recorder-webui:latest
docker save ghcr.io/fnguu/douyin-live-recorder-webui:latest -o dlr-webui.tar
```

把 `dlr-webui.tar`（约 1.3 GB）用绿联文件管理器 / SMB 传到 NAS，然后 SSH 到 NAS：

```bash
docker load -i /volume1/docker/dlr-webui.tar
docker images | grep douyin-live-recorder
```

**B. 把镜像包设为公开**

仓库 → Packages → `douyin-live-recorder-webui` → Package settings → Change visibility → Public，
然后 NAS 上直接 `docker pull ghcr.io/fnguu/douyin-live-recorder-webui:latest`
（仓库本身可以继续保持私有，只有镜像包公开）。国内直连 ghcr 仍然可能超时，需要 NAS 有代理。

**C. 给 NAS 配好代理后直接拉**

UGOS 的「网络设置」里可以填 HTTP 代理；Docker 守护进程如需单独配置，可给 `docker.service` 加
`HTTP_PROXY`/`HTTPS_PROXY` 环境变量后重启服务。

**第二步：建项目**

图形界面：打开「Docker」→ 左侧「项目」→ 创建 → 项目名 `douyin-live-recorder` →
存储空间选放录像的那个卷 → 选择导入/编辑 compose，把 `docker-compose.ugreen.yml` 的内容整段粘进去 → 部署。

SSH 方式：

```bash
mkdir -p /volume1/docker/douyin-live-recorder
cd /volume1/docker/douyin-live-recorder
# 把 docker-compose.ugreen.yml 传进来并改名为 docker-compose.yml
docker compose up -d
```

**第三步：访问**

浏览器打开 `http://NAS的IP:8000`，粘一个直播间地址试录。

**绿联上容易遇到的几个点**

| 问题 | 处理 |
| --- | --- |
| 端口冲突 | 8000 被占就把映射改成 `"8080:8000"`（只改左边宿主机端口，`WEBUI_PORT` 保持 8000） |
| 目录权限 | 容器以 root 运行，挂载目录会自动创建；若提前手动建了目录且属主不对，`chown -R root:root` 一下 |
| 录像占空间 | `./downloads` 指向容量大的卷，或改「直播保存路径」到挂载点内的子目录 |
| 海外平台 | 需要代理时填 `HTTP_PROXY`，代理地址用 NAS 同网段机器的 IP，不能用 `127.0.0.1` |
| 更新镜像 | push 后等 Actions 变绿，NAS 上 `docker compose pull && docker compose up -d` |

---


## 3. 目录结构

```
.
├── main.py                     # 原版录制主程序（含少量 WebUI 挂钩，见第 6 节）
├── webui/
│   ├── app.py                  # FastAPI 应用与 REST 接口
│   ├── schema.py               # 设置项元数据（分组 / 控件类型 / 说明）
│   ├── config_io.py            # config.ini 无损读写（保留注释、顺序、BOM）
│   ├── anchors.py              # 主播列表：状态文件 + URL_config.ini 行级增删改
│   ├── runtime.py              # 录制进程心跳、日志、磁盘读取
│   ├── bridge.py               # 录制进程侧桥接（仅标准库，被 main.py 导入）
│   ├── paths.py                # 路径、URL 规范化、平台识别
│   └── static/                 # 前端单页（index.html / app.js / style.css）
├── docker/
│   ├── entrypoint.sh           # 初始化挂载目录 + 启动 supervisord
│   └── supervisord.conf        # 两个进程：recorder + webui
├── Dockerfile
├── docker-compose.yml
├── docker-compose.ugreen.yml   # 绿联 NAS 专用（拉现成镜像，不在 NAS 上构建）
├── requirements-webui.txt      # fastapi + uvicorn
└── config/
    ├── config.ini              # 全局设置（WebUI 原地修改）
    ├── URL_config.ini          # 主播列表（录制器唯一数据源）
    └── webui_state.json        # WebUI 元数据：通知开关、备注、排序（自动生成）
```

容器内两个进程由 supervisor 托管，任一崩溃自动拉起：

```
supervisord
 ├── recorder : python -u main.py
 └── webui    : uvicorn webui.app:app --port ${WEBUI_PORT}
```

---

## 4. 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `WEBUI_PORT` | `8000` | WebUI 端口，改了记得同步 compose 的端口映射 |
| `WEBUI_PASSWORD` | 空 | 设置后启用 HTTP Basic 认证，远程访问务必设置 |
| `TZ` | `Asia/Shanghai` | 时区，影响文件名时间戳 |
| `HTTP_PROXY` / `HTTPS_PROXY` | 空 | 录制海外平台时用，宿主机代理写 `http://host.docker.internal:7890` |
| `DLR_NO_CLEAR` | `1` | 容器内不清屏，日志按行输出（镜像已设） |
| `DLR_NONINTERACTIVE` | `1` | 主播列表为空时不阻塞等待键盘输入（镜像已设） |

---

## 5. 使用要点

**添加主播**：粘贴浏览器里的直播间地址即可，支持短链。备注名留空则显示平台昵称（录制器抓到名字后自动补全）。

**关闭录制**：把「录制」开关拨掉。如果它正在录制，会在 1~2 秒内给 ffmpeg 发中断信号完成封装，然后退出监测。

**关闭通知**：拨掉「通知」开关，只影响该主播的推送，全局推送渠道仍在设置里配置。注意这是**监听级**开关——如果该主播在关闭前已经推送过"开播"，关播通知也会一起被抑制。

**改画质 / 改名字**：未开播时会自动重建监测线程，立即生效；正在录制时保持当前录制不变，下次开播生效（避免打断写入中的文件）。

**设置项**：左侧按分组切换，「保存设置」只提交改动过的项，写回 `config/config.ini` 并自动备份到 `backup_config/`。深色底的小字标出该项在 ini 中的 `节 · 键`，方便对照官方文档。

**录像回放**：点主播名进入录像列表。ts/mkv/flv 浏览器无法直接播放，后端会用容器内的 ffmpeg 以 `-c copy`无损转封装成 mp4 再返回（首次点击等几秒，之后走缓存秒开），进度条可拖动，也可以直接下载原文件。关闭播放窗口会立即停止播放与声音。

**日志**：三个视图分别对应 `logs/recorder.log`（容器标准输出）、`logs/PlayURL.log`（开播/关播事件）、`logs/streamget.log`（调试）。

**版本号**：WebUI 采用「基版本 + 修订号」，格式 `1.0.0.N`，N 是影响 WebUI 的改动次数（初版记为 1）。
当前版本可在「系统状态 → 系统信息 → WebUI 版本」看到，下方「WebUI 更新记录」列出每次修订的内容。
维护方式：改动后往 `webui/version.py` 的 `REVISIONS` 末尾追加一条，版本号自动取列表长度，不需要手改数字。

---

## 6. 实现原理与对原版的改动

原版录制器每轮循环都会重新读取 `config/URL_config.ini`；录制过程中每秒检查当前地址是否被 `#` 注释，被注释就给 ffmpeg 发 SIGINT 安全收尾。这正好是一个天然的无损开关，因此 WebUI 的主播操作全部落在这个文件上：

| 操作 | 对文件的影响 | 生效方式 |
| --- | --- | --- |
| 添加 | 追加一行 `画质,地址,主播: 名称` | 主循环下一轮拉起线程并立即检测 |
| 停止录制 | 行首加 `#` | 录制中 1~2 秒安全收尾；等待中下一轮退出 |
| 开始录制 | 去掉行首 `#` | 主循环重建线程，立即检测 |
| 删除 | 先加 `#`，等线程退出后删除整行 | 避免留下僵尸线程 |
| 改画质/名字 | 替换该行内容 | 未开播时立即重建线程 |

`main.py` 只有 5 处小改动，全部可开关、缺失时自动降级：

1. 导入 `webui.bridge`（失败则退化为原版行为）。
2. `display_info()` 每 5 秒把运行时状态写入 `/tmp/dlr_runtime.json`（心跳、监测列表、每线程状态），供 UI 展示；容器模式下改为每 60 秒输出一行摘要，不再清屏刷日志。
3. 推送前调用 `ui_notify_ok(url)`，实现按主播的推送开关。
4. 主播列表为空且无交互终端时不再卡在 `input()`。
5. `SIGTERM` 时先等待 ffmpeg 收尾（最长 15 秒）再退出，`docker stop` 不会损坏录像。

`config.ini` 采用按行原地改写，保留原有注释、顺序、空行与 BOM，不会像 `configparser.write()` 那样把文件重排。

---

## 7. 常见问题

**Q：打开页面提示 401？** 设置了 `WEBUI_PASSWORD`，浏览器会弹出登录框，密码即该值。

**Q：加了主播但一直"等待开播"？** 录制器轮询间隔由设置里的「循环时间(秒)」控制（默认 300 秒）。新加的主播会立即检测一次，之后每 300 秒查一次，可酌情调小。

**Q：抖音一直"获取失败"？** 需要有效的抖音 Cookie，在「全局设置 → Cookie 凭证 → 抖音 Cookie」填入。Cookie 过期会表现为获取失败。

**Q：海外平台无法录制？** 给容器配代理环境变量，并在「网络与代理」里填写代理地址、勾选启用代理；「走代理的平台」里列出的平台才会使用代理。

**Q：录像文件在哪？** 默认 `/app/downloads`，映射到宿主机 `./downloads`。播放器/目录规则由「录制与保存」里的选项控制。

**Q：容器能录多少路？** 受「同一时间访问网络的线程数」限制（默认 3），不是硬上限，但并发过高容易触发平台风控。

**Q：升级了上游代码怎么办？** 把新版 `src/`、`main.py` 里的 5 处改动（见第 6 节）重新套一遍即可；`webui/` 目录与上游完全解耦。设置项若上游新增，WebUI 会自动识别并归入"其他"分组，无需改代码。

---

## 8. 推送到 GitHub 自动构建

本目录已经是初始化好的 git 仓库（分支 `main`，含 `.gitattributes` 强制脚本 LF 换行，避免
容器里出现 `bash\r: No such file`）。配好凭证后一条命令即可推送，推送后 Actions 自动构建镜像：

本仓库：<https://github.com/fnguu/DouyinLiveRecorder-webui>
镜像：`ghcr.io/fnguu/douyin-live-recorder-webui:latest`

```bash
./push.sh https://github.com/<你的用户名>/douyin-live-recorder-webui.git
```

推送后：

| 项目 | 说明 |
| --- | --- |
| 触发条件 | push 到 `main` / `master`、打 `v*` tag，或在 Actions 页面手动 Run workflow |
| 产出镜像 | `ghcr.io/<用户名>/<仓库名>:latest`（默认 `linux/amd64`；Actions 手动触发时可填 `linux/amd64,linux/arm64`） |
| 构建耗时 | 首次约 5~10 分钟（含 apt + node + pip 安装），后续走缓存约 2 分钟 |
| 进度查看 | 仓库 → Actions → Build WebUI Image |

拿到镜像后：

```bash
docker pull ghcr.io/<用户名>/<仓库名>:latest
docker tag ghcr.io/<用户名>/<仓库名>:latest douyin-live-recorder-webui:latest
docker compose up -d
```

> **私有仓库注意**：GHCR 的镜像包默认是私有的，要免登录拉取需改成公开：
> 仓库 → Packages → 选中镜像包 → Package settings → Change visibility → Public。
> 不想公开也行，先 `echo $TOKEN | docker login ghcr.io -u <用户名> --password-stdin`。

**凭证要求**

- HTTPS + PAT：fine-grained token，权限至少 `Contents: Read and write`；
  若要修改仓库里的 workflow 文件，还需 `Workflows: Read and write`。
- 或直接用 SSH：`./push.sh git@github.com:<用户名>/<仓库名>.git`（需已配置 SSH key）。
- 把 token 写进 remote URL 会留在 `.git/config` 里，用完建议还原：
  `git remote set-url origin https://github.com/<用户名>/<仓库名>.git`

**仓库可见性提醒**：`config/config.ini` 里有 Cookie 与推送密钥的模板值（上游自带的已过期）。
新建仓库建议设为 **私有**；若要公开，请先把 `config/config.ini` 里的 Cookie、token 清空再提交。

---

## 9. 安全提示

- WebUI 默认无认证，**不要直接把 8000 端口暴露到公网**；需要远程访问请设置 `WEBUI_PASSWORD`，或只监听内网 / 走反向代理加 TLS。
- `config/config.ini` 含有 Cookie 与推送密钥，挂载目录请注意权限，不要提交到公开仓库。
- 本项目仅供个人学习与备份使用，请遵守各平台服务条款及当地法律法规，勿用于侵权或商业传播。
