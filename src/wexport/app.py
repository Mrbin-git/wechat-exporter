"""WeChat chat exporter launcher: starts a local web UI and opens the browser.

Usage:
    WeChatExport.exe                # normal run (double click)
    WeChatExport.exe --console      # keep a console window for diagnostics
    WeChatExport.exe --port 8765    # fixed port
    WeChatExport.exe --scan         # print environment/account detection as JSON
    WeChatExport.exe --export --account <folder> --out <dir> --formats html,csv
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path


def _bootstrap_paths() -> None:
    """Make src/ and vendor/ importable both in source and frozen builds."""
    here = Path(getattr(sys, "_MEIPASS", "")) if getattr(sys, "_MEIPASS", None) else None
    pkg_dir = Path(__file__).resolve().parent          # .../wexport
    src_root = pkg_dir.parent                           # .../src
    repo_root = src_root.parent                         # repo root
    candidates = [src_root, repo_root]
    if here:
        candidates.extend([here, here / "src"])
    candidates.append(repo_root / "vendor")
    for cand in candidates:
        sp = str(cand)
        if cand.is_dir() and sp not in sys.path:
            sys.path.insert(0, sp)


_bootstrap_paths()

from wexport.core import discover, pipeline  # noqa: E402


def cmd_scan() -> int:
    info = discover.scan()
    accounts = []
    for a in info["accounts"]:
        accounts.append({
            "wxid": a.wxid, "folder": a.folder, "path": str(a.path),
            "db_storage": str(a.db_storage),
            "message_dbs": len(a.message_dbs),
        })
    print(json.dumps({
        "version": info["version"], "install_dir": info["install_dir"],
        "running": info["running"], "roots": info["roots"],
        "accounts": accounts, "warnings": info["warnings"],
    }, ensure_ascii=False, indent=2))
    return 0 if accounts else 1


def cmd_diag() -> int:
    from wexport.core import keys
    info = discover.find_install()
    pids = keys.list_processes()
    internal = keys.extract_internal_keys(info.get("dll", "")) if info.get("dll") else []
    cand = 0
    for pid in pids:
        try:
            cand += len(keys.collect_raw_candidates(pid, limit=50))
        except Exception:
            pass
    try:
        proc_list = list(keys._iter_processes())
    except Exception as exc:
        proc_list = [("error", str(exc))]
    session = 0
    try:
        sid = __import__("ctypes").wintypes.DWORD()
        __import__("ctypes").windll.kernel32.ProcessIdToSessionId(os.getpid(), __import__("ctypes").byref(sid))
        session = sid.value
    except Exception:
        pass
    print(json.dumps({
        "exe": sys.executable,
        "frozen": bool(getattr(sys, "frozen", False)),
        "pid": os.getpid(),
        "session": session,
        "install": info,
        "pids": pids,
        "enum_total": len(proc_list),
        "enum_sample": proc_list[:12],
        "window_pid": keys.pid_from_window(),
        "internal_keys": len(internal),
        "candidates": cand,
        "python": sys.version,
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_dump_key(args) -> int:
    """Recover and print the database key for one account (manual fallback)."""
    from wexport.core.keys import get_passphrase

    info = discover.scan()
    target = None
    for a in info["accounts"]:
        if a.folder == args.account or a.wxid == args.account:
            target = a
            break
    if target is None and info["accounts"]:
        target = info["accounts"][0]
    if target is None:
        print(json.dumps({"ok": False, "error": "未找到微信账号目录"}, ensure_ascii=False))
        return 2
    result = get_passphrase(target.wxid, target.db_storage,
                            manual_key=(args.key or None),
                            on_progress=lambda stage, msg: print(f"[{stage}] {msg}", file=sys.stderr))
    if not result.get("ok"):
        print(json.dumps({"ok": False, "account": target.folder,
                          "error": result.get("error")}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps({
        "ok": True,
        "account": target.folder,
        "source": result.get("source"),
        "key": result["passphrase"].hex(),
        "usage": "把 key 粘贴到导出界面的“密钥”输入框，或命令行传 --key",
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_export(args) -> int:
    info = discover.scan()
    target = None
    for a in info["accounts"]:
        if a.folder == args.account or a.wxid == args.account:
            target = a
            break
    if target is None and info["accounts"]:
        target = info["accounts"][0]
    if target is None:
        print("未找到微信账号数据目录", file=sys.stderr)
        return 2
    progress = pipeline.Progress()

    class Req:
        out_dir = args.out
        chats = [c for c in (args.chats or "").split(",") if c.strip()]
        formats = [f.strip() for f in (args.formats or "html").split(",") if f.strip()]
        since = int(args.since or 0)
        until = int(args.until or 0)
        export_media = not args.no_media
        include_system = not args.no_system

    result = pipeline.prepare_and_export(target, Req(), progress, manual_key=(args.key or None))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="WeChatExport", add_help=True)
    parser.add_argument("--scan", action="store_true", help="扫描环境并输出 JSON")
    parser.add_argument("--diag", action="store_true", help="诊断密钥获取链路")
    parser.add_argument("--dump-key", action="store_true", help="获取并打印本机账号的数据库密钥（手动兜底）")
    parser.add_argument("--export", action="store_true", help="命令行导出")
    parser.add_argument("--key", default="", help="手动指定 64 位十六进制数据库密钥（可选）")
    parser.add_argument("--account", default="", help="账号目录名（如 wxid_xxx_abcd）")
    parser.add_argument("--out", default="", help="输出目录")
    parser.add_argument("--formats", default="html", help="html,csv,json,txt,xlsx")
    parser.add_argument("--chats", default="", help="逗号分隔的会话 username，缺省为全部")
    parser.add_argument("--since", type=int, default=0)
    parser.add_argument("--until", type=int, default=0)
    parser.add_argument("--no-media", action="store_true")
    parser.add_argument("--no-system", action="store_true")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)

    if args.scan:
        return cmd_scan()
    if args.diag:
        return cmd_diag()
    if args.dump_key:
        return cmd_dump_key(args)
    if args.export:
        return cmd_export(args)

    from wexport.web.server import serve
    return serve(port=args.port, open_browser=not args.no_browser)


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    sys.exit(main())
