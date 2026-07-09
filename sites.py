#!/usr/bin/env python3
"""平台派发层:按 URL 域名找到对应平台模块并解析。

新增平台 = 写一个模块(实现下面的接口)+ 在 SITES 里登记,server.py / cli.py 无需改动。

平台模块接口:
    DOMAINS       list[str]   匹配的域名关键字(如 ["huya.com"])
    PLAY_HEADERS  dict        拉流时用的 HTTP 头(Referer / User-Agent)
    parse(url)    -> dict     {rid, nick, title, living, streams{名:{quality,url,backups}}}
"""
import urllib.parse

import huya

# 已支持的平台模块(按需追加,如 douyu、bilibili)
SITES = [huya]


def get_site(url: str):
    host = urllib.parse.urlparse(url).netloc.lower()
    for mod in SITES:
        if any(d in host for d in mod.DOMAINS):
            return mod
    raise RuntimeError(f"不支持的平台: {url}")


def parse(url: str) -> dict:
    return get_site(url).parse(url)


def play_headers(url: str) -> dict:
    return getattr(get_site(url), "PLAY_HEADERS", {})


def supported() -> list:
    """所有已支持的域名,用于提示。"""
    return [d for mod in SITES for d in mod.DOMAINS]
