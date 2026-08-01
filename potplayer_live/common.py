#!/usr/bin/env python3
"""平台无关的公共工具:HTTP、清晰度选择、m3u 生成。

各平台解析模块(如 huya.py)与派发层(sites.py)共用这里的东西。
"""

import gzip
import hashlib
import urllib.request


def _gunzip(raw: bytes) -> bytes:
    """部分虎牙接口即使未声明也返回 gzip(magic 1f 8b),透明解压。"""
    if raw[:2] == b"\x1f\x8b":
        return gzip.decompress(raw)
    return raw


def http_get(url, headers=None, timeout=15, data=None):
    """data 为 None 时 GET,否则 POST(bytes body)。返回已透明解压的原始字节。"""
    req = urllib.request.Request(url, data=data, headers=headers or {})
    return _gunzip(urllib.request.urlopen(req, timeout=timeout).read())


def md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def pick(info: dict, quality: str = None):
    """选清晰度:quality 为 None 取最高(原画优先,quality==0 视为原画);
    否则按显示名或码率匹配。返回 (名称, stream)。"""
    streams = info["streams"]
    if not streams:
        return None, None
    if quality:
        for name, s in streams.items():
            if quality == name or quality == str(s["quality"]):
                return name, s
    name = max(
        streams, key=lambda k: (streams[k]["quality"] == 0, streams[k]["quality"])
    )
    return name, streams[name]


def m3u_content(title: str, stream: dict) -> str:
    """多线路 m3u 播放列表(卡住可切备用线路);#EXTINF 名即 PotPlayer 播放列表显示的标题。"""
    out = ["#EXTM3U"]
    urls = [stream["url"]] + stream["backups"]
    for i, u in enumerate(urls):
        out.append(f"#EXTINF:-1 ,{title}" + ("" if i == 0 else f" - 备用{i}"))
        out.append(u)
    return "\n".join(out)
