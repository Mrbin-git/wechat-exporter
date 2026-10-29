"""Media collection: decrypt and copy images/files/voices into the export."""
from __future__ import annotations

import shutil
from pathlib import Path

from ..core import media


class MediaCollector:
    def __init__(self, account_dir: Path, wxid: str, enabled: bool = True):
        self.account_dir = Path(account_dir)
        self.wxid = wxid
        self.enabled = enabled
        self.keys = media.resolve_keys(self.account_dir, wxid) if enabled else []
        self.stats = {"images": 0, "files": 0, "voices": 0, "missing": 0, "skipped": 0}

    def collect(self, msgs, dest_dir: Path) -> dict[str, list[Path]]:
        """Decrypt/copy media for the given messages, returning md5 -> files."""
        if not self.enabled:
            return {}
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        out: dict[str, list[Path]] = {}
        for m in msgs:
            try:
                entries = media.resolve_media(m, self.account_dir)
            except OSError:
                entries = []
            if not entries:
                if m.kind in ("image", "video", "voice", "file", "emoji"):
                    self.stats["missing"] += 1
                continue
            md5 = (getattr(m, "extras", {}) or {}).get("md5", "") or m.local_id
            for entry in entries:
                src = Path(entry["path"])
                try:
                    if entry["type"] == "file" and src.suffix.lower() != ".dat":
                        target = dest_dir / src.name
                        if not target.exists():
                            shutil.copyfile(src, target)
                        out.setdefault(str(md5), []).append(target)
                        self.stats["files"] += 1
                    elif entry["type"] == "voice" and src.suffix.lower() != ".dat":
                        target = dest_dir / src.name
                        if not target.exists():
                            shutil.copyfile(src, target)
                        out.setdefault(str(md5), []).append(target)
                        self.stats["voices"] += 1
                    else:
                        target = media.decrypt_media(src, dest_dir, self.keys)
                        if target:
                            out.setdefault(str(md5), []).append(target)
                            self.stats["images"] += 1
                        else:
                            self.stats["skipped"] += 1
                except OSError:
                    self.stats["skipped"] += 1
        return out
