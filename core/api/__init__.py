"""astrbot_plugin_qqmusic 的上游音乐 API 客户端包（自 core/api.py 拆分）。

子模块划分（原文件的分段注释 → 模块）：
    _core          复用 HTTP 会话、通用取值辅助（统一各处的剥壳/回退写法）、辅助
    _account       登录态、用户
    _search        搜索、搜索扩展
    _song          歌曲详情/播放链、歌词/热搜
    _rank          排行榜、新歌速递
    _mv            MV
    _recommend     推荐、每日推荐 / 收藏（dirid: 202=日推, 201=收藏）
    _browse        歌手、专辑、歌单
    _comment       评论
    _resolve_link  链接/卡片解析

本包对外符号与拆分前的 core/api.py 完全一致（``from ..core.api import X`` 与
``api.X`` 的既有用法不受影响）；模块级状态（HTTP 会话、设备 Cookie、特权缓存）
由 ``_core`` / 对应子模块持有，外部一律通过函数访问。
"""

from ._account import (
    list_accounts,
    pull_login_meta,
    refresh_login,
    user_cookie,
    user_detail,
)
from ._browse import (
    _playlist_track_view,
    album_songs,
    singer_desc,
    singer_songs,
    songlist_detail,
)
from ._comment import (
    comment,
)
from ._core import (
    _API_TIMEOUT_SEC,
    UA,
    ApiError,
    _cfg,
    _cfg_getter,
    _empty_url_result,
    _get_base,
    _get_session,
    _get_token,
    _handle_response,
    _query_safe_params,
    _safe_num,
    _sanitize_for_header,
    _session,
    _strip_tags,
    close_session,
    get_session,
    normalize_api_base,
    payplay_of,
    request,
    set_config_getter,
    singer_text,
    unwrap_data,
)
from ._mv import (
    mv_by_tag,
    mv_category,
    mv_url,
    normalize_mv_item,
    search_mv,
)
from ._rank import (
    new_songs,
    top_category,
    top_detail,
)
from ._recommend import (
    _diss_payload,
    daily_recommend,
    personal_radio,
    recommend_feed,
    recommend_hot,
    user_diss_list,
    user_favorites,
)
from ._resolve_link import (
    _try_parse_json_loose,
    cover_url,
    parse_qqmusic_card,
    parse_qqmusic_extended_ids,
    parse_qqmusic_ids,
)
from ._search import (
    normalize_track_item,
    search_albums,
    search_singers,
    search_songlists,
    search_songs,
)
from ._song import (
    _build_url_error,
    _map_song_url_body,
    _probe_media_info,
    _probe_url_alive,
    _try_one_quality,
    extract_track_meta,
    hot_keys,
    lyric,
    song_detail,
    song_info_batch,
    song_url,
    song_url_best,
)

__all__ = [
    "extract_track_meta",
    "ApiError",
    "UA",
    "_API_TIMEOUT_SEC",
    "_build_url_error",
    "_cfg",
    "_cfg_getter",
    "_diss_payload",
    "_empty_url_result",
    "_get_base",
    "_get_session",
    "_get_token",
    "_handle_response",
    "_map_song_url_body",
    "_playlist_track_view",
    "_probe_media_info",
    "_probe_url_alive",
    "_query_safe_params",
    "_safe_num",
    "_sanitize_for_header",
    "_session",
    "_strip_tags",
    "_try_one_quality",
    "_try_parse_json_loose",
    "album_songs",
    "close_session",
    "comment",
    "cover_url",
    "daily_recommend",
    "get_session",
    "hot_keys",
    "list_accounts",
    "lyric",
    "mv_by_tag",
    "mv_category",
    "mv_url",
    "new_songs",
    "normalize_api_base",
    "normalize_mv_item",
    "normalize_track_item",
    "parse_qqmusic_card",
    "parse_qqmusic_extended_ids",
    "parse_qqmusic_ids",
    "payplay_of",
    "personal_radio",
    "pull_login_meta",
    "recommend_feed",
    "recommend_hot",
    "refresh_login",
    "request",
    "search_albums",
    "search_mv",
    "search_singers",
    "search_songlists",
    "search_songs",
    "set_config_getter",
    "singer_desc",
    "singer_songs",
    "singer_text",
    "song_detail",
    "song_info_batch",
    "song_url",
    "song_url_best",
    "songlist_detail",
    "top_category",
    "top_detail",
    "unwrap_data",
    "user_cookie",
    "user_detail",
    "user_diss_list",
    "user_favorites",
]
