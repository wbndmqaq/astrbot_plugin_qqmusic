from __future__ import annotations

import re
import time
from typing import ClassVar
from urllib.parse import urlparse

# ──────────── 会话存储 ────────────


class SessionStore:
    _mem: ClassVar[dict] = {}
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
    async def set(cls, plugin, scope: str, session: dict) -> dict:
        data = {"group_id": scope, "updatedAt": time.time(), **session}
        cls._mem[str(scope)] = data
        k = cls._key(scope)
        try:
            import json

            await plugin.put_kv_data(k, json.dumps(data, ensure_ascii=False))
        except Exception:
            pass
        return data


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


def format_song_list(lst: list, title: str) -> str:
    if not isinstance(lst, list) or not lst:
        return (
            f"♫ {title}\n\n"
            "📭 暂无数据\n"
            "可能原因：\n"
            "1. API 未启动或网络异常\n"
            "2. 账号未登录（需要 #qqm登录）\n"
            "3. 请求超时，请稍后重试"
        )
    lines = [f"♫ {title}"]
    for i, s in enumerate(lst):
        idx = i + 1
        pay = s.get("pay") if isinstance(s.get("pay"), dict) else {}
        payplay = s.get("payplay")
        if payplay is None:
            payplay = pay.get("pay_play")
        if payplay is None:
            payplay = pay.get("payplay")
        is_vip = bool(payplay)
        is_paid = bool(pay.get("pay_down")) and not is_vip
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
        mv = " 🎬" if s.get("mvVid") else ""
        dur = f" ({s['duration']})" if s.get("duration") else ""
        singer = s.get("singerName") or s.get("singer") or ""
        lines.append(f"{i + 1}. {s.get('songName', '')} - {singer}{pay}{mv}{dur}")
    return (
        "♫ QQ音乐点歌结果（#qqm听序号 或 #听序号；🎬=有MV，可 #qqmMV 播放 序号）\n"
        + "\n".join(lines)
    )


def format_mv_list_text(lst: list) -> str:
    lines = []
    for i, m in enumerate(lst):
        name = m.get("mvtitle") or m.get("name") or m.get("songName") or "未知"
        singer = m.get("singerName") or "未知"
        lines.append(f"{i + 1}. {name} - {singer}")
    return "♫ MV 搜索结果（发 #qqmMV 播放 / 下载 序号）\n" + "\n".join(lines)


def _fmt_count(n) -> str:
    """播放量友好格式化：1.2亿 / 12018万 / 9999"""
    try:
        v = float(n or 0)
    except (TypeError, ValueError):
        return ""
    if not v:
        return ""
    if v >= 100000000:
        return f"{v / 100000000:.1f}".rstrip("0").rstrip(".") + "亿"
    if v >= 10000:
        return f"{v / 10000:.1f}".rstrip("0").rstrip(".") + "万"
    return str(int(v))


def format_mv_text(mv: dict) -> str:
    title = mv.get("mvtitle") or mv.get("name") or mv.get("songName") or "MV"
    singer = mv.get("singerName") or "未知歌手"
    lines = [f"🎬 MV：{title} - {singer}"]
    dur = mv.get("duration")
    if dur:
        if isinstance(dur, str) and ":" in str(dur):
            lines.append(f"时长：{dur}")  # 已是 m:ss 形式（来自卡片数据）
        else:
            sec = int(dur)
            if sec > 0:
                lines.append(f"时长：{sec // 60}:{sec % 60:02d}")
    if mv.get("listennum"):
        lines.append(f"累计播放：{_fmt_count(mv.get('listennum'))}")
    if mv.get("pubdate"):
        lines.append(f"发行：{mv['pubdate']}")
    return "\n".join(lines)


def format_hot_text(items: list) -> str:
    lst = (items if isinstance(items, list) else [])[:15]
    if not lst:
        return "暂无热搜"
    out = []
    for i, item in enumerate(lst):
        word = (
            item.get("k")
            or item.get("keyword")
            or item.get("query")
            or item.get("name")
            or item.get("title")
            or str(item)
        )
        out.append(f"{i + 1}. {word}")
    return "QQ音乐热搜\n" + "\n".join(out)


def format_lyric_text(data: dict) -> str:
    song_name = data.get("songName")
    singer = data.get("singerName")
    head = (
        f"歌词：{song_name or '未知'} - {singer or '未知'}\n"
        if (song_name or singer)
        else ""
    )
    body = "\n".join(data.get("lines") or [])
    return (head + body).strip() or "暂无歌词"


def format_detail_text(
    song: dict, *, quality_label: str = "", has_url: bool = False
) -> str:
    is_vip = bool(song.get("payplay"))
    lines = [
        f"♪ {song.get('songName') or '未知'} - {song.get('singerName') or '未知'}"
        + (" [会员/付费]" if is_vip else "")
    ]
    if song.get("albumName"):
        lines.append(f"专辑：{song['albumName']}")
    if song.get("duration"):
        lines.append(f"时长：{song['duration']}")
    if quality_label:
        lines.append(f"音质：{quality_label}")
    if not has_url and is_vip:
        lines.append("⚠️ 该曲需会员，请 #qqm登录")
    return "\n".join(lines)


def format_status_text(data: dict) -> str:
    return "\n".join(
        [
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
        ]
    ).rstrip()


# ──────────── 卡片数据装配 ────────────


def build_list_card_data(
    keyword: str, songs: list, options: dict | None = None, cfg: dict | None = None
) -> dict:
    cfg = cfg or {}
    options = options or {}
    has_mv = any(bool(s.get("mvVid")) for s in songs)
    # 常用指令：原卡片底部「小提示」的内容并入此列表；options.commands 可整体替换
    default_commands = [
        {
            "name": "#qqm听序号",
            "desc": options.get("tip")
            or "播放当前列表中的指定歌曲（会话内也可 #听序号）",
            "example": "#qqm听1",
        },
        {
            "name": "#qqm歌词 序号",
            "desc": "查看指定歌曲的纯文本歌词",
            "example": "#qqm歌词1",
        },
    ]
    if not options.get("commands"):
        default_commands.append(
            {
                "name": "#qqm听所有",
                "desc": "依次连播当前列表的全部歌曲（上限 30 首）",
                "example": "#qqm听所有",
            }
        )
    if has_mv:
        default_commands.append(
            {
                "name": "#qqmMV 播放 序号",
                "desc": "播放 / 下载该曲 MV（列表带 🎬 即是有 MV 的歌曲）",
                "example": "#qqmMV 播放 1",
            }
        )
    default_commands.append(
        {
            "name": "列表有效期",
            "desc": "本列表约 10 分钟内有效，过期请重新搜索",
            "example": "#qqm点歌 关键词",
        }
    )
    commands = options.get("commands") or default_commands
    return {
        "keyword": keyword or "歌曲列表",
        "total": len(songs),
        "quality": str(cfg.get("quality") or "auto").upper(),
        "apiHint": api_hint_for(cfg),
        "songs": [
            {
                "index": i + 1,
                "songName": s.get("songName") or "未知",
                "singerName": s.get("singerName") or "未知",
                "albumName": s.get("albumName") or "",
                "cover": s.get("cover") or "",
                "duration": s.get("duration") or "",
                "payplay": bool(s.get("payplay")),
                "hasMv": bool(s.get("mvVid")),
            }
            for i, s in enumerate(songs)
        ],
        "hasMv": has_mv,
        "commands": commands,
    }


def build_mv_list_card_data(
    keyword: str, mvs: list, *, cfg: dict | None = None
) -> dict:
    """MV 列表卡片：复用 qqmusic-list 模板，MV 字段映射为列表条目。

    序号与 #qqmMV 播放/下载 序号 对应；封面/时长沿用 MV 数据。
    """
    cfg = cfg or {}
    songs = []
    for i, m in enumerate(mvs if isinstance(mvs, list) else []):
        title = m.get("mvtitle") or m.get("name") or m.get("songName") or "未知"
        sec = int(m.get("duration") or 0)
        songs.append(
            {
                "index": i + 1,
                "songName": str(title),
                "singerName": m.get("singerName") or "未知",
                "albumName": m.get("pubdate") or "",
                "cover": m.get("cover") or "",
                "duration": f"{sec // 60}:{sec % 60:02d}" if sec > 0 else "",
                "payplay": False,
                "mvVid": m.get("vid") or "",  # 有 vid 即带 🎬 徽标
            }
        )
    return build_list_card_data(
        keyword,
        songs,
        options={
            "tip": "播放当前列表中的指定 MV（也可 #qqmMV 下载 序号）",
            "commands": [
                {
                    "name": "#qqmMV 播放 序号",
                    "desc": "播放该 MV（视频消息，可直接观看）",
                    "example": "#qqmMV 播放 1",
                },
                {
                    "name": "#qqmMV 下载 序号",
                    "desc": "以文件形式发送该 MV，可保存",
                    "example": "#qqmMV 下载 1",
                },
                {
                    "name": "列表有效期",
                    "desc": "本列表约 10 分钟内有效，过期请重新搜索",
                    "example": "#qqmMV 搜索 关键词",
                },
            ],
        },
        cfg=cfg,
    )


def build_hot_card_data(items: list, cfg: dict | None = None) -> dict:
    cfg = cfg or {}
    lst = []
    for i, item in enumerate(items if isinstance(items, list) else []):
        word = (
            item.get("k")
            or item.get("keyword")
            or item.get("query")
            or item.get("name")
            or item.get("title")
            or (item if isinstance(item, str) else "")
        )
        if not word:
            continue
        lst.append(
            {
                "index": i + 1,
                "word": str(word),
                "hot": item.get("n")
                or item.get("hot")
                or item.get("score")
                or item.get("rank")
                or "",
            }
        )
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


def build_lyric_card_data(
    *,
    song_name="未知",
    singer_name="未知",
    cover="",
    album_name="",
    lines=None,
    songmid="",
    cfg: dict | None = None,
) -> dict:
    cfg = cfg or {}
    body = [str(ln or "").strip() for ln in (lines or []) if str(ln or "").strip()]
    return {
        "songName": song_name,
        "singerName": singer_name,
        "cover": cover or "",
        "albumName": album_name or "",
        "songmid": songmid or "",
        "lines": body,
        "lineCount": len(body),
        "apiHint": api_hint_for(cfg),
        "tip": "已去除时间戳，纯文本歌词（超长歌词自动分页）",
    }


def clean_comment_text(text: str = "") -> str:
    s = str(text or "")
    s = re.sub(
        r"\[em\]e?\d+\[/em\]", "", s, flags=re.IGNORECASE
    )  # [em]e400668[/em] 表情代码
    s = re.sub(r"\[[\w一-鿿]{1,10}\]", " ", s)  # [音频] [图片] 等剩余标记
    s = s.replace("\\r\\n", " ").replace(
        "\\n", " "
    )  # 字面量 \r\n / \n（接口常返回反斜杠+n）
    s = re.sub(r"\r?\n", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def format_comment_time(ts) -> str:

    if not ts:
        return ""
    try:
        t = int(ts)
        if t <= 0:
            return ""
        from datetime import datetime, timezone

        return (
            datetime.fromtimestamp(t, tz=timezone.utc).astimezone().strftime("%Y-%m-%d")
        )
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def build_comment_card_data(
    *,
    song_name="未知",
    singer_name="未知",
    cover="",
    album_name="",
    comments=None,
    songmid="",
    tip="",
    cfg: dict | None = None,
) -> dict:

    cfg = cfg or {}
    lst = []
    for i, c in enumerate(comments or []):
        if not isinstance(c, dict):
            continue
        nick = c.get("nick") or c.get("nickname") or "匿名"
        raw = (
            c.get("rootcommentcontent")
            or c.get("middlecommentcontent")
            or c.get("content")
            or c.get("comment")
            or ""
        )
        content = clean_comment_text(raw) or "（仅表情 / 图片）"
        lst.append(
            {
                "index": i + 1,
                "nick": str(nick),
                "avatar": c.get("avatarurl")
                or c.get("headurl")
                or c.get("headPic")
                or "",
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


def build_detail_card_data(
    song: dict, *, quality_label="", payplay=False, source="", has_url=False
) -> dict:
    is_vip = bool(song.get("payplay")) or payplay

    source_text = source or "未知来源"
    if source == "链接":
        source_text = "🔗 链接解析"
    elif source == "卡片":
        source_text = "📋 卡片解析"

    return {
        "songName": song.get("songName") or "未知",
        "singerName": song.get("singerName") or "未知歌手",
        "albumName": song.get("albumName") or "",
        "cover": song.get("cover") or "",
        "duration": song.get("duration") or "",
        "qualityLabel": quality_label or "",
        "payplay": is_vip,
        "showPay": True,
        "source": source_text,
        "tip": f"正在下载并发送语音（{quality_label or '默认音质'}）..."
        if has_url
        else "未获取到播放链接",
    }


def build_mv_card_data(mv: dict, cfg: dict | None = None) -> dict:
    """MV 详情卡片 - 复用 qqmusic-detail 模板；按 MV 语义映射（无专辑/无音质/显示时长）"""
    cfg = cfg or {}
    title = mv.get("mvtitle") or mv.get("name") or mv.get("songName") or "MV"
    singer = mv.get("singerName") or mv.get("singer_name") or ""
    play = int(mv.get("listennum") or mv.get("listenNum") or 0)
    pubdate = mv.get("pubdate") or mv.get("pub_date") or mv.get("publish_date") or ""
    # 时长：秒 → m:ss
    duration = ""
    sec = int(mv.get("duration") or mv.get("durationSec") or 0)
    if sec > 0:
        duration = f"{sec // 60}:{sec % 60:02d}"
    tip_parts = [
        f"累计播放 {_fmt_count(play)}" if play else "",
        f"发行 {pubdate}" if pubdate else "",
        "发 #qqmMV 播放/下载 序号",
    ]
    return {
        "songName": title,
        "singerName": singer or "未知歌手",
        "albumName": "",  # MV 无专辑概念，发行日期进 tip
        "cover": mv.get("cover") or mv.get("picurl") or "",
        "duration": duration,
        "qualityLabel": "",  # 音质概念不适用于 MV
        "payplay": False,
        "showPay": False,  # 不显示 付费/免费 徽章
        "source": "MV",
        "tip": " · ".join(x for x in tip_parts if x) or "发 #qqmMV 播放/下载 序号",
        "vid": mv.get("vid") or "",
        "listennum": play,
        "apiHint": api_hint_for(cfg),
    }


def build_help_card_data(cfg: dict | None = None, version: str = "?") -> dict:
    cfg = cfg or {}
    quality = (cfg.get("quality") or "flac").upper()
    song_on = cfg.get("enableSongRequest") is not False
    resolve_on = cfg.get("enableResolve") is not False
    if song_on and resolve_on:
        stat_mode = "全开"
    elif song_on:
        stat_mode = "点歌"
    elif resolve_on:
        stat_mode = "解析"
    else:
        stat_mode = "待机"
    return {
        "version": f"v{version}",
        "statCommands": "30+",
        "statQuality": quality,
        "statMode": stat_mode,
        "apiHint": api_hint_for(cfg),
        "tip": (
            "付费曲需主人扫码登录；指令统一 #qqm 前缀；"
            "#听序号 仅在本群点歌会话有效；分享 QQ 音乐卡片/链接可自动解析。"
        ),
        "sections": [
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
                        "desc": "按歌名或 mid 取歌词",
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
                        "desc": "按分类 / 标签浏览 MV",
                        "example": "#qqmMV",
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
                        "name": "随机推荐",
                        "desc": "随机推荐一首歌并播放",
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
        ],
    }
