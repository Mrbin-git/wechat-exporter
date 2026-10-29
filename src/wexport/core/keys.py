"""Key recovery for WeChat 4.x (Windows).

Strategy (in order):
1. cached passphrase (DPAPI-protected file in %LOCALAPPDATA%\\WeChatExporter)
2. memory scan of Weixin.exe: find the 32-byte key buffer referenced by the
   known YaRA signature, XOR with the internal key(s) embedded in Weixin.dll,
   and verify each candidate against page 1 of a real database.
3. manual key supplied by the user (64 hex chars).

The passphrase is a property of the logged-in account and is stable across
restarts; the internal key is a property of the client build.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import hashlib
import hmac
import os
import re
import struct
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from .sqlcipher import PAGE_SIZE, SALT_SIZE, derive_keys, page_hmac, verify_key

APP_NAME = "WeChatExporter"
KEY_SIZE = 32
SIG = b"\x00" * 8 + b"\x20" + b"\x00" * 7 + b"\x2f" + b"\x00" * 7
DLL_PATTERN = re.compile(
    b"^\x48\xBA(.{8})"
    b".{3,8}?"
    b"\x48\xBA(.{8})"
    b".{3,8}?"
    b"\x48\xBA(.{8})"
    b".{3,8}?"
    b"\x48\xBA(.{8})"
    b".{3,8}?"
    b"\x48\x85\xC0",
    re.DOTALL,
)


# --------------------------------------------------------------------------- #
# install / data discovery
# --------------------------------------------------------------------------- #
def find_install() -> dict:
    """Locate Weixin.exe / Weixin.dll from registry hints and common paths."""
    import winreg

    candidates: list[Path] = []
    for root, sub in ((winreg.HKEY_CURRENT_USER, r"Software\Tencent\Weixin"),
                      (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Tencent\Weixin"),
                      (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Tencent\Weixin"),
                      (winreg.HKEY_CURRENT_USER, r"Software\Tencent\WeChat"),
                      (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Tencent\WeChat")):
        try:
            with winreg.OpenKey(root, sub) as key:
                for name in ("InstallPath", "InstallDir", "FileSavePath"):
                    try:
                        val, _ = winreg.QueryValueEx(key, name)
                        if isinstance(val, str) and val.strip():
                            candidates.append(Path(val.strip()))
                    except OSError:
                        pass
        except OSError:
            pass

    for drive in "CDEFGH":
        candidates.append(Path(f"{drive}:\\Program Files\\Tencent\\Weixin"))
        candidates.append(Path(f"{drive}:\\Program Files (x86)\\Tencent\\Weixin"))
        candidates.append(Path(f"{drive}:\\Program Files\\Tencent\\WeChat"))
        candidates.append(Path(f"{drive}:\\Program Files (x86)\\Tencent\\WeChat"))

    result = {"install_dir": "", "exe": "", "dll": "", "version": ""}
    for cand in candidates:
        try:
            if cand.is_file():
                exe = cand
                base = cand.parent
            else:
                exe = None
                base = cand
                for name in ("Weixin.exe", "WeChat.exe"):
                    if (base / name).is_file():
                        exe = base / name
                        break
            if exe is None:
                continue
            result["exe"] = str(exe)
            result["install_dir"] = str(base)
            # version subdirectory holds the payload dll
            dlls = sorted(base.glob("*/Weixin.dll"), key=lambda p: p.stat().st_mtime, reverse=True)
            if not dlls:
                dlls = sorted(base.glob("**/Weixin.dll"), key=lambda p: p.stat().st_mtime, reverse=True)
            if dlls:
                result["dll"] = str(dlls[0])
                result["version"] = dlls[0].parent.name
            return result
        except OSError:
            continue
    return result


def extract_internal_keys(dll_path: str | Path) -> list[bytes]:
    """Scan Weixin.dll code sections for the 4x mov rdx signature."""
    path = Path(dll_path)
    if not path.is_file():
        return []
    data = path.read_bytes()
    keys: list[bytes] = []
    pos = 0
    while True:
        idx = data.find(b"\x48\xBA", pos)
        if idx == -1:
            break
        m = DLL_PATTERN.match(data[idx: idx + 85])
        if m:
            key = m.group(1) + m.group(2) + m.group(3) + m.group(4)
            if key not in keys:
                keys.append(key)
            pos = idx + len(m.group(0))
        else:
            pos = idx + 1
    return keys


# --------------------------------------------------------------------------- #
# memory scan
# --------------------------------------------------------------------------- #
class _MBI(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wt.DWORD),
        ("__p1", wt.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wt.DWORD),
        ("Protect", wt.DWORD),
        ("Type", wt.DWORD),
        ("__p2", wt.DWORD),
    ]


class _PE32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD), ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
        ("szExeFile", ctypes.c_char * 260),
    ]


def list_processes(names: tuple[str, ...] = ("weixin.exe", "wechat.exe")) -> list[int]:
    """Enumerate process ids by image name.

    Uses EnumProcesses + QueryFullProcessImageNameW instead of a Toolhelp
    snapshot: the latter proved unreliable inside the frozen (PyInstaller)
    build, returning a truncated process list.
    """
    if os.name != "nt":
        return []
    wanted = {n.lower() for n in names}
    pids: list[int] = []
    for pid, exe in _iter_processes():
        if exe in wanted and pid not in pids:
            pids.append(pid)
    if not pids:
        pid = pid_from_window()
        if pid:
            pids.append(pid)
    return pids


def _iter_processes():
    """Yield (pid, lowercase image name) for every process we can inspect."""
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    arr = (wt.DWORD * 8192)()
    needed = wt.DWORD(0)
    if not psapi.EnumProcesses(arr, ctypes.sizeof(arr), ctypes.byref(needed)):
        return
    count = int(needed.value) // ctypes.sizeof(wt.DWORD)
    for pid in arr[:count]:
        if not pid:
            continue
        handle = k32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            continue
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wt.DWORD(len(buf))
            if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                yield int(pid), buf.value.rsplit("\\", 1)[-1].lower()
        finally:
            k32.CloseHandle(handle)


def pid_from_window(titles: tuple[str, ...] = ("微信", "WeChat", "Weixin")) -> int:
    """Find the WeChat main window's process id (works even when process
    enumeration is restricted)."""
    if os.name != "nt":
        return 0
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def _cb(hwnd, _lparam):
        length = user32.GetWindowTextLengthW(hwnd)
        if length:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            text = buf.value.strip()
            if text in titles or any(text.startswith(t) for t in titles):
                pid = wt.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value:
                    found.append(int(pid.value))
                    return False
        return True

    user32.EnumWindows(_cb, 0)
    return found[0] if found else 0


def list_processes_debug() -> dict:
    """Diagnostics for the process enumeration path (used by --diag)."""
    if os.name != "nt":
        return {"os": os.name}
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    k32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
    snap = k32.CreateToolhelp32Snapshot(2, 0)
    err = ctypes.get_last_error()
    pe = _PE32()
    pe.dwSize = ctypes.sizeof(_PE32)
    first = False
    count = 0
    weixin: list[int] = []
    if snap and int(snap) != -1 and int(snap) != -1 % (1 << 64):
        first = bool(k32.Process32First(snap, ctypes.byref(pe)))
        while first:
            count += 1
            name = pe.szExeFile.decode(errors="ignore").strip("\x00").lower()
            if "weixin" in name or "wechat" in name:
                weixin.append(int(pe.th32ProcessID))
            first = bool(k32.Process32Next(snap, ctypes.byref(pe)))
        k32.CloseHandle(ctypes.c_void_p(snap))
    return {"snapshot": snap, "error": err, "count": count, "weixin": weixin,
            "pe_size": ctypes.sizeof(_PE32), "is64": ctypes.sizeof(ctypes.c_void_p) == 8}


def _memory_regions(handle) -> list[tuple[int, int]]:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    out: list[tuple[int, int]] = []
    addr = 0
    while True:
        mbi = _MBI()
        if not k32.VirtualQueryEx(handle, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
            break
        size = int(mbi.RegionSize or 0)
        base = int(mbi.BaseAddress) if mbi.BaseAddress else addr
        if size <= 0:
            break
        if mbi.State == 0x1000 and mbi.Type == 0x20000:
            out.append((base, size))
        addr = base + size
        if addr <= 0 or addr > 0x7FFFFFFF0000:
            break
    return out


def _open_process(pid: int) -> tuple[int, str]:
    """Open a process for memory reads, trying the usual access masks."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    for access in (0x10 | 0x400, 0x10 | 0x1000, 0x10):
        handle = k32.OpenProcess(access, False, int(pid))
        if handle:
            return int(handle), ""
    return 0, f"OpenProcess 失败，错误码 {ctypes.get_last_error()}"


def collect_raw_candidates(pid: int, limit: int = 4000) -> list[bytes]:
    """Return the 32-byte buffers referenced by the DB-key signature."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle, error = _open_process(pid)
    if not handle:
        last_error[0] = error
        return []
    found: list[bytes] = []
    seen: set[bytes] = set()
    try:
        for base, size in _memory_regions(handle):
            if size > (256 << 20):
                continue
            buf = ctypes.create_string_buffer(size)
            read = ctypes.c_size_t(0)
            if not k32.ReadProcessMemory(handle, ctypes.c_void_p(base), buf, size, ctypes.byref(read)):
                continue
            data = buf.raw[: read.value]
            pos = data.find(SIG)
            while pos != -1:
                if pos >= 8:
                    ptr = struct.unpack_from("<Q", data, pos - 8)[0]
                    if ptr:
                        kb = ctypes.create_string_buffer(KEY_SIZE)
                        rd = ctypes.c_size_t(0)
                        ok = k32.ReadProcessMemory(handle, ctypes.c_void_p(ptr), kb, KEY_SIZE, ctypes.byref(rd))
                        if ok and rd.value == KEY_SIZE:
                            cand = kb.raw
                            if cand not in seen:
                                seen.add(cand)
                                found.append(cand)
                                if len(found) >= limit:
                                    return found
                pos = data.find(SIG, pos + 1)
    finally:
        k32.CloseHandle(handle)
    return found


last_error: list[str] = [""]


# worker globals (set via initializer so page1 is shipped once per process)
_W_PROBE = b""
_W_INTERNAL = ()


def _init_worker(page1: bytes, internal_keys: tuple[bytes, ...]) -> None:
    global _W_PROBE, _W_INTERNAL
    _W_PROBE = page1
    _W_INTERNAL = internal_keys


def _variant_hit(candidate: bytes) -> bytes | None:
    """Return the verified passphrase for one raw candidate, if any."""
    global _W_PROBE, _W_INTERNAL
    page1 = _W_PROBE
    salt = page1[:SALT_SIZE]
    stored = page1[PAGE_SIZE - 64:]
    tried: list[bytes] = []
    if _W_INTERNAL:
        for ik in _W_INTERNAL:
            tried.append(bytes(a ^ b for a, b in zip(candidate, ik)))
    else:
        tried.append(candidate)
    # raw candidate itself is also a valid passphrase in some builds
    if candidate not in tried:
        tried.append(candidate)
    for phrase in tried:
        enc = hashlib.pbkdf2_hmac("sha512", phrase, salt, 256000, dklen=KEY_SIZE)
        mac = hashlib.pbkdf2_hmac("sha512", enc, bytes(b ^ 0x3A for b in salt), 2, dklen=KEY_SIZE)
        if hmac.compare_digest(page_hmac(mac, page1, 1), stored):
            return phrase
    return None


def _verify_passphrase(args: tuple[bytes, bytes]) -> bytes | None:
    phrase, page1 = args
    return phrase if verify_key(phrase, page1) else None


def recover_from_memory(
    probe_db: Path,
    internal_keys: list[bytes],
    *,
    workers: int | None = None,
    on_progress=None,
) -> dict:
    """Scan all Weixin.exe processes and verify candidates until one matches."""
    page1 = Path(probe_db).read_bytes()[:PAGE_SIZE]
    pids = list_processes()
    if not pids:
        return {"ok": False, "error": "未检测到正在运行的微信进程，请先登录微信后重试", "candidates": 0}

    candidates: list[bytes] = []
    seen: set[bytes] = set()
    errors: list[str] = []
    for pid in pids:
        last_error[0] = ""
        got = collect_raw_candidates(pid)
        if not got and last_error[0]:
            errors.append(f"PID {pid}: {last_error[0]}")
        for cand in got:
            if cand not in seen:
                seen.add(cand)
                candidates.append(cand)
        if on_progress:
            on_progress("scan", f"已从进程 {pid} 收集 {len(candidates)} 个候选密钥")
    if not candidates:
        detail = "；".join(errors[:2])
        msg = "未能在微信进程内存中找到密钥特征，请确认微信为 4.x 且已登录"
        if detail:
            msg += f"（{detail}）"
        return {"ok": False, "error": msg, "candidates": 0}

    total = len(candidates)
    if on_progress:
        on_progress("verify", f"正在校验 {total} 个候选密钥…")
    worker_count = workers or max(2, min(8, (os.cpu_count() or 4)))
    keys = tuple(internal_keys)
    done = 0
    try:
        with ProcessPoolExecutor(max_workers=worker_count, initializer=_init_worker,
                                 initargs=(page1, keys)) as pool:
            for phrase in pool.map(_variant_hit, candidates, chunksize=4):
                done += 1
                if on_progress and done % 25 == 0:
                    on_progress("verify", f"已校验 {done}/{total}")
                if phrase:
                    pool.shutdown(wait=False, cancel_futures=True)
                    return {"ok": True, "passphrase": phrase, "candidates": total}
    except Exception as exc:  # pragma: no cover - defensive
        return {"ok": False, "error": f"内存扫描失败: {exc}", "candidates": total}
    return {"ok": False, "error": "候选密钥均未通过校验（客户端版本可能已变更）", "candidates": total}


# --------------------------------------------------------------------------- #
# cache (DPAPI)
# --------------------------------------------------------------------------- #
def _cache_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / APP_NAME / "keys.dat"


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, protect: bool) -> bytes | None:
    if os.name != "nt":
        return None
    try:
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        blob_in = _DATA_BLOB(len(data), ctypes.cast(ctypes.create_string_buffer(data, len(data)), ctypes.POINTER(ctypes.c_char)))
        blob_out = _DATA_BLOB()
        fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
        if protect:
            ok = fn(ctypes.byref(blob_in), None, None, None, None, 0x1, ctypes.byref(blob_out))
        else:
            ok = fn(ctypes.byref(blob_in), None, None, None, None, 0x1, ctypes.byref(blob_out))
        if not ok:
            return None
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            kernel32.LocalFree(blob_out.pbData)
    except Exception:
        return None


def load_cached_keys() -> dict[str, str]:
    path = _cache_path()
    if not path.is_file():
        return {}
    try:
        raw = _dpapi(path.read_bytes(), protect=False)
        if not raw:
            return {}
        import json
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_cached_key(account: str, key_hex: str) -> bool:
    data = load_cached_keys()
    data[account] = key_hex
    blob = _dpapi(__import__("json").dumps(data).encode("utf-8"), protect=True)
    if not blob:
        return False
    try:
        path = _cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blob)
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# high level
# --------------------------------------------------------------------------- #
def find_probe_db(db_storage: Path) -> Path | None:
    preferred = ["message\\message_0.db", "message\\message_1.db", "session\\session.db", "contact\\contact.db"]
    for rel in preferred:
        p = db_storage / Path(rel)
        if p.is_file() and p.stat().st_size >= PAGE_SIZE:
            return p
    for p in sorted(db_storage.rglob("*.db")):
        if p.is_file() and p.stat().st_size >= PAGE_SIZE:
            return p
    return None


def get_passphrase(
    account: str,
    db_storage: Path,
    *,
    install: dict | None = None,
    manual_key: str | None = None,
    use_cache: bool = True,
    on_progress=None,
) -> dict:
    """Resolve the SQLCipher passphrase for one account."""
    probe = find_probe_db(Path(db_storage))
    if probe is None:
        return {"ok": False, "error": "账号目录中未找到可用数据库"}

    if manual_key:
        try:
            phrase = bytes.fromhex(manual_key.strip())
        except ValueError:
            return {"ok": False, "error": "手动密钥格式错误（需要 64 位十六进制）"}
        if len(phrase) != KEY_SIZE:
            return {"ok": False, "error": "手动密钥长度错误（需要 32 字节）"}
        page1 = probe.read_bytes()[:PAGE_SIZE]
        if verify_key(phrase, page1):
            return {"ok": True, "passphrase": phrase, "source": "manual"}
        return {"ok": False, "error": "手动密钥与该账号数据库不匹配"}

    if use_cache:
        cached = load_cached_keys().get(account)
        if cached:
            try:
                phrase = bytes.fromhex(cached)
                page1 = probe.read_bytes()[:PAGE_SIZE]
                if verify_key(phrase, page1):
                    return {"ok": True, "passphrase": phrase, "source": "cache"}
            except (ValueError, OSError):
                pass

    info = install or find_install()
    internal = extract_internal_keys(info.get("dll", "")) if info.get("dll") else []
    if not internal:
        return {"ok": False, "error": "未找到微信客户端 DLL，无法完成密钥恢复"}
    result = recover_from_memory(probe, internal, on_progress=on_progress)
    if result.get("ok"):
        phrase = result["passphrase"]
        if use_cache:
            save_cached_key(account, phrase.hex())
        return {"ok": True, "passphrase": phrase, "source": "memory", "candidates": result.get("candidates")}
    return {"ok": False, "error": result.get("error", "密钥恢复失败")}


if __name__ == "__main__":
    import json
    import time

    info = find_install()
    print("install:", json.dumps(info, ensure_ascii=False))
    t0 = time.time()
    ikeys = extract_internal_keys(info.get("dll", ""))
    print("internal keys:", [k.hex() for k in ikeys], f"({time.time()-t0:.1f}s)")
    sys.exit(0)
