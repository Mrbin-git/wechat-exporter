"""TXT exporter."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .html_export import safe_name


def write_chat(out_dir: Path, display: str, msgs) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{safe_name(display)}.txt"
    lines = [f"# {display}", f"# 导出时间 {datetime.now():%Y-%m-%d %H:%M}", ""]
    for m in msgs:
        lines.append(f"[{m.time_str}] {m.sender}: {m.display_text}")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
