from __future__ import annotations

import re
import time
from urllib.parse import urlparse

from .quality import QUALITY_LABEL


# ──────────── 会话存储 ────────────


class SessionStore:

    _mem: dict = {}
    TTL = 600

    @classmethod
    def _key(cls, scope: str) -> str:
        return f"qqmusic:song:{scope}"

    @classmethod
    async def get(cls, plugin, scope: str) -> dict | None:
        k = cls._key(scope)
        # 先查内存（同进程最快）
        mem_val = cls._mem.get(str(scope))
        if mem_val:
            ts = mem_val.get("updatedAt") or 0
            if time.time() - ts < cls.TTL:
                return mem_val
            cls._mem.pop(str(scope), None)
        # 再查 KV（跨进程持久）
        try:
            raw = await plugin.get_kv_data(k, None)
            if raw:
                if isinstance(raw, str):
                    import json
                    raw = json.loads(raw)
                ts = raw.get("updatedAt") or 0
                if time.time() - ts < cls.TTL:
                    return raw
                await plugin.delete_kv_data(k)
        except Exception:
            pass
        return None

    @classmethod
    async def set(cls, plugin, scope: str, session: dict, ttl_sec: int = TTL) -> dict:
        data = {"group_id": scope, "updatedAt": time.time(), **session}
        cls._mem[str(scope)] = data
        k = cls._key(scope)
        try:
            import json
            await plugin.put_kv_data(k, json.dumps(data, ensure_ascii=False))
        except Exception:
            pass
        return data

    @classmethod
    async def clear(cls, plugin, scope: str) -> None:
        cls._mem.pop(str(scope), None)
        try:
            await plugin.delete_kv_data(cls._key(scope))
        except Exception:
            pass


# ──────────── 隐私脱敏 ────────────


def mask_api_base(url: str) -> str:
    u = str(url or "").strip()
    if not u:
        return "****"
    try:
        parsed = urlparse(u)
        host = parsed.hostname or ""
        masked_host = "***"
        last_dot = host.rfind(".")
        if last_dot > 0:
            masked_host = "***" + host[last_dot:]
        port = f":{parsed.port}" if parsed.port else ""
        path_part = parsed.path if (parsed.path and parsed.path != "/") else ""
        return f"{parsed.scheme}://{masked_host}{port}{path_part}"
    except Exception:
        return "****"


def api_hint_for(cfg: dict) -> str:
    if not cfg.get("apiBase"):
        return "API 未配置"
    return f"API · {mask_api_base(cfg['apiBase']).replace('https://', '').replace('http://', '')}"


# ──────────── 文本格式化（纯文本兜底） ────────────


def format_song_list(lst: list, title: str, start_idx: int = 0) -> str:
    if not isinstance(lst, list) or not lst:
        return f"♫ {title}\n\n📭 暂无数据\n可能原因：\n1. API 未启动或网络异常\n2. 账号未登录（需要 #qqm登录）\n3. 请求超时，请稍后重试"
    lines = [f"♫ {title}"]
    for i, s in enumerate(lst):
        idx = start_idx + i + 1
        is_vip = bool(s.get("payplay")) or (s.get("pay") or {}).get("pay_play") if isinstance(s.get("pay"), dict) else bool(s.get("payplay"))
        is_paid = isinstance(s.get("pay"), dict) and s.get("pay").get("pay_down") and not is_vip
        tag = " [会员]" if is_vip else (" [付费]" if is_paid else "")
        dur = f" ({s['duration']})" if s.get("duration") else ""
        name = s.get("songName") or s.get("title") or s.get("name") or "未知"
        singer = s.get("singerName") or s.get("singer") or "未知"
        lines.append(f"{idx}. {name} - {singer}{tag}{dur}")
    lines.append(f"\n发送 #qqm听序号 播放（共{len(lst)}首）")
    return "\n".join(lines)


def format_list_text(lst: list) -> str:
    lines = []
    for i, s in enumerate(lst):
        pay = " [付费]" if s.get("payplay") else ""
        dur = f" ({s['duration']})" if s.get("duration") else ""
        singer = s.get("singerName") or s.get("singer") or ""
        lines.append(f"{i + 1}. {s.get('songName', '')} - {singer}{pay}{dur}")
    return f"♫ QQ音乐点歌结果（#qqm听序号 或 #听序号）\n" + "\n".join(lines)


def format_hot_text(items: list) -> str:
    lst = (items if isinstance(items, list) else [])[:15]
    if not lst:
        return "暂无热搜"
    out = []
    for i, item in enumerate(lst):
        word = item.get("k") or item.get("keyword") or item.get("query") or item.get("name") or item.get("title") or str(item)
        out.append(f"{i + 1}. {word}")
    return "QQ音乐热搜\n" + "\n".join(out)


def format_lyric_text(data: dict) -> str:
    song_name = data.get("songName")
    singer = data.get("singerName")
    head = f"歌词：{song_name or '未知'} - {singer or '未知'}\n" if (song_name or singer) else ""
    body = "\n".join(data.get("lines") or [])
    return (head + body).strip() or "暂无歌词"


def format_detail_text(song: dict, *, quality_label: str = "", has_url: bool = False) -> str:
    is_vip = bool(song.get("payplay"))
    lines = [f"♪ {song.get('songName') or '未知'} - {song.get('singerName') or '未知'}{' [会员/付费]' if is_vip else ''}"]
    if song.get("albumName"):
        lines.append(f"专辑：{song['albumName']}")
    if song.get("duration"):
        lines.append(f"时长：{song['duration']}")
    if quality_label:
        lines.append(f"音质：{quality_label}")
    if not has_url and is_vip:
        lines.append("⚠️ 该曲需会员，请 #qqm登录")
    return "\n".join(lines)


def format_settings_text(data: dict) -> str:
    return "\n".join([
        "【QQ音乐插件配置】",
        f"enable: {data.get('enableRaw') is not False}",
        f"apiBase: {data.get('apiBase')}",
        f"login: {data.get('loginText')}",
        f"adapter: {data.get('adapterName')} ({data.get('adapterKind')})",
        f"点歌: {data.get('song')}  解析: {data.get('resolve')}  列表卡: {data.get('listCard')}",
        f"音质: {data.get('qualityKey')}（自动降级: {data.get('qualityFallback')}）  列表: {data.get('maxList')}",
        f"语音: {data.get('sendVocal')}  群文件: {data.get('uploadFile')}",
        f"原生卡: {data.get('sendNativeCard')}  自定义卡: {data.get('sendCustomCard')}",
        "",
        "主人命令：",
        "#qqm登录 / #qqm状态 / #qqm 音质 flac",
        "#qqm api <地址>   （设置 API 地址，主人）",
        "#qqm 开启点歌 / #qqm 关闭解析 / #qqm 测试",
    ])


def format_status_text(data: dict) -> str:
    return "\n".join([
        f"【{data.get('title') or 'QQ音乐状态'}】",
        f"昵称: {data.get('nickname')}",
        f"UIN: {data.get('uin')}",
        f"登录: {data.get('loginTypeText')}",
        f"会员: {data.get('vipTitle')} · {data.get('vipStateText')}",
        f"最高音质: {data.get('musicQuality')}",
        data.get("vipExpireText") or "",
        f"API: {data.get('apiBase')}",
        f"Key: {data.get('keyStatus')}",
        "" if data.get("loggedIn") else "发送 #qqm登录 扫码绑定",
    ]).rstrip()


# ──────────── 卡片数据装配 ────────────


def build_list_card_data(keyword: str, songs: list, options: dict | None = None, cfg: dict | None = None) -> dict:
    cfg = cfg or {}
    options = options or {}
    return {
        "keyword": keyword or "歌曲列表",
        "total": len(songs),
        "quality": str(cfg.get("quality") or "auto").upper(),
        "apiHint": api_hint_for(cfg),
        "singerInfo": options.get("singerInfo") or "",
        "albumInfo": options.get("albumInfo") or "",
        "songs": [
            {
                "index": i + 1,
                "songName": s.get("songName") or "未知",
                "singerName": s.get("singerName") or "未知",
                "albumName": s.get("albumName") or "",
                "cover": s.get("cover") or "",
                "duration": s.get("duration") or "",
                "payplay": bool(s.get("payplay")),
            }
            for i, s in enumerate(songs)
        ],
        "tip": options.get("tip") or "发送 #qqm听序号 播放（会话内也可 #听序号）；列表约 10 分钟内有效",
    }


def build_hot_card_data(items: list, cfg: dict | None = None) -> dict:
    cfg = cfg or {}
    lst = []
    for i, item in enumerate(items if isinstance(items, list) else []):
        word = item.get("k") or item.get("keyword") or item.get("query") or item.get("name") or item.get("title") or (item if isinstance(item, str) else "")
        if not word:
            continue
        lst.append({"index": i + 1, "word": str(word), "hot": item.get("n") or item.get("hot") or item.get("score") or item.get("rank") or ""})
        if len(lst) >= 15:
            break
    return {
        "title": "QQ音乐热搜",
        "subtitle": "实时热搜 · 可直接 #qqm点歌 关键词",
        "total": len(lst),
        "items": lst,
        "apiHint": api_hint_for(cfg),
        "tip": "复制热搜词后发送 #qqm点歌 关键词 即可搜索",
    }


def build_lyric_card_data(*, song_name="未知", singer_name="未知", cover="", album_name="", lines=None, songmid="", cfg: dict | None = None) -> dict:
    cfg = cfg or {}
    body = [str(l or "").strip() for l in (lines or []) if str(l or "").strip()][:36]
    return {
        "songName": song_name,
        "singerName": singer_name,
        "cover": cover or "",
        "albumName": album_name or "",
        "songmid": songmid or "",
        "lines": body,
        "lineCount": len(body),
        "apiHint": api_hint_for(cfg),
        "tip": "仅展示前 36 行，完整歌词请到 QQ 音乐查看" if len(body) >= 36 else "已去除时间戳，纯文本歌词",
    }


def clean_comment_text(text: str = "") -> str:

    import re as _re

    s = str(text or "")
    s = _re.sub(r"\[em\]e?\d+\[/em\]", "", s, flags=_re.I)  # [em]e400668[/em] 表情代码
    s = _re.sub(r"\[[\w一-鿿]{1,10}\]", " ", s)  # [音频] [图片] 等剩余标记
    s = s.replace("\\r\\n", " ").replace("\\n", " ")  # 字面量 \r\n / \n（接口常返回反斜杠+n）
    s = _re.sub(r"\r?\n", " ", s)
    s = _re.sub(r"\s+", " ", s)
    return s.strip()


def format_comment_time(ts) -> str:
  
    if not ts:
        return ""
    try:
        t = int(ts)
        if t <= 0:
            return ""
        from datetime import datetime
        return datetime.fromtimestamp(t).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def build_comment_card_data(*, song_name="未知", singer_name="未知", cover="", album_name="", comments=None, songmid="", tip="", cfg: dict | None = None) -> dict:

    cfg = cfg or {}
    lst = []
    for i, c in enumerate(comments or []):
        if not isinstance(c, dict):
            continue
        nick = c.get("nick") or c.get("nickname") or "匿名"
        raw = c.get("rootcommentcontent") or c.get("middlecommentcontent") or c.get("content") or c.get("comment") or ""
        content = clean_comment_text(raw) or "（仅表情 / 图片）"
        lst.append(
            {
                "index": i + 1,
                "nick": str(nick),
                "avatar": c.get("avatarurl") or c.get("headurl") or c.get("headPic") or "",
                "avatarPh": str(nick)[:1] if nick != "匿名" else "匿",
                "time": format_comment_time(c.get("time")),
                "likes": c.get("praisenum") or c.get("likeCount") or 0,
                "content": content,
                "hot": bool(c.get("is_hot") or c.get("is_hot_cmt")),
            }
        )
    lst = lst[:20]
    return {
        "songName": song_name,
        "singerName": singer_name,
        "cover": cover or "",
        "albumName": album_name or "",
        "songmid": songmid or "",
        "comments": lst,
        "total": len(lst),
        "tip": tip or "发送 #qqm点歌 关键词 可以搜索播放",
        "apiHint": api_hint_for(cfg),
    }


def build_detail_card_data(song: dict, *, quality_label="", payplay=False, source="", has_url=False) -> dict:
    is_vip = bool(song.get("payplay")) or payplay
    pay_info = "🔒 会员" if is_vip else ("💰 付费" if (isinstance(song.get("pay"), dict) and song["pay"].get("pay_down")) else "🆓 免费")
    url_status = "✅ 有播放链接" if has_url else "⚠️ 仅免费链接"

    title = song.get("songName") or "未知"
    if is_vip:
        title += " [会员]"
    elif isinstance(song.get("pay"), dict) and song["pay"].get("pay_down"):
        title += " [付费]"

    source_text = source or "未知来源"
    if source == "链接":
        source_text = "🔗 链接解析"
    elif source == "卡片":
        source_text = "📋 卡片解析"

    return {
        "title": title,
        "songName": song.get("songName") or "未知",
        "singerName": song.get("singerName") or "未知歌手",
        "albumName": song.get("albumName") or "",
        "cover": song.get("cover") or "",
        "songmid": song.get("songmid") or "",
        "duration": song.get("duration") or "",
        "qualityLabel": quality_label or "",
        "payplay": is_vip,
        "payInfo": pay_info,
        "urlStatus": url_status,
        "source": source_text,
        "tip": f"正在下载并发送语音（{quality_label or '默认音质'}）..." if has_url else "未获取到播放链接",
    }


def build_help_card_data(cfg: dict | None = None, version: str = "?") -> dict:
    cfg = cfg or {}
    quality = (cfg.get("quality") or "flac").upper()
    song_on = cfg.get("enableSongRequest") is not False
    resolve_on = cfg.get("enableResolve") is not False
    return {
        "version": f"v{version}",
        "statCommands": "25+",
        "statQuality": quality,
        "statMode": "全开" if (song_on and resolve_on) else ("点歌" if song_on else ("解析" if resolve_on else "待机")),
        "apiHint": api_hint_for(cfg),
        "tip": "付费曲需主人扫码登录；指令统一 #qqm 前缀；#听序号 仅在本群点歌会话有效；分享 QQ 音乐卡片/链接可自动解析。",
        "sections": [
            {"title": "点歌播放", "tag": "全员", "items": [
                {"name": "搜索点歌", "desc": "按关键词搜索并展示列表", "example": "#qqm点歌 七里香"},
                {"name": "选择曲目", "desc": "播放当前列表第 N 首", "example": "#qqm听1"},
                {"name": "直接播放", "desc": "搜索并立即播放第一条", "example": "#qqm播放 晴天"},
                {"name": "查看歌词", "desc": "按歌名或 mid 取歌词", "example": "#qqm歌词 七里香"},
                {"name": "热搜榜", "desc": "查看 QQ 音乐热搜", "example": "#qqm热搜"},
            ]},
            {"title": "发现音乐", "tag": "探索", "items": [
                {"name": "排行榜", "desc": "查看各大榜单歌曲", "example": "#qqm排行 飙升"},
                {"name": "推荐歌单", "desc": "热门推荐歌单列表", "example": "#qqm推荐"},
                {"name": "随机推荐", "desc": "随机推荐一首歌并播放", "example": "#qqm来首歌"},
                {"name": "个性电台", "desc": "根据口味推荐 5 首", "example": "#qqm电台"},
                {"name": "每日推荐", "desc": "每日推荐歌曲（需登录）", "example": "#qqm日推"},
                {"name": "我的收藏", "desc": "查看收藏歌曲（需登录）", "example": "#qqm收藏"},
                {"name": "歌手搜索", "desc": "搜索歌手并展示热门歌曲", "example": "#qqm歌手 周杰伦"},
                {"name": "专辑搜索", "desc": "搜索专辑并展示曲目列表", "example": "#qqm专辑 叶惠美"},
                {"name": "歌单搜索", "desc": "搜索歌单并展示歌曲", "example": "#qqm歌单 华语流行"},
                {"name": "歌曲评论", "desc": "查看歌曲热门评论", "example": "#qqm评论 晴天"},
            ]},
            {"title": "账号状态", "tag": "登录", "items": [
                {"name": "扫码登录", "desc": "主人扫码登录（付费音质）", "example": "#qqm登录"},
                {"name": "状态卡片", "desc": "账号 / 会员 / 音质 可视化", "example": "#qqm状态"},
                {"name": "快捷状态", "desc": "状态卡短指令", "example": "#qms"},
                {"name": "登出解绑", "desc": "清除登录态", "example": "#qqm登出"},
            ]},
            {"title": "主人管理", "tag": "Master", "items": [
                {"name": "查看配置", "desc": "API、开关、音质与发送方式", "example": "#qqm设置"},
                {"name": "设置 API", "desc": "修改接口地址", "example": "#qqm api <地址>"},
                {"name": "切换音质", "desc": "128 / 320 / flac / hires …", "example": "#qqm 音质 flac"},
                {"name": "功能开关", "desc": "开启或关闭点歌、解析", "example": "#qqm 开启点歌"},
                {"name": "连通测试", "desc": "探测 API 是否可用", "example": "#qqm 测试"},
                {"name": "插件更新", "desc": "git 拉取最新代码", "example": "#qqm更新"},
                {"name": "强制更新", "desc": "丢弃本地改动同步远程", "example": "#qqm强制更新"},
                {"name": "更新日志", "desc": "查看最近提交", "example": "#qqm更新日志"},
            ]},
            {"title": "智能解析", "tag": "自动", "items": [
                {"name": "分享卡片", "desc": "群内 QQ 音乐分享自动识别", "example": "（发送音乐卡片）"},
                {"name": "链接解析", "desc": "y.qq.com 链接自动取链播放", "example": "https://y.qq.com/…"},
            ]},
        ],
    }
