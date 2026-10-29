"""Shared fixtures for the WeChat exporter test-suite.

Not a test module (leading underscore) so ``unittest discover`` skips it.

Rules honoured here:
* ``probe/decrypted`` and the real WeChat data directory are strictly read-only.
* every artefact goes to the system temp dir (``%TEMP%``) or the project ``.tmp``.
* tests needing real machine data are skipped - never faked - when unavailable.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT / "vendor", ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

#: pytest-style alias used by the tests when ``python -m unittest`` is used.
fixture = None
try:  # pragma: no cover - optional convenience
    import pytest  # type: ignore

    fixture = pytest.fixture
except ImportError:  # pragma: no cover
    pass


def _load_tmp_base():
    """Prefer the project ``.tmp`` dir, fall back to the system temp dir."""
    for cand in (ROOT / ".tmp", Path(tempfile.gettempdir()) / "wechat-exporter-tests"):
        try:
            cand.mkdir(parents=True, exist_ok=True)
            probe = cand / ".write-probe"
            probe.write_bytes(b"ok")
            probe.unlink()
            return cand
        except OSError:
            continue
    return Path(tempfile.gettempdir())


TMP_BASE = _load_tmp_base()

#: Known-good passphrase for the local fixture account.
TEST_PASSPHRASE_HEX = "0000000000000000000000000000000000000000000000000000000000000000"
TEST_WXID = "wxid_example"
TEST_FOLDER = "wxid_example_9f3a"

REAL_ACCOUNT_DIR = Path(r"D:\WeChat\xwechat_files") / TEST_FOLDER
REAL_SESSION_DB = REAL_ACCOUNT_DIR / "db_storage" / "session" / "session.db"
REAL_ATTACH = REAL_ACCOUNT_DIR / "msg" / "attach"
DECRYPTED = ROOT / "probe" / "decrypted"
DIST_EXE = ROOT / "dist" / "WeChatExport" / "WeChatExport.exe"

JPEG_MAGIC = b"\xff\xd8\xff"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


# --------------------------------------------------------------------------- #
# temp dirs
# --------------------------------------------------------------------------- #
def make_tmp(prefix: str = "case-") -> Path:
    path = Path(tempfile.mkdtemp(prefix=prefix, dir=str(TMP_BASE)))
    return path


def cleanup(path: Path | None) -> None:
    if path:
        shutil.rmtree(str(path), ignore_errors=True)


class TempDirMixin:
    """Mixin creating a fresh temp dir per test method."""

    prefix = "wexport-"

    def setUp(self):  # noqa: N802 - unittest naming
        super().setUp()
        self.tmp = make_tmp(self.prefix)

    def tearDown(self):  # noqa: N802
        cleanup(getattr(self, "tmp", None))
        super().tearDown()

    def sub(self, name: str) -> Path:
        path = self.tmp / name
        path.mkdir(parents=True, exist_ok=True)
        return path


# --------------------------------------------------------------------------- #
# skips
# --------------------------------------------------------------------------- #
PLATFORM_REASON = (
    f"平台不匹配：当前为 {sys.platform}，微信 4.x 数据目录与密钥恢复仅支持 Windows"
)
WINDOWS_ONLY = unittest.skipUnless(sys.platform == "win32", PLATFORM_REASON)


def require_path(path: Path, what: str):
    if not Path(path).exists():
        raise unittest.SkipTest(f"缺少真实数据：{what} 不存在（{path}）")


def skip_unless(cond: bool, reason: str):
    if not cond:
        raise unittest.SkipTest(reason)


def has_real_account() -> bool:
    return REAL_ACCOUNT_DIR.is_dir() and REAL_SESSION_DB.is_file()


def has_decrypted() -> bool:
    return (DECRYPTED / "session" / "session.db").is_file()


def real_dat_files(limit: int = 12) -> list[Path]:
    if not REAL_ATTACH.is_dir():
        return []
    try:
        return sorted(REAL_ATTACH.rglob("*.dat"))[:limit]
    except OSError:
        return []


# --------------------------------------------------------------------------- #
# message stubs
# --------------------------------------------------------------------------- #
def make_messages():
    """Five stubbed messages covering text/image/file/link/system.

    Mirrors what :class:`wexport.core.reader.Message` produces for the export
    layer: ``extras`` is set after construction (same as the reader does).
    """
    from wexport.core.reader import Message

    base_time = 1735660800  # 2025-01-01 00:00:00 UTC+8
    text = Message(local_id=1, server_id=101, local_type=1, sender="wxid_friend_x",
                   create_time=base_time, content="你好，这是文本消息 / hello <world> & 100%",
                   kind="text")
    text.extras = {"base_type": 1, "flags": 0}

    image = Message(local_id=2, server_id=102, local_type=3, sender="wxid_friend_x",
                    create_time=base_time + 60, content="<msg><img md5=\"abc\"/></msg>",
                    kind="image", media=[{"md5": "0123456789abcdef0123456789abcdef"}])
    image.extras = {"base_type": 3, "flags": 0, "md5": "0123456789abcdef0123456789abcdef",
                    "aeskey": "deadbeef" * 4, "attachid": "attach-id-1"}

    file_name = "项目需求说明书_2025年第一季度_v3.2_最终版.pdf"
    file_msg = Message(local_id=3, server_id=103, local_type=49, sender="wxid_friend_x",
                       create_time=base_time + 120, content="<msg><appmsg type=6/></msg>",
                       kind="file", title=file_name,
                       media=[{"md5": "fedcba9876543210fedcba9876543210"}])
    file_msg.extras = {"base_type": 49, "flags": 0, "app_type": 6, "file_name": file_name,
                       "file_ext": "pdf", "total_len": "1048576",
                       "attachid": "attach-id-2", "aeskey": "cafebabe" * 4,
                       "md5": "fedcba9876543210fedcba9876543210"}

    link = Message(local_id=4, server_id=104, local_type=49, sender="wxid_self_001",
                   create_time=base_time + 180, content="<msg><appmsg type=5/></msg>",
                   kind="link", title="微信 4.x 数据目录结构解析", url="https://example.com/a?x=1&y=2")
    link.extras = {"base_type": 49, "flags": 0, "app_type": 5,
                   "title": "微信 4.x 数据目录结构解析", "aeskey": "0badf00d" * 4}

    system = Message(local_id=5, server_id=105, local_type=10000, sender="",
                     create_time=base_time + 240, content="你已添加了 wxid_friend_x，现在可以开始聊天了。",
                     kind="system")
    system.extras = {"base_type": 10000, "flags": 0}

    return [text, image, file_msg, link, system]


# --------------------------------------------------------------------------- #
# frozen app helpers
# --------------------------------------------------------------------------- #
def popen_text(exe: Path, args: list[str], cwd: Path | None = None) -> str:
    """Run a frozen exe and capture stdout as UTF-8 regardless of console CP.

    Windows console apps emit their ANSI codepage when stdout is a pipe, so the
    raw bytes are decoded with the active console codepage first.
    """
    proc = subprocess.run([str(exe), *args], capture_output=True, timeout=120,
                          cwd=str(cwd) if cwd else None)
    raw = proc.stdout or b""
    # the frozen build writes UTF-8 to the pipe; fall back to the console code page
    for enc in ("utf-8", _console_codepage(), "latin-1"):
        try:
            text = raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        if text.strip():
            return text
    return raw.decode("utf-8", "replace")


def _console_codepage() -> str:
    try:
        cp = subprocess.run(["cmd", "/c", "chcp"], capture_output=True, timeout=10)
        digits = "".join(ch for ch in cp.stdout.decode("ascii", "ignore") if ch.isdigit())
        if digits:
            return f"cp{digits}"
    except (OSError, subprocess.SubprocessError):
        pass
    return "cp936"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wait_http(url: str, timeout: float = 40.0) -> bool:
    import urllib.request

    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.25)
    return False


def terminate(proc) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
        except OSError:
            pass
    for stream in (getattr(proc, "stdout", None), getattr(proc, "stderr", None)):
        try:
            if stream is not None:
                stream.close()
        except OSError:
            pass


__all__ = [
    "ROOT", "TMP_BASE", "TEST_PASSPHRASE_HEX", "TEST_WXID", "TEST_FOLDER",
    "REAL_ACCOUNT_DIR", "REAL_SESSION_DB", "REAL_ATTACH", "DECRYPTED", "DIST_EXE",
    "JPEG_MAGIC", "PNG_MAGIC", "TempDirMixin", "WINDOWS_ONLY", "PLATFORM_REASON",
    "make_tmp", "cleanup", "require_path", "skip_unless", "has_real_account",
    "has_decrypted", "real_dat_files", "make_messages", "popen_text", "free_port",
    "wait_http", "terminate", "fixture", "os",
]
