"""用户可见文案常量。

同一句文案在全仓出现 ≥2 次时统一放这里，各调用点引用常量，避免再次漂移。
一次性文案留在各自调用点。
"""

from __future__ import annotations

# ──────────── 会话类型引导 ────────────

# 榜单分类（topCategory）/ 推荐歌单（recommend）会话中误用序号类指令时的引导
SESSION_TOP_CATEGORY_HINT = "当前是榜单分类列表，请先 #qqm排行 榜单名 列出歌曲"
SESSION_RECOMMEND_HINT = "当前是推荐歌单列表，请先 #qqm推荐听序号 列出歌曲"

# ──────────── MV ────────────

NO_MV_HINT = "该曲没有 MV"
# MV 列表底部提示：播放与下载必须分别带序号书写；写成「播放/下载 序号」会被
# 解析成 vid="/下载" 并必然失败，故此处只给单动词写法。
MV_LIST_HINT = "发 #qqmMV 播放 序号 或 #qqmMV 下载 序号"

# ──────────── 点歌 / 登录 ────────────

NO_SONG_FOUND = "没有搜到相关歌曲"
NO_REFRESH_WARN = "⚠️ 无 refresh，过期后需重新扫码"
SCAN_LOGIN_HINT = "发送 #qqm登录 扫码绑定"
POLL_FAIL_MAX_HINT = "轮询失败过多，请检查 API 或自行获取ck"
# 解析出列表（专辑/歌单/歌手）后的收尾引导
LISTEN_TO_PLAY_HINT = "发送 #qqm听序号 播放"

# 配置写盘失败（不得再回复「已设置/已更新/已开启」，避免假成功）
CONFIG_SAVE_FAIL_HINT = (
    "⚠ 配置写入失败（本机 AstrBot 版本可能过旧），"
    "请到 WebUI 插件配置中修改，或升级 AstrBot"
)

# 平台不支持语音（delivery._NO_VOCAL_PLATFORMS）且未开启群文件发送：音频下载了却
# 发不出去，必须明确告知用户（否则既没有语音也没有文件，用户只会以为「正在发送」）
NO_VOCAL_NO_FILE_HINT = (
    "当前平台不支持语音且未开启群文件发送，请开启 uploadFile 或换平台"
)


def out_of_range(total: int) -> str:
    """序号越界提示（所有列表类指令共用同一句式）。"""
    return f"请选择 1-{total}"


def listen_reply_hint(verb: str) -> str:
    """选歌列表底部的「回复 #qqm听N 即可 <动作>」提示。"""
    return f"回复 #qqm听N 即可{verb}"

# ──────────── 探索类指令的通用失败提示 ────────────


TOP_FAIL_HINT = "排行失败，请稍后重试"
RECOMMEND_FAIL_HINT = "推荐失败，请稍后重试"
SCAN_LOGIN_DISABLED = "扫码登录已在配置中关闭"
UNKNOWN_ERROR = "未知错误"


def mv_search_fail(err) -> str:
    """MV 搜索失败提示（搜索 / 分类浏览 / 裸关键词三个入口共用）。"""
    return f"MV 搜索失败：{err}"


def qr_fetch_fail(reason) -> str:
    """二维码获取失败提示（MQTT 通道与 webqr 通道共用）。"""
    return f"获取二维码失败：{reason or UNKNOWN_ERROR}"


# ──────────── 帮助清单（帮助卡片与纯文本兜底共用同一份数据）────────────

# 帮助卡片的 sections：卡片模板（qqmusic-help.html 的 {{each data.sections}}）与
# ``cards.format_help_text`` 消费的是同一份数据，静态清单只此一份，不会再出现
# 「卡片改了、文本没改」的措辞分叉。调用方取用副本（build_help_card_data 内部
# deepcopy），避免卡片数据被下游修改后污染这份全局常量。
HELP_SECTIONS: list = [
    {
        "title": "点歌播放",
        "tag": "全员",
        "items": [
            {
                "name": "搜索点歌",
                "desc": "按关键词搜索并展示列表",
                "example": "#qqm点歌 七里香",
            },
            {
                "name": "选择曲目",
                "desc": "播放当前列表第 N 首",
                "example": "#qqm听1",
            },
            {
                "name": "连播列表",
                "desc": "依次发送当前列表全部歌曲（上限 30 首）",
                "example": "#qqm听所有",
            },
            {
                "name": "直接播放",
                "desc": "搜索并立即播放第一条",
                "example": "#qqm播放 晴天",
            },
            {
                "name": "查看歌词",
                "desc": (
                    "按歌名/mid/链接取歌词；"
                    "点歌后 #qqm歌词1 取列表第 1 首歌词"
                ),
                "example": "#qqm歌词 七里香",
            },
            {
                "name": "热搜榜",
                "desc": "查看 QQ 音乐热搜",
                "example": "#qqm热搜",
            },
        ],
    },
    {
        "title": "MV 专区",
        "tag": "视频",
        "items": [
            {
                "name": "MV 搜索",
                "desc": "搜索 MV 并展示列表",
                "example": "#qqmMV 搜索 周杰伦",
            },
            {
                "name": "MV 播放",
                "desc": "播放列表第 N 个 MV（无参数=本曲 MV）",
                "example": "#qqmMV 播放 1",
            },
            {
                "name": "MV 下载",
                "desc": "下载列表第 N 个 MV",
                "example": "#qqmMV 下载 1",
            },
            {
                "name": "MV 分类",
                "desc": "按分类 / 标签浏览 MV（可跟 序号 或 分类名）",
                "example": "#qqmMV 分类",
            },
        ],
    },
    {
        "title": "发现音乐",
        "tag": "探索",
        "items": [
            {
                "name": "排行榜",
                "desc": "查看各大榜单歌曲",
                "example": "#qqm排行 飙升",
            },
            {
                "name": "新歌速递",
                "desc": "最新歌曲（1内地 2欧美 3日本 4韩国 5最新 6港台）",
                "example": "#qqm新歌",
            },
            {
                "name": "推荐歌单",
                "desc": "热门推荐歌单列表",
                "example": "#qqm推荐",
            },
            {
                "name": "打开推荐歌单",
                "desc": "查看第 N 个推荐歌单的歌曲",
                "example": "#qqm推荐听1",
            },
            {
                "name": "随机推荐",
                "desc": "随机推荐一首歌并播放（也可 #qqm随机 / #qqm放一首 / #qqm来一首）",
                "example": "#qqm来首歌",
            },
            {
                "name": "个性电台",
                "desc": "根据口味推荐 5 首",
                "example": "#qqm电台",
            },
            {
                "name": "每日推荐",
                "desc": "每日推荐歌曲（需登录）",
                "example": "#qqm日推",
            },
            {
                "name": "我的收藏",
                "desc": "查看收藏歌曲（需登录）",
                "example": "#qqm收藏",
            },
            {
                "name": "歌手搜索",
                "desc": "搜索歌手并展示热门歌曲",
                "example": "#qqm歌手 周杰伦",
            },
            {
                "name": "专辑搜索",
                "desc": "搜索专辑并展示曲目列表",
                "example": "#qqm专辑 叶惠美",
            },
            {
                "name": "歌单搜索",
                "desc": "搜索歌单并展示歌曲",
                "example": "#qqm歌单 华语流行",
            },
            {
                "name": "歌曲评论",
                "desc": "查看歌曲热门评论",
                "example": "#qqm评论 晴天",
            },
        ],
    },
    {
        "title": "账号状态",
        "tag": "登录",
        "items": [
            {
                "name": "无感扫码",
                "desc": "主通道：一张 QQ 码覆盖 QQ / QQ音乐 App",
                "example": "#qqm登录",
            },
            {
                "name": "微信扫码",
                "desc": "无感扫码（微信码，备用）",
                "example": "#qqm登录微信",
            },
            {
                "name": "App 扫码",
                "desc": "QQ音乐 App 扫码（备用通道）",
                "example": "#qqm登录qq",
            },
            {
                "name": "状态卡片",
                "desc": "账号 / 会员 / 音质 可视化",
                "example": "#qqm状态",
            },
            {"name": "快捷状态", "desc": "状态卡短指令", "example": "#qms"},
            {"name": "登出解绑", "desc": "清除登录态", "example": "#qqm登出"},
            {
                "name": "续期 key",
                "desc": "刷新登录 key（无需重新扫码）",
                "example": "#qqm刷新",
            },
            {
                "name": "同步登录态",
                "desc": "从 API 侧同步登录态到插件",
                "example": "#qqm同步",
            },
            {
                "name": "DeepLink 绑定",
                "desc": "用 qqmusic:// 链接导入登录态",
                "example": "#qqm绑定 qqmusic://…",
            },
            {
                "name": "导入登录态",
                "desc": "直接发 qqmusic:// 链接自动导入",
                "example": "#qqm导入 qqmusic://…",
            },
            {
                "name": "账号列表",
                "desc": "查看已登录账号",
                "example": "#qqm账号",
            },
        ],
    },
    {
        "title": "主人管理",
        "tag": "Master",
        "items": [
            {
                "name": "查看配置",
                "desc": "API、开关、音质与发送方式",
                "example": "#qqm设置",
            },
            {
                "name": "设置 API",
                "desc": "修改接口地址",
                "example": "#qqm api <地址>",
            },
            {
                "name": "切换音质",
                "desc": "128 / 320 / flac / hires …",
                "example": "#qqm 音质 flac",
            },
            {
                "name": "功能开关",
                "desc": "开启或关闭点歌、解析",
                "example": "#qqm 开启点歌",
            },
            {
                "name": "连通测试",
                "desc": "探测 API 是否可用",
                "example": "#qqm 测试",
            },
        ],
    },
    {
        "title": "智能解析",
        "tag": "自动",
        "items": [
            {
                "name": "分享卡片",
                "desc": "群内 QQ 音乐分享自动识别",
                "example": "（发送音乐卡片）",
            },
            {
                "name": "链接解析",
                "desc": "y.qq.com 链接自动取链播放",
                "example": "https://y.qq.com/…",
            },
        ],
    },
]
