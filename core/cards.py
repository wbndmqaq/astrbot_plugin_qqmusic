from __future__ import annotations

import asyncio
import json
import os
import re
import time
from copy import deepcopy
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlparse

from . import messages as msg
from .messages import HELP_SECTIONS
from .utils import safe_int

PLUGIN_NAME = "astrbot_plugin_qqmusic"

# ──────────── 裸 #听N 跨插件抢占（共享「最近活跃归属」标记） ────────────


def _owner_marker_path() -> Path:
    """三个音乐插件（网易云/酷狗/QQ）共用的「最近活跃归属」标记文件路径。

    必须显式传插件名：StarTools 只在调用栈位于插件主模块（main.py）时才能反查
    插件元数据，本函数在 core/cards.py 里，不传名会抛 RuntimeError 并回退到
    「插件自身目录」——那样三个插件各写各的，跨插件抢占就永远不生效。
    """
    try:
        from astrbot.api.star import StarTools

        data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        return data_dir.parent / "_music_session_owner.json"
    except Exception:
        return Path(__file__).resolve().parent.parent / "_music_session_owner.json"


def mark_session_owner(plugin_name: str) -> None:
    """记录某插件为最近一次会话活跃的音乐插件（几十字节小文件，微秒级同步 IO）。"""
    try:
        p = _owner_marker_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(
            json.dumps({"plugin": plugin_name, "ts": int(time.time())}, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(tmp, p)  # 原子替换，避免并发写坏
    except Exception:
        pass


def is_session_owner(plugin_name: str) -> bool:
    """判断某插件是否为最近活跃的音乐插件（无标记时视为 True，退化为「谁有会话谁响应」）。"""
    try:
        p = _owner_marker_path()
        if not p.exists():
            return True
        data = json.loads(p.read_text(encoding="utf-8"))
        return data.get("plugin") == plugin_name
    except Exception:
        return True


# ──────────── 会话存储 ────────────


class SessionStore:
    # scope -> 会话数据的内存缓存。类级（进程内共享）而非实例级：会话本就按 scope
    # （群/私聊）寻址，同一份数据又以相同 key 持久化在 KV 里，共享缓存与 KV 视图
    # 天然一致；单实例加载下与实例级无差别，多实例并存也只是各持一份同内容缓存。
    _mem: ClassVar[dict] = {}
    # 落盘串行化锁：保证同 scope（乃至全局）KV 写入顺序与调用顺序一致
    _write_lock: ClassVar[asyncio.Lock] = asyncio.Lock()
    TTL = 600
    # 内存缓存条数上限：防止大量群/私聊会话长期驻留导致无界增长。
    # 超过上限时按 updatedAt 淘汰最旧条目（数据已持久化到 KV，淘汰不丢数据）。
    MAX_MEM = 512

    @classmethod
    def _evict_if_needed(cls) -> None:
        if len(cls._mem) <= cls.MAX_MEM:
            return
        overflow = len(cls._mem) - cls.MAX_MEM
        oldest = sorted(
            cls._mem.items(), key=lambda kv: kv[1].get("updatedAt") or 0
        )[:overflow]
        for k, _ in oldest:
            cls._mem.pop(k, None)

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
                    raw = json.loads(raw)
                if not isinstance(raw, dict):
                    return None
                ts = raw.get("updatedAt") or 0
                if time.time() - ts < cls.TTL:
                    # 命中 KV 回填内存，避免同一会话每次仍读 KV
                    cls._mem[str(scope)] = raw
                    cls._evict_if_needed()
                    return raw
                await plugin.delete_kv_data(k)
        except Exception:
            pass
        return None

    @staticmethod
    def _strip_raw(session: dict) -> dict:
        """剥离会话里冗余的 ``raw``（上游原始对象）后再入库。

        ``raw`` 只有投递链的 ``play`` 会读（见 ``resolve._fail_hint``），会话数据没人读；
        而收藏/推荐类会话最多 100 条，带 ``raw`` 时整条会话会被原样 ``json.dumps`` 写进 KV，
        并在 ``_mem`` 里再留一份 —— 内存与 KV 双份占用。
        """
        # 覆盖两层：列表值（data/songs 等曲目列表）与字典值（playlist/singer 等本身就是
        # 带 raw 的上游对象）。更深的嵌套（字典里再套列表）当前没有会话使用，不做递归。
        cleaned = {}
        changed = False
        for key, value in session.items():
            if isinstance(value, list):
                new_list = [
                    {k: v for k, v in item.items() if k != "raw"}
                    if isinstance(item, dict) and "raw" in item
                    else item
                    for item in value
                ]
                if any(a is not b for a, b in zip(new_list, value)):
                    changed = True
                cleaned[key] = new_list
            elif isinstance(value, dict) and "raw" in value:
                cleaned[key] = {k: v for k, v in value.items() if k != "raw"}
                changed = True
            else:
                cleaned[key] = value
        return cleaned if changed else session

    @classmethod
    async def set(cls, plugin, scope: str, session: dict) -> dict:
        # updatedAt 必须放在 **session 之后：调用方常传入 dict(旧会话)（内含旧 updatedAt），
        # 若放在前面会被旧值覆盖，导致「读→改→写回」不推进时间戳、会话从首次创建起计时过期。
        data = {"group_id": scope, **cls._strip_raw(session), "updatedAt": time.time()}
        cls._mem[str(scope)] = data
        # 插入后淘汰，保证 _mem 容量严格不超 MAX_MEM
        cls._evict_if_needed()
        # 会话写入即记录本插件为「最近活跃」，供裸 #听N 跨插件抢占
        # 标记文件写盘（mkdir + write + os.replace）是阻塞 IO，且每次会话写入都会走：
        # 与读侧 service.is_session_owner 保持一致，放线程池执行。
        await asyncio.to_thread(
            mark_session_owner, str(getattr(plugin, "name", "") or "")
        )
        k = cls._key(scope)
        # 写盘串行化：等锁期间本次数据可能已被更新的一次写入取代（内存里 updatedAt
        # 更大），此时直接跳过本次旧值，避免 KV 被旧值覆盖
        async with cls._write_lock:
            try:
                cur = cls._mem.get(str(scope))
                if cur is not None and (cur.get("updatedAt") or 0) > (
                    data.get("updatedAt") or 0
                ):
                    return data
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


def format_list_text(lst: list, title: str = "") -> str:
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


def format_mv_list_text(lst: list, title: str = "") -> str:
    lines = []
    for i, m in enumerate(lst):
        name = m.get("mvtitle") or m.get("name") or m.get("songName") or "未知"
        singer = m.get("singerName") or "未知"
        lines.append(f"{i + 1}. {name} - {singer}")
    return f"♫ MV 搜索结果（{msg.MV_LIST_HINT}）\n" + "\n".join(lines)


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
            "" if data.get("loggedIn") else msg.SCAN_LOGIN_HINT,
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
                "desc": (
                    "播放该曲 MV（下载用 #qqmMV 下载 序号；"
                    "列表带 🎬 即是有 MV 的歌曲）"
                ),
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
        "commands": commands,
    }


def build_mv_list_card_data(
    keyword: str, mvs: list, *, cfg: dict | None = None
) -> dict:
    """MV 列表卡片：复用 qqmusic-list 模板，MV 字段映射为列表条目。

    序号与 #qqmMV 播放 序号 / #qqmMV 下载 序号 对应；封面/时长沿用 MV 数据。
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
                # 上游计数可能是字符串，而模板要跟 0 做大小比较（str > int 抛 TypeError，
                # 会让整张评论卡渲染失败退化成纯文本），这里统一转成 int。
                "likes": safe_int(c.get("praisenum") or c.get("likeCount") or 0, 0),
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


def build_delivery_tip(
    cfg: dict | None = None,
    *,
    quality_label: str = "",
    has_url: bool = False,
    degrade_note: str = "",
    error: str = "",
    no_vocal: bool = False,
) -> str:
    """生成基于当前配置的动态提示文案：精准反映当前会发送什么（语音 / 群文件 / 原生卡 / 自定义卡）。

    no_vocal：当前平台出站不支持语音（``delivery._NO_VOCAL_PLATFORMS``，如 weixin_oc）。
    这类平台即使 ``sendVocal`` 开着也发不出语音，tip 必须按「实际会发什么」生成，
    否则会与现实相反地显示「正在发送语音（128）…」。
    """
    cfg = cfg or {}
    if not has_url:
        if error:
            return f"获取播放链接失败：{error}"
        return "未获取到播放链接，请 #qqm登录"

    q = quality_label or "默认音质"
    want_vocal = cfg.get("sendVocal") is not False
    upload_file = cfg.get("uploadFile") is not False
    send_native = bool(cfg.get("sendNativeCard"))
    send_custom = bool(cfg.get("sendCustomCard"))

    actions = []
    if want_vocal and not no_vocal:
        actions.append("语音")
    if upload_file:
        actions.append("群文件")
    if send_native or send_custom:
        actions.append("音乐卡片")

    if actions:
        main = f"正在发送{' + '.join(actions)}（{q}）..."
    elif no_vocal and want_vocal:
        # 语音被平台排除、又没开群文件/音乐卡：实际什么都不发，如实说明
        main = msg.NO_VOCAL_NO_FILE_HINT
    else:
        main = f"已获取播放链接（{q}）"
    if degrade_note:
        main += f" · {degrade_note}"
    return main


def build_detail_card_data(
    song: dict,
    *,
    quality_label="",
    payplay=False,
    source="",
    has_url=False,
    mv_vid="",
    tip="",
    degrade_note="",
    error="",
    cfg: dict | None = None,
    no_vocal: bool = False,
) -> dict:
    is_vip = bool(song.get("payplay")) or payplay

    source_text = source or "未知来源"
    if source == "链接":
        source_text = "🔗 链接解析"
    elif source == "卡片":
        source_text = "📋 卡片解析"

    active_cfg = cfg or {}
    base_tip = tip or build_delivery_tip(
        active_cfg,
        quality_label=quality_label,
        has_url=has_url,
        degrade_note=degrade_note,
        error=error,
        no_vocal=no_vocal,
    )
    mv_hint = " · 🎬 该曲有 MV：#qqmMV 播放 或 #qqmMV 下载 直接操作" if mv_vid else ""

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
        "tip": f"{base_tip}{mv_hint}",
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
        msg.MV_LIST_HINT,
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
        "tip": " · ".join(x for x in tip_parts if x) or msg.MV_LIST_HINT,
    }


def build_help_card_data(
    cfg: dict | None = None, version: str = "?", command_count: int | None = None
) -> dict:
    """帮助卡片数据。

    command_count 由调用方传入真实路由数（``len(handlers.ALL_ROUTES)``），
    不再硬编码一个与实现漂移的假数字。
    """
    cfg = cfg or {}
    quality = (cfg.get("quality") or "auto").upper()
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
        "statCommands": str(command_count) if command_count else "",
        "statQuality": quality,
        "statMode": stat_mode,
        "apiHint": api_hint_for(cfg),
        "tip": (
            "付费曲需主人扫码登录；指令统一 #qqm 前缀；"
            "#听序号 仅在本群点歌会话有效；分享 QQ 音乐卡片/链接可自动解析。"
        ),
        "sections": deepcopy(HELP_SECTIONS),
    }


def format_help_text(data: dict) -> str:
    """帮助卡片的纯文本兜底：与卡片共用同一份 sections 数据，两份清单不会再漂移。"""
    lines = [f"【QQ音乐插件帮助】{data.get('version') or ''}".rstrip(), ""]
    for section in data.get("sections") or []:
        lines.append(f"— {section.get('title') or ''} —")
        for item in section.get("items") or []:
            name = item.get("name") or ""
            desc = item.get("desc") or ""
            example = item.get("example") or ""
            lines.append(f"{example}  {name}：{desc}" if example else f"{name}：{desc}")
        lines.append("")
    for tail in (data.get("tip") or "", data.get("apiHint") or ""):
        if tail:
            lines.append(tail)
    return "\n".join(x for x in lines).strip()
