#!/usr/bin/env python3
"""抖音(live.douyin.com)平台解析模块。

接口见 sites/__init__.py。单房间走 web enter 接口(只需 ttwid cookie);
分区浏览走 partition/detail/room 接口。只用 flv(serve 中继不吃 HLS)。
"""

import re
import json
import urllib.parse
import urllib.request

from ..common import http_get

DOMAINS = ["live.douyin.com", "douyin.com"]
UA_DESKTOP = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
REFERER = "https://live.douyin.com/"
PLAY_HEADERS = {"User-Agent": UA_DESKTOP, "Referer": REFERER}

# enter 接口(只带 ttwid cookie 即可,先不做 a_bogus 签名)
ENTER_URL = (
    "https://live.douyin.com/webcast/room/web/enter/?aid=6383&app_name=douyin_web"
    "&live_id=1&device_platform=web&language=zh-CN&enter_from=web_live"
    "&cookie_enabled=true&browser_platform=Win32&browser_name=Chrome"
    "&browser_version=120.0.0.0&web_rid={web_rid}&room_id_str=&enter_source="
)

ROOM_URL = "https://live.douyin.com/{web_rid}"

# 分区房间列表接口(非 v2,ttwid-only)
PARTITION_URL = (
    "https://live.douyin.com/webcast/web/partition/detail/room/?aid=6383"
    "&app_name=douyin_web&live_id=1&device_platform=web&count={count}"
    "&offset={offset}&partition={partition}&partition_type={ptype}&req_from=2"
)

# 中文别名 → (partition, partition_type)。抖音分区 id 需抓包校准,可能随平台调整。
# 未命中的分区请用 id,type 形式(如 .../category/720,1)。
ALIASES = {
    "英雄联盟": (2701, 1),
    "王者荣耀": (694, 1),
    "和平精英": (2354, 1),
}

# flv_pull_url 无 sdk 清晰度名时的兜底名映射(抖音键名固定)
_FALLBACK_NAMES = {"FULL_HD1": "原画", "HD1": "高清", "SD1": "标清", "SD2": "流畅"}


def resolve_web_rid(url: str) -> str:
    """从房间地址取 web_rid(路径最后一段)。"""
    return urllib.parse.urlparse(url).path.strip("/").split("/")[-1]


def _ttwid() -> str:
    """GET 首页从 Set-Cookie 抓 ttwid(抖音 web 接口所需)。"""
    req = urllib.request.Request(REFERER, headers={"User-Agent": UA_DESKTOP})
    with urllib.request.urlopen(req, timeout=15) as r:
        cookies = r.headers.get_all("Set-Cookie") or []
    for c in cookies:
        m = re.search(r"ttwid=([^;]+)", c)
        if m:
            return m.group(1)
    return ""


def _parse_enter(payload: dict, web_rid: str) -> dict:
    """把 enter 接口 JSON 解析成房间信息。纯函数,便于不触网测试。

    清晰度优先取 live_core_sdk_data(带中文名 + 码率),回退 flv_pull_url。只收 flv。
    """
    d = payload.get("data", {}) or {}
    rooms = d.get("data") or []
    room = rooms[0] if rooms else {}
    user = d.get("user", {}) or {}
    info = {
        "rid": web_rid,
        "nick": user.get("nickname") or room.get("owner", {}).get("nickname"),
        "title": room.get("title"),
        "living": room.get("status") == 2,
        "streams": {},
    }
    if not info["living"]:
        return info
    su = room.get("stream_url", {}) or {}
    sdk = (su.get("live_core_sdk_data") or {}).get("pull_data", {})
    quals = sdk.get("options", {}).get("qualities") or []
    try:
        flv_map = json.loads(sdk["stream_data"]).get("data", {}) if quals and sdk.get("stream_data") else None
    except (json.JSONDecodeError, KeyError):
        flv_map = None  # 非法 JSON:清空 quals 使下面走回退分支
        quals = []
    if quals and flv_map is not None:
        for q in quals:
            url = (flv_map.get(q.get("sdk_key")) or {}).get("main", {}).get("flv")
            if url:
                info["streams"][q["name"]] = {
                    "quality": q.get("v_bit_rate", 0),
                    "url": url,
                    "backups": [],
                }
    else:  # 兜底:flv_pull_url 键名 → 中文名,码率未知记 0
        for key, url in (su.get("flv_pull_url") or {}).items():
            info["streams"][_FALLBACK_NAMES.get(key, key)] = {
                "quality": 0,
                "url": url,
                "backups": [],
            }
    return info


def parse(url: str) -> dict:
    """解析抖音房间(网络壳:取 ttwid → 请求 enter → _parse_enter)。"""
    web_rid = resolve_web_rid(url)
    headers = {"User-Agent": UA_DESKTOP, "Referer": REFERER, "Cookie": f"ttwid={_ttwid()}"}
    payload = json.loads(http_get(ENTER_URL.format(web_rid=web_rid), headers=headers))
    return _parse_enter(payload, web_rid)


def is_category(url: str) -> bool:
    """分区页判定:http 路径首段为 category(如 /category/720,1)。"""
    p = urllib.parse.urlparse(url)
    if p.scheme in ("http", "https"):
        return p.path.strip("/").split("/")[0] == "category"
    return False


def resolve_partition(ident):
    """把分区标识解析成 (partition, partition_type, 显示名)。

    "720,1" → 直接拆;中文别名 → 查 ALIASES;都不中 → 抛带引导的错误。
    """
    ident = str(ident).strip()
    if "," in ident:
        p, t = ident.split(",", 1)
        return int(p), int(t), ident
    if ident in ALIASES:
        p, t = ALIASES[ident]
        return p, t, ident
    raise RuntimeError(
        f"未知抖音分区「{ident}」。请用 id,type 形式,如 "
        "https://live.douyin.com/category/720,1"
    )


def _parse_rooms(payload: dict) -> list:
    """从分区接口 JSON 提取房间卡片。纯函数,便于不触网测试。

    web_rid 可能在 item 顶层或 room/owner 下;人气取 total_user_str。
    """
    out = []
    for it in payload.get("data", {}).get("data") or []:
        room = it.get("room", {}) or {}
        owner = room.get("owner", {}) or {}
        web_rid = it.get("web_rid") or room.get("web_rid") or owner.get("web_rid")
        if not web_rid:
            continue
        out.append(
            {
                "room": str(web_rid),
                "nick": owner.get("nickname"),
                "viewers": room.get("stats", {}).get("total_user_str"),
                "title": room.get("title"),
                "url": ROOM_URL.format(web_rid=web_rid),
            }
        )
    return out


def list_category(ident, pages=3, count=15, fetch=None):
    """列出抖音分区在播房间(跨页按 web_rid 去重保序)。

    fetch(partition, partition_type, offset)->dict 可注入,便于不触网测试。
    """
    partition, ptype, name = resolve_partition(ident)
    if fetch is None:
        ttwid = _ttwid()

        def fetch(partition, ptype, offset):
            headers = {"User-Agent": UA_DESKTOP, "Referer": REFERER,
                       "Cookie": f"ttwid={ttwid}"}
            url = PARTITION_URL.format(
                count=count, offset=offset, partition=partition, ptype=ptype
            )
            return json.loads(http_get(url, headers=headers))

    seen, rooms = set(), []
    for i in range(pages):
        try:
            payload = fetch(partition, ptype, i * count)
        except Exception:
            break
        page_rooms = _parse_rooms(payload)
        if not page_rooms:
            break  # 该页无房间 → 到底
        for r in page_rooms:
            if r["room"] not in seen:
                seen.add(r["room"])
                rooms.append(r)
    return {"name": name, "slug": ident, "rooms": rooms}
