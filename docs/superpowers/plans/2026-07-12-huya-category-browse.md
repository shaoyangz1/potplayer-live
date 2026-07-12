# 虎牙分区房间浏览 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给一个分区标识(分区页 URL / 中文名 / 别名 / gid),列出该分区在播房间,交互式选序号 → 复用现有 serve 流程用 PotPlayer 播放。

**Architecture:** 分区目录 `bussLive`(把中文名/别名映射到 gid)+ 移动端 SSR 页 `m.huya.com/g/<slug>?page=N`(正则抓房间卡片,跨页去重)。浏览发生在现有 parse/play 流程之前:选中房间 → 构造 `https://www.huya.com/<room>` → 走现有逻辑。

**Tech Stack:** Python 3.9+ 纯标准库(urllib / re / json / gzip),unittest。

## Global Constraints

- **纯标准库,零第三方依赖**(项目硬约束)。
- **不触网测试**:所有新测试注入 fixture / 可注入函数,不发真实请求。
- **不破坏现有单房间语义**:完整房间 URL(`https://www.huya.com/xxx`,非 `/g/`)行为不变。
- 中文注释,风格对齐现有 `huya.py` / `common.py`。
- 房间列表页编码:先 `utf-8` strict,失败回退 `gb18030`。
- 分区目录 `bussLive` 有时返回 gzip(magic `\x1f\x8b`),需透明解压。

---

### Task 1: common.py — gzip 透明解压 + 编码回退解码

**Files:**
- Modify: `common.py`(顶部 import + `http_get`,新增 `_gunzip`/`decode_text`/`http_get_text`)
- Test: `test_potplayer.py`(新增 `TestHttpHelpers`)

**Interfaces:**
- Produces:
  - `common._gunzip(raw: bytes) -> bytes`
  - `common.decode_text(raw: bytes) -> str`
  - `common.http_get_text(url, headers=None, timeout=15) -> str`
  - `common.http_get(...)` 现有签名不变,但返回值已透明解 gzip。

- [ ] **Step 1: 写失败测试**(加到 `test_potplayer.py` 末尾 `if __name__` 之前)

```python
class TestHttpHelpers(unittest.TestCase):
    def test_gunzip_passthrough_plain(self):
        self.assertEqual(common._gunzip(b'{"a":1}'), b'{"a":1}')

    def test_gunzip_decompresses_gzip(self):
        import gzip as _gz
        self.assertEqual(common._gunzip(_gz.compress(b"hello")), b"hello")

    def test_decode_text_utf8(self):
        self.assertEqual(common.decode_text("英雄联盟".encode("utf-8")), "英雄联盟")

    def test_decode_text_gbk_fallback(self):
        # GBK 字节序列不是合法 utf-8(在“雄”处触发),应回退 gb18030 得到正确中文
        self.assertEqual(common.decode_text("英雄联盟".encode("gb18030")), "英雄联盟")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m unittest test_potplayer.TestHttpHelpers -v`
Expected: FAIL / ERROR — `module 'common' has no attribute '_gunzip'`

- [ ] **Step 3: 实现**(修改 `common.py`)

顶部 import 改为:
```python
import gzip
import hashlib
import urllib.request
```

`http_get` 改为并新增三函数(放在 `http_get` 附近):
```python
def _gunzip(raw: bytes) -> bytes:
    """部分虎牙接口即使未声明也返回 gzip(magic 1f 8b),透明解压。"""
    if raw[:2] == b"\x1f\x8b":
        return gzip.decompress(raw)
    return raw


def http_get(url, headers=None, timeout=15):
    req = urllib.request.Request(url, headers=headers or {})
    return _gunzip(urllib.request.urlopen(req, timeout=timeout).read())


def decode_text(raw: bytes) -> str:
    """虎牙页面多为 UTF-8,个别节点为 GBK;先 utf-8 strict,失败回退 gb18030。"""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("gb18030", "replace")


def http_get_text(url, headers=None, timeout=15) -> str:
    return decode_text(http_get(url, headers, timeout))
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m unittest test_potplayer.TestHttpHelpers -v`
Expected: PASS(4 项)

- [ ] **Step 5: 跑全量确认无回归**

Run: `python -m unittest test_potplayer`
Expected: OK

- [ ] **Step 6: 提交**

```bash
git add common.py test_potplayer.py
git commit -m "feat(common): http_get 透明解 gzip + http_get_text 编码回退"
```

---

### Task 2: huya.py — 分区目录 `_categories` / `_parse_categories`

**Files:**
- Modify: `huya.py`(import 补 `http_get_text, decode_text`;新增 `CATEGORY_URL` / `_parse_categories` / `_categories` / `_safe_categories`)
- Test: `test_potplayer.py`(新增 `TestCategories`)

**Interfaces:**
- Consumes: `common.decode_text`(Task 1)
- Produces:
  - `huya._parse_categories(raw: bytes) -> list[dict]`,元素 `{"gid": int, "host": str, "name": str, "online": Any}`
  - `huya._categories() -> list[dict]`(拉网 + 重试)
  - `huya._safe_categories() -> list[dict]`(失败返回 `[]`)

- [ ] **Step 1: 写失败测试**

```python
class TestCategories(unittest.TestCase):
    def test_parse_categories_extracts_fields(self):
        import json as _j
        raw = _j.dumps({"data": [
            {"gid": 1, "gameHostName": "lol", "gameFullName": "英雄联盟", "totalCount": 123},
            {"gid": 2, "gameHostName": "", "gameFullName": "", "totalCount": 0},
        ]}).encode("utf-8")
        cats = huya._parse_categories(raw)
        self.assertEqual(cats[0], {"gid": 1, "host": "lol", "name": "英雄联盟", "online": 123})
        self.assertEqual(cats[1]["gid"], 2)

    def test_parse_categories_skips_missing_gid(self):
        import json as _j
        raw = _j.dumps({"data": [{"gameHostName": "x"}]}).encode("utf-8")
        self.assertEqual(huya._parse_categories(raw), [])
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m unittest test_potplayer.TestCategories -v`
Expected: FAIL — `module 'huya' has no attribute '_parse_categories'`

- [ ] **Step 3: 实现**(修改 `huya.py`)

import 行改为:
```python
from common import http_get, http_get_text, decode_text, md5
```

在 `PLAY_HEADERS` 定义之后新增:
```python
# 分区目录(把中文名/别名映射到 gid);bussType=1 返回热门分区汇总
CATEGORY_URL = "https://live.cdn.huya.com/liveconfig/game/bussLive?bussType=1"


def _parse_categories(raw: bytes) -> list:
    """解析 bussLive 返回的分区目录。返回 [{gid, host(别名), name(中文名), online}]。"""
    data = json.loads(decode_text(raw)).get("data", []) or []
    out = []
    for c in data:
        gid = c.get("gid")
        if gid is None:
            continue
        out.append({"gid": int(gid),
                    "host": c.get("gameHostName") or "",
                    "name": c.get("gameFullName") or "",
                    "online": c.get("totalCount")})
    return out


def _categories() -> list:
    """拉分区目录(gzip 兼容 + 3 次重试)。全部失败则抛最后一次异常。"""
    last = None
    for _ in range(3):
        try:
            return _parse_categories(http_get(CATEGORY_URL, headers={"User-Agent": UA_MOBILE}))
        except Exception as e:      # noqa: BLE001 目录不可用不应连累透传路径
            last = e
            time.sleep(1)
    raise last


def _safe_categories() -> list:
    """目录不可用时返回空表(gid/别名/slug 透传不依赖目录)。"""
    try:
        return _categories()
    except Exception:
        return []
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m unittest test_potplayer.TestCategories -v`
Expected: PASS(2 项)

- [ ] **Step 5: 跑全量**

Run: `python -m unittest test_potplayer`
Expected: OK

- [ ] **Step 6: 提交**

```bash
git add huya.py test_potplayer.py
git commit -m "feat(huya): 分区目录解析 _categories/_parse_categories"
```

---

### Task 3: huya.py — `resolve_category`(标识 → slug + 显示名)

**Files:**
- Modify: `huya.py`(新增 `resolve_category`)
- Test: `test_potplayer.py`(新增 `TestResolveCategory`)

**Interfaces:**
- Consumes: `_safe_categories`(Task 2)
- Produces: `huya.resolve_category(ident, categories=None) -> (slug: str, display: str)`

- [ ] **Step 1: 写失败测试**

```python
class TestResolveCategory(unittest.TestCase):
    CATS = [{"gid": 1, "host": "lol", "name": "英雄联盟", "online": 9}]

    def test_gid_passthrough_with_display(self):
        self.assertEqual(huya.resolve_category("1", categories=self.CATS), ("1", "英雄联盟"))

    def test_alias_passthrough_with_display(self):
        self.assertEqual(huya.resolve_category("lol", categories=self.CATS), ("lol", "英雄联盟"))

    def test_chinese_name_maps_to_alias(self):
        self.assertEqual(huya.resolve_category("英雄联盟", categories=self.CATS), ("lol", "英雄联盟"))

    def test_unknown_passthrough_uses_ident_as_display(self):
        self.assertEqual(huya.resolve_category("wzry", categories=[]), ("wzry", "wzry"))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m unittest test_potplayer.TestResolveCategory -v`
Expected: FAIL — `module 'huya' has no attribute 'resolve_category'`

- [ ] **Step 3: 实现**(修改 `huya.py`,加在 `_safe_categories` 之后)

```python
def resolve_category(ident, categories=None):
    """把分区标识解析成 (slug, 显示名)。

    ident: gid / 别名(gameHostName) / 中文名(gameFullName) / 分区页 URL 的 slug。
    slug 优先直接用 ident(gid/别名/slug 透传,不阻塞于目录能否命中);
    中文名输入则查目录匹配到别名或 gid 作 slug。
    显示名尽量从目录按 gid/别名反查中文名,查不到就用 ident 本身。
    """
    ident = str(ident).strip()
    cats = categories if categories is not None else _safe_categories()
    by_host = {c["host"].lower(): c for c in cats if c.get("host")}
    by_gid = {str(c["gid"]): c for c in cats if c.get("gid") is not None}
    by_name = {c["name"]: c for c in cats if c.get("name")}
    if ident.isdigit():                       # gid 透传,显示名反查
        c = by_gid.get(ident)
        return ident, (c["name"] if c and c["name"] else ident)
    c = by_host.get(ident.lower())            # 别名透传,显示名反查
    if c:
        return ident, (c["name"] or ident)
    c = by_name.get(ident)                    # 中文名 → 别名/gid 作 slug
    if c:
        return (c["host"] or str(c["gid"])), c["name"]
    return ident, ident                       # 目录外:当 slug 透传
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m unittest test_potplayer.TestResolveCategory -v`
Expected: PASS(4 项)

- [ ] **Step 5: 跑全量**

Run: `python -m unittest test_potplayer`
Expected: OK

- [ ] **Step 6: 提交**

```bash
git add huya.py test_potplayer.py
git commit -m "feat(huya): resolve_category 标识→slug+显示名"
```

---

### Task 4: huya.py — `list_category` + `_parse_rooms`(抓房间列表、去重)

**Files:**
- Modify: `huya.py`(新增 `ROOM_LIST_URL` / `_ROOM_CARD` / `_parse_rooms` / `list_category`)
- Test: `test_potplayer.py`(新增 `TestListCategory`)

**Interfaces:**
- Consumes: `resolve_category`(Task 3)、`common.http_get_text`(Task 1)
- Produces:
  - `huya._parse_rooms(html: str) -> list[dict]`,元素 `{"room","nick","viewers","title"}`
  - `huya.list_category(ident, pages=3, categories=None, fetch=None) -> {"name","slug","rooms":[...]}`
    - `fetch(slug, page) -> html` 可注入(默认拉真实页面)。

- [ ] **Step 1: 写失败测试**

```python
def _card(room, nick, viewers, title):
    return (f'<a href="/{room}" class="qqqq g-link"><div class="g-item">'
            f'<span class="nick">{nick}</span>'
            f'<span class="viewer-count">{viewers}</span>'
            f'<p class="title">{title}</p></div></a>')


class TestListCategory(unittest.TestCase):
    CATS = [{"gid": 1, "host": "lol", "name": "英雄联盟", "online": 9}]

    def test_parse_rooms_fields(self):
        html = "<html>" + _card("333003", "主播A", "911万", "标题A") + "</html>"
        self.assertEqual(huya._parse_rooms(html),
                         [{"room": "333003", "nick": "主播A", "viewers": "911万", "title": "标题A"}])

    def test_list_category_dedup_preserves_order(self):
        pages = {1: _card("a", "na", "9万", "ta") + _card("b", "nb", "8万", "tb"),
                 2: _card("b", "nb", "8万", "tb") + _card("c", "nc", "7万", "tc")}
        res = huya.list_category("lol", pages=3, categories=self.CATS,
                                 fetch=lambda slug, page: pages.get(page, ""))
        self.assertEqual([r["room"] for r in res["rooms"]], ["a", "b", "c"])
        self.assertEqual(res["name"], "英雄联盟")
        self.assertEqual(res["slug"], "lol")

    def test_list_category_stops_on_empty_page(self):
        pages = {1: _card("a", "na", "9万", "ta")}
        res = huya.list_category("lol", pages=5, categories=self.CATS,
                                 fetch=lambda slug, page: pages.get(page, ""))
        self.assertEqual([r["room"] for r in res["rooms"]], ["a"])
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m unittest test_potplayer.TestListCategory -v`
Expected: FAIL — `module 'huya' has no attribute '_parse_rooms'`

- [ ] **Step 3: 实现**(修改 `huya.py`,加在 `resolve_category` 之后)

```python
# 移动端 SSR 分区页(UTF-8),?page=N 翻页;每页约 9 个房间卡片
ROOM_LIST_URL = "https://m.huya.com/g/{slug}?page={page}"
_ROOM_CARD = re.compile(
    r'<a href="/([^"]+)" class="qqqq g-link">.*?'
    r'<span class="nick">(.*?)</span>.*?'
    r'<span class="viewer-count">(.*?)</span>.*?'
    r'<p class="title">(.*?)</p>', re.S)


def _parse_rooms(html: str) -> list:
    """从分区页 HTML 提取房间卡片。"""
    out = []
    for room, nick, viewers, title in _ROOM_CARD.findall(html):
        out.append({"room": room, "nick": nick.strip(),
                    "viewers": viewers.strip(), "title": title.strip()})
    return out


def list_category(ident, pages=3, categories=None, fetch=None):
    """列出分区在播房间(按人气,跨页去重保序)。

    返回 {"name": 显示名, "slug": slug, "rooms": [{room, nick, viewers, title}, ...]}。
    fetch(slug, page)->html 可注入,便于不触网测试。
    """
    slug, name = resolve_category(ident, categories=categories)
    if fetch is None:
        def fetch(slug, page):
            return http_get_text(ROOM_LIST_URL.format(slug=slug, page=page),
                                 headers={"User-Agent": UA_MOBILE})
    seen, rooms = set(), []
    for page in range(1, pages + 1):
        try:
            html = fetch(slug, page)
        except Exception:
            break
        page_rooms = _parse_rooms(html)
        if not page_rooms:
            break                     # 该页无卡片 → 到底了
        for r in page_rooms:
            if r["room"] not in seen:
                seen.add(r["room"])
                rooms.append(r)
    return {"name": name, "slug": slug, "rooms": rooms}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m unittest test_potplayer.TestListCategory -v`
Expected: PASS(3 项)

- [ ] **Step 5: 跑全量**

Run: `python -m unittest test_potplayer`
Expected: OK

- [ ] **Step 6: 提交**

```bash
git add huya.py test_potplayer.py
git commit -m "feat(huya): list_category 抓分区房间列表(去重保序)"
```

---

### Task 5: sites.py — `is_category` / `category_slug` / `list_rooms` 派发

**Files:**
- Modify: `sites.py`(新增三函数)
- Test: `test_potplayer.py`(新增 `TestSitesCategory`)

**Interfaces:**
- Consumes: `huya.list_category`(Task 4)、`get_site`(现有)
- Produces:
  - `sites.is_category(url: str) -> bool`
  - `sites.category_slug(url: str) -> str`
  - `sites.list_rooms(url: str, pages: int = 3) -> dict`

- [ ] **Step 1: 写失败测试**

```python
class TestSitesCategory(unittest.TestCase):
    def test_is_category_g_url(self):
        self.assertTrue(sites.is_category("https://www.huya.com/g/lol"))

    def test_is_category_room_url_false(self):
        self.assertFalse(sites.is_category("https://www.huya.com/lpl"))
        self.assertFalse(sites.is_category("https://www.huya.com/660000"))

    def test_is_category_bare_string(self):
        self.assertTrue(sites.is_category("英雄联盟"))
        self.assertTrue(sites.is_category("lol"))
        self.assertTrue(sites.is_category("1"))

    def test_category_slug_from_g_url(self):
        self.assertEqual(sites.category_slug("https://www.huya.com/g/lol"), "lol")

    def test_category_slug_bare(self):
        self.assertEqual(sites.category_slug("英雄联盟"), "英雄联盟")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m unittest test_potplayer.TestSitesCategory -v`
Expected: FAIL — `module 'sites' has no attribute 'is_category'`

- [ ] **Step 3: 实现**(修改 `sites.py`,加在 `supported()` 之后)

```python
def is_category(url: str) -> bool:
    """判断输入意图是否为分区浏览:分区页 /g/ URL,或非 http 的裸标识(名/别名/gid)。

    完整房间 URL(非 /g/)返回 False,走单房间流程,现有语义不变。
    """
    p = urllib.parse.urlparse(url)
    if p.scheme in ("http", "https"):
        return p.path.strip("/").split("/")[0] == "g"
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m unittest test_potplayer.TestSitesCategory -v`
Expected: PASS(5 项)

- [ ] **Step 5: 跑全量**

Run: `python -m unittest test_potplayer`
Expected: OK

- [ ] **Step 6: 提交**

```bash
git add sites.py test_potplayer.py
git commit -m "feat(sites): is_category/list_rooms 分区浏览派发"
```

---

### Task 6: cli.py — 抽出 `play_room`(纯重构,行为不变)

**Files:**
- Modify: `cli.py`(把 `main` 里单房间流程抽成 `play_room(url, a)`,`main` 调用它)
- Test: `test_potplayer.py`(现有测试保证无回归;本任务不新增行为测试)

**Interfaces:**
- Produces: `cli.play_room(url: str, a) -> int`(a 为 argparse 命名空间,含 quality/line/title/mode/port/grace)
- `cli.main()` 行为不变。

- [ ] **Step 1: 重构 `main`**

把 `cli.py` 中 `main()` 从 `a = ap.parse_args()` **之后**的全部房间处理逻辑(`info = sites.parse(a.url)` 起到函数末尾 `return` 结束)整体移入新函数 `play_room(url, a)`,把其中所有 `a.url` 替换为参数 `url`。`main` 尾部改为:

```python
def main():
    ap = argparse.ArgumentParser(prog="potplayer-live")
    ap.add_argument("url", help="直播间地址(如 https://www.huya.com/lpl),或分区(如 /g/lol、英雄联盟、lol)")
    ap.add_argument("--quality", default=None)
    ap.add_argument("--line", type=int, default=0)
    ap.add_argument("--title", default=None)
    ap.add_argument("--mode", default="serve", choices=["serve", "m3u", "direct", "print"])
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--grace", type=int, default=180,
                    help="serve 模式:无连接空闲多少秒后自动退出，<=0 常驻，默认 180")
    a = ap.parse_args()
    return play_room(a.url, a)


def play_room(url, a):
    info = sites.parse(url)
    print(f"房间号 : {info['rid']}")
    print(f"主播   : {info['nick']}")
    print(f"标题   : {info['title']}")
    print(f"直播中 : {info['living']}")
    if not info["living"]:
        print("主播未开播。")
        return 1

    name, stream = common.pick(info, a.quality)
    title = a.title or info["nick"] or info["title"]
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
        _open_potplayer(path, title, is_url=False)
        print(f"已用 m3u 播放列表打开:{path}\n卡住时在 PotPlayer 播放列表切换「备用N」。")
        return 0

    port, reuse = _choose_port(a.port)
    local = _serve_url(port, url, a.quality)

    srv = None
    if reuse:
        print(f"复用已有代理 (端口 {port})，无需新起。")
    else:
        here = os.path.dirname(os.path.abspath(__file__))
        srv = subprocess.Popen([sys.executable, os.path.join(here, "server.py"),
                                url, str(port), a.quality or "", str(a.grace)])
        if not _wait_ready(port):
            print("警告:本地代理未在预期时间内就绪，仍尝试打开播放器。")
        print(f"本地代理已启动 (PID {srv.pid}，端口 {port})。")

    _open_potplayer(local, title, is_url=True)
    print(f"地址:{local}")

    if reuse:
        return 0
    print("直播断流由服务器自动重解析续播，播放器无感。Ctrl+C 结束。")
    try:
        srv.wait()
    except KeyboardInterrupt:
        srv.terminate()
    return 0
```

- [ ] **Step 2: 跑全量确认无回归**

Run: `python -m unittest test_potplayer`
Expected: OK(现有 `_serve_url` 用 `url` 参数,契约不变)

- [ ] **Step 3: 冒烟确认可导入、参数解析正常**

Run: `python cli.py --help`
Expected: 打印用法,无异常。

- [ ] **Step 4: 提交**

```bash
git add cli.py
git commit -m "refactor(cli): 抽出 play_room,main 仅解析参数并转调"
```

---

### Task 7: cli.py — 分区浏览流程(`browse_category` + `_choose_index` + `--pages`)

**Files:**
- Modify: `cli.py`(新增 `_choose_index` / `browse_category`;`main` 加分支与 `--pages`)
- Test: `test_potplayer.py`(新增 `TestChooseIndex`)

**Interfaces:**
- Consumes: `sites.is_category` / `sites.list_rooms`(Task 5)、`play_room`(Task 6)
- Produces:
  - `cli._choose_index(n: int, isatty: bool, input_fn=input) -> int | None`
  - `cli.browse_category(a) -> int`

- [ ] **Step 1: 写失败测试**

```python
class TestChooseIndex(unittest.TestCase):
    def test_enter_selects_first(self):
        self.assertEqual(cli._choose_index(5, True, input_fn=lambda p: ""), 0)

    def test_number_selects_that_index(self):
        self.assertEqual(cli._choose_index(5, True, input_fn=lambda p: "3"), 2)

    def test_q_cancels(self):
        self.assertIsNone(cli._choose_index(5, True, input_fn=lambda p: "q"))

    def test_non_interactive_returns_none(self):
        self.assertIsNone(cli._choose_index(5, False))

    def test_out_of_range_then_valid(self):
        seq = iter(["99", "2"])
        self.assertEqual(cli._choose_index(5, True, input_fn=lambda p: next(seq)), 1)

    def test_eof_cancels(self):
        def boom(_):
            raise EOFError
        self.assertIsNone(cli._choose_index(5, True, input_fn=boom))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m unittest test_potplayer.TestChooseIndex -v`
Expected: FAIL — `module 'cli' has no attribute '_choose_index'`

- [ ] **Step 3: 实现**(修改 `cli.py`)

`main` 里新增参数与分支:
```python
    ap.add_argument("--pages", type=int, default=3,
                    help="分区浏览抓取页数(每页约 9 个房间)，默认 3")
    a = ap.parse_args()
    if sites.is_category(a.url):
        return browse_category(a)
    return play_room(a.url, a)
```

新增两个函数(放在 `play_room` 之前):
```python
def _choose_index(n, isatty, input_fn=input):
    """返回选中的 0-based 索引;回车=第 1 个;q/EOF/非交互=None(取消);越界重试。"""
    if not isatty:
        return None
    while True:
        try:
            s = input_fn("选择房间序号(回车看第 1 个,q 退出): ").strip()
        except EOFError:
            return None
        if s == "":
            return 0
        if s.lower() == "q":
            return None
        if s.isdigit() and 1 <= int(s) <= n:
            return int(s) - 1
        print(f"请输入 1~{n} 的序号,或 q 退出。")


def browse_category(a):
    """列出分区在播房间 → 选序号 → 复用 play_room 播放。非交互则只打印带地址的列表。"""
    data = sites.list_rooms(a.url, pages=a.pages)
    rooms = data["rooms"]
    if not rooms:
        print("没解析到房间(可能分区无人直播或页面改版)。")
        return 1
    interactive = sys.stdin.isatty()
    print(f"{data['name']} · 正在直播(前 {len(rooms)} 个,按人气):")
    for i, r in enumerate(rooms, 1):
        line = f"  {i:2}. [{r['viewers']:>7}] {r['nick']}  {r['title']}"
        if not interactive:
            line += f"  → https://www.huya.com/{r['room']}"
        print(line)
    idx = _choose_index(len(rooms), interactive)
    if idx is None:
        if not interactive:
            print("非交互模式:复制上面的地址直接跑,如 python cli.py https://www.huya.com/<房间>")
        return 0
    return play_room(f"https://www.huya.com/{rooms[idx]['room']}", a)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m unittest test_potplayer.TestChooseIndex -v`
Expected: PASS(6 项)

- [ ] **Step 5: 跑全量**

Run: `python -m unittest test_potplayer`
Expected: OK

- [ ] **Step 6: 真实冒烟(手动,可选)**

Run: `python cli.py https://www.huya.com/g/lol --mode print`
Expected: 打印分区房间编号列表;交互选一个后进入 print 模式解析。若分区无人在播则提示"没解析到房间"。

- [ ] **Step 7: 提交**

```bash
git add cli.py test_potplayer.py
git commit -m "feat(cli): 分区浏览(选序号播放)+ --pages + 非交互降级"
```

---

### Task 8: 文档 — SKILL.md / README.md

**Files:**
- Modify: `SKILL.md`(新增分区浏览说明与触发场景)
- Modify: `README.md`(用法 + `--pages`)

**Interfaces:** 无代码接口。

- [ ] **Step 1: 更新 README.md**

在「快速开始」后新增小节:
```markdown
## 浏览分区(挑房间看)

不知道看哪个房间时,给一个分区,列出正在直播的房间,选序号直接看:

```bash
python cli.py https://www.huya.com/g/lol   # 分区页地址
python cli.py 英雄联盟                       # 中文名
python cli.py lol                            # 别名
python cli.py 1                              # 分区 gid
```

列出后输入序号(回车看第 1 个,q 退出)即复用 serve 流程播放。
`--pages N` 控制抓取页数(每页约 9 个,默认 3)。
```

并在「常用选项」表格追加一行:
```markdown
--pages N     分区浏览抓取页数(每页约 9 个房间),默认 3
```

- [ ] **Step 2: 更新 SKILL.md**

在 "## Usage" 之前新增小节:
```markdown
## Category browsing

Given a category instead of a room, list its live rooms and pick one to play:

```bash
python cli.py https://www.huya.com/g/lol   # category page URL
python cli.py 英雄联盟 / lol / 1             # Chinese name / alias / gid
```

Categories resolve via `bussLive` (name/alias → gid) and rooms are scraped from
the mobile SSR page `m.huya.com/g/<slug>?page=N` (UTF-8, deduped across pages).
Selecting a number reuses the normal serve flow. `--pages N` controls how many
pages to fetch (~9 rooms each, default 3).
```

同时把 "Overview" 首句触发场景补上"浏览分区/列出直播房间"。

- [ ] **Step 3: 提交**

```bash
git add README.md SKILL.md
git commit -m "docs: 补充分区浏览用法与 --pages"
```

---

## Self-Review(计划完成后自查记录)

- **Spec 覆盖**:数据源(Task 1 gzip/编码、Task 2 目录、Task 4 列表)、输入识别(Task 5 `is_category`)、交互+非交互降级(Task 7)、分页去重(Task 4)、模块改动(1-7)、文档(8)。均有对应任务。
- **占位符**:无 TBD/TODO;每个代码步骤给出完整代码。
- **类型一致**:`resolve_category` 返回 `(slug, display)` 贯穿 Task 3/4;`list_category` 返回 `{"name","slug","rooms"}` 在 Task 5/7 一致使用;房间 dict 键 `room/nick/viewers/title` 全程一致;`_choose_index(n, isatty, input_fn)` 签名 Task 7 定义与测试一致。
