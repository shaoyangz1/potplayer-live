# 抖音(douyin)平台支持 — 设计

日期:2026-08-01
范围:**本轮只做抖音**(单房间 `parse` + 分区浏览 `list_category`)。斗鱼(douyu)在抖音完成并测试通过后另开一轮。

## 目标

给 `potplayer-live` 增加抖音直播支持,复用现有平台插件接口与 serve 自愈中继,不改变虎牙现有行为。

## 平台插件接口(现状回顾)

模块暴露:`DOMAINS`、`PLAY_HEADERS`、`parse(url) -> {rid, nick, title, living, streams{清晰度:{quality,url,backups}}}`;分区浏览可选暴露 `list_category(ident, pages) -> {name, slug, rooms:[...]}`。在 `sites/__init__.py` 的 `SITES` 里登记即接入,server/cli 无需为具体平台改动。

## 一、新模块 `sites/douyin.py`

```
DOMAINS = ["live.douyin.com", "douyin.com"]
UA_DESKTOP = "…Chrome…"
PLAY_HEADERS = {"User-Agent": UA_DESKTOP, "Referer": "https://live.douyin.com/"}
```

### ttwid 获取

抖音 web 接口需 `ttwid` cookie。首次 GET `https://live.douyin.com/`,从响应的 `Set-Cookie` 里取 `ttwid`。新增模块内私有 `_ttwid()`:用 `urllib.request` 直接发请求、读 `response.headers.get_all("Set-Cookie")` 正则抓 `ttwid=...`。分区翻页时只取一次、跨页复用。

### `parse(url)` — 单房间

1. `resolve_web_rid(url)`:取路径最后一段作 `web_rid`(`https://live.douyin.com/<web_rid>`)。
2. 取 ttwid。
3. GET enter 接口(带 ttwid cookie):
   `https://live.douyin.com/webcast/room/web/enter/?aid=6383&app_name=douyin_web&live_id=1&device_platform=web&language=zh-CN&enter_from=web_live&cookie_enabled=true&browser_platform=Win32&browser_name=Chrome&browser_version=…&web_rid=<web_rid>&room_id_str=&enter_source=`
4. 解析 JSON:
   - `data.data[0].status == 2` → 在播。
   - 标题 `data.data[0].title`;主播名 `data.user.nickname`(或 `data.data[0].owner.nickname`);`rid` 用 `web_rid`。
   - 流:优先 `stream_url.live_core_sdk_data.pull_data.stream_data`(内嵌一段 JSON 字符串,含各清晰度中文名 原画/蓝光/超清/高清/标清 + 码率 + flv 地址);回退 `stream_url.flv_pull_url`(`{FULL_HD1,HD1,SD1,SD2}`,无中文名时用这几个键名映射)。
5. 组装 `streams{中文名:{quality:码率, url:flv, backups:[]}}`。**只用 flv**(server 的 `relay_flv` 只处理 flv,不吃 HLS)。多档之间不互为 backups(抖音每档就一条 flv),`backups=[]`。

未开播:返回 `living=False`、`streams={}`(与虎牙一致)。

### a_bogus 风险

抖音较新的 `/v2/` 接口开始要 `a_bogus`(JS 计算的签名)。**先按 ttwid-only 实现 enter(非 v2)并测试**。若抖音返回 403/空数据,再评估移植纯 Python 版 `a_bogus`(社区有现成实现)。本设计不预先引入签名逻辑(YAGNI)。

### `list_category(ident, pages=3, fetch=None)` — 分区浏览

分区标识为 **双字段** `(partition, partition_type)`,与虎牙的单 slug 不同。

- 输入 `ident`:
  - `"720,1"` 形式 → 直接拆成 `(partition=720, partition_type=1)`。
  - 中文别名(如「英雄联盟」)→ 查模块内小型 `ALIASES` 映射到 `(partition, partition_type)`。别名表在实现/测试时用抓到的真实 id 填充,保持精简、明确标注「可能随抖音调整」。
  - 未命中 → 抛出带引导的错误(提示改用 `id,type` 形式)。
- 请求(带 ttwid):
  `https://live.douyin.com/webcast/web/partition/detail/room/?aid=6383&app_name=douyin_web&live_id=1&device_platform=web&count=15&offset=<N>&partition=<p>&partition_type=<t>&req_from=2`
  逐页 `offset += count`,解析每个房间的 `web_rid`、昵称、人气、标题;跨页按 `web_rid` 去重保序。
- 返回 `{"name": 显示名, "slug": ident, "rooms": [{room, nick, viewers, title, url}]}`。
  `url = "https://live.douyin.com/<web_rid>"`(完整可播地址,见下节 cli 去耦)。
- `fetch(partition, partition_type, offset)->json` 可注入,便于不触网测试。

## 二、派发层去虎牙耦合

现状:`sites/__init__.py` 的 `is_category` 写死 `/g/`、`category_slug` 写死 `/g/` 取段;`cli.browse_category` 写死 `https://www.huya.com/{room}`。抖音房间 url 形态不同(`live.douyin.com/<web_rid>`),需要去耦。**小重构,不新增抽象。**

### 每个 room 携带完整 `url`

`list_category` 返回的每个 room **新增 `url` 字段**(完整可播地址)。虎牙 `list_category` 给 room 补 `url = "https://www.huya.com/" + room`。

### huya 特定规则移入 huya 模块

给 `huya` 增加:
- `is_category(url)`:http 路径首段 == `"g"`。
- `category_ident(url)`:`/g/` 后第一段(即原 `category_slug` 的逻辑)。

### `sites/__init__.py` 变通用

- `is_category(url)`:
  - http/https → `mod = get_site(url)`;有 `mod.is_category` 就用它,否则 `False`。
  - 裸串 → `bool(url.strip())`(**保持现状:裸串仍默认虎牙分区浏览**)。
- `list_rooms(url, pages)`:
  - http/https → `mod = get_site(url)`;`ident = mod.category_ident(url)`(有则用,否则回退取路径);`mod.list_category(ident, pages)`。
  - 裸串 → `huya.list_category(url, pages)`(现状)。

抖音分区浏览通过 URL 触发(`https://live.douyin.com/category/720,1` 或 `/category/英雄联盟`),`douyin.is_category` 判路径首段 == `"category"`,`douyin.category_ident` 取 `/category/` 后一段。

### cli 去硬编码

`browse_category`:
- 打印行:`→ {r['url']}`(不再拼 huya 域名)。
- 播放:`play_room(rooms[idx]['url'], a)`。

## 三、登记

`sites/__init__.py`:`from . import huya, douyin`;`SITES = [huya, douyin]`。

## 四、测试(非联网,注入桩)

新增 `tests` 用例:
- **douyin.parse**:喂桩 enter JSON —— 在播(多清晰度→streams 名/码率/flv 正确)、未开播(living=False、streams 空)、缺 sdk_data 回退 flv_pull_url。
- **douyin.list_category**:注入 `fetch`,多页返回、跨页去重、room 带 `url`;别名解析(命中 ALIASES、`id,type` 直传、未命中报错)。
- **派发层**:`get_site("https://live.douyin.com/xxx")` 命中 douyin;`is_category`/`list_rooms` 对抖音 category URL 正确路由;裸串仍走虎牙。
- **虎牙回归**:`list_category` room 现在带 `url` 字段(附加,不破坏原字段);huya 新增 `is_category`/`category_ident` 行为。
- **cli**:`browse_category` 用 `r['url']`(可用现有注入方式)。

跑 `uv run -m unittest tests.test_potplayer` 全绿再提交。

## 非目标

- 不做斗鱼(下一轮)。
- 不预先实现 `a_bogus` 签名(除非测试证明 ttwid-only 被拒)。
- 不做抖音分区目录接口的中文名自动解析(该接口可能要签名、脆);中文名走小型静态别名表。
- 不支持抖音的 HLS(server 中继只吃 flv)。
