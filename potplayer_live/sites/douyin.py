#!/usr/bin/env python3
"""抖音(live.douyin.com)平台解析模块。

接口见 sites/__init__.py。单房间从房间页 SSR 内嵌数据取(见 parse 说明)。
只用 flv(serve 中继不吃 HLS)。
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

ROOM_URL = "https://live.douyin.com/{web_rid}"

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
        "nick": user.get("nickname") or (room.get("owner") or {}).get("nickname"),
        "title": room.get("title"),
        "living": room.get("status") == 2,
        "streams": {},
    }
    if not info["living"]:
        return info
    su = room.get("stream_url", {}) or {}
    # pull_data/options 均可为显式 null,用 or {} 而非 .get(...,{}) 防 AttributeError
    sdk = (su.get("live_core_sdk_data") or {}).get("pull_data") or {}
    quals = (sdk.get("options") or {}).get("qualities") or []
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


def _balanced_obj(s: str, start_key: str):
    """从 start_key 处的 { 起做括号平衡,返回该对象文本(考虑字符串内引号转义)。"""
    i = s.find(start_key)
    if i < 0:
        return None
    i = s.find("{", i)
    if i < 0:
        return None
    depth = in_str = esc = 0
    for k in range(i, len(s)):
        c = s[k]
        if in_str:
            if esc:
                esc = 0
            elif c == "\\":
                esc = 1
            elif c == '"':
                in_str = 0
        elif c == '"':
            in_str = 1
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return s[i : k + 1]
    return None


def _room_from_html(html: str, web_rid: str) -> dict:
    """从房间页 SSR 数据提取当前房间对象(roomStore.roomInfo.room)。

    抖音 web enter 接口现要求 a_bogus 签名(ttwid-only 返回空 body),
    改从页面内嵌的 self.__pace_f flight 块取。页面有两个 roomStore:初始
    空壳(roomInfo={})与注水后的真数据块;用当前房间 web_rid(全页唯一)
    锚定真数据块,避免命中空壳或推荐位其他房间。块内为 JSON 转义文本,
    反转义后按括号平衡抠出 roomInfo.room —— 其结构与 enter 的 data.data[0]
    一致,故可直接复用 _parse_enter。任一步取不到都返回 {}(→ 未开播)。
    """
    anchor = 'web_rid\\":\\"' + web_rid  # 转义态 web_rid":"<rid>,定位真数据块
    i = html.find(anchor)
    if i < 0:
        return {}
    start = html.rfind("self.__pace_f.push", 0, i)
    if start < 0:
        return {}
    end = html.find("</script>", start)
    block = html[start : end if end > 0 else len(html)]
    m = re.search(r'push\(\[\d+,"(.*)"\]\)', block, re.S)  # 贪婪到该块末尾的 "])
    if not m:
        return {}
    try:
        text = json.loads('"' + m.group(1) + '"')  # JS 字符串字面量 → 真实文本
        obj = _balanced_obj(text, '"roomInfo"')
        return (json.loads(obj).get("room") or {}) if obj else {}
    except json.JSONDecodeError:
        return {}


def parse(url: str) -> dict:
    """解析抖音房间(取 ttwid → 抓房间页 → 提取 SSR 房间数据 → _parse_enter)。"""
    web_rid = resolve_web_rid(url)
    headers = {"User-Agent": UA_DESKTOP, "Referer": REFERER, "Cookie": f"ttwid={_ttwid()}"}
    html = http_get(ROOM_URL.format(web_rid=web_rid), headers=headers).decode("utf-8", "ignore")
    room = _room_from_html(html, web_rid)
    return _parse_enter({"data": {"data": [room], "user": {}}}, web_rid)
