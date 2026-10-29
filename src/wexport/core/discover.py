"""Locate WeChat 4.x install, data root, accounts and running state."""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .keys import find_install, list_processes


@dataclass
class Account:
    wxid: str
    folder: str
    path: Path
    db_storage: Path
    display_name: str = ""
    alias: str = ""
    last_active: float = 0.0
    message_dbs: list[Path] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{self.display_name or self.wxid}"


def _read_ini_root(ini: Path) -> Path | None:
    try:
        raw = ini.read_bytes()
    except OSError:
        return None
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            text = raw.decode(enc).strip()
        except UnicodeDecodeError:
            continue
        if text and re.match(r"^[A-Za-z]:[\\/]", text):
            return Path(text)
    return None


def find_data_roots() -> list[Path]:
    """Return candidate directories that contain (or are) xwechat_files."""
    roots: list[Path] = []
    ini_dirs = [
        Path(os.environ.get("APPDATA", "")) / "Tencent" / "xwechat" / "config",
        Path(os.environ.get("APPDATA", "")) / "Tencent" / "WeChat" / "config",
    ]
    for d in ini_dirs:
        if not d.is_dir():
            continue
        for ini in sorted(d.glob("*.ini")):
            base = _read_ini_root(ini)
            if base:
                for cand in (base / "xwechat_files", base / "WeChat Files", base):
                    if cand.is_dir():
                        roots.append(cand)
    # common locations
    home = Path.home()
    for cand in (
        home / "Documents" / "xwechat_files",
        home / "Documents" / "WeChat Files",
        home / "xwechat_files",
    ):
        if cand.is_dir():
            roots.append(cand)
    # shallow scan of drive roots (data is often moved to D:/E:/F:)
    for drive in "CDEFGH":
        base = Path(f"{drive}:\\")
        if not base.exists():
            continue
        try:
            for child in base.iterdir():
                if not child.is_dir():
                    continue
                if child.name.lower() in {"xwechat_files", "wechat files"}:
                    roots.append(child)
                elif child.name in {"微信文件", "WeChat Files", "Weixin"}:
                    for sub in child.glob("*"):
                        if sub.is_dir() and sub.name.lower() in {"xwechat_files", "wechat files"}:
                            roots.append(sub)
        except OSError:
            continue
    # de-duplicate preserving order
    out: list[Path] = []
    seen: set[str] = set()
    for r in roots:
        key = str(r).lower()
        if key not in seen and r.is_dir():
            seen.add(key)
            out.append(r)
    return out


def list_accounts(root: Path) -> list[Account]:
    accounts: list[Account] = []
    try:
        children = list(root.iterdir())
    except OSError:
        return accounts
    for child in children:
        if not child.is_dir():
            continue
        db_storage = child / "db_storage"
        if not db_storage.is_dir():
            continue
        wxid = child.name
        base = re.sub(r"_[0-9a-f]{4}$", "", wxid, flags=re.I)
        msg_dbs: list[Path] = []
        mdir = db_storage / "message"
        if mdir.is_dir():
            msg_dbs = sorted(mdir.glob("message_*.db"))
        last = 0.0
        try:
            last = max((p.stat().st_mtime for p in db_storage.rglob("*.db")), default=0.0)
        except OSError:
            pass
        accounts.append(Account(wxid=base, folder=wxid, path=child, db_storage=db_storage,
                                last_active=last, message_dbs=msg_dbs))
    accounts.sort(key=lambda a: a.last_active, reverse=True)
    return accounts


def is_wechat_running() -> bool:
    return bool(list_processes())


def integrity_level() -> str:
    """Return the process integrity level (low/medium/high/system/unknown).

    A copy of the tool extracted into a folder carrying a Low mandatory label
    runs at low integrity, which silently blocks writes outside that folder and
    prevents reading WeChat's process memory.
    """
    if os.name != "nt":
        return "unknown"
    try:
        import ctypes
        import ctypes.wintypes as wt

        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wt.HANDLE
        advapi.OpenProcessToken.argtypes = [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)]
        advapi.OpenProcessToken.restype = wt.BOOL
        advapi.GetTokenInformation.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                               wt.DWORD, ctypes.POINTER(wt.DWORD)]
        advapi.GetTokenInformation.restype = wt.BOOL
        advapi.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
        advapi.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
        advapi.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wt.DWORD]
        advapi.GetSidSubAuthority.restype = ctypes.POINTER(wt.DWORD)
        kernel.CloseHandle.argtypes = [wt.HANDLE]
        token = wt.HANDLE()
        if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            return "unknown"
        try:
            size = wt.DWORD(0)
            advapi.GetTokenInformation(token, 25, None, 0, ctypes.byref(size))  # TokenIntegrityLevel
            if not size.value:
                return "unknown"
            buf = ctypes.create_string_buffer(size.value)
            if not advapi.GetTokenInformation(token, 25, buf, size, ctypes.byref(size)):
                return "unknown"
            # TOKEN_MANDATORY_LABEL: SID_AND_ATTRIBUTES { PSID Sid; DWORD Attributes; }
            sid_ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
            advapi.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
            count_ptr = advapi.GetSidSubAuthorityCount(sid_ptr)
            rid_ptr = advapi.GetSidSubAuthority(sid_ptr, count_ptr.contents.value - 1)
            rid = int(rid_ptr.contents.value)
            if rid >= 0x4000:
                return "system"
            if rid >= 0x3000:
                return "high"
            if rid >= 0x2000:
                return "medium"
            return "low"
        finally:
            kernel.CloseHandle(token)
    except Exception:
        return "unknown"


def scan() -> dict:
    """Full environment scan used by CLI and the web UI."""
    t0 = time.time()
    install = find_install()
    roots = find_data_roots()
    accounts: list[Account] = []
    for root in roots:
        for acc in list_accounts(root):
            if not any(a.folder == acc.folder and a.path == acc.path for a in accounts):
                accounts.append(acc)
    accounts.sort(key=lambda a: a.last_active, reverse=True)
    return {
        "version": install.get("version", ""),
        "install_dir": install.get("install_dir", ""),
        "dll": install.get("dll", ""),
        "running": is_wechat_running(),
        "roots": [str(r) for r in roots],
        "accounts": accounts,
        "elapsed": round(time.time() - t0, 2),
        "integrity": integrity_level(),
        "warnings": [] if install.get("dll") else ["未定位到 Weixin.dll，密钥自动恢复可能不可用"],
        "low_integrity": integrity_level() in ("low", "untrusted"),
    }
