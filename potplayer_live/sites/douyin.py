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
    sdk = su.get("live_core_sdk_data", {}).get("pull_data", {})
    quals = sdk.get("options", {}).get("qualities") or []
    if quals and sdk.get("stream_data"):
        flv_map = json.loads(sdk["stream_data"]).get("data", {})
        for q in quals:
            url = flv_map.get(q.get("sdk_key"), {}).get("main", {}).get("flv")
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
