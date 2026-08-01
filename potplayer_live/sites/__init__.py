#!/usr/bin/env python3
"""平台派发层:按 URL 域名找到对应平台模块并解析。

新增平台 = 在本包内写一个模块(实现下面的接口)+ 在 SITES 里登记,server / cli 无需改动。

平台模块接口:
    DOMAINS       list[str]   匹配的域名关键字(如 ["huya.com"])
    PLAY_HEADERS  dict        拉流时用的 HTTP 头(Referer / User-Agent)
    parse(url)    -> dict     {rid, nick, title, living, streams{名:{quality,url,backups}}}
"""

import urllib.parse

from . import huya, douyin

# 已支持的平台模块(按需追加,如 douyu、bilibili)
SITES = [huya, douyin]


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


def is_category(url: str) -> bool:
    """判断输入意图是否为分区浏览。

    http URL 按域名委派给对应平台模块的 is_category(不支持的平台/房间 URL → False);
    非 http 的裸标识(名/别名/gid)默认走虎牙分区浏览(现状不变)。
    """
    p = urllib.parse.urlparse(url)
    if p.scheme in ("http", "https"):
        try:
            mod = get_site(url)
        except RuntimeError:
            return False
        f = getattr(mod, "is_category", None)
        return bool(f and f(url))
    return bool(url.strip())


def category_slug(url: str) -> str:
    """从分区页 URL 取 slug(/g/ 后第一段);裸串原样返回。"""
    p = urllib.parse.urlparse(url)
    if p.scheme in ("http", "https"):
        parts = p.path.strip("/").split("/")
        return parts[1] if len(parts) > 1 else ""
    return url.strip()


def list_rooms(url: str, pages: int = 3) -> dict:
    """列出分区房间。/g/ URL 按域名派发到对应平台;裸串默认虎牙(当前唯一平台)。"""
    p = urllib.parse.urlparse(url)
    mod = get_site(url) if p.scheme in ("http", "https") else huya
    return mod.list_category(category_slug(url), pages=pages)
