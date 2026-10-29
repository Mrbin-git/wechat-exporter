"""End-to-end orchestration: scan -> key -> decrypt -> export."""
from __future__ import annotations

import os
import pathlib
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import discover
from .keys import get_passphrase
from .reader import AccountReader
from .sqlcipher import decrypt_database


def _find_media_dir(account_dir: Path) -> str:
    return str(Path(account_dir))


class Progress:
    """Thread-safe progress sink polled by the web layer."""

    def __init__(self):
        self._lock = threading.Lock()
        self.state = {"stage": "idle", "message": "", "percent": 0, "done": False,
                      "error": "", "log": [], "result": None}

    def update(self, stage: str | None = None, message: str | None = None, percent: int | None = None):
        with self._lock:
            if stage is not None:
                self.state["stage"] = stage
            if message is not None:
                self.state["message"] = message
                self.state["log"].append(message)
                self.state["log"] = self.state["log"][-200:]
            if percent is not None:
                self.state["percent"] = max(0, min(100, int(percent)))

    def finish(self, result=None, error: str = ""):
        with self._lock:
            self.state.update({"done": True, "result": result, "error": error,
                               "stage": "error" if error else "done"})
            if not error:
                self.state["percent"] = 100

    def snapshot(self) -> dict:
        with self._lock:
            return {**self.state, "log": list(self.state["log"])}


@dataclass
class PrepareResult:
    work_dir: Path
    decrypted: Path
    chats: list[dict]
    key_source: str
    warnings: list[str] = field(default_factory=list)


def _quick_check(path: Path) -> bool:
    """True when the decrypted sqlite file passes a quick integrity check."""
    import sqlite3
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            row = con.execute("pragma quick_check(1)").fetchone()
            return bool(row and str(row[0]).lower() == "ok")
        finally:
            con.close()
    except sqlite3.Error:
        return False


def work_root() -> Path:
    """Pick a writable scratch directory (honours WEXPORT_WORK_DIR)."""
    candidates: list[Path] = []
    override = os.environ.get("WEXPORT_WORK_DIR")
    if override:
        candidates.append(Path(override))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "WeChatExporter" / "work")
    candidates.append(Path(tempfile.gettempdir()) / "WeChatExporter" / "work")
    candidates.append(Path.home() / ".wechat-exporter" / "work")
    last_error: Exception | None = None
    for base in candidates:
        try:
            base.mkdir(parents=True, exist_ok=True)
            probe = base / ".write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return base
        except OSError as exc:
            last_error = exc
            continue
    raise RuntimeError(f"无法创建临时工作目录（请检查磁盘权限）: {last_error}")


def prepare(account, progress: Progress, manual_key: str | None = None) -> PrepareResult:
    """Recover keys, decrypt the account databases, list chats."""
    progress.update("key", "正在定位微信客户端…", 3)
    install = discover.find_install()
    progress.update("key", "正在获取数据库密钥（首次约 1-3 分钟）…", 6)
    key = get_passphrase(account.wxid, account.db_storage, install=install, manual_key=manual_key,
                         on_progress=lambda stage, msg: progress.update("key", msg, 15))
    if not key.get("ok"):
        raise RuntimeError(key.get("error") or "密钥获取失败")
    progress.update("key", f"密钥已就绪（来源：{key.get('source')}）", 30)

    work = work_root() / f"{account.folder}-{int(time.time())}"
    decrypted = work / "db"
    decrypted.mkdir(parents=True, exist_ok=True)

    wanted: list[Path] = []
    for sub in ("session", "contact", "message", "head_image", "general"):
        d = account.db_storage / sub
        if d.is_dir():
            wanted.extend(sorted(p for p in d.glob("*.db") if not p.name.endswith("_fts.db")))
    total = max(1, len(wanted))
    warnings: list[str] = []
    ok_any = False
    for i, db in enumerate(wanted):
        rel = db.relative_to(account.db_storage)
        progress.update("decrypt", f"正在解密 {rel}（{i + 1}/{total}）", 30 + int(50 * i / total))
        res = decrypt_database(db, decrypted / rel, key["passphrase"])
        if res.ok:
            ok_any = True
            # pages past the end of the used page count are pre-allocated and
            # legitimately fail the HMAC check; only surface real corruption
            if res.bad_pages and not _quick_check(decrypted / rel):
                warnings.append(f"{rel} 有 {res.bad_pages} 页未通过校验，可能读取不完整")
    if not ok_any:
        raise RuntimeError("数据库解密失败，请确认微信已登录且版本为 4.x")

    progress.update("index", "正在建立会话索引…", 85)
    reader = AccountReader(decrypted)
    contacts = reader.contacts()
    sessions = reader.sessions()
    tmap = reader.table_map()

    chats: list[dict] = []
    seen: set[str] = set()

    def add(name: str, last_time: int = 0, summary: str = ""):
        if not name or name in seen:
            return
        seen.add(name)
        c = contacts.get(name, {})
        chats.append({
            "username": name,
            "display": c.get("remark") or c.get("nick") or name,
            "alias": c.get("alias", ""),
            "nick": c.get("nick", ""),
            "is_group": name.endswith("@chatroom"),
            "last_time": int(last_time or 0),
            "summary": (summary or "")[:100],
            "has_messages": name in tmap or f"biz:{name}" in tmap,
        })

    for s in sessions:
        add(s["username"], s.get("last_timestamp") or 0, s.get("summary") or "")
    for name in tmap:
        clean = name[4:] if name.startswith("biz:") else name
        add(clean)
    chats.sort(key=lambda x: (x["has_messages"], x["last_time"]), reverse=True)
    progress.update("index", f"共发现 {len(chats)} 个会话", 92)
    return PrepareResult(work_dir=work, decrypted=decrypted, chats=chats,
                         key_source=str(key.get("source")), warnings=warnings)


def prepare_and_export(account, req, progress: Progress, manual_key: str | None = None) -> dict:
    """Full run used by the UI: prepare + export + cleanup."""
    from ..export.runner import ExportRequest, run as run_export

    prep = prepare(account, progress, manual_key=manual_key)
    try:
        result = run_export(ExportRequest(
            decrypted_dir=prep.decrypted,
            account_dir=Path(account.path),
            wxid=account.wxid,
            out_dir=str(req.out_dir or ""),
            chats=list(req.chats or []),
            formats=list(req.formats or ["html"]),
            since=int(req.since or 0),
            until=int(req.until or 0),
            export_media=bool(req.export_media),
            include_system=bool(req.include_system),
            progress=lambda stage, msg, pct: progress.update(stage, msg, 95 + int(5 * pct / 100)),
        ))
        result["key_source"] = prep.key_source
        result["warnings"] = prep.warnings
        progress.update("done", "导出完成", 100)
        return result
    finally:
        if not os.environ.get("WEXPORT_KEEP_WORK"):
            shutil.rmtree(prep.work_dir, ignore_errors=True)


def cleanup(work_dir: str | Path) -> None:
    shutil.rmtree(str(work_dir), ignore_errors=True)
