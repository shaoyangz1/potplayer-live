# 虎牙分区房间浏览 — 设计

## 背景与目标

现状:只能给**已知房间地址**(完整 URL / 别名 / 房间号)播放单个房间。用户想"浏览虎牙某个分区**正在直播**的房间,从里面挑一个直接看",不必事先知道房间地址。

**目标**:给一个分区标识(分区页 URL / 中文名 / 别名 / gid),列出该分区人气靠前的在播房间(编号),交互式选序号 → 复用现有 serve 流程用 PotPlayer 播放。

## 用户交互

```
$ python cli.py https://www.huya.com/g/lol
英雄联盟 · 正在直播(前 27 个,按人气):
   1. [3235万] 虎牙英雄联盟赛事    【能做到吗?】BLG 1:1 HLE MSI
   2. [1404万] Zz1tai姿态          Zhuo Rita一起解说 MSI
   3. [ 662万] kRYST4L            MSI 决赛 就在今天
   ...
选择房间序号(回车看第 1 个,q 退出): 2
→ 复用现有 serve 流程,打开 PotPlayer 播放 huya.com/333003
```

- 等价输入:`python cli.py 英雄联盟`、`python cli.py lol`、`python cli.py 1`(gid)。
- **非交互降级**:`sys.stdin.isatty()` 为 False(被管道/重定向)时,不 `input()` 阻塞,只打印每行带 `https://www.huya.com/<房间>` 的编号列表 + 提示"复制地址再跑",返回 0。

## 数据来源(均已实测可用,纯标准库)

1. **分区目录** `https://live.cdn.huya.com/liveconfig/game/bussLive?bussType=1`
   - 返回热门分区(约 38–112 个,随时段变):`gid`、`gameHostName`(别名)、`gameFullName`(中文名)、`totalCount`(人气)。
   - 有时返回 gzip(响应体以 `\x1f\x8b` 开头),需兼容解压;偶发网络失败,需重试。
   - **用途**:把用户给的"中文名/别名"**精确**匹配到 `gid`/别名(仅覆盖热门分区)。gid 与 URL slug 不依赖它。

2. **房间列表** `https://m.huya.com/g/<slug>?page=N`
   - `<slug>` 可为别名(`lol`)或 gid(`1`),二者实测都可用;移动端 SSR 的 HTML,**UTF-8 编码**。
   - 正则提取房间卡片:
     ```
     <a href="/<room>" class="qqqq g-link"> … <span class="nick">昵称</span>
       … <span class="viewer-count">人气</span> … <p class="title">标题</p>
     ```
   - 每页约 9 个,`?page=N` 翻页;跨页**去重**(用 set 记已见 `room`,保持首次出现顺序)。
   - **编码**:优先 `utf-8` strict,`UnicodeDecodeError` 时回退 `gb18030`(防偶发 GBK 节点)。

## 输入识别(cli.py)

现有房间输入一直是完整 URL(`https://www.huya.com/xxx`),据此定规则,不破坏现有语义:

- **是 URL 且 path 以 `/g/` 开头** → 分区浏览,`slug` = `/g/` 后第一段。
- **是裸串(非 http)**:
  - 纯数字 → 当 gid,直接浏览。
  - 否则 → 直接当 slug 透传(不阻塞于目录);同时查 bussLive 目录(仅覆盖热门分区)按 `gameFullName`(中文名)/ `gameHostName`(别名)**精确**反查显示名。抓不到房间时提示"分区无人直播或分区名/别名打错",并引导"看单房间请给完整地址",而非直接报错(保留 gid / URL-slug 透传路径)。
- **其它 URL(非 /g/)** → 房间(现状不变)。

## 模块改动

- **common.py**
  - `http_get` 加 gzip 解压兼容(响应体 magic 为 `\x1f\x8b` 时 `gzip.decompress`)。
  - 新增 `http_get_text(url, headers=None)`:取字节后按 `utf-8` strict → `gb18030` 回退解码为 str。
  - 全平台受益。

- **huya.py** 新增:
  - `CATEGORY_URL`(bussLive)、房间列表 URL 模板。
  - `_categories()` → 拉 bussLive(3 次重试 + gzip 兼容),返回 `[{gid, host, name, online}]`。
  - `resolve_category(ident)` → `ident` 为 gid / 别名 / 中文名 / URL-slug;返回 `(slug, 显示名)`。`slug` 优先直接用 `ident`(gid/别名/URL-slug 透传,**不阻塞于目录能否命中**);中文名输入则查目录匹配到别名/gid 作 `slug`。**显示名**尽量从 bussLive 目录按 gid/别名反查 `gameFullName`,查不到就用 `ident` 本身。
  - `list_category(ident, pages=3)` → 抓 `m.huya.com/g/<slug>` 各页,正则提取、去重,返回 `{name, rooms:[{room, nick, title, viewers}]}`。

- **sites.py** 新增派发:
  - `is_category(arg)`:判断输入是否为"分区浏览"(见上识别规则)。
  - `list_rooms(arg, pages)`:裸名默认派发到虎牙(当前唯一平台);`/g/` URL 按域名派发。保留"新增平台 = 写模块"扩展性。

- **cli.py**
  - 抽出 `play_room(url, a)`:把 `main` 里"解析房间 url → 选清晰度 → 按 mode 播放"整段抽成函数。
  - `main`:先 `sites.is_category(a.url)`;是则 `sites.list_rooms` → 打印编号列表 → 选序号(交互 / 非交互降级)→ 构造 `https://www.huya.com/<room>` → `play_room`。否则走现有单房间流程。
  - 新增 `--pages N`(默认 3)。

- **test_potplayer.py** 新增(注入 fixture,**不触网**):
  - 正则提取:给固定 UTF-8 HTML 片段,断言解析出正确 `room/nick/viewers/title`;含"万"字保留。
  - 编码回退:`utf-8` 与 `gb18030` 两种字节都能被 `http_get_text` 正确解码。
  - `resolve_category` 映射:gid / 别名 / URL-slug 透传、中文名查目录(注入假 `_categories`)。
  - 输入识别:`is_category` 对 `/g/` URL、纯数字、房间 URL 的判定。
  - gzip 兼容:`http_get` 对 gzip 响应体正确解压。

- **SKILL.md / README.md**:补触发词("看虎牙 XX 分区"、"列出直播房间")、用法与 `--pages`。

## 错误处理

- bussLive 重试后仍失败:仅影响"中文名/别名"解析 → 报错提示改用 gid 或分区页 URL(gid/别名/slug 不依赖目录)。
- 分区无在播房间 / 页面结构变化(正则 0 命中):提示"没解析到房间(可能分区无人直播或页面改版)"。
- 选择序号越界 / 非数字:重新提示或 `q` 退出。

## 不做(YAGNI)

- 不做"先列所有分区"的两级菜单(用户选择直接给分区)。
- 不做搜索、不做整分区 m3u 批量导出、不做人气数值换算/排序(移动端已按人气返回,`viewers` 原样保留字符串)。
