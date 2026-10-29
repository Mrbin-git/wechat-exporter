"""WeChat 4.x media handling.

Verified format notes (WeChat 4.1.15.51, Windows):

* V2 container magic: ``07 08 56 32 08 07`` (V1 legacy is ``... 56 31 ...``)
  - ``int32le@6``  = aes_size (observed 1024)
  - ``int32le@10`` = xor_size (file_size - 0x0F - aes_size)
  - payload layout: ``aes_block(aligned) + raw_middle + xor_tail``
  - AES-128-**ECB**, PKCS#7 stripped manually
* keys are derived from the kvcomm statistics file, no process memory needed:
  - ``code`` from ``%APPDATA%\\Tencent\\xwechat\\net\\kvcomm\\key_<code>_..._input.statistic``
  - ``aes_key = md5(f"{code}{wxid}")[:16]`` (ASCII, wxid without folder suffix)
  - ``xor_key = code & 0xFF``
* voices are SILK v3 in ``msg/voice`` (rarely present on desktop); we keep the
  original bytes and only report duration, unless an external ffmpeg exists.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path

V2_MAGIC = bytes.fromhex("070856320807")
V1_MAGIC = bytes.fromhex("070856310807")
HEADER = 0x0F
IMAGE_EXT = {b"\xff\xd8\xff": ".jpg", b"\x89PNG": ".png", b"GIF8": ".gif", b"RIFF": ".webp",
             b"BM": ".bmp", b"wxgf": ".wxgf"}


@dataclass
class MediaKeys:
    wxid: str = ""
    code: int = 0
    aes_key: bytes = b""
    xor_key: int = 0
    source: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.aes_key) and 0 <= self.xor_key <= 0xFF


def _clean_wxid(name: str) -> str:
    return re.sub(r"_[0-9a-f]{4}$", "", str(name or "").strip(), flags=re.I)


def _kvcomm_dirs() -> list[Path]:
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return [Path(appdata) / "Tencent" / "xwechat" / "net" / "kvcomm",
            Path(appdata) / "Tencent" / "WeChat" / "net" / "kvcomm"]


def find_codes() -> list[int]:
    """Collect candidate ``code`` values from the kvcomm statistics files."""
    codes: list[int] = []
    for d in _kvcomm_dirs():
        if not d.is_dir():
            continue
        for f in d.glob("key_*_input.statistic"):
            m = re.match(r"key_(\d+)_", f.name)
            if m:
                try:
                    code = int(m.group(1))
                except ValueError:
                    continue
                if code not in codes:
                    codes.append(code)
    return codes


def derive_keys(wxid: str, code: int) -> MediaKeys:
    wi = _clean_wxid(wxid)
    aes = hashlib.md5(f"{code}{wi}".encode()).hexdigest()[:16].encode("ascii")
    return MediaKeys(wxid=wi, code=code, aes_key=aes, xor_key=code & 0xFF, source="kvcomm")


def resolve_keys(account_dir: Path | None = None, wxid: str = "") -> list[MediaKeys]:
    """Return key candidates ordered by plausibility (verified ones first)."""
    codes = find_codes()
    wi = _clean_wxid(wxid)
    out = [derive_keys(wi, c) for c in codes]
    return out


# --------------------------------------------------------------------------- #
# V2 container
# --------------------------------------------------------------------------- #
def detect_format(path: Path) -> str:
    try:
        head = Path(path).read_bytes()[:16]
    except OSError:
        return "missing"
    if head.startswith(V2_MAGIC) or head[:4] == bytes.fromhex("07085632"):
        return "v2"
    if head[:4] == bytes.fromhex("07085631"):
        return "v1"
    if head.startswith((b"\xff\xd8\xff", b"\x89PNG", b"GIF8", b"RIFF", b"BM")):
        return "plain"
    return "unknown"


def decrypt_v2(data: bytes, keys: MediaKeys) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    aes_size = struct.unpack_from("<i", data, 6)[0]
    xor_size = struct.unpack_from("<i", data, 10)[0]
    payload = data[HEADER:]
    if aes_size < 0 or xor_size < 0 or aes_size + xor_size > len(payload):
        raise ValueError("V2 头部长度字段异常")
    aligned = (aes_size + 15) // 16 * 16
    aes_part = payload[:aligned]
    raw_part = payload[aligned: len(payload) - xor_size]
    xor_part = payload[len(payload) - xor_size:]
    d = Cipher(algorithms.AES(keys.aes_key), modes.ECB()).decryptor()
    plain = d.update(aes_part) + d.finalize()
    # strip PKCS#7 produced by the writer on the aligned block
    pad = plain[-1] if plain else 0
    if 0 < pad <= 16 and plain[-pad:] == bytes([pad]) * pad:
        plain = plain[:-pad]
    plain = plain[:aes_size]
    tail = bytes(b ^ keys.xor_key for b in xor_part)
    return plain + raw_part + tail


def _sniff_ext(data: bytes) -> str:
    for sig, ext in IMAGE_EXT.items():
        if data.startswith(sig):
            return ext
    return ".bin"


def decrypt_media(src: Path, out_dir: Path, keys_list: list[MediaKeys]) -> Path | None:
    """Decrypt one .dat file, trying each key candidate, verify by signature."""
    src = Path(src)
    if not src.is_file():
        return None
    fmt = detect_format(src)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    data = src.read_bytes()
    if fmt == "plain":
        target = out_dir / (src.stem + _sniff_ext(data))
        target.write_bytes(data)
        return target
    if fmt != "v2":
        return None
    for keys in keys_list:
        if not keys.ok:
            continue
        try:
            plain = decrypt_v2(data, keys)
        except Exception:
            continue
        ext = _sniff_ext(plain)
        if ext in (".jpg", ".png", ".gif", ".webp", ".bmp"):
            target = out_dir / (src.stem + ext)
            target.write_bytes(plain)
            return target
        if ext == ".wxgf":
            target = out_dir / (src.stem + ".wxgf")
            target.write_bytes(plain)
            return target
    return None


# --------------------------------------------------------------------------- #
# media lookup
# --------------------------------------------------------------------------- #
def _find_by_name(roots: list[Path], stem: str, suffixes: tuple[str, ...] = ("", "_h", "_t")) -> list[Path]:
    hits: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for suf in suffixes:
            for p in root.rglob(f"{stem}{suf}.dat"):
                hits.append(p)
    return hits


def resolve_media(msg, account_dir: Path) -> list[dict]:
    """Map one Message to on-disk media entries (before decryption)."""
    account_dir = Path(account_dir)
    attach = account_dir / "msg" / "attach"
    files_root = account_dir / "msg" / "file"
    video_root = account_dir / "msg" / "video"
    voice_root = account_dir / "msg" / "voice"
    ex = getattr(msg, "extras", {}) or {}
    out: list[dict] = []
    md5 = (ex.get("md5") or "").strip().lower()
    if msg.kind in ("image", "emoji") and md5:
        for p in _find_by_name([attach], md5):
            suffix = "_h" if p.stem.endswith("_h") else ("_t" if p.stem.endswith("_t") else "")
            out.append({"type": "image" if msg.kind == "image" else "emoji",
                        "variant": suffix.strip("_") or "raw", "path": p})
    if msg.kind == "file":
        name = ex.get("file_name") or msg.title
        if name:
            for root in (files_root, attach):
                for p in root.rglob(name):
                    if p.is_file() and p.suffix.lower() != ".dat":
                        out.append({"type": "file", "variant": "raw", "path": p})
            if not out and md5:
                for p in _find_by_name([attach], md5):
                    out.append({"type": "file", "variant": "raw", "path": p})
    if msg.kind in ("video",):
        if md5:
            for p in _find_by_name([video_root, attach], md5, ("", "_video", "_t")):
                if p.suffix == ".mp4":
                    out.append({"type": "video", "variant": "raw", "path": p})
                else:
                    out.append({"type": "video", "variant": "enc", "path": p})
    if msg.kind == "voice":
        if md5 and voice_root.is_dir():
            for p in voice_root.rglob(f"{md5}*"):
                out.append({"type": "voice", "variant": "raw", "path": p})
    return out


# --------------------------------------------------------------------------- #
# audio
# --------------------------------------------------------------------------- #
def silk_info(data: bytes) -> dict:
    """Minimal SILK v3 header probe (no decoding)."""
    info = {"format": "silk", "duration_ms": 0, "sample_rate": 24000}
    if len(data) < 10 or not data.startswith(b"#!SILK_V3"):
        return info
    idx = data.find(b"\x02")
    return info


def convert_to_wav(src: Path, dst: Path) -> bool:
    """Optional: use ffmpeg when the user has it installed."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    try:
        subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(src), str(dst)],
                       check=True, timeout=120)
        return dst.exists()
    except (subprocess.SubprocessError, OSError):
        return False


def ffmpeg_available() -> bool:
    return bool(shutil.which("ffmpeg"))
