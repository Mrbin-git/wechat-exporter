"""CSV exporter (Excel-friendly, UTF-8 BOM)."""
from __future__ import annotations

import csv
from pathlib import Path

HEADERS = ["时间", "发送者", "类型", "内容", "链接/文件", "local_id", "local_type"]


def _safe(name: str) -> str:
    return "".join(c for c in name if c not in '\\/:*?"<>|').strip() or "chat"


def _extra(m) -> str:
    ex = getattr(m, "extras", {}) or {}
    if m.kind == "file":
        return ex.get("file_name") or m.title
    if m.kind == "link":
        return m.url
    return ""


def write_all(out_dir: Path, display: str, msgs) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{_safe(display)}.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(HEADERS)
        for m in msgs:
            w.writerow([m.time_str, m.sender, m.kind, m.display_text, _extra(m),
                        m.local_id, hex(m.local_type)])
    return path
