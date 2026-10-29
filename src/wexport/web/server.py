"""Local HTTP server + Web UI for the exporter (stdlib only)."""
from __future__ import annotations

import json
import mimetypes
import os
import socket
import sys
import threading
import time
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from ..core import discover, pipeline

WEB_DIR = Path(__file__).resolve().parent
UI_DIR = WEB_DIR.parent / "web"

_state_lock = threading.RLock()
_job = {"progress": pipeline.Progress(), "running": False, "account": "", "result": None}
_prepared = {"folder": "", "prep": None, "progress": None}
_pick_lock = threading.Lock()


def _json(handler: BaseHTTPRequestHandler, obj, status: int = 200) -> None:
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _file(handler: BaseHTTPRequestHandler, path: Path) -> None:
    if not path.is_file():
        handler.send_error(HTTPStatus.NOT_FOUND)
        return
    ctype, _ = mimetypes.guess_type(str(path))
    data = path.read_bytes()
    if ctype and ctype.startswith("text/"):
        ctype += "; charset=utf-8"
    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", ctype or "application/octet-stream")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


class Handler(BaseHTTPRequestHandler):
    server_version = "WeChatExport/1.0"

    def log_message(self, fmt, *args):  # keep the console quiet
        if os.environ.get("WEXPORT_DEBUG"):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # -- routing ---------------------------------------------------------
    def do_GET(self):
        route = urlparse(self.path).path
        try:
            if route in ("/", "/index.html"):
                return _file(self, UI_DIR / "ui.html")
            if route == "/app.js":
                return _file(self, UI_DIR / "app.js")
            if route == "/style.css":
                return _file(self, UI_DIR / "style.css")
            if route == "/api/scan":
                info = discover.scan()
                return _json(self, {
                    "version": info["version"],
                    "install_dir": info["install_dir"],
                    "running": info["running"],
                    "roots": info["roots"],
                    "warnings": info["warnings"],
                    "integrity": info.get("integrity", "unknown"),
                    "low_integrity": info.get("low_integrity", False),
                    "accounts": [{
                        "wxid": a.wxid, "folder": a.folder, "path": str(a.path),
                        "db_storage": str(a.db_storage), "message_dbs": len(a.message_dbs),
                    } for a in info["accounts"]],
                })
            if route == "/api/progress":
                snap = _job["progress"].snapshot()
                return _json(self, {"running": _job["running"], **snap})
            if route == "/api/result":
                return _json(self, {"result": _job.get("result")})
            if route == "/api/health":
                return _json(self, {"ok": True})
        except BrokenPipeError:
            return
        except Exception as exc:  # pragma: no cover - defensive
            return _json(self, {"error": str(exc), "trace": traceback.format_exc()[-1500:]}, 500)
        return _json(self, {"error": "not found"}, 404)

    def do_POST(self):
        route = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            payload = {}
        try:
            if route == "/api/start":
                return self._start(payload)
            if route == "/api/chats":
                return self._chats(payload)
            if route == "/api/open-folder":
                return self._open_folder(payload)
            if route == "/api/pick-folder":
                return self._pick_folder()
            if route == "/api/quit":
                threading.Thread(target=lambda: (time.sleep(0.3), os._exit(0)), daemon=True).start()
                return _json(self, {"ok": True})
        except Exception as exc:  # pragma: no cover
            return _json(self, {"error": str(exc), "trace": traceback.format_exc()[-1500:]}, 500)
        return _json(self, {"error": "not found"}, 404)

    # -- actions ---------------------------------------------------------
    def _open_folder(self, payload: dict) -> None:
        raw = str(payload.get("path") or "").strip()
        target = Path(raw)
        if not raw or not target.is_dir():
            return _json(self, {"error": f"目录不存在：{raw or '(空)'}"}, 400)
        try:
            os.startfile(str(target))  # type: ignore[attr-defined]
        except OSError as exc:
            try:
                import subprocess
                subprocess.Popen(["explorer.exe", str(target)])
            except OSError:
                return _json(self, {"error": f"无法打开目录：{exc}"}, 500)
        # Explorer often opens behind the browser; pull it to the foreground.
        threading.Timer(0.9, lambda: _bring_to_front(target.name)).start()
        return _json(self, {"ok": True})

    def _pick_folder(self) -> None:
        """Show the native Windows folder picker and return the chosen path."""
        if not _pick_lock.acquire(blocking=False):
            return _json(self, {"error": "目录选择窗口已经打开，请先完成选择"}, 409)
        tmp = ""
        try:
            import subprocess
            import tempfile
            fd, tmp = tempfile.mkstemp(suffix=".txt", prefix="wxdir-")
            os.close(fd)
            # A TopMost owner form keeps the dialog above the browser window.
            script = (
                "Add-Type -AssemblyName System.Windows.Forms;"
                "$o=New-Object System.Windows.Forms.Form;"
                "$o.TopMost=$true;$o.ShowInTaskbar=$false;"
                "$o.WindowState=[System.Windows.Forms.FormWindowState]::Minimized;"
                "$o.Show();"
                "$d=New-Object System.Windows.Forms.FolderBrowserDialog;"
                "$d.Description='选择微信聊天记录导出目录';"
                "$d.ShowNewFolderButton=$true;"
                "if($d.ShowDialog($o) -eq [System.Windows.Forms.DialogResult]::OK){"
                f"[IO.File]::WriteAllText('{tmp}', $d.SelectedPath, [Text.Encoding]::UTF8)"
                "};"
                "$o.Close()"
            )
            subprocess.run(
                ["powershell", "-NoProfile", "-STA", "-WindowStyle", "Hidden", "-Command", script],
                timeout=300,
                creationflags=0x08000000,  # CREATE_NO_WINDOW
            )
            chosen = ""
            if Path(tmp).is_file():
                chosen = Path(tmp).read_text(encoding="utf-8-sig").strip()
            if not chosen:
                return _json(self, {"ok": False, "cancelled": True})
            return _json(self, {"ok": True, "path": chosen})
        except subprocess.TimeoutExpired:
            return _json(self, {"error": "目录选择超时（5 分钟）"}, 504)
        except Exception as exc:
            return _json(self, {"error": f"无法打开目录选择窗口：{exc}"}, 500)
        finally:
            if tmp:
                try:
                    Path(tmp).unlink(missing_ok=True)
                except OSError:
                    pass
            _pick_lock.release()

    def _resolve_account(self, folder: str):
        info = discover.scan()
        for a in info["accounts"]:
            if a.folder == folder or a.wxid == folder:
                return a
        return info["accounts"][0] if info["accounts"] else None

    def _chats(self, payload: dict) -> None:
        target = self._resolve_account(str(payload.get("account") or ""))
        if target is None:
            return _json(self, {"error": "未找到微信账号目录"}, 400)

        with _state_lock:
            cached = _prepared["prep"]
            if cached is not None and _prepared["folder"] == target.folder:
                return _json(self, {"ok": True, "chats": cached.chats, "cached": True})
            if _job["running"]:
                return _json(self, {"error": "正在处理上一个请求，请稍候"}, 409)
            progress = pipeline.Progress()
            _job["progress"] = progress
            _job["running"] = True
            _job["result"] = None

        # The lock must NOT be held while preparing (it can take minutes and the
        # progress endpoint/other requests must stay responsive).
        try:
            prep = pipeline.prepare(target, progress)
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            progress.finish(error=detail)
            return _json(self, {"error": detail}, 500)
        finally:
            with _state_lock:
                _job["running"] = False

        with _state_lock:
            _prepared.update({"folder": target.folder, "prep": prep})
        progress.finish({"chats": prep.chats})
        return _json(self, {"ok": True, "chats": prep.chats, "cached": False,
                            "warnings": prep.warnings, "key_source": prep.key_source})

    def _start(self, payload: dict) -> None:
        with _state_lock:
            if _job["running"]:
                return _json(self, {"error": "已有任务在运行"}, 409)
            target = self._resolve_account(str(payload.get("account") or ""))
            if target is None:
                return _json(self, {"error": "未找到微信账号目录"}, 400)

            out_dir = str(payload.get("out_dir") or "").strip()
            req = _Request(
                out_dir=out_dir,
                chats=[c for c in (payload.get("chats") or []) if c],
                formats=[f for f in (payload.get("formats") or ["html"]) if f] or ["html"],
                since=int(payload.get("since") or 0),
                until=int(payload.get("until") or 0),
                export_media=bool(payload.get("export_media", True)),
                include_system=bool(payload.get("include_system", True)),
            )
            progress = pipeline.Progress()
            _job["progress"] = progress
            _job["running"] = True
            _job["account"] = target.folder
            _job["result"] = None
            cached = _prepared["prep"] if _prepared["folder"] == target.folder else None
            manual_key = (str(payload.get("key") or "").strip() or None)

            def worker():
                try:
                    from ..export.runner import ExportRequest, run as run_export
                    prep = cached or pipeline.prepare(target, progress, manual_key=manual_key)
                    if cached:
                        progress.update("export", "使用已解密的会话，开始导出…", 60)
                    try:
                        result = run_export(ExportRequest(
                            decrypted_dir=prep.decrypted,
                            account_dir=Path(target.path),
                            wxid=target.wxid,
                            out_dir=str(req.out_dir or ""),
                            chats=list(req.chats or []),
                            formats=list(req.formats or ["html"]),
                            since=int(req.since or 0),
                            until=int(req.until or 0),
                            export_media=bool(req.export_media),
                            include_system=bool(req.include_system),
                            progress=lambda stage, msg, pct: progress.update(stage, msg, 90 + int(10 * pct / 100)),
                        ))
                    finally:
                        pipeline.cleanup(prep.work_dir)
                    result["key_source"] = prep.key_source
                    result["warnings"] = prep.warnings
                    with _state_lock:
                        _job["result"] = result
                        _prepared["prep"] = None
                        _prepared["folder"] = ""
                    progress.finish(result)
                except Exception as exc:
                    detail = str(exc) if isinstance(exc, RuntimeError) else f"{type(exc).__name__}: {exc}"
                    if os.environ.get("WEXPORT_DEBUG"):
                        detail += "\n" + traceback.format_exc()
                    # the prepared temp copy is gone once the export attempt ends
                    with _state_lock:
                        _prepared["prep"] = None
                        _prepared["folder"] = ""
                    progress.finish(error=detail)
                finally:
                    with _state_lock:
                        _job["running"] = False

            threading.Thread(target=worker, daemon=True).start()
            return _json(self, {"ok": True, "out_dir": out_dir, "account": target.folder})


class _Request:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _bring_to_front(title_part: str) -> None:
    """Best-effort: bring the Explorer window for ``title_part`` to the front.

    Windows only lets the foreground process change activation, so we attach to
    the current foreground thread before calling SetForegroundWindow.
    """
    if os.name != "nt" or not title_part:
        return
    try:
        import ctypes
        import ctypes.wintypes as wt

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        target = title_part.lower()
        matches: list[int] = []

        @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
        def _cb(hwnd, _lparam):
            length = user32.GetWindowTextLengthW(hwnd)
            if length:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                if target in buf.value.lower():
                    matches.append(int(hwnd))
                    return False
            return True

        user32.EnumWindows(_cb, 0)
        if not matches:
            return
        hwnd = matches[0]
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        fg = user32.GetForegroundWindow()
        tid_fg = user32.GetWindowThreadProcessId(fg, None)
        tid_me = kernel.GetCurrentThreadId()
        user32.AttachThreadInput(tid_fg, tid_me, True)
        try:
            user32.SetForegroundWindow(hwnd)
            user32.BringWindowToTop(hwnd)
        finally:
            user32.AttachThreadInput(tid_fg, tid_me, False)
    except Exception:
        pass


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve(port: int = 0, open_browser: bool = True) -> int:
    port = port or _free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    _set_console_title("微信聊天记录导出 - 请勿关闭本窗口")
    print("=" * 64)
    print("  微信聊天记录导出工具已启动")
    print(f"  浏览器地址：{url}")
    print("  浏览器通常会自动打开；如果没有，请复制上面的地址到浏览器。")
    print("  本窗口是本地服务，导出期间请勿关闭；按 Ctrl+C 可退出。")
    print("  （若窗口标题变成“选择…”，说明处于选中状态，按 Esc 即可恢复）")
    print("=" * 64)
    _remember_url(url)
    if open_browser:
        threading.Timer(0.6, lambda: _open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。")
    finally:
        httpd.server_close()
    return 0


def _set_console_title(title: str) -> None:
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleTitleW(title)
    except Exception:
        pass


def _remember_url(url: str) -> None:
    try:
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "WeChatExporter"
        base.mkdir(parents=True, exist_ok=True)
        (base / "last_url.txt").write_text(url, encoding="utf-8")
    except OSError:
        pass


def _open(url: str) -> None:
    """Open the UI in the default browser, trying the most reliable path first."""
    if os.name == "nt":
        try:
            os.startfile(url)  # ShellExecute: works with Edge/Chrome default handlers
            return
        except OSError:
            pass
    try:
        import webbrowser
        if webbrowser.open(url):
            return
    except Exception:
        pass
    if os.name == "nt":
        try:
            import subprocess
            subprocess.Popen(["cmd", "/c", "start", "", url], shell=False)
        except OSError:
            pass
