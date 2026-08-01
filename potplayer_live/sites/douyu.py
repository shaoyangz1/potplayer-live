#!/usr/bin/env python3
"""斗鱼(douyu.com)平台解析模块。

平台模块统一接口(见 sites/__init__.py):
    DOMAINS        匹配的域名关键字
    PLAY_HEADERS   拉流时用的 HTTP 头(Referer/User-Agent)
    parse(url)     -> {rid, nick, title, living, streams{清晰度:{quality,url,backups}}}

取流走 2025 起的「免 JS」链路(biliup 主线 / DanmakuRender 现行做法):
getEncryption 下发密钥 → 纯 MD5 迭代算 auth → POST getH5PlayV1 拿 flv。
全程只用 hashlib.md5,不依赖房间页那段混淆 JS(ub98484234),也无需 JS 引擎。
"""

import re
import json
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from ..common import http_get, md5

DOMAINS = ["douyu.com"]
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
REFERER = "https://www.douyu.com"
PLAY_HEADERS = {"User-Agent": UA, "Referer": REFERER}

# 固定 did 免带 Cookie(值来自社区通行做法);getEncryption 与 getH5PlayV1 必须同一 UA。
DID = "10000000000000000000000000001501"
_API_HEADERS = {"User-Agent": UA, "Referer": REFERER}

# m.douyu.com 房间页里真实房号的两种常见位置(别名/短号 → 真 rid)
_RID_PATS = (r'"rid"\s*:\s*(\d+)\s*,\s*"vipId', r'"roomInfo"\s*:\s*\{\s*"rid"\s*:\s*(\d+)')


def _get_text(url):
    return http_get(url, headers=_API_HEADERS).decode("utf-8", "ignore")


def resolve_rid(url, fetch=_get_text):
    """房间号解析:query 里的 rid / 纯数字路径直接用;别名或短号抓 m 页正则取真 rid。

    fetch 做成可注入参数,测试用假页面驱动、不触网。"""
    u = urllib.parse.urlparse(url)
    q = urllib.parse.parse_qs(u.query)
    if q.get("rid") and q["rid"][0].isdigit():
        return q["rid"][0]
    slug = u.path.strip("/").split("/")[0]
    if slug.isdigit():
        return slug
    html = fetch(f"https://m.douyu.com/{slug}")
    for pat in _RID_PATS:
        m = re.search(pat, html)
        if m:
            return m.group(1)
    raise RuntimeError("找不到斗鱼房间号,检查地址是否正确")


def _auth(enc: dict, rid: str, ts: int) -> str:
    """按 getEncryption 下发的参数算鉴权 auth(纯 MD5,不执行 JS)。

    secret 从 rand_str 起对 (secret+key) 迭代 enc_time 次;is_special==1 时 salt 为空,
    否则 salt = f"{rid}{ts}";最终 auth = md5(secret + key + salt)。"""
    secret, key = enc["rand_str"], enc["key"]
    for _ in range(int(enc["enc_time"])):
        secret = md5(secret + key)
    salt = "" if int(enc.get("is_special", 0)) == 1 else f"{rid}{ts}"
    return md5(secret + key + salt)


def _play_url(data: dict) -> str:
    """getH5PlayV1 响应拼完整 flv:rtmp_url + '/' + rtmp_live(rtmp_live 已带鉴权 token)。"""
    return data["rtmp_url"].rstrip("/") + "/" + data["rtmp_live"]


def _room_from_betard(bet: dict) -> dict:
    """从 betard 接口取昵称/标题/开播状态。开播 = show_status==1 且 videoLoop==0(非轮播)。"""
    room = bet.get("room", bet)
    nick = room.get("nickname")
    return {
        "nick": nick,
        "title": room.get("room_name") or nick,
        "living": room.get("show_status") == 1 and room.get("videoLoop") == 0,
    }


def _encryption(did):
    d = http_get(
        f"https://www.douyu.com/wgapi/livenc/liveweb/websec/getEncryption?did={did}",
        headers=_API_HEADERS,
    )
    return json.loads(d)["data"]


def _get_play(rid, rate, enc_data, tt, did, auth, cdn="hw-h5"):
    """POST getH5PlayV1 取某档清晰度(rate)的播放数据。auth/enc_data/tt 与 rate 无关,可跨档复用。"""
    body = {
        "cdn": cdn, "rate": rate, "ver": "Douyu_new",
        "iar": 0, "ive": 0, "rid": rid, "hevc": 0, "fa": 0, "sov": 0,
        "enc_data": enc_data, "tt": tt, "did": did, "auth": auth,
    }
    data = http_get(
        f"https://www.douyu.com/lapi/live/getH5PlayV1/{rid}",
        headers={**_API_HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
        data=urllib.parse.urlencode(body).encode(),
    )
    return json.loads(data)["data"]


def parse(url: str) -> dict:
    """解析斗鱼房间,返回房间信息与各清晰度。每档单独发一次 getH5PlayV1(单线路,backups 空)。"""
    rid = resolve_rid(url)
    room = _room_from_betard(json.loads(_get_text(f"https://www.douyu.com/betard/{rid}")))
    info = {
        "rid": rid, "nick": room["nick"], "title": room["title"],
        "living": room["living"], "streams": {},
    }
    if not info["living"]:
        return info

    enc = _encryption(DID)
    ts = int(time.time())
    auth = _auth(enc, rid, ts)
    enc_data = enc["enc_data"]

    # rate=0 一次拿到最高清 flv + 全部清晰度清单(multirates),其余档并发各取一次。
    # ponytail: 未处理 scdn 限速换线(返回 scdn 前缀时应取 cdnsWithName[-1] 重试);
    # 默认 hw-h5 通常直给正常线路,serve 断流重解析也会自然重取,够用。
    first = _get_play(rid, 0, enc_data, ts, DID, auth)
    rates = list(first.get("multirates") or [])
    if not any(m.get("rate") == 0 for m in rates):  # 清单未含 rate=0 时,用 first 补一档
        rates.insert(0, {"name": "原画", "rate": 0, "bit": 0})

    def one(mr):
        rate = mr.get("rate", 0)
        return mr, (first if rate == 0 else _get_play(rid, rate, enc_data, ts, DID, auth))

    with ThreadPoolExecutor(max_workers=min(8, len(rates))) as ex:
        for mr, data in ex.map(one, rates):
            name = mr.get("name") or str(mr.get("rate"))
            # quality 用码率 bit(与虎牙 iBitRate 语义一致):pick 默认取最高、--quality 可按码率匹配
            info["streams"][name] = {
                "quality": mr.get("bit") or 0, "url": _play_url(data), "backups": []
            }
    return info
