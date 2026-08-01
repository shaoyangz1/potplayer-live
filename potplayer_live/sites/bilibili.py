#!/usr/bin/env python3
"""哔哩哔哩直播(live.bilibili.com)平台解析模块。

平台模块统一接口见 sites/__init__.py。取流走 web getRoomPlayInfo 明文链路:
room_init 短号转真房号 → getRoomPlayInfo 拿多档多线路 flv。

直播取流无需 wbi 签名(与点播 x/player/wbi/playurl 不同,streamlink/yt-dlp/ihmily
现行做法均明文 query),纯 urllib+json 即可。原画/4K 需登录:设环境变量
BILI_COOKIE(浏览器里的 SESSDATA)自动解锁,不设则免登录、最高约蓝光。
"""

import os
import json
import urllib.parse

from ..common import http_get

DOMAINS = ["live.bilibili.com"]
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) "
    "Gecko/20100101 Firefox/127.0"
)
REFERER = "https://live.bilibili.com/"
PLAY_HEADERS = {"User-Agent": UA, "Referer": REFERER}
# 调 API 额外带 Origin,CDN 拉流只认 Referer(PLAY_HEADERS 已含)。
_API_HEADERS = {"User-Agent": UA, "Origin": "https://live.bilibili.com", "Referer": REFERER}

# qn 档位 → 显示名。quality 存 qn 数值:pick 默认取最大档(原画 10000),
# --quality 可按显示名或数值匹配。
_QN_NAME = {30000: "杜比", 20000: "4K", 10000: "原画", 400: "蓝光", 250: "超清", 150: "高清", 80: "流畅"}


def _get_json(url, cookie=None):
    """GET 返回 JSON;cookie 非空时带上(解锁原画/4K)。"""
    h = dict(_API_HEADERS)
    if cookie:
        h["Cookie"] = cookie
    return json.loads(http_get(url, headers=h))


def resolve_room(short, fetch=_get_json):
    """短号→(真房号 int, 开播?)。room_init 对短号/真号通用,顺带拿 live_status。

    fetch 可注入,测试用假响应驱动、不触网。"""
    d = fetch(f"https://api.live.bilibili.com/room/v1/Room/room_init?id={short}")["data"]
    return d["room_id"], d["live_status"] == 1


def _room_meta(rid, fetch=_get_json):
    """取标题/主播名(getInfoByRoom);title 缺失回退主播名。"""
    d = fetch(
        f"https://api.live.bilibili.com/xlive/web-room/v1/index/getInfoByRoom?room_id={rid}"
    )["data"]
    nick = (d.get("anchor_info") or {}).get("base_info", {}).get("uname")
    title = (d.get("room_info") or {}).get("title") or nick
    return nick, title


def _streams_from_playinfo(data: dict) -> dict:
    """从 getRoomPlayInfo 的 data 提取各档流(纯函数,不触网)。

    完整地址 = host + base_url + extra;同一 codec 的 url_info 多条 = 多线路
    (首条主线,其余 backups)。stream 排序让 flv(http_stream)在前,同档
    已存则保留先到的 → 优先给 PotPlayer flv,hls 仅作补充。"""
    streams = {}
    playurl = (data.get("playurl_info") or {}).get("playurl") or {}
    stream_list = sorted(
        playurl.get("stream", []), key=lambda s: s.get("protocol_name") != "http_stream"
    )
    for stream in stream_list:
        for fmt in stream.get("format", []):
            for codec in fmt.get("codec", []):
                base = codec["base_url"]
                urls = [ui["host"] + base + ui.get("extra", "") for ui in codec["url_info"]]
                if not urls:
                    continue
                name = _QN_NAME.get(codec["current_qn"], str(codec["current_qn"]))
                if name not in streams:
                    streams[name] = {"quality": codec["current_qn"], "url": urls[0], "backups": urls[1:]}
    return streams


def parse(url: str) -> dict:
    """解析 B 站直播间,返回房间信息与各清晰度(多线路 backups)。"""
    short = urllib.parse.urlparse(url).path.strip("/").split("/")[0]
    rid, living = resolve_room(short)
    info = {"rid": str(rid), "nick": None, "title": None, "living": living, "streams": {}}
    if not living:
        return info

    info["nick"], info["title"] = _room_meta(rid)
    # 请求 qn=10000 原画;权限不足接口静默降档。codec=0 取 H.264,PotPlayer 最稳。
    q = urllib.parse.urlencode({
        "room_id": rid, "protocol": "0,1", "format": "0,1,2", "codec": "0",
        "qn": 10000, "platform": "web", "ptype": 8,
    })
    data = _get_json(
        f"https://api.live.bilibili.com/xlive/web-room/v2/index/getRoomPlayInfo?{q}",
        os.environ.get("BILI_COOKIE"),
    )["data"]
    if data.get("live_status") == 0:  # room_init 到出流间隙下播
        info["living"] = False
        return info
    info["streams"] = _streams_from_playinfo(data)
    return info
