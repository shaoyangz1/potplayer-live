#!/usr/bin/env python3
"""本地转流代理:给 PotPlayer 一个固定的 localhost 地址。

每段连接结束时(很多平台的 flv 每 ~2 分钟正常关闭一次),服务器自动重新解析+重签,
并改写 FLV 时间戳把新段无缝拼到上一段之后 —— 播放器完全无感、自愈,无需手动刷新。

自动关闭: 客户端断开后,若在宽限期(默认 180 秒)内无新连接则进程自动退出,
避免关掉播放器后代理空占端口常驻。设 GRACE<=0 可关闭该行为(保持常驻)。

用法: python -m potplayer_live.server <房间地址> [端口=8787] [清晰度] [宽限秒数=180]
"""

import sys
import time
import threading
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import sites
from .common import pick

# 全局配置:导入时用安全默认(模块可无副作用导入、便于测试),main 块再从命令行覆盖。
ROOM = "https://www.huya.com/lpl"  # 默认房间(未带 ?room= 的请求回退到它)
PORT = 8787
QUALITY = None  # 默认清晰度(None/"" = 最高);可被请求 ?quality= 覆盖
GRACE = 180  # 空闲自动退出秒数;<=0 表示常驻
# 默认房间的来源(scheme://host/),供路径网关按同平台拼房间地址
_ORIGIN = "https://www.huya.com/"


def _origin_of(room: str) -> str:
    o = urllib.parse.urlparse(room)
    return f"{o.scheme}://{o.netloc}/" if o.netloc else "https://www.huya.com/"


def room_from_path(path: str) -> str:
    """把请求路径解析成房间地址(按默认房间所在平台)。

    /live.flv 或 / → 启动时指定的默认房间(ROOM)
    /lpl.flv       → <默认平台>/lpl
    /660000.flv    → <默认平台>/660000
    完整 http 路径直接用;忽略 .flv 后缀与查询串。
    """
    slug = urllib.parse.unquote(urllib.parse.urlparse(path).path).strip("/")
    if slug.endswith(".flv"):
        slug = slug[:-4]
    if not slug or slug == "live":
        return ROOM
    if slug.startswith("http"):
        return slug
    return _ORIGIN + slug


def parse_request(path: str):
    """把请求(路径 + 查询串)解析成 (room, quality)。

    room   : ?room=<完整地址> 优先(cli 复用/跨平台时携带);否则按路径 slug 拼(见 room_from_path)。
    quality: ?quality=<显示名或码率> 优先;否则用启动时的全局默认 QUALITY。
    如此一个常驻代理即可服务任意房间/清晰度,复用现有代理时也不受其启动参数限制。
    """
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)
    room_q = qs.get("room", [None])[0]  # parse_qs 已解码
    room = room_q if room_q else room_from_path(path)
    quality = qs.get("quality", [None])[0]
    return room, (quality if quality else QUALITY)


# 活动连接计数与最后活动时间,供自动关闭看门狗判断
_lock = threading.Lock()
_active = 0
_last_active = time.time()


def resolve_lines(room, quality=None):
    """重新解析指定房间,返回 (线路列表[主+备用], 标题, 拉流头)。quality 由调用方按请求给定。"""
    info = sites.parse(room)
    if not info["living"]:
        raise RuntimeError("未开播")
    _, s = pick(info, quality)
    if s is None:
        raise RuntimeError("未取到可播放的 flv 流(可能仅提供 HLS 或流结构异常)")
    return [s["url"]] + s["backups"], info["title"], sites.play_headers(room)


def read_exact(fp, n):
    buf = b""
    while len(buf) < n:
        c = fp.read(n - len(buf))
        if not c:
            break
        buf += c
    return buf


def open_stream(url, headers):
    req = urllib.request.Request(url, headers=headers or {})
    return urllib.request.urlopen(req, timeout=15)


# 上游断连后的重连退避与放弃阈值
INITIAL_BACKOFF = 0.5  # 首次重试前的退避(秒),之后指数增长
BACKOFF_MAX = 3.0  # 退避上限(秒)
RETRY_DEADLINE = 120.0  # 上游持续不可用超过此秒数才放弃退出(<=0 视为一直重试)


def relay_flv(
    write,
    flush,
    urls,
    headers,
    room,
    quality,
    resolve_fn=resolve_lines,
    open_fn=open_stream,
    retry_deadline=RETRY_DEADLINE,
    sleep_fn=time.sleep,
    clock=time.monotonic,
):
    """把上游 flv 段持续转发给下游(write/flush 回调),跨平台断流自动重连续播。

    上游暂时不可用时退避重试(而非几次失败就放弃);仅当持续失败超过 retry_deadline 秒
    才干净退出,让端口被 watchdog 回收。客户端关播放器(write 抛 ConnectionError)则立即结束。
    write/flush/open_fn/resolve_fn/sleep_fn/clock 可注入,便于不触网测试。"""
    tag = room  # 日志前缀:一个代理服务多房间时按完整房间地址区分各自的段
    GAP = 40  # 段间隔(ms),仅换线/时钟跳变时用于接续
    WINDOW = 60000  # ms,原始 ts 相差在此以内视为“同一时钟”
    out_base = None  # 原始 ts -> 输出 ts 的偏移(让第 1 帧从 0 开始)
    last_src = None  # 已输出的最大原始时间戳(跨段,用于丢重复回放)
    last_out = -GAP  # 已输出的最大输出时间戳
    first_segment = True
    seg = 0
    line = 0
    fail_since = None  # 上游连续不可用的起始时刻;成功输出数据即清空
    backoff = INITIAL_BACKOFF

    def wait_or_giveup(reason):
        """推进失败计时:超过时限返回 True(应退出),否则退避+重解析后返回 False(继续重试)。"""
        nonlocal fail_since, backoff, line, urls, headers
        if fail_since is None:
            fail_since = clock()
        elif retry_deadline > 0 and clock() - fail_since > retry_deadline:
            print(
                f"[{tag}][seg {seg}] 上游持续不可用超过 {retry_deadline:.0f}s（{reason}），退出转流。",
                flush=True,
            )
            return True
        line += 1
        sleep_fn(min(backoff, BACKOFF_MAX))
        backoff = min(backoff * 2, BACKOFF_MAX)
        # 退避后重解析拿全新签名地址(避免一直连失效的旧线路)
        try:
            urls, _, headers = resolve_fn(room, quality)
        except Exception as e:
            print(f"[{tag}][seg {seg}] 重解析失败: {e!r}", flush=True)
        return False

    while True:
        url = urls[line % len(urls)]
        try:
            fp = open_fn(url, headers)
        except Exception as e:
            print(f"[{tag}][seg {seg}] 线路{line % len(urls)} open 失败: {e!r}", flush=True)
            if wait_or_giveup("连接失败"):
                return
            continue
        seg += 1
        print(
            f"[{tag}][seg {seg}] 线路{line % len(urls)} 连接，last_out={last_out}", flush=True
        )

        # FLV 文件头(9)+PreviousTagSize0(4)：仅第一段转发，后续段丢弃
        header = read_exact(fp, 13)
        if len(header) < 13:
            print(
                f"[{tag}][seg {seg}] 连接后未读到完整 FLV 头(len={len(header)})，重连",
                flush=True,
            )
            try:
                fp.close()
            except Exception:
                pass
            if wait_or_giveup("连接后无数据"):
                return
            continue
        if first_segment:
            try:
                write(header)
                flush()
            except ConnectionError:  # 含 Broken/Reset/Aborted(Windows 断开为 Aborted)
                return
            first_segment = False

        resume = last_src  # 本段丢弃阈值:原始 ts <= resume 的都是重复回放
        first_tag = True
        dropped = 0  # 本段丢弃的重复帧数(用于日志)
        drop_from = None  # 首个被丢帧的原始 ts,用于算丢弃时长
        try:
            while True:
                th = read_exact(fp, 11)
                if len(th) < 11:
                    break  # 本段结束（平台断开）→ 跳出去重连
                dsize = (th[1] << 16) | (th[2] << 8) | th[3]
                ts = (th[7] << 24) | (th[4] << 16) | (th[5] << 8) | th[6]
                data = read_exact(fp, dsize)
                prev = read_exact(fp, 4)
                if len(data) < dsize or len(prev) < 4:
                    break

                if first_tag:
                    first_tag = False
                    if out_base is None:
                        out_base = -ts  # 首段:输出从 0 开始
                        resume = None  # 首段不丢弃
                    elif last_src is not None and abs(ts - last_src) > WINDOW:
                        # 时钟跳变(多半换了 CDN 线路)→ 重新基线,本段整体接到末尾之后
                        out_base = (last_out + GAP) - ts
                        resume = None
                # 平台新连接开头会重发几秒“回看缓冲”,原始 ts 连续,
                # 丢掉 ts <= 上段末尾 的重复帧(音视频一并丢),避免回放
                if resume is not None and ts <= resume:
                    if drop_from is None:
                        drop_from = ts
                    dropped += 1
                    continue
                if dropped:
                    print(
                        f"[{tag}][seg {seg}] 丢弃重复回放 {dropped} 帧(~{resume - drop_from}ms)，"
                        f"从 out_ts={ts + out_base} 续播",
                        flush=True,
                    )
                    dropped = 0

                new_ts = ts + out_base
                nh = bytes(
                    [
                        th[0],
                        (dsize >> 16) & 0xFF,
                        (dsize >> 8) & 0xFF,
                        dsize & 0xFF,
                        (new_ts >> 16) & 0xFF,
                        (new_ts >> 8) & 0xFF,
                        new_ts & 0xFF,
                        (new_ts >> 24) & 0xFF,
                        th[8],
                        th[9],
                        th[10],
                    ]
                )
                try:
                    write(nh)
                    write(data)
                    write(prev)
                except ConnectionError:
                    return  # 播放器关了 → 结束(Windows 为 ConnectionAbortedError)
                fail_since = None  # 成功输出数据 → 上游确已恢复,清空失败计时
                backoff = INITIAL_BACKOFF
                if new_ts > last_out:
                    last_out = new_ts
                if last_src is None or ts > last_src:
                    last_src = ts
        except Exception as e:
            print(f"[{tag}][seg {seg}] 读取异常: {e!r}", flush=True)
        finally:
            try:
                fp.close()
            except Exception:
                pass

        # 段结束后重新解析拿全新签名地址(沿用同一 quality)
        try:
            urls, _, headers = resolve_fn(room, quality)
            print(f"[{tag}][seg {seg}] 段结束，已重解析，线路数={len(urls)}", flush=True)
        except Exception as e:
            print(f"[{tag}][seg {seg}] 段结束，重解析失败: {e!r}(沿用旧地址重连)", flush=True)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def handle_one_request(self):
        # PotPlayer 常先开一条“探测连接”未发完整请求就关闭;Windows 上表现为
        # ConnectionResetError/AbortedError,吞掉以免 socketserver 打出无意义的 traceback。
        try:
            super().handle_one_request()
        except ConnectionError:
            self.close_connection = True

    def do_GET(self):
        global _active, _last_active
        # 健康探测:供 cli 识别本 skill 的代理(须在房间解析之前拦截)
        if self.path.split("?")[0].rstrip("/") == "/__ping__":
            body = b"potplayer-live"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except ConnectionError:
                pass
            return
        room, quality = parse_request(self.path)
        if not room:
            # 纯中转代理(serve-only 起,无默认房间):裸连 /live.flv 无从解析,明确提示
            self.send_error(400, "未指定房间:请用 /<房间号>.flv 或 /live.flv?room=<完整地址>")
            return
        try:
            urls, title, headers = resolve_lines(room, quality)
        except Exception as e:
            self.send_error(503, str(e))
            return

        with _lock:
            _active += 1
        try:
            self._stream(urls, title, room, headers, quality)
        finally:
            with _lock:
                _active -= 1
                _last_active = time.time()

    def _stream(self, urls, title, room, headers, quality):
        self.send_response(200)
        self.send_header("Content-Type", "video/x-flv")
        self.send_header("Connection", "close")
        self.end_headers()
        relay_flv(self.wfile.write, self.wfile.flush, urls, headers, room, quality)


def watchdog(httpd):
    """无连接且空闲超过 GRACE 秒则关闭服务器，使进程自然退出。"""
    while True:
        time.sleep(5)
        with _lock:
            idle = _active == 0 and (time.time() - _last_active) > GRACE
        if idle:
            print(f"空闲超过 {GRACE}s，自动关闭代理。", flush=True)
            httpd.shutdown()
            return


if __name__ == "__main__":
    if len(sys.argv) > 1:
        ROOM = sys.argv[1] or None  # 显式传空串=无默认房间(纯中转,裸连 /live.flv 报错);没传则保留内置默认
    PORT = int(sys.argv[2]) if len(sys.argv) > 2 else PORT
    QUALITY = sys.argv[3] if len(sys.argv) > 3 else QUALITY
    GRACE = int(sys.argv[4]) if len(sys.argv) > 4 else GRACE
    _ORIGIN = _origin_of(ROOM or "")
    if ROOM:  # 有默认房间(直接跑 server 或 serve 起)才打启动提示
        print(f"默认房间: {ROOM}")
        print(f"默认地址: http://127.0.0.1:{PORT}/live.flv")
    # 纯中转(ROOM 为空,serve-only 起)时不打提示,由 cli 给更友好的复用指引
    if GRACE > 0:
        print(f"自动关闭: 无连接空闲 {GRACE}s 后退出")
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    if GRACE > 0:
        threading.Thread(target=watchdog, args=(httpd,), daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:  # Ctrl+C 干净退出,不打无意义的调用栈
        print("\n已退出。", flush=True)
