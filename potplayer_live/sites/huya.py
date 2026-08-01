#!/usr/bin/env python3
"""虎牙(huya.com)平台解析模块。

平台模块统一接口(见 sites/__init__.py):
    DOMAINS        匹配的域名关键字
    PLAY_HEADERS   拉流时用的 HTTP 头(Referer/User-Agent)
    parse(url)     -> {rid, nick, title, living, streams{清晰度:{quality,url,backups}}}
"""

import re
import json
import time
import random
import base64
import urllib.parse

from ..common import http_get, http_get_text, decode_text, md5

# 本模块的域名与拉流头
DOMAINS = ["huya.com"]
UA_MOBILE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/14.0 Mobile/15E148 Safari/604.1"
)
UA_DESKTOP = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.3.1 Safari/605.1.15"
)
REFERER = "https://www.huya.com/"
PLAY_HEADERS = {"User-Agent": UA_DESKTOP, "Referer": REFERER}

# 分区目录(把中文名/别名映射到 gid);bussType=1 返回热门分区汇总
CATEGORY_URL = "https://live.cdn.huya.com/liveconfig/game/bussLive?bussType=1"


def _parse_categories(raw: bytes) -> list:
    """解析 bussLive 返回的分区目录。返回 [{gid, host(别名), name(中文名), online}]。"""
    data = json.loads(decode_text(raw)).get("data", []) or []
    out = []
    for c in data:
        gid = c.get("gid")
        if gid is None:
            continue
        out.append(
            {
                "gid": int(gid),
                "host": c.get("gameHostName") or "",
                "name": c.get("gameFullName") or "",
                "online": c.get("totalCount"),
            }
        )
    return out


def _categories() -> list:
    """拉分区目录(gzip 兼容 + 3 次重试)。全部失败则抛最后一次异常。"""
    last = None
    for i in range(3):
        try:
            return _parse_categories(
                http_get(CATEGORY_URL, headers={"User-Agent": UA_MOBILE})
            )
        except Exception as e:  # noqa: BLE001 目录不可用不应连累透传路径
            last = e
            if i < 2:  # 最后一次失败不再空等,直接抛给 _safe_categories 兜底
                time.sleep(1)
    raise last


def _safe_categories() -> list:
    """目录不可用时返回空表(gid/别名/slug 透传不依赖目录)。"""
    try:
        return _categories()
    except Exception:
        return []


def is_category(url: str) -> bool:
    """分区页判定:http 路径首段为 g(如 /g/lol)才算分区浏览。"""
    p = urllib.parse.urlparse(url)
    if p.scheme in ("http", "https"):
        return p.path.strip("/").split("/")[0] == "g"
    return False


def resolve_category(ident, categories=None):
    """把分区标识解析成 (slug, 显示名)。

    ident: gid / 别名(gameHostName) / 中文名(gameFullName) / 分区页 URL 的 slug。
    slug 优先直接用 ident(gid/别名/slug 透传,不阻塞于目录能否命中);
    中文名输入则查目录匹配到别名或 gid 作 slug。
    显示名尽量从目录按 gid/别名反查中文名,查不到就用 ident 本身。
    """
    ident = str(ident).strip()
    cats = categories if categories is not None else _safe_categories()
    by_host = {c["host"].lower(): c for c in cats if c.get("host")}
    by_gid = {str(c["gid"]): c for c in cats if c.get("gid") is not None}
    by_name = {c["name"]: c for c in cats if c.get("name")}
    if ident.isdigit():  # gid 透传,显示名反查
        c = by_gid.get(ident)
        return ident, (c["name"] if c and c["name"] else ident)
    c = by_host.get(ident.lower())  # 别名透传,显示名反查
    if c:
        return ident, (c["name"] or ident)
    c = by_name.get(ident)  # 中文名 → 别名/gid 作 slug
    if c:
        return (c["host"] or str(c["gid"])), c["name"]
    return ident, ident  # 目录外:当 slug 透传


# 移动端 SSR 分区页(UTF-8),?page=N 翻页;每页约 9 个房间卡片
ROOM_LIST_URL = "https://m.huya.com/g/{slug}?page={page}"
_ROOM_CARD = re.compile(
    r'<a href="/([^"]+)" class="qqqq g-link">.*?'
    r'<span class="nick">(.*?)</span>.*?'
    r'<span class="viewer-count">(.*?)</span>.*?'
    r'<p class="title">(.*?)</p>',
    re.S,
)


def _parse_rooms(html: str) -> list:
    """从分区页 HTML 提取房间卡片。"""
    out = []
    for room, nick, viewers, title in _ROOM_CARD.findall(html):
        out.append(
            {
                "room": room,
                "nick": nick.strip(),
                "viewers": viewers.strip(),
                "title": title.strip(),
            }
        )
    return out


def list_category(ident, pages=3, categories=None, fetch=None):
    """列出分区在播房间(按人气,跨页去重保序)。

    返回 {"name": 显示名, "slug": slug, "rooms": [{room, nick, viewers, title}, ...]}。
    fetch(slug, page)->html 可注入,便于不触网测试。
    """
    slug, name = resolve_category(ident, categories=categories)
    if fetch is None:

        def fetch(slug, page):
            return http_get_text(
                ROOM_LIST_URL.format(slug=slug, page=page),
                headers={"User-Agent": UA_MOBILE},
            )

    seen, rooms = set(), []
    for page in range(1, pages + 1):
        try:
            html = fetch(slug, page)
        except Exception:
            break
        page_rooms = _parse_rooms(html)
        if not page_rooms:
            break  # 该页无卡片 → 到底了
        for r in page_rooms:
            if r["room"] not in seen:
                seen.add(r["room"])
                r["url"] = "https://www.huya.com/" + r["room"]  # cli 直接用,不再拼域名
                rooms.append(r)
    return {"name": name, "slug": slug, "rooms": rooms}


# 签名 query 参数顺序模板(用来保持各参数顺序一致)
_EXAMPLE = (
    "wsSecret=x&wsTime=x&seqid=x&ctype=x&ver=1&fs=bgct&ratio=2000&dMod=mseh-8"
    "&sdkPcdn=1_1&u=x&t=100&sv=2407051433&sdk_sid=x&a_block=0&sf=1"
)


def _api_get(url):
    return http_get(url, headers={"User-Agent": UA_MOBILE})


# ---- uid 的 64bit 循环移位 ----
def _rot_uid(uid: int) -> int:
    s = format(uid, "b").zfill(64)
    a, r = s[:32], s[32:]
    i = 8
    n = r[i:32] + r[:i]  # 把 r 的前 8 位挪到末尾
    return int(a + n, 2)


def _anti_dict(anti: str) -> dict:
    d = {}
    for kv in anti.split("&"):
        if "=" in kv:
            k, v = kv.split("=", 1)
            d[k] = v
    return d


# ---- wsSecret:防盗链签名 ----
def _ws_secret(anti: dict, convert_uid: int, seqid: int, stream_name: str) -> str:
    fm = base64.b64decode(
        urllib.parse.unquote(anti["fm"])
    ).decode()  # 形如 xxx_$0_$1_$2_$3
    wstime, ctype = anti["wsTime"], anti["ctype"]
    t = anti.get("t", "100")
    s = md5(f"{seqid}|{ctype}|{t}")
    u = (
        fm.replace("$0", str(convert_uid))
        .replace("$1", stream_name)
        .replace("$2", s)
        .replace("$3", wstime)
    )
    return md5(u)


# ---- 拼出带签名的 flv 地址(ratio=0 占位,后按码率替换)----
def _sign_url(uid, s_stream_name, s_flv_url, s_flv_suffix, s_flv_anticode):
    now_ms = int(time.time() * 1000)
    anti = _anti_dict(s_flv_anticode)
    seqid = uid + now_ms
    convert_uid = _rot_uid(uid)
    anti["wsSecret"] = _ws_secret(anti, convert_uid, seqid, s_stream_name)
    anti["u"] = str(convert_uid)
    anti["seqid"] = str(seqid)
    anti["sdk_sid"] = str(now_ms)
    anti["ratio"] = "0"
    base = s_flv_url.replace("http://", "https://")
    pars = []
    for item in _EXAMPLE.split("&"):
        k, v = item.split("=", 1)
        pars.append(f"{k}={anti.get(k, v)}")
    return f"{base}/{s_stream_name}.{s_flv_suffix}?" + "&".join(pars)


# ---- 房间号解析:数字直接用;别名(如 lpl)先抓页面拿 lProfileRoom ----
def resolve_rid(url: str) -> int:
    slug = urllib.parse.urlparse(url).path.strip("/").split("/")[0]
    if slug.isdigit():
        return int(slug)
    html = _api_get(url).decode("utf-8", "ignore")
    for pat in (r'"lProfileRoom"\s*:\s*(\d+)', r'"profileRoom"\s*:\s*(\d+)'):
        m = re.search(pat, html)
        if m:
            return int(m.group(1))
    raise RuntimeError("找不到 profileRoom,检查房间地址是否正确")


def parse(url: str) -> dict:
    """解析虎牙房间,返回房间信息与各清晰度(每档含主线路 url + 备用 backups)。"""
    rid = resolve_rid(url)
    api = f"https://mp.huya.com/cache.php?m=Live&do=profileRoom&roomid={rid}"
    data = json.loads(_api_get(api))["data"]
    ld = data["liveData"]
    info = {
        "rid": rid,
        "nick": ld.get("nick"),
        "title": ld.get("roomName") or ld.get("introduction") or ld.get("nick"),
        "living": data.get("liveStatus") == "ON",
        "streams": {},
    }
    if not info["living"]:
        return info
    uid = (
        int(time.time() * 1000) % int(1e10) * int(1e3) + random.randint(100, 999)
    ) % 4294967295
    lines = data["stream"]["baseSteamInfoList"]
    lines = sorted(
        lines, key=lambda b: "txdirect.flv.huya.com" in b["sFlvUrl"]
    )  # txdirect 排后
    base_urls = [
        _sign_url(
            uid, b["sStreamName"], b["sFlvUrl"], b["sFlvUrlSuffix"], b["sFlvAntiCode"]
        )
        for b in lines
    ]
    for br in json.loads(ld["bitRateInfo"]):
        name, rate = br["sDisplayName"], br["iBitRate"]
        us = [
            (
                u.replace("&ratio=0", f"&ratio={rate}")
                if rate
                else u.replace("&ratio=0", "")
            )
            for u in base_urls
        ]
        info["streams"][name] = {"quality": rate, "url": us[0], "backups": us[1:]}
    return info
