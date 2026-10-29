"""Render one chat as a self-contained, offline-browsable HTML page."""
from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path

KIND_LABEL = {
    "image": "图片", "voice": "语音", "video": "视频", "file": "文件", "emoji": "表情",
    "link": "链接", "quote": "引用", "location": "位置", "card": "名片",
    "miniprogram": "小程序", "channels": "视频号", "system": "系统消息", "app": "应用消息",
}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


def _esc(text: str) -> str:
    return html.escape(text or "", quote=False)


def safe_name(name: str) -> str:
    cleaned = "".join(c for c in name if c not in '\\/:*?"<>|').strip()
    return cleaned or "chat"


def _date_line(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def write_chat(out_dir: Path, chat: str, display: str, msgs, contacts: dict,
               media_map: dict[str, list[Path]] | None = None,
               media_dir_rel: str = "") -> Path:
    """Write ``<safe display>.html``.

    ``media_map`` maps message md5 -> absolute paths of collected media; the
    ``media_dir_rel`` prefix is used for the relative href inside the export.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = safe_name(display)
    rows: list[str] = []
    last_day = ""
    for m in msgs:
        day = datetime.fromtimestamp(m.create_time).strftime("%Y-%m-%d")
        if day != last_day:
            rows.append(f'<div class="day">{day}</div>')
            last_day = day
        sender = m.sender or chat
        info = contacts.get(sender) or {}
        name = info.get("remark") or info.get("nick") or sender
        ex = getattr(m, "extras", {}) or {}
        md5 = str(ex.get("md5") or "")
        files = (media_map or {}).get(md5) or []
        body = _render_body(m, files, media_dir_rel)
        cls = "me" if sender.endswith("_self") else "other"
        rows.append(
            f'<div class="msg {cls}">'
            f'<div class="who">{_esc(name)} <span class="t">{_date_line(m.create_time)}</span></div>'
            f'<div class="bubble">{body}</div></div>'
        )
    header = (f'<header><h1>{_esc(display)}</h1>'
              f'<div class="meta">{len(msgs)} 条消息 · 导出于 {datetime.now():%Y-%m-%d %H:%M}</div>'
              f'<div class="meta small"><a href="index.html">← 返回会话列表</a></div></header>')
    page = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(display)} - 微信聊天记录</title>
<link rel="stylesheet" href="style.css">
</head><body class="chat-page">
{header}
<main class="chat">
{''.join(rows)}
</main>
<footer>本页面由本地导出工具生成，仅包含您本人设备上的聊天数据。</footer>
</body></html>"""
    path = out_dir / f"{safe}.html"
    path.write_text(page, encoding="utf-8")
    return path


def _rel(target: Path, base: Path) -> str:
    try:
        return target.relative_to(base).as_posix()
    except ValueError:
        return target.as_posix()


def _render_body(m, files: list[Path], media_dir_rel: str) -> str:
    ex = getattr(m, "extras", {}) or {}
    if m.kind == "text":
        return _esc(m.display_text).replace("\n", "<br>")
    if m.kind in ("image", "emoji", "video"):
        images = [f for f in files if f.suffix.lower() in IMAGE_EXT]
        others = [f for f in files if f.suffix.lower() not in IMAGE_EXT]
        if images:
            best = sorted(images, key=lambda p: p.stat().st_size, reverse=True)[0]
            rel = f"{media_dir_rel}/{best.name}" if media_dir_rel else best.name
            return f'<a href="{rel}" target="_blank"><img loading="lazy" src="{rel}" alt="图片"></a>'
        if others:
            f = others[0]
            rel = f"{media_dir_rel}/{f.name}" if media_dir_rel else f.name
            label = "视频" if m.kind == "video" else "媒体"
            return f'<a href="{rel}" target="_blank">[{label}] {_esc(f.name)}</a>'
        return f'<span class="media-missing">[{KIND_LABEL.get(m.kind, m.kind)}·未找到本地文件]</span>'
    if m.kind == "file":
        name = ex.get("file_name") or m.title or "文件"
        size = str(ex.get("total_len") or "")
        extra = f' <span class="dim">({int(size)/1024/1024:.1f} MB)</span>' if size.isdigit() else ""
        link = ""
        picked = [f for f in files if f.suffix.lower() != ".dat"]
        if picked:
            rel = f"{media_dir_rel}/{picked[0].name}" if media_dir_rel else picked[0].name
            link = f' <a href="{rel}" target="_blank">打开</a>'
        return f'<span class="file-ico">📄</span> {_esc(name)}{extra}{link}'
    if m.kind == "link":
        return (f'<div class="card"><div class="title">{_esc(m.title or m.url)}</div>'
                f'<div class="url">{_esc(m.url)}</div></div>')
    if m.kind == "quote":
        return f'<div class="card"><div class="title">{_esc(ex.get("title", ""))}</div></div>'
    if m.kind == "location":
        return f'<span class="loc">📍 {_esc(ex.get("poi", "") or ex.get("label", ""))}</span>'
    if m.kind == "card":
        return f'<span class="loc">👤 {_esc(ex.get("nickname", ""))}</span>'
    if m.kind in ("miniprogram", "channels"):
        return f'<div class="card"><div class="title">[{KIND_LABEL[m.kind]}] {_esc(m.title)}</div></div>'
    if m.kind == "system":
        return f'<span class="sys">{_esc(m.display_text)}</span>'
    return f'<span class="dim">[{m.kind}] {_esc(m.title or m.content[:80])}</span>'


def write_index(out_dir: Path, stats: list[dict]) -> Path:
    out_dir = Path(out_dir)
    items = []
    for s in stats:
        safe = safe_name(s["display"])
        items.append(
            f'<li><a href="{safe}.html">{_esc(s["display"])}</a>'
            f'<span class="count">{s["count"]} 条</span></li>')
    page = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>微信聊天记录导出</title><link rel="stylesheet" href="style.css"></head>
<body class="index-page"><header><h1>微信聊天记录</h1>
<div class="meta">{len(stats)} 个会话 · {sum(s['count'] for s in stats)} 条消息 · 导出于 {datetime.now():%Y-%m-%d %H:%M}</div></header>
<main><ul class="chat-list">{''.join(items)}</ul></main>
<footer>本页面由本地导出工具生成，可离线浏览。</footer></body></html>"""
    path = out_dir / "index.html"
    path.write_text(page, encoding="utf-8")
    return path
