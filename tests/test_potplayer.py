#!/usr/bin/env python3
"""potplayer-live 测试(标准库 unittest，零依赖,不触网)。

    uv run -m unittest discover -s tests

覆盖:清晰度选择、m3u 生成、虎牙签名(uid 移位/wsSecret)、
serve 代理的按请求 room/quality 解析、cli 的就绪轮询。
纯函数用独立参考实现比对,而非仅锁定魔数值。
"""

import base64
import hashlib
import json as _json
import unittest
import urllib.parse

from potplayer_live import common, server, cli, sites
from potplayer_live.sites import huya, douyin, douyu


def _stream(quality, url="u0", backups=("u1", "u2")):
    return {"quality": quality, "url": url, "backups": list(backups)}


class TestPick(unittest.TestCase):
    def test_empty_streams_returns_none(self):
        self.assertEqual(common.pick({"streams": {}}), (None, None))

    def test_default_picks_highest_bitrate(self):
        info = {"streams": {"高清": _stream(500), "蓝光": _stream(2000)}}
        name, s = common.pick(info)
        self.assertEqual(name, "蓝光")
        self.assertEqual(s["quality"], 2000)

    def test_default_prefers_yuanhua_quality_zero(self):
        # quality==0 视为原画,应优先于任何正码率
        info = {"streams": {"原画": _stream(0), "蓝光": _stream(2000)}}
        name, _ = common.pick(info)
        self.assertEqual(name, "原画")

    def test_pick_by_display_name(self):
        info = {"streams": {"高清": _stream(500), "蓝光": _stream(2000)}}
        name, _ = common.pick(info, "高清")
        self.assertEqual(name, "高清")

    def test_pick_by_bitrate_string(self):
        info = {"streams": {"高清": _stream(500), "蓝光": _stream(2000)}}
        name, _ = common.pick(info, "2000")
        self.assertEqual(name, "蓝光")

    def test_unknown_quality_falls_back_to_highest(self):
        info = {"streams": {"高清": _stream(500), "蓝光": _stream(2000)}}
        name, _ = common.pick(info, "不存在")
        self.assertEqual(name, "蓝光")


class TestM3U(unittest.TestCase):
    def test_layout_with_backups(self):
        content = common.m3u_content("房间", _stream(0, "u0", ["u1", "u2"]))
        self.assertEqual(
            content.splitlines(),
            [
                "#EXTM3U",
                "#EXTINF:-1 ,房间",
                "u0",
                "#EXTINF:-1 ,房间 - 备用1",
                "u1",
                "#EXTINF:-1 ,房间 - 备用2",
                "u2",
            ],
        )

    def test_single_line_no_backups(self):
        content = common.m3u_content("A", _stream(0, "only", []))
        self.assertEqual(content.splitlines(), ["#EXTM3U", "#EXTINF:-1 ,A", "only"])


class TestRotUid(unittest.TestCase):
    @staticmethod
    def _reference(uid):
        # 独立参考实现:高 32 位不变,低 32 位循环左移 8 位
        hi = (uid >> 32) & 0xFFFFFFFF
        lo = uid & 0xFFFFFFFF
        rotl = ((lo << 8) | (lo >> 24)) & 0xFFFFFFFF
        return (hi << 32) | rotl

    def test_matches_reference(self):
        for uid in (0, 1, 0x12345678, 0xDEADBEEF, 0x00000001FF00AB00, 4294967294):
            self.assertEqual(huya._rot_uid(uid), self._reference(uid), f"uid={uid:#x}")

    def test_zero(self):
        self.assertEqual(huya._rot_uid(0), 0)


class TestWsSecret(unittest.TestCase):
    def test_matches_independent_recompute(self):
        fm_plain = "prefix_$0_$1_$2_$3"
        fm_enc = urllib.parse.quote(base64.b64encode(fm_plain.encode()).decode())
        anti = {"fm": fm_enc, "wsTime": "5f000000", "ctype": "huya_live", "t": "100"}
        convert_uid, seqid, stream_name = 123456, 987654321, "someStream-1"

        got = huya._ws_secret(anti, convert_uid, seqid, stream_name)

        # 独立复算(照 wsSecret 公开算法),锁定当前实现
        s = hashlib.md5(f"{seqid}|huya_live|100".encode()).hexdigest()
        u = f"prefix_{convert_uid}_{stream_name}_{s}_5f000000"
        expected = hashlib.md5(u.encode()).hexdigest()
        self.assertEqual(got, expected)

    def test_default_t_when_missing(self):
        # anti 无 t 时默认 "100"
        fm_plain = "p_$0_$1_$2_$3"
        anti = {
            "fm": urllib.parse.quote(base64.b64encode(fm_plain.encode()).decode()),
            "wsTime": "abc",
            "ctype": "c",
        }
        got = huya._ws_secret(anti, 1, 2, "n")
        s = hashlib.md5("2|c|100".encode()).hexdigest()
        expected = hashlib.md5(f"p_1_n_{s}_abc".encode()).hexdigest()
        self.assertEqual(got, expected)


class TestParseRequest(unittest.TestCase):
    """serve 代理按请求解析 (room, quality):query 优先,回退到路径 slug 与全局默认。"""

    def setUp(self):
        server.ROOM = "https://www.huya.com/lpl"
        server._ORIGIN = "https://www.huya.com/"
        server.QUALITY = None

    def test_slug_path_uses_default_platform(self):
        self.assertEqual(
            server.parse_request("/lpl.flv"), ("https://www.huya.com/lpl", None)
        )

    def test_numeric_slug(self):
        self.assertEqual(
            server.parse_request("/660000.flv"), ("https://www.huya.com/660000", None)
        )

    def test_live_or_root_uses_default_room(self):
        self.assertEqual(
            server.parse_request("/live.flv")[0], "https://www.huya.com/lpl"
        )
        self.assertEqual(server.parse_request("/")[0], "https://www.huya.com/lpl")

    def test_room_query_overrides_path_supports_cross_platform(self):
        # 点3:完整 room 由请求携带,不再依赖代理启动时的平台
        room = "https://example.com/abc"
        path = "/live.flv?room=" + urllib.parse.quote(room, safe="")
        self.assertEqual(server.parse_request(path)[0], room)

    def test_quality_query_parsed(self):
        room, quality = server.parse_request(
            "/lpl.flv?quality=" + urllib.parse.quote("原画")
        )
        self.assertEqual(quality, "原画")
        self.assertEqual(room, "https://www.huya.com/lpl")

    def test_quality_query_overrides_global_default(self):
        # 点2:复用代理时请求 query 的 quality 覆盖启动时的全局 QUALITY
        server.QUALITY = "蓝光"
        self.assertEqual(server.parse_request("/lpl.flv?quality=原画")[1], "原画")

    def test_quality_falls_back_to_global_default(self):
        server.QUALITY = "蓝光"
        self.assertEqual(server.parse_request("/lpl.flv")[1], "蓝光")

    def test_room_and_quality_together(self):
        room = "https://www.huya.com/123"
        path = f"/live.flv?room={urllib.parse.quote(room, safe='')}&quality=2000"
        self.assertEqual(server.parse_request(path), (room, "2000"))


class TestServeUrl(unittest.TestCase):
    """cli 生成的 serve 地址把 room/quality 写进 query,并与 server.parse_request 契约一致。"""

    def test_room_only_when_no_quality(self):
        url = cli._serve_url(8787, "https://www.huya.com/lpl", None)
        pr = urllib.parse.urlparse(url)
        self.assertEqual(pr.netloc, "127.0.0.1:8787")
        self.assertEqual(pr.path, "/live.flv")
        qs = urllib.parse.parse_qs(pr.query)
        self.assertEqual(qs.get("room"), ["https://www.huya.com/lpl"])
        self.assertNotIn("quality", qs)

    def test_roundtrips_through_parse_request(self):
        # cli 生成地址 ↔ server 解析闭环(点2 复用清晰度、点3 跨平台房间)
        server.ROOM = "https://www.huya.com/lpl"
        server.QUALITY = None
        room = "https://example.com/xyz"
        pr = urllib.parse.urlparse(cli._serve_url(9000, room, "原画"))
        self.assertEqual(server.parse_request(pr.path + "?" + pr.query), (room, "原画"))

    def test_room_url_stays_readable(self):
        # :/ 不做百分号编码,常见地址保持可读;闭环仍由 parse_request 保证
        url = cli._serve_url(8787, "https://www.huya.com/lpl", None)
        self.assertIn("room=https://www.huya.com/lpl", url)


class TestWaitReady(unittest.TestCase):
    """cli 启动 server 后轮询 __ping__ 直到就绪(替代硬编码 sleep)。"""

    def test_returns_true_once_proxy_becomes_ours(self):
        calls = {"n": 0}
        orig = cli._probe

        def fake(port):
            calls["n"] += 1
            return "ours" if calls["n"] >= 3 else "free"

        cli._probe = fake
        try:
            self.assertTrue(cli._wait_ready(1234, timeout=5))
        finally:
            cli._probe = orig
        self.assertGreaterEqual(calls["n"], 3)

    def test_returns_false_on_timeout(self):
        orig = cli._probe
        cli._probe = lambda port: "free"  # 永不就绪
        try:
            self.assertFalse(cli._wait_ready(1234, timeout=0))
        finally:
            cli._probe = orig


import io


def _flv_header():
    """FLV 文件头(9) + PreviousTagSize0(4) = 13 字节。"""
    return b"FLV\x01\x05\x00\x00\x00\x09" + b"\x00\x00\x00\x00"


def _flv_tag(ts=0, dsize=1, ttype=8, data=b"\x00"):
    """构造一个最小 FLV tag:11 字节 tag 头 + data(dsize) + 4 字节 PreviousTagSize。"""
    payload = data[:dsize].ljust(dsize, b"\x00")
    th = bytes(
        [
            ttype,
            (dsize >> 16) & 0xFF,
            (dsize >> 8) & 0xFF,
            dsize & 0xFF,
            (ts >> 16) & 0xFF,
            (ts >> 8) & 0xFF,
            ts & 0xFF,
            (ts >> 24) & 0xFF,
            0,
            0,
            0,
        ]
    )
    return th + payload + b"\x00\x00\x00\x00"


def _segment(*tags):
    """一段完整 flv 流(文件头 + 若干 tag),供 open_fn 返回。BytesIO 具备 read/close。"""
    return io.BytesIO(_flv_header() + b"".join(tags))


class _FakeUpstream:
    """受控上游:script 里 'fail' 表示这次连接抛超时,BytesIO 表示返回一段流。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def __call__(self, url, headers):
        item = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        if item == "fail":
            raise TimeoutError("The read operation timed out")
        return item


class TestRelayReconnect(unittest.TestCase):
    """serve 代理的跨断流自愈:上游暂时不可用应持续重试到恢复,而不是放弃退出。"""

    def test_recovers_after_transient_open_failures(self):
        # 上游前 3 次连接失败(超过旧的 len*2 阈值),第 4 次恢复。代理应续播,而非放弃。
        out = bytearray()

        def write(b):
            out.extend(b)
            if len(out) > 13:  # 首段头(13B)之后收到数据 → 模拟客户端关闭,终止
                raise ConnectionError("client closed")

        good = _segment(_flv_tag(ts=100))
        up = _FakeUpstream(["fail", "fail", "fail", good])
        server.relay_flv(
            write,
            lambda: None,
            ["u0"],
            {},
            "room",
            None,
            resolve_fn=lambda r, q: (["u0"], "t", {}),
            open_fn=up,
            sleep_fn=lambda *_: None,
        )
        self.assertIn(
            b"FLV", bytes(out), "恢复后应把 FLV 头转发给下游,而不是在失败上限处放弃"
        )
        self.assertGreaterEqual(
            up.calls, 4, "应重试到第 4 次成功,而不是在失败上限处放弃"
        )

    def test_gives_up_after_deadline_when_upstream_never_recovers(self):
        # 上游永远连不上:应在超过 retry_deadline 后干净退出,而不是无限重试。
        out = bytearray()
        clock = {"t": 0.0}

        def fake_clock():
            return clock["t"]

        def fake_sleep(_):
            clock["t"] += 5.0  # 每次退避推进 5s 虚拟时间

        up = _FakeUpstream(["fail"])  # 永远失败
        server.relay_flv(
            out.extend,
            lambda: None,
            ["u0"],
            {},
            "room",
            None,
            resolve_fn=lambda r, q: (["u0"], "t", {}),
            open_fn=up,
            retry_deadline=30.0,
            sleep_fn=fake_sleep,
            clock=fake_clock,
        )
        self.assertLess(up.calls, 20, "超过时限应退出,不能无限重试")
        self.assertEqual(bytes(out), b"", "从未连上,不应有任何输出")

    def test_client_disconnect_stops_immediately(self):
        # 客户端关播放器(write 抛 ConnectionError)应立即结束,不再重试上游。
        def write(_):
            raise ConnectionError("client closed")

        good = _segment(_flv_tag(ts=100))
        up = _FakeUpstream([good, good, good])
        server.relay_flv(
            write,
            lambda: None,
            ["u0"],
            {},
            "room",
            None,
            resolve_fn=lambda r, q: (["u0"], "t", {}),
            open_fn=up,
            sleep_fn=lambda *_: None,
        )
        self.assertEqual(up.calls, 1, "客户端断开后应立即停止,不应再连上游")


class TestHttpHelpers(unittest.TestCase):
    def test_gunzip_passthrough_plain(self):
        self.assertEqual(common._gunzip(b'{"a":1}'), b'{"a":1}')

    def test_gunzip_decompresses_gzip(self):
        import gzip as _gz

        self.assertEqual(common._gunzip(_gz.compress(b"hello")), b"hello")


def _enter(status=2, with_sdk=True):
    room = {"status": status, "title": "早安", "owner": {"nickname": "主播A"}}
    stream_url = {
        "flv_pull_url": {"FULL_HD1": "http://x/full.flv", "HD1": "http://x/hd.flv"}
    }
    if with_sdk:
        stream_url["live_core_sdk_data"] = {
            "pull_data": {
                "options": {
                    "qualities": [
                        {"name": "原画", "sdk_key": "origin", "v_bit_rate": 0},
                        {"name": "高清", "sdk_key": "sd", "v_bit_rate": 1000},
                    ]
                },
                "stream_data": _json.dumps(
                    {
                        "data": {
                            "origin": {"main": {"flv": "http://x/origin.flv"}},
                            "sd": {"main": {"flv": "http://x/sd.flv"}},
                        }
                    }
                ),
            }
        }
    room["stream_url"] = stream_url
    return {"data": {"data": [room], "user": {"nickname": "主播A"}}}


class TestDouyinResolveWebRid(unittest.TestCase):
    def test_last_path_segment(self):
        self.assertEqual(
            douyin.resolve_web_rid("https://live.douyin.com/123456"), "123456"
        )


class TestDouyinParseEnter(unittest.TestCase):
    def test_living_with_sdk_qualities(self):
        info = douyin._parse_enter(_enter(), "123456")
        self.assertTrue(info["living"])
        self.assertEqual(info["rid"], "123456")
        self.assertEqual(info["nick"], "主播A")
        self.assertEqual(info["title"], "早安")
        self.assertEqual(
            info["streams"]["原画"], {"quality": 0, "url": "http://x/origin.flv", "backups": []}
        )
        self.assertEqual(info["streams"]["高清"]["quality"], 1000)
        self.assertEqual(info["streams"]["高清"]["url"], "http://x/sd.flv")

    def test_not_living_empty_streams(self):
        info = douyin._parse_enter(_enter(status=0), "1")
        self.assertFalse(info["living"])
        self.assertEqual(info["streams"], {})

    def test_fallback_to_flv_pull_url(self):
        info = douyin._parse_enter(_enter(with_sdk=False), "1")
        self.assertTrue(info["living"])
        self.assertEqual(info["streams"]["原画"]["url"], "http://x/full.flv")
        self.assertEqual(info["streams"]["高清"]["url"], "http://x/hd.flv")
        self.assertEqual(info["streams"]["原画"]["backups"], [])

    def test_invalid_stream_data_falls_back_to_flv_pull_url(self):
        # stream_data 非法 JSON 时不应崩溃,应回退到 flv_pull_url
        p = _enter(with_sdk=True)
        p["data"]["data"][0]["stream_url"]["live_core_sdk_data"]["pull_data"]["stream_data"] = "NOT_JSON"
        info = douyin._parse_enter(p, "1")
        self.assertTrue(info["living"])
        self.assertEqual(info["streams"]["原画"]["url"], "http://x/full.flv")

    def test_pull_data_null_falls_back_to_flv_pull_url(self):
        # pull_data 为 null 时不应 AttributeError,应回退到 flv_pull_url
        p = _enter(with_sdk=False)
        p["data"]["data"][0]["stream_url"]["live_core_sdk_data"] = {"pull_data": None}
        info = douyin._parse_enter(p, "1")
        self.assertTrue(info["living"])
        self.assertEqual(info["streams"]["原画"]["url"], "http://x/full.flv")

    def test_options_null_falls_back_to_flv_pull_url(self):
        # options 为 null 时不应 AttributeError,应回退到 flv_pull_url
        p = _enter(with_sdk=False)
        p["data"]["data"][0]["stream_url"]["live_core_sdk_data"] = {
            "pull_data": {"options": None}
        }
        info = douyin._parse_enter(p, "1")
        self.assertTrue(info["living"])
        self.assertEqual(info["streams"]["原画"]["url"], "http://x/full.flv")

    def test_living_true_but_no_flv_streams_empty(self):
        # living=True 但无 sdk 也无 flv_pull_url → streams 为空 dict(不崩)
        payload = {"data": {"data": [{"status": 2, "title": "t",
                                      "stream_url": {}}],
                            "user": {"nickname": "n"}}}
        info = douyin._parse_enter(payload, "1")
        self.assertTrue(info["living"])
        self.assertEqual(info["streams"], {})


def _ssr_block(obj):
    """把 dict 包成一个抖音 SSR flight 块:JSON 双重转义后塞进 pace_f script。

    内层用紧凑分隔符(无空格),对齐真机抖音 SSR 的紧凑 JSON —— web_rid 锚
    依赖 web_rid":"<rid> 这种无空格形态。
    """
    inner = _json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    literal = _json.dumps(inner, ensure_ascii=False)  # 再转义为 JS 字符串字面量(带 \")
    return f"<script>self.__pace_f.push([1,{literal}])</script>"


class TestDouyinRoomFromHtml(unittest.TestCase):
    def _html(self, rid="999", status=2):
        # 页面含两个块:初始空壳(roomInfo={})在前,注水真数据块在后
        empty = _ssr_block({"roomStore": {"roomInfo": {}}})
        real = _ssr_block(
            {"roomStore": {"roomInfo": {"room": {
                "status": status, "title": "标题",
                "owner": {"nickname": "主播", "web_rid": rid},
                "stream_url": {"flv_pull_url": {"FULL_HD1": "http://x/f.flv"}},
            }}}}
        )
        return f"<html>{empty}{real}</html>"

    def test_extract_skips_empty_shell(self):
        # web_rid 锚应跳过空壳块,命中真数据块
        room = douyin._room_from_html(self._html(rid="999"), "999")
        self.assertEqual(room["status"], 2)
        self.assertEqual(room["title"], "标题")

    def test_missing_web_rid_returns_empty(self):
        # 页面无该 web_rid(锚落空)→ {} → 上层判未开播
        self.assertEqual(douyin._room_from_html(self._html(rid="888"), "999"), {})

    def test_extracted_room_feeds_parse_enter(self):
        # 提取的 room 结构可直接喂 _parse_enter(与 enter data.data[0] 一致)
        room = douyin._room_from_html(self._html(rid="999"), "999")
        info = douyin._parse_enter({"data": {"data": [room], "user": {}}}, "999")
        self.assertTrue(info["living"])
        self.assertEqual(info["nick"], "主播")
        self.assertEqual(info["streams"]["原画"]["url"], "http://x/f.flv")


class TestDouyinDispatch(unittest.TestCase):
    def test_get_site_routes_to_douyin(self):
        self.assertIs(sites.get_site("https://live.douyin.com/123456"), douyin)

    def test_play_headers_has_referer(self):
        h = sites.play_headers("https://live.douyin.com/123456")
        self.assertEqual(h["Referer"], "https://live.douyin.com/")


class TestPlayRoomNoStreams(unittest.TestCase):
    """living=True 但 streams={} 时 cli/server 不崩、给出提示/抛错。"""

    def _make_args(self, quality=None, mode="serve", line=0):
        import types
        a = types.SimpleNamespace(quality=quality, mode=mode, line=line,
                                  title=None, port=8787, grace=180)
        return a

    def test_cli_prints_message_and_returns_nonzero(self):
        # living 但 streams 空 → pick 返回 (None,None) → cli 打印提示并 return 1
        printed = []
        orig_parse = sites.parse
        orig_print = cli.__builtins__["print"] if isinstance(cli.__builtins__, dict) else None

        import builtins
        orig_print = builtins.print
        builtins.print = lambda *a, **kw: printed.append(" ".join(str(x) for x in a))
        sites.parse = lambda url: {"rid": "1", "nick": "n", "title": "t",
                                   "living": True, "streams": {}}
        try:
            ret = cli.play_room("https://live.douyin.com/1", self._make_args())
        finally:
            builtins.print = orig_print
            sites.parse = orig_parse
        self.assertEqual(ret, 1)
        self.assertTrue(any("flv" in m for m in printed), f"未见 flv 提示: {printed}")

    def test_server_resolve_lines_raises_on_empty_streams(self):
        # living 但 streams 空 → server.resolve_lines 抛 RuntimeError
        orig_parse = sites.parse
        sites.parse = lambda url: {"rid": "1", "nick": "n", "title": "t",
                                   "living": True, "streams": {}}
        try:
            with self.assertRaises(RuntimeError) as ctx:
                server.resolve_lines("https://live.douyin.com/1")
        finally:
            sites.parse = orig_parse
        self.assertIn("flv", str(ctx.exception))


class TestDouyuAuth(unittest.TestCase):
    """auth 用独立参考实现比对(不锁魔数),重点验证 enc_time 迭代次数与 is_special/salt 分支。"""

    @staticmethod
    def _ref(rand_str, key, enc_time, is_special, rid, ts):
        s = rand_str
        for _ in range(enc_time):
            s = hashlib.md5((s + key).encode()).hexdigest()
        salt = "" if is_special == 1 else f"{rid}{ts}"
        return hashlib.md5((s + key + salt).encode()).hexdigest()

    def test_special_salt_empty(self):
        enc = {"rand_str": "abc", "key": "k1", "enc_time": 3, "is_special": 1}
        self.assertEqual(
            douyu._auth(enc, "9527", 1700000000),
            self._ref("abc", "k1", 3, 1, "9527", 1700000000),
        )

    def test_nonspecial_salt_rid_ts(self):
        enc = {"rand_str": "xyz", "key": "k2", "enc_time": 5, "is_special": 0}
        self.assertEqual(
            douyu._auth(enc, "6666", 1700000001),
            self._ref("xyz", "k2", 5, 0, "6666", 1700000001),
        )

    def test_is_special_changes_result(self):
        # 同 rand_str/key/enc_time,仅 is_special 不同 → auth 必须不同(salt 分支生效)
        base = {"rand_str": "abc", "key": "k1", "enc_time": 3}
        a = douyu._auth({**base, "is_special": 1}, "9527", 1700000000)
        b = douyu._auth({**base, "is_special": 0}, "9527", 1700000000)
        self.assertNotEqual(a, b)


class TestDouyuResolveRid(unittest.TestCase):
    """resolve_rid:fetch 注入假页面,不触网。"""

    def test_numeric_path(self):
        self.assertEqual(
            douyu.resolve_rid("https://www.douyu.com/123456", fetch=self._boom), "123456"
        )

    def test_query_rid(self):
        self.assertEqual(
            douyu.resolve_rid("https://www.douyu.com/topic/x?rid=888", fetch=self._boom),
            "888",
        )

    def test_alias_pat_vipid(self):
        html = 'foo "rid":9527,"vipId":0 bar'
        self.assertEqual(douyu.resolve_rid("https://www.douyu.com/lpl", fetch=lambda u: html), "9527")

    def test_alias_pat_roominfo(self):
        html = 'x "roomInfo":{"rid":6666,"name":"y"} z'
        self.assertEqual(douyu.resolve_rid("https://www.douyu.com/king", fetch=lambda u: html), "6666")

    def test_not_found_raises(self):
        with self.assertRaises(RuntimeError):
            douyu.resolve_rid("https://www.douyu.com/nobody", fetch=lambda u: "no room here")

    @staticmethod
    def _boom(url):  # 数字/带 rid 时不应触发抓取
        raise AssertionError(f"不应抓取: {url}")


class TestDouyuPlayUrl(unittest.TestCase):
    def test_join_strips_trailing_slash(self):
        data = {"rtmp_url": "https://d.com/live/", "rtmp_live": "abc.flv?t=1"}
        self.assertEqual(douyu._play_url(data), "https://d.com/live/abc.flv?t=1")


class TestDouyuRoomFromBetard(unittest.TestCase):
    def test_living(self):
        bet = {"room": {"nickname": "主播", "room_name": "标题",
                        "show_status": 1, "videoLoop": 0}}
        r = douyu._room_from_betard(bet)
        self.assertEqual((r["nick"], r["title"], r["living"]), ("主播", "标题", True))

    def test_off(self):
        bet = {"room": {"nickname": "主播", "show_status": 2, "videoLoop": 0}}
        self.assertFalse(douyu._room_from_betard(bet)["living"])

    def test_loop_not_living(self):
        # 轮播(videoLoop==1)不算开播
        bet = {"room": {"nickname": "主播", "show_status": 1, "videoLoop": 1}}
        self.assertFalse(douyu._room_from_betard(bet)["living"])

    def test_title_falls_back_to_nick(self):
        bet = {"room": {"nickname": "主播", "show_status": 1, "videoLoop": 0}}
        self.assertEqual(douyu._room_from_betard(bet)["title"], "主播")


class TestDouyuDispatch(unittest.TestCase):
    def test_get_site_routes_to_douyu(self):
        self.assertIs(sites.get_site("https://www.douyu.com/123456"), douyu)

    def test_play_headers_has_referer(self):
        h = sites.play_headers("https://www.douyu.com/123456")
        self.assertEqual(h["Referer"], "https://www.douyu.com")


if __name__ == "__main__":
    unittest.main(verbosity=2)
