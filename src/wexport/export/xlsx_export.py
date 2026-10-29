"""XLSX exporter using openpyxl (bundled in vendor/)."""
from __future__ import annotations

from pathlib import Path


def _safe(name: str) -> str:
    return "".join(c for c in name if c not in '\\/:*?"<>|').strip() or "chat"


def write_chat(out_dir: Path, display: str, msgs) -> Path | None:
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font
    except ImportError:
        return None
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{_safe(display)}.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "聊天记录"
    headers = ["时间", "发送者", "类型", "内容", "链接/文件"]
    ws.append(headers)
    for c in ws[1]:
        c.font = Font(bold=True)
    for m in msgs:
        ex = getattr(m, "extras", {}) or {}
        extra = (ex.get("file_name") or m.title) if m.kind == "file" else (m.url if m.kind == "link" else "")
        ws.append([m.time_str, m.sender, m.kind, m.display_text, extra])
    widths = [20, 22, 8, 60, 40]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[chr(64 + i)].width = w
    ws.freeze_panes = "A2"
    wb.save(path)
    return path
