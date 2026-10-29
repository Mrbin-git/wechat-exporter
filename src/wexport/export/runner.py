"""High level export driver used by the CLI and the web UI."""
from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ..core.reader import AccountReader
from . import csv_export, html_export, json_export, txt_export, xlsx_export
from .media_collect import MediaCollector


@dataclass
class ExportRequest:
    decrypted_dir: Path
    account_dir: Path
    wxid: str
    out_dir: str
    chats: list[str] = field(default_factory=list)
    formats: list[str] = field(default_factory=lambda: ["html"])
    since: int = 0
    until: int = 0
    export_media: bool = True
    include_system: bool = True
    progress: object | None = None


def _timestamp() -> str:
    import time
    return time.strftime("%Y%m%d-%H%M%S")


def resolve_out_dir(preferred: str) -> Path:
    """Pick a writable output directory.

    Windows' Controlled Folder Access (ransomware protection) commonly blocks
    unsigned tools from writing under Documents, which surfaces as WinError 5.
    When the user did not pick a folder we fall back to other writable spots
    instead of failing the whole export.
    """
    import os
    from pathlib import Path

    chosen = (preferred or "").strip()
    ts = _timestamp()
    if chosen:
        base = Path(chosen).expanduser()
        # 用户选的是父目录：在其下自动新建“年月日-时分秒”子目录。
        # 若传入的已经是这种目录（重复导出），就直接使用，避免层层嵌套。
        import re
        target = base if re.fullmatch(r"\d{8}-\d{6}", base.name) else base / ts
        try:
            target.mkdir(parents=True, exist_ok=True)
            probe = target / ".write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return target
        except PermissionError as exc:
            raise RuntimeError(
                f"无法写入导出目录：{base}（Windows 拒绝访问）。请把「输出目录」改成 "
                f"D:\\微信导出 这类可写位置；或到 Windows 安全中心 → 病毒和威胁防护 → "
                f"勒索软件防护 → 受控文件夹访问，把 WeChatExport.exe 加入允许列表。"
            ) from exc
        except OSError as exc:
            raise RuntimeError(f"无法创建导出目录：{base}（{exc}）") from exc

    home = Path.home()
    local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
    if getattr(sys, "frozen", False):
        app_dir = Path(sys.executable).resolve().parent
    else:
        app_dir = Path(__file__).resolve().parents[2]
    candidates = [
        home / "Documents" / "微信导出" / ts,
        app_dir / "导出" / ts,                   # portable: next to the exe
        local / "WeChatExporter" / "exports" / ts,
        home / "微信导出" / ts,
    ]
    errors: list[str] = []
    for cand in candidates:
        try:
            cand.mkdir(parents=True, exist_ok=True)
            probe = cand / ".write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return cand
        except OSError as exc:
            errors.append(f"{cand}: {exc}")
    raise RuntimeError(
        "无法创建导出目录（可能被 Windows 受控文件夹访问拦截）。请在「输出目录」中"
        "手动填写一个可写路径，例如 D:\\微信导出。详情：" + "；".join(errors[:2])
    )


def run(req: ExportRequest) -> dict:
    out = resolve_out_dir(str(req.out_dir))
    reader = AccountReader(Path(req.decrypted_dir))
    contacts = reader.contacts()
    sessions = {s["username"]: s for s in reader.sessions()}
    table_map = reader.table_map()

    chats = req.chats or [c for c in sessions if c in table_map or f"biz:{c}" in table_map]
    total = max(1, len(chats))
    stats: list[dict] = []
    collector = MediaCollector(Path(req.account_dir), req.wxid, enabled=req.export_media)

    for idx, chat in enumerate(chats):
        if req.progress:
            req.progress("export", f"正在导出 {chat}（{idx + 1}/{total}）", int(95 * idx / total))
        msgs = list(reader.iter_messages(chat, since=req.since, until=req.until))
        if not req.include_system:
            msgs = [m for m in msgs if m.kind != "system"]
        if not msgs:
            continue
        info = contacts.get(chat, {})
        display = info.get("remark") or info.get("nick") or chat
        safe = html_export.safe_name(display)
        media_map: dict[str, list[Path]] = {}
        media_rel = ""
        if req.export_media and "html" in req.formats:
            media_dir = out / "media" / safe
            media_map = collector.collect(msgs, media_dir)
            media_rel = f"media/{safe}"
        if "html" in req.formats:
            html_export.write_chat(out, chat, display, msgs, contacts, media_map, media_rel)
        if "txt" in req.formats:
            txt_export.write_chat(out, display, msgs)
        if "csv" in req.formats:
            csv_export.write_all(out, display, msgs)
        if "json" in req.formats:
            json_export.write_chat(out, display, msgs)
        if "xlsx" in req.formats:
            xlsx_export.write_chat(out, display, msgs)
        stats.append({"chat": chat, "display": display, "count": len(msgs)})

    if "html" in req.formats:
        html_export.write_index(out, stats)
        css = Path(__file__).resolve().parent.parent / "web" / "style.css"
        if css.is_file():
            shutil.copyfile(css, out / "style.css")
    return {"out_dir": str(out), "chats": stats, "media": collector.stats}
