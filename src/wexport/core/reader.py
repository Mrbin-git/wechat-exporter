"""Readers for decrypted WeChat 4.x databases (messages, sessions, contacts)."""
from __future__ import annotations

import hashlib
import re
import sqlite3
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

import zstandard

_dctx = zstandard.ZstdDecompressor()

# local_type flags observed on WeChat 4.1.x
TYPE_TEXT = 1
TYPE_IMAGE = 3
TYPE_VOICE = 0x22
TYPE_VIDEO = 0x2B
TYPE_EMOJI = 0x2F
TYPE_LOCATION = 0x30
TYPE_CARD = 0x2A
TYPE_SYSTEM = 0x2710
TYPE_REVOKE = 0x2710 | 9
APPMSG = 0x31
SUB_FILE = 6
SUB_LINK = 5
SUB_QUOTE = 57
SUB_MINIPROG = 33
SUB_VIDEO_CHANNEL = 51


def _md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


@dataclass
class Message:
    local_id: int
    server_id: int
    local_type: int
    sender: str
    create_time: int
    content: str
    kind: str = "text"
    title: str = ""
    url: str = ""
    media: list[dict] = field(default_factory=list)
    raw_type: str = ""
    extras: dict = field(default_factory=dict)

    @property
    def time_str(self) -> str:
        import datetime as _dt
        return _dt.datetime.fromtimestamp(self.create_time).strftime("%Y-%m-%d %H:%M:%S")

    @property
    def display_text(self) -> str:
        """Human readable one-line summary used by exports."""
        if self.kind == "text":
            return self.content
        if self.kind == "file":
            return f"[文件] {self.extras.get('file_name') or self.title}"
        if self.kind == "image":
            return "[图片]"
        if self.kind == "voice":
            return "[语音]"
        if self.kind == "video":
            return "[视频]"
        if self.kind == "emoji":
            return "[表情]"
        if self.kind == "link":
            return f"[链接] {self.title} {self.url}".strip()
        if self.kind == "quote":
            return f"[引用] {self.title}".strip()
        if self.kind == "location":
            return f"[位置] {self.extras.get('poi', '')}"
        if self.kind == "card":
            return f"[名片] {self.extras.get('nickname', '')}"
        if self.kind == "miniprogram":
            return f"[小程序] {self.title}"
        if self.kind == "channels":
            return f"[视频号] {self.title}"
        if self.kind == "system":
            return f"[系统消息] {self.content}"
        if self.kind == "app":
            return f"[应用消息] {self.title}" if self.title else "[应用消息]"
        return self.title or f"[{self.kind}]"


def decode_content(mc, ctype: int) -> str:
    if mc is None:
        return ""
    if isinstance(mc, str):
        return mc
    b = bytes(mc)
    if ctype == 4:
        try:
            return _dctx.decompress(b, max_output_size=128 << 20).decode("utf-8", "replace")
        except zstandard.ZstdError:
            return b.decode("utf-8", "replace")
    return b.decode("utf-8", "replace")


def _xml(text: str) -> ET.Element | None:
    m = re.search(r"<msg[\s>]", text)
    if not m:
        return None
    try:
        return ET.fromstring(text[m.start():])
    except ET.ParseError:
        return None


def _packed_info(blob) -> dict:
    """Parse packed_info_data (protobuf) minimally: md5 / filename / size hints."""
    if not blob:
        return {}
    b = bytes(blob)
    info: dict = {}
    # md5 appears as a 32-char ascii run starting with tag 0x22 (field 4, length-delim)
    for m in re.finditer(rb"\x22\x20([0-9a-fA-F]{32})", b):
        info.setdefault("md5", m.group(1).decode().lower())
    for m in re.finditer(rb"\x12([\x08-\xff])\x08\x00", b):
        pass
    idx = b.find(b"\x08\x00\x12")
    if idx != -1 and idx + 3 < len(b):
        ln = b[idx + 2]
        name = b[idx + 3: idx + 3 + ln]
        try:
            info["file_name"] = name.decode("utf-8")
        except UnicodeDecodeError:
            pass
    return info


def classify(local_type: int, content: str) -> tuple[str, dict]:
    """Return (kind, extras) for one message.

    4.x packs flags into the upper bits of local_type; the real content type is
    the low 32 bits.
    """
    base = local_type & 0xFFFFFFFF
    extras: dict = {"base_type": base, "flags": local_type ^ base}
    if base == APPMSG:
        root = _xml(content)
        sub = 0
        if root is not None:
            appmsg = root.find("appmsg")
            if appmsg is not None:
                raw_sub = (appmsg.get("type") or appmsg.findtext("type") or "0").strip()
                try:
                    sub = int(raw_sub)
                except ValueError:
                    sub = 0
                extras["title"] = (appmsg.findtext("title") or "").strip()
                extras["url"] = (appmsg.findtext("url") or "").strip()
                extras["app_type"] = sub
                md5 = (appmsg.findtext("md5") or "").strip()
                if md5:
                    extras["md5"] = md5
                if sub == SUB_FILE:
                    att = appmsg.find("appattach")
                    if att is not None:
                        extras["file_name"] = (att.findtext("filename") or "").strip()
                        extras["file_ext"] = (att.findtext("fileext") or "").strip()
                        extras["total_len"] = (att.findtext("totallen") or "").strip()
                        extras["attachid"] = (att.findtext("attachid") or "").strip()
                        extras["aeskey"] = (att.findtext("aeskey") or "").strip()
                        if not md5:
                            extras["md5"] = (att.findtext("md5") or "").strip()
                    extras["file_name"] = extras.get("file_name") or extras["title"]
                    return "file", extras
                if sub == SUB_LINK:
                    return "link", extras
                if sub == SUB_QUOTE:
                    title = appmsg.find("title")
                    if title is not None:
                        extras["quoted"] = (title.text or "").strip()
                    return "quote", extras
                if sub == SUB_MINIPROG:
                    return "miniprogram", extras
                if sub == SUB_VIDEO_CHANNEL:
                    return "channels", extras
                if sub == 2:
                    return "image", extras
                if sub in (3, 4):
                    return "voice", extras
                if sub == 6:
                    return "file", extras
        return "app", extras
    if base == TYPE_IMAGE:
        root = _xml(content)
        if root is not None:
            img = root.find(".//img")
            if img is not None:
                extras["md5"] = img.get("md5", "")
                extras["aeskey"] = img.get("aeskey", "")
                extras["thumb_len"] = img.get("cdnthumblength", "")
        return "image", extras
    if base == TYPE_VOICE:
        root = _xml(content)
        if root is not None:
            v = root.find(".//voicemsg")
            if v is not None:
                extras["voice_len"] = v.get("voicelength", "")
                extras["md5"] = v.get("md5", "")
        return "voice", extras
    if base == TYPE_VIDEO:
        root = _xml(content)
        if root is not None:
            v = root.find(".//videomsg")
            if v is not None:
                extras["md5"] = v.get("md5", "")
                extras["playlength"] = v.get("playlength", "")
        return "video", extras
    if base == TYPE_EMOJI:
        root = _xml(content)
        if root is not None:
            e = root.find(".//emoji")
            if e is not None:
                extras["md5"] = e.get("md5", "")
        return "emoji", extras
    if base == TYPE_LOCATION:
        root = _xml(content)
        if root is not None:
            loc = root.find(".//location")
            if loc is not None:
                extras["poi"] = loc.get("poiname", "")
                extras["label"] = loc.get("label", "")
        return "location", extras
    if base == TYPE_CARD:
        root = _xml(content)
        if root is not None:
            extras["nickname"] = (root.findtext(".//nickname") or "").strip()
            extras["username"] = (root.findtext(".//username") or "").strip()
        return "card", extras
    if base == TYPE_SYSTEM or base == TYPE_REVOKE:
        return "system", extras
    return "text", extras


class AccountReader:
    """Read messages for one account from its decrypted database tree."""

    def __init__(self, decrypted_root: Path):
        self.root = Path(decrypted_root)
        self.db_storage = self.root / "db_storage" if (self.root / "db_storage").is_dir() else self.root
        self._names: dict[Path, dict[int, str]] = {}

    # -- helpers ---------------------------------------------------------
    def _connect(self, path: Path) -> sqlite3.Connection:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        return con

    def _name_map(self, db: Path) -> dict[int, str]:
        if db in self._names:
            return self._names[db]
        mapping: dict[int, str] = {}
        try:
            con = self._connect(db)
            if any(r[0] == "Name2Id" for r in con.execute("select name from sqlite_master where type='table'")):
                for r in con.execute("select rowid, user_name from Name2Id"):
                    mapping[int(r[0])] = str(r[1])
            con.close()
        except sqlite3.Error:
            pass
        self._names[db] = mapping
        return mapping

    def contacts(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        db = self.db_storage / "contact" / "contact.db"
        if not db.is_file():
            return out
        try:
            con = self._connect(db)
            for r in con.execute("select username, alias, remark, nick_name, local_type from contact"):
                out[str(r["username"])] = {
                    "alias": r["alias"] or "",
                    "remark": r["remark"] or "",
                    "nick": r["nick_name"] or "",
                    "type": r["local_type"],
                }
            con.close()
        except sqlite3.Error:
            pass
        return out

    def sessions(self) -> list[dict]:
        db = self.db_storage / "session" / "session.db"
        rows: list[dict] = []
        if not db.is_file():
            return rows
        try:
            con = self._connect(db)
            for r in con.execute(
                    "select username, type, unread_count, summary, last_timestamp, last_msg_type from SessionTable "
                    "order by last_timestamp desc"):
                rows.append(dict(r))
            con.close()
        except sqlite3.Error:
            pass
        return rows

    def _msg_tables(self, db: Path) -> dict[str, list[str]]:
        """Map chat-username -> msg table names inside one message db."""
        mapping: dict[str, list[str]] = {}
        try:
            con = self._connect(db)
            tables = [r[0] for r in con.execute(
                "select name from sqlite_master where type='table' and name like 'Msg_%'")]
            names = self._name_map(db)
            con.close()
        except sqlite3.Error:
            return mapping
        # the hash suffix is md5(chat username) per 4.x layout
        by_md5 = {_md5(u): u for u in names.values()}
        for t in tables:
            suffix = t[4:]
            chat = by_md5.get(suffix)
            if chat is None:
                # fall back: find any username whose md5 prefix matches
                for h, u in by_md5.items():
                    if h.startswith(suffix) or suffix.startswith(h[:len(suffix)]):
                        chat = u
                        break
            if chat:
                mapping.setdefault(chat, []).append(t)
        return mapping

    def table_map(self) -> dict[str, list[tuple[Path, str]]]:
        out: dict[str, list[tuple[Path, str]]] = {}
        for db in sorted((self.db_storage / "message").glob("message_*.db")):
            for chat, tables in self._msg_tables(db).items():
                for t in tables:
                    out.setdefault(chat, []).append((db, t))
        biz = sorted((self.db_storage / "message").glob("biz_message_*.db"))
        for db in biz:
            for chat, tables in self._msg_tables(db).items():
                for t in tables:
                    out.setdefault(f"biz:{chat}", []).append((db, t))
        return out

    def iter_messages(self, chat: str, since: int = 0, until: int = 0,
                      limit: int = 0) -> Iterator[Message]:
        contacts = self.contacts()
        entries = self.table_map().get(chat) or self.table_map().get(f"biz:{chat}") or []
        emitted = 0
        for db, table in entries:
            names = self._name_map(db)
            con = self._connect(db)
            query = f'select local_id, server_id, local_type, real_sender_id, create_time, message_content, packed_info_data, WCDB_CT_message_content from "{table}"'
            conds = []
            args: list = []
            if since:
                conds.append("create_time >= ?")
                args.append(int(since))
            if until:
                conds.append("create_time <= ?")
                args.append(int(until))
            if conds:
                query += " where " + " and ".join(conds)
            query += " order by create_time, local_id"
            for row in con.execute(query, args):
                content = decode_content(row["message_content"], int(row["WCDB_CT_message_content"] or 0))
                sender = names.get(int(row["real_sender_id"] or 0), "")
                kind, extras = classify(int(row["local_type"]), content)
                info = _packed_info(row["packed_info_data"])
                if info.get("md5") and kind in ("image", "emoji", "video", "file"):
                    # packed_info carries the on-disk resource md5 (attach tree name)
                    extras["xml_md5"] = extras.get("md5", "")
                    extras["md5"] = info["md5"]
                media = []
                md5 = extras.get("md5") or info.get("md5")
                if md5:
                    media.append({"md5": md5})
                msg = Message(
                    local_id=int(row["local_id"]),
                    server_id=int(row["server_id"] or 0),
                    local_type=int(row["local_type"]),
                    sender=sender,
                    create_time=int(row["create_time"]),
                    content=content,
                    kind=kind,
                    title=extras.get("title", "") or extras.get("file_name", "") or "",
                    url=extras.get("url", ""),
                    media=media,
                    raw_type=hex(int(row["local_type"])),
                )
                msg.extras = extras  # type: ignore[attr-defined]
                yield msg
                emitted += 1
                if limit and emitted >= limit:
                    con.close()
                    return
            con.close()

    def chat_summary(self, chat: str) -> dict:
        n = 0
        first = last = 0
        for m in self.iter_messages(chat):
            n += 1
            first = first or m.create_time
            last = m.create_time
        return {"count": n, "first": first, "last": last}
