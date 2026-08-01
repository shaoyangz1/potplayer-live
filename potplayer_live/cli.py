#!/usr/bin/env python3
"""potplayer-live 命令行入口(PotPlayer 专用)。

    python -m potplayer_live <房间地址> [选项]

选项:
    --room_id URL   直播间地址,与位置参数等价的命名写法(如 --room_id https://live.bilibili.com/24678311)
    --quality Q     清晰度显示名或码率(如 "原画" / 蓝光10M / 2000)，默认最高
    --line K        直链/m3u 模式下选第 K 条线路(0 起)，默认 0
    --title T       自定义 PotPlayer 窗口标题，默认用房间名(主播名)
    --mode MODE     打开方式，默认 serve:
                      serve      本地转流代理(推荐)：固定地址，自动跨 2 分钟断流自愈
                      serve-only 只起常驻代理、不拉起 PotPlayer(房间地址可省)：别处用 serve 复用它播放,日志集中于此
                      m3u        多线路播放列表：卡住时在 PotPlayer 播放列表切备用线路
                      direct     单条 flv 直链：最简单，卡住无法恢复
                      print      只解析打印各清晰度地址，不打开播放器
    --port P        serve 模式端口，默认 8787

PotPlayer 路径:环境变量 POTPLAYER 优先，否则自动探测默认安装位置 / Scoop。
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from . import sites, common

PORT_SCAN = 20  # 从 --port 起最多向后扫描多少个端口


def _probe(port):
    """探测端口:'ours'=本 skill 代理 / 'free'=无人监听 / 'other'=被别的占用。"""
    try:
        # 超时给到 4s:某些环境对已关闭 loopback 端口要 ~2s 才回“拒绝”,超时太短会
        # 在收到拒绝前先超时、把空闲端口误判为 other。正常机器瞬间拒绝,上限无副作用。
        r = urllib.request.urlopen(f"http://127.0.0.1:{port}/__ping__", timeout=4)
        return "ours" if b"potplayer-live" in r.read(32) else "other"
    except urllib.error.URLError as e:
        return "free" if isinstance(e.reason, ConnectionRefusedError) else "other"
    except Exception:
        return "other"


def _wait_ready(port, timeout=10.0):
    """轮询 __ping__ 直到本 skill 的代理就绪或超时,返回是否就绪。

    替代启动后固定 sleep:解析快时立刻返回(不空等),解析慢时也不会过早打开
    播放器(固定 sleep 两头都不讨好)。"""
    deadline = time.monotonic() + timeout
    while True:
        if _probe(port) == "ours":
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.15)


def _choose_port(base):
    """优先复用已有代理端口；否则返回第一个空闲端口。返回 (port, reuse)。

    并发探测:某些环境(如启用了 TUN/过滤驱动的代理软件)对已关闭的 loopback 端口
    也要 ~2s 才返回“拒绝”,串行扫 20 个端口会拖到 ~40s,故并发一次扫完。"""
    ports = list(range(base, base + PORT_SCAN))
    with ThreadPoolExecutor(max_workers=PORT_SCAN) as ex:
        res = dict(zip(ports, ex.map(_probe, ports)))
    for p in ports:  # 先找可复用的
        if res[p] == "ours":
            return p, True
    for p in ports:  # 再找空闲的新起
        if res[p] == "free":
            return p, False
    raise RuntimeError(f"{base}~{base + PORT_SCAN - 1} 端口都被占用，换个 --port")


# PotPlayer 默认安装位置(找不到时用环境变量 POTPLAYER 指定)
_POTPLAYER_CANDIDATES = [
    r"C:\Program Files\DAUM\PotPlayer\PotPlayerMini64.exe",
    r"C:\Program Files (x86)\DAUM\PotPlayer\PotPlayerMini.exe",
    r"C:\Program Files\DAUM\PotPlayer64\PotPlayerMini64.exe",
]


def _scoop_candidates():
    """Scoop 安装的 PotPlayer(scoop 不给 GUI 程序建 shim，需按 apps 目录找)。
    兼容重定位过的 scoop 根:SCOOP 环境变量 → ~\\scoop → 从当前解释器路径反推。"""
    roots = []
    if os.environ.get("SCOOP"):
        roots.append(os.environ["SCOOP"])
    roots.append(os.path.join(os.path.expanduser("~"), "scoop"))
    low = sys.executable.replace("/", "\\").lower()
    marker = "\\scoop\\apps\\"
    if marker in low:
        roots.append(sys.executable[: low.index(marker) + len("\\scoop")])
    out = []
    for r in roots:
        for exe in ("PotPlayerMini64.exe", "PotPlayerMini.exe"):
            out.append(os.path.join(r, "apps", "potplayer", "current", exe))
    return out


def _potplayer_exe():
    p = os.environ.get("POTPLAYER")
    if p and os.path.exists(p):
        return p
    for c in _POTPLAYER_CANDIDATES + _scoop_candidates():
        if os.path.exists(c):
            return c
    for name in ("PotPlayerMini64.exe", "PotPlayerMini.exe"):
        found = shutil.which(name)
        if found:
            return found
    raise RuntimeError(
        "找不到 PotPlayer。请安装后重试，或用环境变量 POTPLAYER 指向 exe，"
        r"例如 set POTPLAYER=D:\Apps\PotPlayer\PotPlayerMini64.exe"
    )


def _serve_url(port, room, quality):
    """本地代理地址:把完整房间地址与清晰度写进 query。

    这样无论是新起的代理还是复用别处已在跑的代理,server 都按本次请求携带的
    room/quality 解析——复用时 --quality 不再被忽略,也不受被复用代理启动平台的限制。"""
    q = {"room": room}
    if quality:
        q["quality"] = quality
    # safe="/:"：放行 : 与 / 不做百分号编码,常见地址保持可读;& ? # 等仍会编码,
    # 不会破坏 query 结构。server 端 parse_qs 解析不受影响(见 test_roundtrips)。
    return f"http://127.0.0.1:{port}/live.flv?" + urllib.parse.urlencode(q, safe="/:")


def _open_potplayer(target, title, is_url):
    """PotPlayer 播放。is_url 时用 PotPlayer 的「地址\\标题」语法设置窗口标题;
    本地 m3u 文件靠内部 #EXTINF 名显示标题,故不拼标题。

    关于请求头:PotPlayer 的 /referer、/user_agent 命令行开关不可靠——值含空格
    (如桌面 UA)时会被其内部解析器拆散、连累标题与输入项。虎牙的已签名 flv
    (wsSecret 即鉴权)无需任何请求头即可播放;若将来接入依赖请求头的平台,请走
    serve 模式:请求头由本地代理负责,最稳妥。"""
    exe = _potplayer_exe()
    arg = f"{target}\\{title}" if (is_url and title) else target
    subprocess.Popen([exe, arg])


def main():
    ap = argparse.ArgumentParser(prog="potplayer-live")
    ap.add_argument(
        "url",
        nargs="?",
        default=None,
        help="直播间地址(虎牙 https://www.huya.com/lpl、抖音 https://live.douyin.com/123456);--mode serve-only 可省",
    )
    ap.add_argument(
        "--room_id",
        default=None,
        help="直播间地址,与位置参数等价的命名写法(如 --room_id https://live.bilibili.com/24678311)",
    )
    ap.add_argument("--quality", default=None)
    ap.add_argument("--line", type=int, default=0)
    ap.add_argument("--title", default=None)
    ap.add_argument(
        "--mode", default="serve",
        choices=["serve", "serve-only", "m3u", "direct", "print"],
    )
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument(
        "--grace",
        type=int,
        default=180,
        help="serve 模式:无连接空闲多少秒后自动退出，<=0 常驻，默认 180",
    )
    a = ap.parse_args()
    room = a.room_id or a.url  # --room_id 与位置参数等价,任一即可(serve-only 都可省)
    if room is None and a.mode != "serve-only":
        ap.error("需要房间地址(位置参数或 --room_id;仅 --mode serve-only 可省)")
    return play_room(room, a)


def _serve_only(a):
    """只起常驻代理,不解析房间、不拉起 PotPlayer。

    纯中转:不绑任何房间(即便给了 --room_id/地址也忽略,只生效 serve-only 语义),
    launcher 各自带 ?room= 播不同房间(裸连 /live.flv 会报错)。所有断流/转流日志都
    打印在本进程——一处 server 常驻,别处用 --mode serve 复用它来播放(那些命令行只
    打印复用提示,日志集中在这里),方便同时从多个命令行启动多个播放。"""
    port, reuse = _choose_port(a.port)
    if reuse:
        print(f"已有代理在端口 {port} 运行,无需重复起(用 --port 指定别的端口可再起一个)。")
        return 0
    srv = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "potplayer_live.server",
            "",          # 纯中转:不绑房间(server 收到空串即无默认,裸连报错);launcher 都带 ?room=
            str(port),
            a.quality or "",
            "0",         # 纯代理强制常驻:没有播放器生命周期可挂靠,空闲自动退出没意义
        ]
    )
    if not _wait_ready(port):
        print("警告:本地代理未在预期时间内就绪。")
    hint = "" if port == 8787 else f" --port {port}"  # 非默认端口才需在播放命令里带上
    print(f"本地代理已启动(端口 {port}),常驻。Ctrl+C 结束。")
    print("另开一个控制台,播放任意房间即会复用本代理(断流/转流日志都集中在这里):")
    print(f"    uv run -m potplayer_live <房间地址>{hint}")
    try:
        srv.wait()
    except KeyboardInterrupt:
        srv.terminate()
    return 0


def play_room(url, a):
    if a.mode == "serve-only":
        return _serve_only(a)  # 纯中转:忽略房间,不解析、不开播放器
    info = sites.parse(url)
    print(f"房间号 : {info['rid']}")
    print(f"主播   : {info['nick']}")
    print(f"标题   : {info['title']}")
    print(f"直播中 : {info['living']}")
    if not info["living"]:
        print("主播未开播。")
        return 1

    name, stream = common.pick(info, a.quality)
    if stream is None:
        print("该直播间未取到可播放的 flv 流(可能仅提供 HLS 或流结构异常)。")
        return 1
    title = a.title or info["nick"] or info["title"]  # 默认用房间名(主播名)
    urls = [stream["url"]] + stream["backups"]
    flv = urls[a.line % len(urls)]
    print(f"清晰度 : {name} (quality={stream['quality']}, 线路数={len(urls)})")

    if a.mode == "print":
        for n, s in sorted(info["streams"].items(), key=lambda x: -x[1]["quality"]):
            print(f"\n[{n}] quality={s['quality']} 线路数={1 + len(s['backups'])}")
            for i, u in enumerate([s["url"]] + s["backups"]):
                print(f"  线路{i}: {u}")
        return 0

    if a.mode == "direct":
        _open_potplayer(flv, title, is_url=True)
        print("已用直链打开 (PotPlayer)。注意:卡住无法自动恢复。")
        return 0

    if a.mode == "m3u":
        d = os.path.join(tempfile.gettempdir(), "POTPLAYER-LIVE")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"{info['rid']}.m3u")
        with open(path, "w", encoding="utf-8") as f:
            f.write(common.m3u_content(title, stream))
        _open_potplayer(path, title, is_url=False)  # 标题靠 m3u 内 #EXTINF
        print(
            f"已用 m3u 播放列表打开:{path}\n卡住时在 PotPlayer 播放列表切换「备用N」。"
        )
        return 0

    # serve 模式:优先复用已有代理，否则在空闲端口新起。房间与清晰度都写进本地地址的
    # query（?room=&quality=），故复用别处已在跑的代理时也按本次请求解析、不受其启动参数限制。
    port, reuse = _choose_port(a.port)
    local = _serve_url(port, url, a.quality)

    srv = None
    if reuse:
        print(f"复用已有代理 (端口 {port})，无需新起。")
    else:
        srv = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "potplayer_live.server",
                url,
                str(port),
                a.quality or "",
                str(a.grace),
            ]
        )
        if not _wait_ready(port):
            print("警告:本地代理未在预期时间内就绪，仍尝试打开播放器。")
        print(f"本地代理已启动 (PID {srv.pid}，端口 {port})。")

    # 本地代理已带好平台头;标题用 PotPlayer 的「地址\\标题」语法。
    # 断流自愈全在代理里，PotPlayer 无感。
    _open_potplayer(local, title, is_url=True)
    print(f"地址:{local}")

    if reuse:
        return 0  # 不占管现有代理，开完即返回
    print("直播断流由服务器自动重解析续播，播放器无感。Ctrl+C 结束。")
    try:
        srv.wait()
    except KeyboardInterrupt:
        srv.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
