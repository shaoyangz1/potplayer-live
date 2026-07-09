#!/usr/bin/env python3
"""potplayer-live 命令行入口(PotPlayer 专用)。

    python cli.py <房间地址> [选项]

选项:
    --quality Q     清晰度显示名或码率(如 "原画" / 蓝光10M / 2000)，默认最高
    --line K        直链/m3u 模式下选第 K 条线路(0 起)，默认 0
    --title T       自定义 PotPlayer 窗口标题，默认用房间名(主播名)
    --mode MODE     打开方式，默认 serve:
                      serve  本地转流代理(推荐)：固定地址，自动跨 2 分钟断流自愈
                      m3u    多线路播放列表：卡住时在 PotPlayer 播放列表切备用线路
                      direct 单条 flv 直链：最简单，卡住无法恢复
                      print  只解析打印各清晰度地址，不打开播放器
    --port P        serve 模式端口，默认 8787

PotPlayer 路径:环境变量 POTPLAYER 优先，否则自动探测默认安装位置 / Scoop。
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sites
import common

PORT_SCAN = 20   # 从 --port 起最多向后扫描多少个端口


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


def _choose_port(base):
    """优先复用已有代理端口；否则返回第一个空闲端口。返回 (port, reuse)。

    并发探测:某些环境(如启用了 TUN/过滤驱动的代理软件)对已关闭的 loopback 端口
    也要 ~2s 才返回“拒绝”,串行扫 20 个端口会拖到 ~40s,故并发一次扫完。"""
    ports = list(range(base, base + PORT_SCAN))
    with ThreadPoolExecutor(max_workers=PORT_SCAN) as ex:
        res = dict(zip(ports, ex.map(_probe, ports)))
    for p in ports:                       # 先找可复用的
        if res[p] == "ours":
            return p, True
    for p in ports:                       # 再找空闲的新起
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
        r"例如 set POTPLAYER=D:\Apps\PotPlayer\PotPlayerMini64.exe")


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
    ap.add_argument("url", help="直播间地址，如 https://www.huya.com/lpl")
    ap.add_argument("--quality", default=None)
    ap.add_argument("--line", type=int, default=0)
    ap.add_argument("--title", default=None)
    ap.add_argument("--mode", default="serve", choices=["serve", "m3u", "direct", "print"])
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--grace", type=int, default=180,
                    help="serve 模式:无连接空闲多少秒后自动退出，<=0 常驻，默认 180")
    a = ap.parse_args()

    info = sites.parse(a.url)
    print(f"房间号 : {info['rid']}")
    print(f"主播   : {info['nick']}")
    print(f"标题   : {info['title']}")
    print(f"直播中 : {info['living']}")
    if not info["living"]:
        print("主播未开播。")
        return 1

    name, stream = common.pick(info, a.quality)
    title = a.title or info["nick"] or info["title"]   # 默认用房间名(主播名)
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
        print(f"已用 m3u 播放列表打开:{path}\n卡住时在 PotPlayer 播放列表切换「备用N」。")
        return 0

    # serve 模式:优先复用已有代理，否则在空闲端口新起。网关按路径解析房间
    # （/lpl.flv → 同平台/lpl），一个代理可服务任意房间。
    slug = urllib.parse.urlparse(a.url).path.strip("/").split("/")[0] or "live"
    port, reuse = _choose_port(a.port)
    local = f"http://127.0.0.1:{port}/{slug}.flv"

    srv = None
    if reuse:
        print(f"复用已有代理 (端口 {port})，无需新起。")
    else:
        here = os.path.dirname(os.path.abspath(__file__))
        srv = subprocess.Popen([sys.executable, os.path.join(here, "server.py"),
                                a.url, str(port), a.quality or "", str(a.grace)])
        import time
        time.sleep(4)
        print(f"本地代理已启动 (PID {srv.pid}，端口 {port})。")

    # 本地代理已带好平台头;标题用 PotPlayer 的「地址\标题」语法。
    # 断流自愈全在代理里，PotPlayer 无感。
    _open_potplayer(local, title, is_url=True)
    print(f"地址:{local}")

    if reuse:
        return 0   # 不占管现有代理，开完即返回
    print("直播断流由服务器自动重解析续播，播放器无感。Ctrl+C 结束。")
    try:
        srv.wait()
    except KeyboardInterrupt:
        srv.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
