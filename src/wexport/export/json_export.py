"""JSON exporter: one record per message, UTF-8, no external deps."""
from __future__ import annotations

import json
from pathlib import Path


def _safe(name: str) -> str:
    return "".join(c for c in name if c not in '\\/:*?"<>|').strip() or "chat"


def write_chat(out_dir: Path, display: str, msgs) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{_safe(display)}.json"
    records = []
    for m in msgs:
        ex = dict(getattr(m, "extras", {}) or {})
        ex.pop("attachid", None)
        ex.pop("aeskey", None)
        records.append({
            "time": m.create_time,
            "time_str": m.time_str,
            "sender": m.sender,
            "kind": m.kind,
            "text": m.display_text,
            "title": m.title,
            "url": m.url,
            "local_id": m.local_id,
            "server_id": m.server_id,
            "local_type": hex(m.local_type),
            "extras": ex,
        })
    path.write_text(json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")
    return path
