#!/usr/bin/env python3
"""虎牙(huya.com)平台解析模块。

平台模块统一接口(见 sites.py):
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

from common import http_get, md5

# 本模块的域名与拉流头
DOMAINS = ["huya.com"]
UA_MOBILE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/14.0 Mobile/15E148 Safari/604.1")
UA_DESKTOP = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/17.3.1 Safari/605.1.15")
REFERER = "https://www.huya.com/"
PLAY_HEADERS = {"User-Agent": UA_DESKTOP, "Referer": REFERER}

# 签名 query 参数顺序模板(用来保持各参数顺序一致)
_EXAMPLE = ("wsSecret=x&wsTime=x&seqid=x&ctype=x&ver=1&fs=bgct&ratio=2000&dMod=mseh-8"
            "&sdkPcdn=1_1&u=x&t=100&sv=2407051433&sdk_sid=x&a_block=0&sf=1")


def _api_get(url):
    return http_get(url, headers={"User-Agent": UA_MOBILE})


# ---- uid 的 64bit 循环移位 ----
def _rot_uid(uid: int) -> int:
    s = format(uid, "b").zfill(64)
    a, r = s[:32], s[32:]
    i = 8
    n = r[i:32] + r[:i]                 # 把 r 的前 8 位挪到末尾
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
    fm = base64.b64decode(urllib.parse.unquote(anti["fm"])).decode()  # 形如 xxx_$0_$1_$2_$3
    wstime, ctype = anti["wsTime"], anti["ctype"]
    t = anti.get("t", "100")
    s = md5(f"{seqid}|{ctype}|{t}")
    u = (fm.replace("$0", str(convert_uid)).replace("$1", stream_name)
           .replace("$2", s).replace("$3", wstime))
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
    info = {"rid": rid, "nick": ld.get("nick"),
            "title": ld.get("roomName") or ld.get("introduction") or ld.get("nick"),
            "living": data.get("liveStatus") == "ON", "streams": {}}
    if not info["living"]:
        return info
    uid = (int(time.time() * 1000) % int(1e10) * int(1e3) + random.randint(100, 999)) % 4294967295
    lines = data["stream"]["baseSteamInfoList"]
    lines = sorted(lines, key=lambda b: "txdirect.flv.huya.com" in b["sFlvUrl"])  # txdirect 排后
    base_urls = [_sign_url(uid, b["sStreamName"], b["sFlvUrl"],
                           b["sFlvUrlSuffix"], b["sFlvAntiCode"]) for b in lines]
    for br in json.loads(ld["bitRateInfo"]):
        name, rate = br["sDisplayName"], br["iBitRate"]
        us = [(u.replace("&ratio=0", f"&ratio={rate}") if rate else u.replace("&ratio=0", ""))
              for u in base_urls]
        info["streams"][name] = {"quality": rate, "url": us[0], "backups": us[1:]}
    return info
