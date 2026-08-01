# potplayer-live: Live Stream Parser for PotPlayer

## Overview

This tool parses real live streams from streaming platforms, browses categories to list live rooms, and plays them via **PotPlayer** on Windows. Currently supporting Huya (huya.com), it reconstructs the platform's stream-signing algorithm while fixing a disconnection issue that occurred around every 2 minutes.

It targets PotPlayer only: the platform parsing runs headless and hands PotPlayer a stable address to play.

## Key Features

**Four operational modes:**
- **serve** (default): Local proxy server with automatic reconnection and timestamp correction when streams drop
- **m3u**: Multi-CDN playlist generation for manual line switching
- **direct**: Single FLV stream link (no automatic recovery)
- **print**: Parse and display addresses without launching a player

## Technical Foundation

The implementation centers on three core mechanisms:

1. **API-first approach**: Uses mobile endpoint `https://mp.huya.com/cache.php?m=Live&do=profileRoom&roomid=<rid>` instead of HTML scraping to retrieve CDN routes and quality tiers.

2. **Anti-hotlinking signature**: The `wsSecret` parameter requires UID bit-rotation, base64-decoded template substitution with converted UID/stream name/MD5 hash/timestamp, then final MD5 hashing.

3. **Connection stability (serve mode)**: A local HTTP proxy re-parses and re-signs the stream when the platform drops the connection (~every 2 minutes), and rewrites FLV timestamps so the new segment stitches seamlessly onto the previous one — PotPlayer just plays a stable `http://127.0.0.1:<port>/…flv` and never notices.

## PotPlayer specifics

- **Window title**: set via PotPlayer's `URL\title` syntax (appending `\<title>` to the address). For a local `.m3u` file the title comes from its `#EXTINF` entry instead.
- **Request headers**: PotPlayer's `/referer` and `/user_agent` command-line switches are unreliable (they break on values containing spaces, e.g. a desktop User-Agent). Huya's signed FLV needs no headers (the `wsSecret` signature is the auth), so direct/m3u modes pass none. If a future platform requires headers, use **serve mode** — the proxy attaches them.
- **PotPlayer location**: resolved from the `POTPLAYER` env var first, then common install paths and Scoop's `apps/potplayer/current`, then `PATH`.

## Windows note

The self-healing proxy catches `ConnectionAbortedError` (WSAECONNABORTED / WinError 10053) on the downstream write — the way PotPlayer/Windows signals a client disconnect — so closing the player cleanly stops the proxy instead of triggering an upstream reconnect storm. Port probing in serve mode runs concurrently, so a machine whose loopback refuses closed ports slowly (TUN/filter drivers) still starts quickly.

## Category browsing

Given a category instead of a room, list its live rooms and pick one to play:

```bash
uv run -m potplayer_live https://www.huya.com/g/lol   # category page URL
uv run -m potplayer_live 英雄联盟 / lol / 1             # Chinese name / alias / gid
```

Categories resolve via `bussLive` (name/alias → gid) and rooms are scraped from
the mobile SSR page `m.huya.com/g/<slug>?page=N` (UTF-8, deduped across pages).
Selecting a number reuses the normal serve flow. `--pages N` controls how many
pages to fetch (~9 rooms each, default 3).

## Usage

```bash
uv run -m potplayer_live https://www.huya.com/lpl [options]
```

Supports room aliases, numeric IDs, quality selection, and custom titles.
