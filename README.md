# potplayer-live

用 **PotPlayer** 看直播:解析直播平台(当前支持虎牙 huya.com、抖音 live.douyin.com、斗鱼 douyu.com、哔哩哔哩 live.bilibili.com)的真实直播流地址,交给
PotPlayer 播放。

只针对 PotPlayer:平台解析在后台完成,给 PotPlayer 一个稳定地址来播放。

## 依赖

- Windows + [PotPlayer](https://potplayer.daum.net/)(或 `scoop install potplayer`)
- [uv](https://github.com/astral-sh/uv)(纯标准库、无第三方依赖;Python 3.14 由 uv 自动装好)

安装 uv:

```powershell
# Windows(PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
# 或
scoop install uv
```

## 快速开始

```bash
# 虎牙(推荐:serve 模式,本地代理,自动跨断流自愈)
uv run -m potplayer_live https://www.huya.com/lpl

# 抖音
uv run -m potplayer_live https://live.douyin.com/123456

# 斗鱼
uv run -m potplayer_live https://www.douyu.com/123456

# 哔哩哔哩
uv run -m potplayer_live https://live.bilibili.com/123456

# 或直接用 python
python -m potplayer_live https://www.huya.com/lpl
```

## 四种模式(`--mode`)

| 模式 | 说明 |
|------|------|
| `serve`(默认) | 本地转流代理,给 PotPlayer 一个固定地址,自动跨 ~2 分钟断流无缝续播 |
| `m3u` | 生成多线路播放列表,卡住时在 PotPlayer 播放列表里切「备用N」线路 |
| `direct` | 单条 flv 直链,最简单,卡住无法自动恢复 |
| `print` | 只解析并打印各清晰度/线路地址,不打开播放器 |

## 常用选项

```
--quality Q   清晰度显示名或码率(如 "原画" / 蓝光10M / 2000),默认最高
--line K      direct/m3u 选第 K 条线路(0 起),默认 0
--title T     自定义 PotPlayer 窗口标题,默认用房间名(主播名)
--port P      serve 模式端口,默认 8787
--grace S     serve 模式无连接空闲 S 秒后自动退出,<=0 常驻,默认 180
```

房间地址支持别名(`https://www.huya.com/lpl`)与纯房间号(`https://www.huya.com/660000`)。

## PotPlayer 路径

优先读环境变量 `POTPLAYER`,否则自动探测默认安装目录、Scoop 的
`apps/potplayer/current`,以及 `PATH`。找不到时:

```bat
set POTPLAYER=D:\Apps\PotPlayer\PotPlayerMini64.exe
```

## 说明

- **窗口标题**:用 PotPlayer 的「地址\标题」语法设置;m3u 文件靠内部 `#EXTINF` 名。
- **请求头**:PotPlayer 的 `/referer`、`/user_agent` 命令行开关不可靠(值含空格会被拆散),
  而虎牙已签名 flv 无需请求头即可播放,故 direct/m3u 不传头;需要请求头的平台请走 serve 模式。
- **Windows 断流风暴修复**:代理在下游写数据时捕获 `ConnectionAbortedError`(WinError 10053),
  这是 Windows 上播放器断开连接的信号 —— 关掉 PotPlayer 会让代理干净退出,而不是疯狂重连上游。
- **端口探测并发**:某些启用了 TUN/过滤驱动的代理软件会让"连接被拒绝"延迟 ~2s,
  serve 模式并发探测端口,避免首次启动被拖慢。
- **哔哩哔哩原画**:B 站原画/4K 需登录后取流,设环境变量 `BILI_COOKIE`(浏览器里的
  `SESSDATA`)即可解锁;不设则走免登录,最高约蓝光。

## 项目结构

```
potplayer_live/          # 主包
  __main__.py            # 入口(uv run -m potplayer_live)
  cli.py                 # 命令行:参数解析、端口选择、启动 PotPlayer
  server.py              # 本地转流代理:跨断流自愈、FLV 时间戳改写
  common.py              # 公共工具:HTTP、清晰度选择、m3u 生成
  sites/
    __init__.py          # 平台派发层(按域名路由)
    huya.py              # 虎牙解析:签名 flv 地址
    douyin.py            # 抖音解析:ttwid cookie / enter 接口
    douyu.py             # 斗鱼解析:getEncryption + 纯 MD5 auth + getH5PlayV1
    bilibili.py          # B 站直播解析:room_init + getRoomPlayInfo(免签名)
tests/                   # 标准库 unittest,零依赖、不触网
```

新增平台见 [CLAUDE.md](CLAUDE.md)。

## 测试

纯标准库、不触网,直接跑:

```bash
uv run -m unittest tests.test_potplayer
```

覆盖清晰度选择、m3u 生成、虎牙签名(uid 移位 / wsSecret)、serve 代理的按请求
`room`/`quality` 解析,以及 cli 的就绪轮询。

## 免责声明

仅供学习研究,请遵守各平台服务条款,勿用于商业或侵权用途。
