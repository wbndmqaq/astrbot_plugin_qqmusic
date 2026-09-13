<p align="center">  
  <img src="resources/img/logo.png" width="120" alt="logo">
</p>

<h1 align="center">qqmusic-plugin</h1>

> 从 Yunzai-Bot [qqmusic-plugin](https://github.com/zaras123/qqmusic-plugin) 移植而来的 AstrBot 版本

---

## 功能一览

| 分类 | 指令 | 说明 |
|------|------|------|
| 🎵 点歌 | `#qqm点歌 关键词` | 搜索列表，`#qqm听N` 选曲播放 |
| 🎵 点歌 | `#qqm播放 关键词` | 直接播放第一条 |
| 🎵 点歌 | `#qqm听所有` | 连播当前列表（最多 30 首） |
| 🎵 点歌 | `#qqm歌词 关键词 / 链接 / 序号` | 歌词查询（支持 songDetail 链接与序号取当前列表歌词） |
| 🎵 点歌 | `#qqm热搜` | 热搜榜 |
| 🎵 点歌 | `#qqm帮助` | 帮助图片卡片 |
| 🎬 MV | `#qqmMV 搜索 关键词` | 搜索 MV 列表 |
| 🎬 MV | `#qqmMV 播放 序号` / `#qqmMV 下载 序号` | 播放 / 下载列表第 N 个 MV（写成 `播放/下载 序号` 会被当 vid，请分开写） |
| 🎬 MV | `#qqmMV 分类 [序号/分类名]` | 不带参数列出 MV 分类；点歌后再发 `#qqmMV 播放` 直接操作本曲 MV |
| 🎧 发现 | `#qqm排行 榜单名` | 排行榜（飙升/热歌/新歌等） |
| 🎧 发现 | `#qqm新歌` | 新歌速递（1内地 2欧美 3日本 4韩国 5最新 6港台） |
| 🎧 发现 | `#qqm推荐` | 热门推荐歌单 |
| 🎧 发现 | `#qqm推荐听 序号` | 打开推荐列表中第 N 个歌单的歌曲 |
| 🎧 发现 | `#qqm来首歌` / `#qqm随机` / `#qqm放一首` / `#qqm来一首` | 随机推荐一首并播放 |
| 🎧 发现 | `#qqm电台` | 个性电台 5 首 |
| 🎧 发现 | `#qqm日推` | 每日推荐（需登录） |
| 🎧 发现 | `#qqm收藏` | 我的收藏（需登录） |
| 🎧 发现 | `#qqm歌手 关键词` | 搜索歌手，展示热门歌曲 |
| 🎧 发现 | `#qqm专辑 关键词` | 搜索专辑，展示曲目列表 |
| 🎧 发现 | `#qqm歌单 关键词` | 搜索歌单，展示歌曲 |
| 🎧 发现 | `#qqm评论 关键词` | 查看歌曲热门评论 |
| 🔗 解析 | 群内发 QQ 音乐分享 / 链接 | 自动识别并下载播放 |
| 🔗 解析 | 专辑 / 歌单 / 歌手链接 | 自动识别并展示歌曲列表 |
| 🔐 登录 | `#qqm登录` | 无感扫码，一张 QQ 码覆盖 QQ / QQ音乐 App（主人，主通道） |
| 🔐 登录 | `#qqm登录微信` | 无感扫码（微信码，备用） |
| 🔐 登录 | `#qqm登录qq` | QQ音乐 App 扫码（备用通道） |
| 🔐 登录 | `#qqm绑定 qqmusic://...` | DeepLink 导入（主人） |
| 🔐 登录 | `#qqm导入 qqmusic://...` | 等价于 `#qqm绑定`；直接发 qqmusic:// 链接也会自动导入 |
| 🔐 登录 | `#qqm状态` / `#qms` | 登录状态卡片 |
| 🔐 登录 | `#qqm登出` | 清除登录态（主人） |
| 🔐 登录 | `#qqm同步` | 从 API 侧同步登录态到插件配置（主人） |
| 🔐 登录 | `#qqm刷新` | 续期 key（主人） |
| ⚙️ 配置 | `#qqm设置` | 查看当前配置 |
| ⚙️ 配置 | `#qqm api <地址>` | 设置 API 地址（主人） |
| ⚙️ 配置 | `#qqm 音质 flac` | 设置最高音质（主人） |
| ⚙️ 配置 | `#qqm 开启点歌` / `#qqm 关闭点歌` | 功能开关（点歌/解析，注意「开启」与目标之间**不能加空格**）（主人） |
| ⚙️ 配置 | `#qqm 测试` | 测试 API 连通（主人） |
| ⚙️ 配置 | `#qqm 账号` | 已登录账号列表（主人） |

---

### 音质选项

`128` / `m4a` / `320` / `flac` / `ape` / `hires` / `atmos` / `master` / `atmos_master`

默认 `auto`：自动匹配歌曲最高可用音质，支持逐级降级。

> 📌 **关于高音质（FLAC 以上）**：`hires` / `atmos` / `master` / `atmos_master` 是 **QQ 音乐绿钻会员限定**，且部分歌曲未上架这些音质。若显式设置了高音质却被降级，点歌卡片会直接标明原因（如「API 未返回链接（通常需绿钻会员）」即账号非会员；「该歌曲未提供此音质」即歌曲本身没出该档）。

> 📌 **关于语音发送**：FLAC 等高音质文件直接作为语音消息会被协议端以体积/格式拒绝，插件发送语音前会自动用 **ffmpeg** 压成紧凑 mp3（需系统安装 ffmpeg，`ffmpeg -version` 可验证；缺失时自动回退原始文件）。若 **PC QQ 播放不了高清语音**，可在管理面板开启「禁用高清语音」（`disableHighQualityVocal`），语音改发 mono16k 低音质，PC 可正常播放；群文件仍保留高音质。

---

## 安装&配置API教程

### 1.安装

AstrBot WebUI → 插件管理 → 搜索 `astrbot_plugin_qqmusic` → 安装。


重启 AstrBot，日志出现加载即成功

### 2. 配置 API

本插件需要配合后端 API 使用。API 不开源，需自行解决（或者去用户群申请）。

**API 端前置条件**（不满足会连不上或被拒，插件只负责把错误原文转达）：

| 项 | 说明 |
| --- | --- |
| 监听端口 | 由 `PORT` / `QQMUSIC_PORT` 决定，API 项目默认 `3300`；`apiBase` 按你实际部署填，如 `http://127.0.0.1:3300` |
| 主密钥 | `QQMUSIC_API_TOKEN` 必须设置；插件侧 `apiToken` 填同一个值，不一致会被鉴权拒绝 |
| IP 白名单 | `QQMUSIC_ALLOW_IPS`（逗号分隔）。插件与 API 不同机时，务必把插件所在机器的出口 IP 加进去，否则 403 |
| 限流 | `QQMUSIC_RATE_LIMIT`，默认 120/min/IP，超限后短时间内请求会被拒 |

在 AstrBot 管理面板的「插件配置」中填写：
- **API 地址**（apiBase）
- **API Token**（apiToken，与 API 端 `QQMUSIC_API_TOKEN` 一致）

或主人发送：`#qqm api <地址>`。

### 3. 登录

主人发送 `#qqm登录` 无感扫码即可开始使用（一张 QQ 码，QQ / QQ音乐 App 通用；微信场景用 `#qqm登录微信`，QQ音乐 App 备用通道 `#qqm登录qq`）。

---

## 用户群

API申请QQ群：[点击加入](https://qm.qq.com/q/GKxEVvF8Ua)
bug反馈QQ群：[点击加入](https://qm.qq.com/q/8sOZdZTnaw)

- API 地址和 Token 申请
- 使用问题反馈
- 更新通知

---

## 卡片渲染

自 v1.5.0 起卡片渲染改为**本地 Playwright** 截图。（不自动安装，需要图片卡片时请按下面步骤手动安装（约 1~2 分钟）。）

### ① 安装 playwright Python 包

```bash
pip install playwright
# 国内网络可用清华镜像：
pip install playwright -i https://pypi.tuna.tsinghua.edu.cn/simple
```

### ② 下载 Chromium 浏览器内核

```bash
python -m playwright install chromium
# 国内网络可用 npmmirror 加速：
PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright/ python -m playwright install chromium
```

Windows PowerShell 写法：

```powershell
$env:PLAYWRIGHT_DOWNLOAD_HOST="https://npmmirror.com/mirrors/playwright/"
python -m playwright install chromium
```

### ③（仅 Linux / Docker 容器）安装系统运行库

仅当启动渲染时报 `libnspr4` / `libnss3` / `error while loading shared libraries` 才需要，需 root：

```bash
python -m playwright install-deps chromium
```

或手动安装系统库：

```bash
apt-get update && apt-get install -y \
  libnspr4 libnss3 libgbm1 libasound2 \
  libatk-bridge2.0-0 libatk1.0-0 libcairo2 libcups2 libdrm2 \
  libx11-xcb1 libxcb1 libxcomposite1 libxdamage1 libxfixes3 \
  libxkbcommon0 libxrandr2 libxext6 libpango-1.0-0
```

容器内 apt 官方源下载慢？可选换阿里镜像源后再装：

```bash
# Debian 12 (bookworm)
sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources
# Ubuntu 22.04
sed -i 's|archive.ubuntu.com|mirrors.aliyun.com|g' /etc/apt/sources.list
apt-get update
```

### ④ 重载插件

WebUI → 插件管理 → 本插件 → 重载。

环境未就绪时，日志会输出一次上述完整教程，所有指令自动回退纯文本展示，不影响点歌/解析/播放功能；安装完成后重载即可正常出图。

---

## 平台适配

支持平台：`aiocqhttp`、`qq_official`、`telegram`、`dingtalk`、`lark`、`kook`、`discord`、`weixin_oc`。

| 平台 | 文本/图片/卡片 | 语音 | 文件 | 视频 (MV) | 原生/自定义音乐卡 |
|---|---|---|---|---|---|
| QQ 个人号 (aiocqhttp) | ✅ | ✅ | ✅ | ✅ | ✅（`call_action` 直发 music 段） |
| QQ 官方 (qq_official) | ✅ | ✅（silk 转码，失败回退文件） | ✅（>10MB 自动分片上传，需 AstrBot ≥4.27.3） | ✅（落盘发送，卡片与视频合并省额度） | ❌ |
| 微信个人号 (weixin_oc) | ✅ | ❌ | ✅ | ✅ | ❌ |
| Telegram | ✅ | ✅ | ✅ | ✅ | ❌ |
| 飞书 (lark) | ✅ | ✅ | ✅ | ✅ | ❌ |
| 钉钉 (dingtalk) | ✅ | ✅ | ✅ | ✅ | ❌ |
| KOOK | ✅ | ✅ | ✅ | ✅ | ❌ |
| Discord | ✅ | ✅ | ✅ | ✅ | ❌ |

- 视频（MV）发送降级链：`Video.fromURL` 直发 → 失败落盘发**下载文件**（`File`，全平台支持）→ `Video` 组件重试 → 链接文本；下载命令直接发文件；受限平台跳过 URL 直发。`aiocqhttp` 下文件发送失败时改走 base64 直发（跨容器 `file://` 不可见），不再用同一套 File 组件重试。
- 原生/自定义音乐卡通过 OneBot `call_action` 直发 `music` 消息段（OneBot 侧不存在 `send_api`），仅 `aiocqhttp` 可用；其余平台自动跳过。
- `aiocqhttp` 语音不走插件侧 silk 转码：插件把紧凑 mp3 以 OneBot v11 标准 `record` 段（base64）直发，由协议端（NapCat / SnowLuma 等）自带 ffmpeg 转 Tencent silk，时长按原始音频精确探测。
- `aiocqhttp`（NapCat/LLOneBot 等 NTQQ 系）群文件对 `.flac` 常报「未知文件类型或路径不存在」，自动优先传压缩 mp3（`_压缩版.mp3`）；其余平台群文件保留原始高音质文件。
- `qq_official` 为被动回复受限平台：文案与首个媒体合并发送以省被动回复额度，语音失败自动回退文件；**大文件（>10MB）自动走 QQ 分片文件上传**（需 AstrBot ≥4.27.3，管理面板 `qqofficialChunkedUpload` 可关；关闭后大文件回退压缩 mp3 兜底）。
- `weixin_oc`（微信个人号）出站不支持语音，自动跳过语音只发文件。
- 卡片渲染为本地 Playwright，各平台均以图片形式发送。

---

## 目录结构

```text
astrbot_plugin_qqmusic/
├── main.py                  # 插件生命周期入口与路由绑定
├── __init__.py              # 顶层包入口
├── metadata.yaml            # 插件元信息（v2.0.3）
├── _conf_schema.json        # 配置定义 Schema
├── README.md                # 插件文档
├── CHANGELOG.md             # 更新日志
├── requirements.txt         # Python 依赖
├── core/                    # 核心业务服务层
│   ├── __init__.py          # 包说明（不在包级再导出，避免导入即拉整条依赖链）
│   ├── service.py           # 门面/组合根：保留全部公开签名并薄委托（实现见下列各模块）
│   ├── login.py             # 扫码登录：两条二维码通道（App/MQTT、无感 webqr）+ 一套轮询骨架
│   ├── resolve.py           # 分享卡片/链接解析流程（卡片→短链→专辑/歌单/歌手/单曲→投递）
│   ├── panels.py            # 状态卡/设置卡数据与脱敏展示（uin/key/登录方式/平台名）
│   ├── utils.py             # 跨模块通用小工具（safe_int；无内部依赖，避免循环导入）
│   ├── api/                 # qqmusic-api 客户端包（_core HTTP 会话与鉴权 + 按域端点模块）
│   ├── cards.py             # 会话存储（SessionStore）/ 卡片数据构造 / API 地址脱敏与文本兜底
│   ├── messages.py          # 用户可见文案常量（跨文件重复使用的提示语）
│   ├── delivery.py          # 音频下载与多平台分发（含 aiocqhttp/QQ 官方专用发送通道）
│   ├── quality.py           # 音质常量与标签映射
│   ├── render.py            # Playwright HTML 渲染引擎
├── handlers/                # 声明式指令路由层（按领域解耦）
│   ├── __init__.py          # 聚合导出 ALL_ROUTES
│   ├── base.py              # 声明式 Route 基类与 AstrBot 精准匹配安装器
│   ├── play_cmds.py         # 播放类指令（点歌/听N/连播/播放）
│   ├── explore_cmds.py      # 探索与榜单指令（排行/新歌/MV/推荐/电台/日推/歌手/专辑/歌单等）
│   ├── detail_cmds.py       # 详情指令（歌词/评论/收藏）
│   ├── auth_cmds.py         # 登录指令（扫码登录/WebQR/DeepLink/状态/登出/账号）
│   ├── system_cmds.py       # 系统与设置指令（帮助/设置/音质/API/开关/测试）
│   └── share_cmds.py        # 链接、小程序与卡片分享解析（EventMessageType.ALL）
└── resources/               # HTML/CSS 渲染卡片与静态资源
```

---

## ⚙️ 配置项

在 AstrBot 管理面板「插件配置」中可改（多数项改完即时生效；`quality` 与开关类指令也可用命令改）：

| 配置项 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `apiBase` | string | `""`（**必填**） | 后端 API 地址，如 `http://127.0.0.1:3300` |
| `apiToken` | string | `""` | 与 API 端 `QQMUSIC_API_TOKEN` 一致，不一致会被鉴权拒绝 |
| `enable` | bool | `true` | 插件总开关：关闭后点歌/播放/歌词/榜单/解析/登录不再响应；`#qqm热搜`/`#qqm帮助` 与配置类指令保留 |
| `enableSongRequest` | bool | `true` | 点歌功能开关 |
| `enableResolve` | bool | `true` | 分享/链接自动解析总开关 |
| `resolveCards` | bool | `true` | 自动解析 QQ 音乐分享卡片 |
| `resolveLinks` | bool | `true` | 自动解析 y.qq.com 链接 |
| `maxList` | int | `10` | 点歌列表最大显示条数（1-20） |
| `quality` | string | `auto` | 最高播放音质：`auto/128/m4a/320/flac/ape/hires/atmos/master/atmos_master` |
| `qualityFallback` | bool | `true` | 音质自动降级（逐级降档直至拿到可用链接） |
| `sendNativeCard` | bool | `false` | 发送 OneBot 原生音乐卡片（部分协议支持） |
| `sendCustomCard` | bool | `false` | 发送自定义音乐卡片（部分协议支持） |
| `sendVocal` | bool | `true` | 以语音消息方式发送音频 |
| `disableHighQualityVocal` | bool | `false` | 禁用高清语音（PC QQ 播放不了高清语音时开启；仅影响语音） |
| `uploadFile` | bool | `true` | 以群/好友文件方式发送音频 |
| `downloadTimeout` | int | `120000` | 音频/视频下载超时（毫秒） |
| `keepFileSec` | int | `60` | 临时文件保留秒数（下限 5 秒；`0` 表示尽快删除） |
| `identifyPrefix` | string | `识别：` | 识别提示前缀 |
| `qrLoginEnable` | bool | `true` | 允许使用 `#qqm登录` 扫码登录命令 |
| `qqofficialAdapt` | bool | `true` | QQ 官方机器人专用适配（合并消息、语音回退文件、跳过原生卡） |
| `qqofficialChunkedUpload` | bool | `true` | QQ 官方大文件分片上传（需 AstrBot ≥ 4.27.3） |
| `renderListCard` | bool | `true` | 将点歌/列表结果渲染为图片卡片（关闭则纯文本） |
| `sendTextInfo` | bool | `true` | 投递音频时附带文本信息（歌名/歌手/音质） |

> 临时文件（音频/卡片图/二维码）固定存放在 `data/plugin_data/astrbot_plugin_qqmusic/temp`，
> 不再提供 `tempDir` 配置项——插件目录会在更新/重装时被整体替换。

## 免责声明

本项目仅供技术学习与交流使用。不提供任何音源服务，API 由用户自行解决；使用者应遵守所在地区法律法规及相关平台用户协议；因使用本项目产生的一切后果由使用者自行承担。

---

## License

本项目采用 [MIT](LICENSE) 许可证开源。

---

<div align="center">

如果觉得这个插件对你有帮助，欢迎 Star 一下哈哈

</div>

