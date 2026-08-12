<p align="center">  
  <img src="resources/img/logo.png" width="120" alt="logo">
</p>

<h1 align="center">qqmusic-plugin</h1>

> 从 Yunzai-Bot [qqmusic-plugin](https://github.com/zaras123/qqmusic-plugin) 移植而来的 AstrBot 版本

## 功能一览

| 分类 | 指令 | 说明 |
|------|------|------|
| 🎵 点歌 | `#qqm点歌 关键词` | 搜索列表，`#qqm听N` 选曲播放 |
| 🎵 点歌 | `#qqm播放 关键词` | 直接播放第一条 |
| 🎵 点歌 | `#qqm歌词 关键词` | 歌词查询 |
| 🎵 点歌 | `#qqm热搜` | 热搜榜 |
| 🎵 点歌 | `#qqm帮助` | 帮助图片卡片 |
| 🎬 MV | `#qqmMV 搜索 关键词` | 搜索 MV 列表 |
| 🎬 MV | `#qqmMV 播放1` / `下载1` | 播放 / 下载列表第 N 个 MV |
| 🎬 MV | `#qqmMV` | MV 分类浏览；点歌后再发 `#qqmMV 播放/下载` 直接操作本曲 MV |
| 🎧 发现 | `#qqm排行 榜单名` | 排行榜（飙升/热歌/新歌等） |
| 🎧 发现 | `#qqm新歌` | 新歌速递（1内地 2欧美 3日本 4韩国 5最新 6港台） |
| 🎧 发现 | `#qqm推荐` | 热门推荐歌单 |
| 🎧 发现 | `#qqm来首歌` | 随机推荐一首并播放 |
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
| 🔐 登录 | `#qqm登录qq` | QQ音乐 App 扫码（MQTT 备用通道） |
| 🔐 登录 | `#qqm绑定 qqmusic://...` | DeepLink 导入（主人） |
| 🔐 登录 | `#qqm状态` / `#qms` | 登录状态卡片 |
| 🔐 登录 | `#qqm登出` | 清除登录态（主人） |
| 🔐 登录 | `#qqm刷新` | 续期 key（主人） |
| ⚙️ 配置 | `#qqm设置` | 查看当前配置 |
| ⚙️ 配置 | `#qqm api <地址>` | 设置 API 地址（主人） |
| ⚙️ 配置 | `#qqm 音质 flac` | 设置最高音质（主人） |
| ⚙️ 配置 | `#qqm 开启/关闭 点歌/解析` | 功能开关（主人） |
| ⚙️ 配置 | `#qqm 测试` | 测试 API 连通（主人） |
| ⚙️ 配置 | `#qqm 账号` | 已登录账号列表（主人） |
| 🔄 更新 | `#qqm更新` | 拉取最新插件代码（主人） |
| 🔄 更新 | `#qqm强制更新` | 丢弃本地改动并同步远程（主人） |
| 🔄 更新 | `#qqm更新日志` | 查看最近提交（主人） |

### 音质选项

`128` / `m4a` / `320` / `flac` / `ape` / `hires` / `atmos` / `master` / `atmos_master`

默认 `auto`：自动匹配歌曲最高可用音质，支持逐级降级。

## 安装

### 1. 安装插件

将本插件放到 AstrBot 的 `data/plugins/`

```bash
cd AstrBot/data/plugins
git clone https://github.com/wbndmqaq/astrbot_plugin_qqmusic
```

重启 AstrBot，日志出现加载即成功

### 2. 配置 API

本插件需要配合后端 API 使用。API 不开源，需自行解决（或者去用户群申请）。

在 AstrBot 管理面板的「插件配置」中填写：
- **API 地址**（apiBase）
- **API Token**（apiToken，与 API 端 `QQMUSIC_API_TOKEN` 一致）

## 📮 用户群

QQ 群：[点击加入](https://qm.qq.com/q/GKxEVvF8Ua)

- API 地址和 Token 申请
- 使用问题反馈
- 更新通知

或主人发送：`#qqm api <地址>`。

### 3. 登录

主人发送 `#qqm登录` 无感扫码即可开始使用（一张 QQ 码，QQ / QQ音乐 App 通用；微信场景用 `#qqm登录微信`，QQ音乐 App 备用通道 `#qqm登录qq`）。

## 卡片渲染

自 v1.5.0 起卡片渲染改为**本地 Playwright**。首次使用需安装浏览器内核：

```bash
pip install playwright
python -m playwright install chromium
```

渲染失败时会自动回退为纯文本展示，不影响点歌/解析功能。

## 平台适配

支持平台：`aiocqhttp`、`qq_official`、`telegram`、`dingtalk`、`lark`、`kook`、`discord`、`weixin_oc`。

| 平台 | 文本/图片/卡片 | 语音 | 文件 | 视频 (MV) | 原生/自定义音乐卡 |
|---|---|---|---|---|---|
| QQ 个人号 (aiocqhttp) | ✅ | ✅ | ✅ | ✅ | ✅（send_api） |
| QQ 官方 (qq_official) | ✅ | ✅（silk 转码，失败回退文件） | ✅ | ✅（落盘发送，卡片与视频合并省额度） | ❌ |
| 微信个人号 (weixin_oc) | ✅ | ❌ | ✅ | ✅ | ❌ |
| Telegram | ✅ | ✅ | ✅ | ✅ | ❌ |
| 飞书 (lark) | ✅ | ✅ | ✅ | ✅ | ❌ |
| 钉钉 (dingtalk) | ✅ | ✅ | ✅ | ✅ | ❌ |
| KOOK | ✅ | ✅ | ✅ | ✅ | ❌ |
| Discord | ✅ | ✅ | ✅ | ✅ | ❌ |

- 视频（MV）发送降级链：`Video.fromURL` 直发 → 失败落盘发**下载文件**（`File`，全平台支持）→ `Video` 组件重试 → 链接文本；下载命令直接发文件；受限平台跳过 URL 直发。
- 原生/自定义音乐卡依赖 OneBot `send_api`，仅 `aiocqhttp` 可用；其余平台自动跳过。
- `qq_official` 为被动回复受限平台：文案与首个媒体合并发送以省被动回复额度，语音失败自动回退文件。
- `weixin_oc`（微信个人号）出站不支持语音，自动跳过语音只发文件。
- 卡片渲染为本地 Playwright，各平台均以图片形式发送。

## 项目结构

```
astrbot_plugin_qqmusic/
├── main.py              # 入口：所有指令与解析处理器
├── api.py               # API客户端
├── quality.py           # 音质档位与自适配降级
├── delivery.py          # 音频下载与语音/文件投递
├── cards.py             # 会话存储、隐私脱敏、卡片数据、文本兜底
├── render.py            # 本地 Playwright 渲染（模板 → PNG）
├── tpl_adapter.py       # art-template → Jinja2 模板适配
├── updater.py           # git 自更新
├── _conf_schema.json    # 配置 Schema（管理面板）
├── metadata.yaml        # 插件元数据
└── resources/
    ├── html/            # 7 个卡片模板（原样保留）
    └── img/             # logo
```

## 免责声明

本项目仅供技术学习与交流使用。不提供任何音源服务，API 由用户自行解决；使用者应遵守所在地区法律法规及相关平台用户协议；因使用本项目产生的一切后果由使用者自行承担。

## License

MIT
