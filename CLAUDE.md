# potplayer-live 开发约定

面向 PotPlayer 的直播流解析器。后续开发遵循以下约定。

## 环境与依赖

- Python 固定 `3.14.*`(见 pyproject),用 [uv](https://github.com/astral-sh/uv) 运行。
- **纯标准库,零第三方依赖**。不要引入新依赖——能几行标准库搞定的不装包。
- 入口统一 `uv run -m potplayer_live`;不再有根级脚本。

## 结构

```
potplayer_live/      主包(cli 入口 / server 代理 / common 工具)
  sites/             平台层:__init__.py 派发,每平台一个模块
tests/               标准库 unittest
```

## 新增平台

在 `potplayer_live/sites/` 下新建一个模块,实现统一接口,再到 `sites/__init__.py`
的 `SITES` 列表登记即可,`cli` / `server` 无需改动:

- `DOMAINS`      `list[str]`  匹配的域名关键字(如 `["huya.com"]`)
- `PLAY_HEADERS` `dict`       拉流 HTTP 头(无则留空 dict)
- `parse(url)`   `-> dict`    `{rid, nick, title, living, streams{名:{quality,url,backups}}}`
- 分区浏览另需 `list_category(ident, pages)`(可选)

## 测试

- 框架:标准库 `unittest`,**不触网**——网络边界(`fetch` / `open_fn` / `resolve_fn`)
  一律做成可注入参数,用假上游驱动。
- 跑:`uv run -m unittest tests.test_potplayer`。
- 非平凡逻辑(签名、FLV 改写、解析、选路)改动后补一条断言;纯函数优先用独立参考实现比对,别只锁魔数值。
- **提交前必须全绿。**

## 风格

- 注释与 commit 信息用中文,风格对齐现有代码(解释「为什么」而非复述代码)。
- 优先最小改动,不做投机性抽象。
