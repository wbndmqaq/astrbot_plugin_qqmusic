# 更新日志 (CHANGELOG)

---
## [v1.7.1] - 指令描述全覆盖 + 测试基建 + 石山清理 + 全仓 ruff 零告警

*   **📋 指令描述全覆盖**: 34/36 个 handler 缺失 docstring（WebUI 指令列表显示「无描述」）全部补齐，
    现有 2 个 + 新增 34 个，覆盖点歌/发现/MV/解析/登录/管理全部分区。
*   **🧪 测试基建**: 新增 `test_commands.py`（本地桩 aiohttp/astrbot，`python test_commands.py` 直接运行），
    169 项覆盖命令路由正则、链接/卡片解析、音质降级提示、平台检测（含分片上传能力判定）、配置 schema、
    文件名清洗、`_play_view` 归一化、handler docstring 全覆盖。
*   **🐛 修复**: `#qqm 测试` 连通测试指令无法触发——正则 `^#?(qqm测试|qq音乐测试)$` 缺 `\s*`，
    与 README/帮助文档写的 `#qqm 测试`（带空格）不一致；改为兼容 `#qqm测试` / `#qqm 测试` / `#qqm ping`。
*   **🧹 死代码清理**: `SessionStore.set` 的 `ttl_sec` 参数从未使用（`get` 走 `cls.TTL`）、
    `_handle_response` 的 `base`/`url` 死参数、无占位符 f-string、mv handler 3 处局部 import 提升到顶层。
*   **🗻 石山重构**: 新增 `_play_view()` 归一化两处重复的 play 视图 dict（顺带补上解析流程缺失的 `mvVid`）；
    `deliver_song` 压缩文件名 `comp_display` 重复计算合并为 `use_compressed` 单一分支。
*   **✨ 全仓 ruff 零告警**: 清理 29 处 E501（ruff 按显示宽度计，CJK 双宽——中文长字符串/注释折行、
    嵌套三元平铺、UA 长串拼接）；顺带 17 处非 E501（SIM105→`contextlib.suppress`、E741→`l` 改 `ln`、
    E731→lambda 改 def、S311/S701→noqa 注释、B904→`raise from`、SIM108→三元）。
    `ruff check .` 全绿；`test_commands.py` 用 `[lint.per-file-ignores]` 豁免测试断言长行。

---
## [v1.7.0] - 对齐 JS 版语音压缩/禁用高清语音/音质降级提示/链接解析加固

*   **🔇 禁用高清语音开关**: 新增配置 `disableHighQualityVocal`（默认关）。PC QQ 播放不了 44.1k 立体声语音时开启，
    语音改发 mono16k/32k 低音质（QQ 语音标准规格），`#qqm设置` 卡片发送区显示开关状态。
*   **🗜 语音 ffmpeg 压缩**: FLAC 等高音质文件直接作语音(Record)会被协议端以体积/格式拒绝，新增
    `prepare_vocal_file` 先 ffmpeg 压成紧凑 mp3（44100Hz/立体声），体积 ≤5MB 且已在直传白名单时跳过转码；
    压缩产物与原始文件同样纳入定时清理，temp 目录不累积。ffmpeg 缺失时自动回退原始文件，功能不受影响。
*   **📁 OneBot 群文件改传压缩版**: aiocqhttp（NapCat/LLOneBot 等 NTQQ 系）群文件对 `.flac` 常报「未知文件类型或路径不存在」，
    与语音压缩解耦——只要语音开启或 OneBot 群文件开启即生成压缩 mp3，OneBot 群文件优先传压缩版（`_压缩版.mp3`），
    其余平台仍保留原始高音质文件。自定义音乐卡补发 `singer` 字段；原生卡失败时自动降级为自定义卡（不再双卡连发）。
*   **🚀 QQ 官方分片文件上传**: 对齐 AstrBot 4.27.3 `QQOfficialChunkedUploader`——qq_official 下 >10MB 大文件
    （FLAC 等无损/长时音频、MV 视频）自动发原始文件走适配器分片上传，修复旧版大文件无法发送的问题。
    新增配置 `qqofficialChunkedUpload`（默认开）；AstrBot <4.27.3 或关闭时自动检测并回退压缩 mp3 兜底，
    不再直发必败的大文件。版本能力经 `astrbot.__version__ ≥ 4.27.3` 判定。
*   **📉 音质降级提示**: 显式请求 `hires/master` 等高音质被降级时，详情卡直接标注原因
    （`API 未返回链接（通常需绿钻会员）` / `该歌曲未提供此音质` / `播放链接不可用`），对齐 JS 版 `buildDegradeNote`。
*   **🔗 链接解析加固**（对齐 JS 版 `875b1af`）:
    *   URL 提取正则排除中文标点，不再吞链接后的「，很好听」之类；
    *   `c6.y.qq.com` 移动端短链跟随重定向拿最终 songDetail 链接，重定向仅允许 QQ 音乐域名白名单
        （y.qq.com/qq.com/gtimg.cn/url.cn/qpic.cn），防 SSRF；
    *   `parse_qqmusic_ids` 支持 `song_mid`/`songMid`/`mid`、`mediaMid`/`mediaid` 参数变体与
        `/song`、`/playsong.html` 路径形态。

---
## [v1.6.0] - 对齐 JS 版 MV/新歌/新端点 + 修复 v1.5.0 加载崩溃

*   **🎬 MV 专区**: 新增 `#qqmMV 搜索/播放/下载/分类`（/search t=12、/mv/category、/mv/tag、/mv/url），
    点歌列表自动打 🎬 徽标（/song/info 批量查询），播放后可直接 `#qqmMV 播放/下载` 操作本曲 MV；
    MV 详情卡复用 detail 模板（无付费/免费误标、真实时长与亿/万播放量）；视频发送直发 URL → 落盘 → 链接兜底。
*   **🎵 新歌速递**: 新增 `#qqm新歌`（1内地 2欧美 3日本 4韩国 5最新 6港台，/song/new）
*   **📋 日推/收藏**: 切换至 `/recommend/daily`、`/user/liked` 专用端点（失败自动回退旧 /cgi 接口），并升级为列表卡片渲染
*   **🃏 列表卡**: 小提示并入常用指令（听序号/歌词/MV/列表有效期），含 MV 时展示 `#qqmMV 播放 序号` 指令；帮助卡新增 MV 专区
*   **🐛 修复 v1.5.0 加载崩溃**: `bind_manual`/`set_quality` 两处孤立引号导致 `IndentationError`（与 v1.4.1 同类问题），
    修复后插件恢复可加载
*   **🔧 修复**: 播放详情卡 `mvVid` 透传丢失导致「本曲 MV」操作失效

---
## [v1.5.0] - 本地 Playwright 渲染 + 代码清理 + 无感扫码登录（webqr 通道）+ 三轮审查修复

*   **🐛 修复 9 个 bug**：
    *   会话类型污染：`#qqm听N` 在排行分类/推荐歌单会话抛 KeyError → `choose_song` 校验会话类型，`topCategory` 提示先查榜单；新增 `_expand_recommend`，`#qqm推荐听N`（死指令）正式可用，推荐歌单一键展开。
    *   卡片 PNG 永久累积：渲染成功后 120s 延迟清理，temp 目录不再无限增长。
    *   强制更新静默假成功：分支非 main/master 时 reset 失败会返回错误（不再继续 `git clean` 误删）；clean 失败也检查。
    *   分享卡片 JSON 非 dict（meta/music/news 为字符串或字段畸形）导致解析崩溃 → isinstance 兜底 + str 转换。
    *   登录轮询 5 处：`pollInterval` 非数值防御、`expired/cancel/loginFailed` 静默终态补提示、`userMessage` 重复转发去重 + fatal 正则收紧（`\binvalid\b` 带词边界）、`_active_logins` 并发注册竞态（`_register_task` 先停旧任务 + `_pop_task_if_current` 身份校验，旧 tick 不再误删新任务条目）。
    *   命令+链接双处理：`\b` 对 CJK 后缀失效（Python `\w` 含汉字）→ 负向断言 `(?![\dA-Za-z_])`，`#qqm点歌 xxx + 链接` 不再同时触发解析。
*   **🧹 死代码清理**：删 `quality.size_new_for_quality`、`SessionStore.clear`、`_reply_card_or_text.fallback_text` 死参数、`render.viewport_width` 死参数、status/settings/detail/list 卡 20+ 无读输出键、会话 `title`/`user_id` 32 个只写不读键、KV `lastLogin*` 4 键只写不读、`quality_candidates` 恒真 `or "128"`、冗余 CJK 判断、`tpl_adapter` 两处同文分支、`api_token` 死别名；`sendTextInfo` 补进 `_conf_schema.json`（此前被配置剥离机制吞掉、开关形同虚设）。
*   **🗻 石山重构**：`api.py` 新增 `unwrap_data`/`payplay_of`/`singer_text` 公共 helper（剥壳 9 处、会员标记 4 处、singer 拼接 4 处统一）；`_handle_response` 401/403/429 状态映射双份合并提前；三层剥壳 `unwrap_data(unwrap_data(...))`；`_pick_login_success` 三同构分支合一；`_is_qqmusic_message` 六 if 合成单正则；`statMode`/`subtitle` 嵌套三元平铺；`qualityLabel` 复用 `_quality_label`；投递 options 常量 `_SKIP_ALL`；`save_config` 静默吞加日志 ×3；`song_url_best` 双 except 合并、`recommend_hot` 死内层 try 删除。
*   **⚠️ 过程中修复**: 批量替换脚本误把 `unwrap_data` 函数体自身替换成递归调用（已修复并回归验证）。

*   **🔍 与原版（qqmusic-plugin）逐模块对比修复**：
    *   热搜文本兜底传错数据类型（收到卡片 dict 而非列表，回退时恒显示"暂无热搜"）→ `format_text` 闭包捕获原始列表。
    *   `chooseSong`/`playDirect` 原版会按配置发原生/自定义音乐卡，移植版 `_SKIP_ALL` 全禁 → 拆分 `_SKIP_TEXT`（仅跳过文本，卡片按配置发送）。
    *   命令路由变体补齐：`#qqm登陆状态`、`#qqm(同步|拉取|sync)(登录态)?`、`#qqm(刷新|续期|refresh)(登录|key)?`、`#qqm accounts`/`#qqm 账号`（`\s*`）。
    *   updater：`formatGitError` 友好错误映射（鉴权/冲突/分叉/非仓库/网络）、`@{u}` 上游探测、commit 一致性"已是最新"判断、更新结果补分支/仓库/重启提示、requirements.txt 变更提示、更新日志表头补仓库地址。
    *   状态卡补 Token 绑定槽四分支（`bound`/`userKey`，对齐原版 status-card）。
    *   resolve 加高优先级（`priority=8`）抢先解析，避免与其他插件重复处理。
    *   `#qqm设置` 文本兜底补版本号。

*   **🔌 平台适配扩展**: `support_platforms` 增加 `telegram`/`dingtalk`/`lark`/`kook`/`discord`/`weixin_oc`。被动回复受限平台泛化为 `qq_official`（weixin_oc 是微信个人号，具备完整主动/被动能力，不属于受限）；`weixin_oc` 出站不支持语音（Record），自动跳过语音只发文件（README 平台矩阵同步）。
*   **🐛 修复**: weixin_oc 等无语音平台下临时文件清理泄漏（语音被跳过、文件分支因 `want_vocal` 不再清理）——统一在媒体发送后调度清理。

*   **✨ 渲染改造**: 卡片渲染改为**本地 Playwright**（`render.py`），模板经 `tpl_adapter` 转 Jinja2 后本地渲染。
*   **✨ 新增**: 无感扫码登录（webqr 通道，走 API 浏览器自动化，对齐 JS 版 `75b459d`）。命令精简（对齐 `f4a3f14`/`fa8299d`/`eefbf3c`/`f9a798b`）：
    *   `#qqm登录` 统一走 webqr **主通道**——一张 QQ 码覆盖 QQ / QQ音乐 App 用户（平台取巧，官方无通用码）；
    *   `#qqm登录微信` 无感扫码（微信码，备用）；
    *   `#qqm登录qq` / `#qqm登录app` → MQTT 通道（QQ音乐 App 扫码，备用以防万一）；
    *   删除 `#qqm扫码` / `#qqmweb` / `#qqm网页` / `#QQ音乐网页登录` 冗余命令。
*   **配置**: 新增 `playwright`、`jinja2` 依赖；首次使用需 `python -m playwright install chromium`（见 README）。渲染失败自动回退纯文本。
*   **修复**: 命令路由——webqr 与 MQTT 规则无交集（对齐 JS 版 `040f5bc`）；保留 `_pick_login_success`（MQTT 轮询依赖，JS 版误删后 `#qqm登录qq` 必崩，`f9a798b` 教训）。
*   **修复**: `render.py` 空 `page.evaluate()` 调用（缺 `expression` 参数抛 `TypeError`，导致所有卡片渲染失败回退纯文本）；补全 JS 版浅绿底 + `fit-content` 样式设置。
*   **修复**: 搜索/歌单/电台结果中 `payplay` 会员标记取值逻辑（旧表达式第二个 `isinstance` 永远为假导致 `pay_play` 分支不可达，且 `pay` 为非 dict 真值时 `.get()` 会抛 `AttributeError`）。
*   **修复**: 移除已废弃的 `@register` 装饰器（版本号与 metadata 不一致），改为 AstrBot 自动识别插件类。
*   **清理**: 删除死代码——`updater._mask_remote`、`quality.SIZE_NEW_INDEX`/`is_quality_available`/`summarize_file_sizes`、`api` 中 5 个无调用函数（`singer_album`/`album_detail`/`user_songlists`/`user_collect_songlists`/`cgi_proxy`）、`cards.format_settings_text`、`delivery.build_music_filename` 的 `ascii_only`/`include_quality` 死参数、`main._get_local_version` 重复实现等。
*   **重构**: 重构多处运算符优先级石山（`albummid`/`cover`/`payplay`/`creator` 三元嵌套）、`recommend_feed`/`personal_radio` 重复取值链、`_handle_resolve` 专辑/歌单 songs 重复构造、`deliver_song` 语音/文件发送分支冗余。
*   **工具**: 接入 ruff（新增 `ruff.toml`，豁免插件防御性写法 BLE001/S110）；修复 70 处 lint（含 async 内阻塞写盘改 `asyncio.to_thread`、类级可变默认加 `ClassVar`、`re.I` 统一 `re.IGNORECASE` 等），全量 `ruff format` 格式化。`ruff check` / `ruff format --check` 双零告警。
*   **优化**: 帮助/设置/README 命令表同步精简后的登录命令（`#qqm登录` / `#qqm登录微信` / `#qqm登录qq`）。

---
## [v1.4.1] - 修复 v1.4.0 插件无法加载的问题

*   **修复**: 修复 `delivery.py` 中 `_send_media` 内层函数一处缩进错误（`comps` 行多缩进一格），该错误导致模块导入即抛 `IndentationError`，且 `main.py` 顶层 `from .delivery import deliver_song` 直接受影响——v1.4.0 插件在 AstrBot 中整体无法加载。修复后点歌、语音/文件投递等功能全部恢复正常。

---
## [v1.4.0] - 卡片渲染优化+扫码登录重构

*   **✨ 新增**: 独立「评论卡片」模板（`qqmusic-comment`），每条评论展示头像、昵称、时间、内容与赞数；评论文本自动清洗表情代码（`[em]...[/em]`）、`[音频]` 等标记与 `\r\n` 转义。
*   **✨ 新增**: API Token 支持从环境变量 `QQMUSIC_API_TOKEN` 读取（与 JS 版对齐）。
*   **优化**: 卡片改为全页渲染（`full_page=True`）+ 自动裁剪两侧空白，修复锁死 720px 视口导致长卡片（评论/歌单等）被截断的问题；渲染结果下载为本地文件，发送改用 `Image.fromFileSystem`。
*   **优化**: QQ 官方适配器下语音与文件「双发」互不阻塞，语音发送失败不再影响文件投递。
*   **修复**: 扫码登录轮询重构——
    *   `elapsed` 参数改为毫秒；`isFirstScan` 仅首次轮询为 true，避免后端误判「二维码被另一 APP 扫描」；
    *   轮询间隔改用 API 返回的 `pollInterval`（默认 2s，超出 0~30s 范围回退）；
    *   过滤无真实用户（`userID`/`openid` 等为空）的伪扫码状态，不再误报「已扫码」；
    *   检测到「已被其它 APP 扫描 / 二维码失效」等致命消息时立即停止轮询并提示重新登录；
    *   登录成功改为 `status=="success"` 即停止轮询，不再等待 `hasKey`（临时 key 稍后异步升级）；
    *   目前扫码登录推荐使用QQ音乐 APP 稳定性更佳。
*   **修复**: `_send_chain` 发送异常时记录完整堆栈并降级为纯文本兜底重发，避免整个 handler 崩溃。
*   **修复**: 分享卡片 JSON 宽松解析（兼容 `\"` 转义乱象）、排行榜详情解析防御非 dict、日推/收藏未登录判断兼容 `err.code == -1`。
*   **修复**: 音频扩展名推断对齐 JS 版（显式扩展名优先，新增 `.ape` 识别）；URL 探测 `Origin` 头拼写错误（`y.yml.com` → `y.qq.com`）。

---
## [v1.2.0] - QQ 官方机器人适配+修复消息发出

*   **✨ 新增**: 支持 QQ 官方机器人（`qq_official` 适配器）。
*   **修复**: 全部出站发送改用 `MessageChain` 包装，修正裸传单组件在新版 AstrBot 上 `AttributeError` 的问题（影响所有平台）。
*   **配置**: 新增 `qqofficialAdapt` 开关（默认开）。

---
## [v1.1.0] - 添加许可证

*   **🎉 发布**: 插件许可证发布。


## [v1.0.0] - 初始版本

*   **🎉 发布**: 插件初始版本发布。
