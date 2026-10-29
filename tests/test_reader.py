"""A5-04..08 — AccountReader against the real decrypted sample tree.

Target: ``probe/decrypted`` (明文 sqlite, read-only).
Covers contacts/sessions/table_map/message decoding + the pure classification
helpers that the exports depend on.
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

from _support import DECRYPTED, require_path

from wexport.core import reader as reader_mod
from wexport.core.reader import AccountReader, Message, classify, decode_content


def setUpModule():
    require_path(DECRYPTED / "session" / "session.db", "明文样本 probe/decrypted")


class TestAccountReaderRealData(unittest.TestCase):
    """TC-RDR-01..06: real decrypted account."""

    @classmethod
    def setUpClass(cls):
        cls.reader = AccountReader(DECRYPTED)
        cls.contacts = cls.reader.contacts()
        cls.sessions = cls.reader.sessions()
        cls.table_map = cls.reader.table_map()

    def test_contacts_not_empty(self):
        self.assertGreater(len(self.contacts), 0, "contacts 为空")
        for username, info in self.contacts.items():
            self.assertIsInstance(username, str)
            self.assertTrue(username)
            self.assertIn("nick", info)
            self.assertIn("type", info)

    def test_known_contacts_present(self):
        # these three always exist in a desktop WeChat account
        for expected in ("filehelper", "weixin"):
            self.assertIn(expected, self.contacts, f"缺少内置联系人 {expected}")

    def test_sessions_not_empty_and_sorted(self):
        self.assertGreater(len(self.sessions), 0, "sessions 为空")
        stamps = [s["last_timestamp"] or 0 for s in self.sessions]
        self.assertEqual(stamps, sorted(stamps, reverse=True), "会话未按时间倒序")
        for s in self.sessions:
            self.assertTrue(s["username"], "会话缺少 username")

    def test_table_map_has_at_least_two_chats(self):
        self.assertGreaterEqual(len(self.table_map), 2, f"table_map 只有 {len(self.table_map)} 个会话")
        for chat, entries in self.table_map.items():
            self.assertTrue(chat)
            self.assertTrue(entries, f"{chat} 没有任何消息表")

    def test_table_map_points_at_existing_tables(self):
        checked = 0
        for chat, entries in self.table_map.items():
            for db_path, table in entries:
                self.assertTrue(Path(db_path).is_file(), f"{db_path} 不存在")
                con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                try:
                    row = con.execute(
                        "select count(*) from sqlite_master where type='table' and name=?",
                        (table,)).fetchone()
                finally:
                    con.close()
                self.assertEqual(row[0], 1, f"{db_path} 中缺少表 {table}")
                checked += 1
        self.assertGreater(checked, 0)

    def test_reads_text_message_with_display_text(self):
        found = None
        for chat in self.table_map:
            for msg in self.reader.iter_messages(chat):
                if msg.kind == "text" and msg.display_text.strip():
                    found = (chat, msg)
                    break
            if found:
                break
        self.assertIsNotNone(found, "没有任何可读文本消息")
        chat, msg = found
        self.assertEqual(msg.display_text, msg.content)
        self.assertGreater(msg.create_time, 0)
        self.assertGreater(msg.local_id, 0)
        self.assertIsInstance(msg.time_str, str)
        self.assertRegex(msg.time_str, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

    def test_file_message_has_kind_and_file_name(self):
        found = None
        for chat in self.table_map:
            for msg in self.reader.iter_messages(chat):
                if msg.kind == "file":
                    found = msg
                    break
            if found:
                break
        self.assertIsNotNone(found, "样本中没有文件类消息，无法验证 kind=='file'")
        self.assertEqual(found.kind, "file")
        name = found.extras.get("file_name")
        self.assertTrue(name, f"文件消息缺少 file_name，extras={found.extras}")
        self.assertTrue(found.display_text.startswith("[文件]"))
        self.assertIn(name, found.display_text)

    def test_iter_messages_respects_unknown_chat(self):
        self.assertEqual(list(self.reader.iter_messages("no_such_chat_xyz")), [])

    def test_chat_summary_matches_iteration(self):
        chat = next(iter(self.table_map))
        msgs = list(self.reader.iter_messages(chat))
        summary = self.reader.chat_summary(chat)
        self.assertEqual(summary["count"], len(msgs))
        if msgs:
            self.assertEqual(summary["first"], min(m.create_time for m in msgs))
            self.assertEqual(summary["last"], max(m.create_time for m in msgs))

    def test_reader_does_not_modify_samples(self):
        before = {p: (p.stat().st_size, p.stat().st_mtime_ns)
                  for p in DECRYPTED.rglob("*.db")}
        AccountReader(DECRYPTED).sessions()
        after = {p: (p.stat().st_size, p.stat().st_mtime_ns)
                 for p in DECRYPTED.rglob("*.db")}
        self.assertEqual(before, after, "测试改动了 probe/decrypted")


class TestReaderLayout(unittest.TestCase):
    """TC-RDR-07: db_storage auto-detection (root vs. account dir)."""

    def test_db_storage_detected_when_nested(self):
        r = AccountReader(DECRYPTED)
        self.assertEqual(Path(r.db_storage), DECRYPTED)

    def test_account_root_without_db_storage_uses_self(self):
        r = AccountReader(DECRYPTED / "session")  # no db_storage child -> itself
        self.assertEqual(Path(r.db_storage), DECRYPTED / "session")
        self.assertEqual(r.sessions(), [])

    def test_empty_root_yields_empty_results(self):
        from _support import cleanup, make_tmp

        tmp = make_tmp("reader-empty-")
        try:
            r = AccountReader(tmp)
            self.assertEqual(r.contacts(), {})
            self.assertEqual(r.sessions(), [])
            self.assertEqual(r.table_map(), {})
        finally:
            cleanup(tmp)


class TestClassify(unittest.TestCase):
    """TC-RDR-08: message type classification (pure function)."""

    def test_plain_text(self):
        kind, extras = classify(1, "hello")
        self.assertEqual(kind, "text")
        self.assertEqual(extras["base_type"], 1)

    def test_type_flags_are_split_out(self):
        # 4.x stores flags in the upper bits; low 32 bits carry the real type
        kind, extras = classify((5 << 32) | 1, "hello")
        self.assertEqual(kind, "text")
        self.assertEqual(extras["base_type"], 1)
        self.assertEqual(extras["flags"], 5 << 32)

    def test_image(self):
        xml = '<msg><img md5="abc123" aeskey="k" cdnthumblength="99"/></msg>'
        kind, extras = classify(3, xml)
        self.assertEqual(kind, "image")
        self.assertEqual(extras["md5"], "abc123")
        self.assertEqual(extras["thumb_len"], "99")

    def test_file_appmsg(self):
        xml = ("<msg><appmsg type=\"6\"><title>报价单.xlsx</title>"
               "<appattach><filename>报价单.xlsx</filename><fileext>xlsx</fileext>"
               "<totallen>2048</totallen><attachid>aid</attachid>"
               "<md5>00112233445566778899aabbccddeeff</md5></appattach></appmsg></msg>")
        kind, extras = classify(49, xml)
        self.assertEqual(kind, "file")
        self.assertEqual(extras["file_name"], "报价单.xlsx")
        self.assertEqual(extras["file_ext"], "xlsx")
        self.assertEqual(extras["total_len"], "2048")
        self.assertEqual(extras["md5"], "00112233445566778899aabbccddeeff")

    def test_link_appmsg(self):
        xml = ("<msg><appmsg type=\"5\"><title>标题</title><url>https://a.b/c</url>"
               "</appmsg></msg>")
        kind, extras = classify(49, xml)
        self.assertEqual(kind, "link")
        self.assertEqual(extras["title"], "标题")
        self.assertEqual(extras["url"], "https://a.b/c")

    def test_quote_appmsg(self):
        xml = "<msg><appmsg type=\"57\"><title>被引用内容</title></appmsg></msg>"
        kind, extras = classify(49, xml)
        self.assertEqual(kind, "quote")
        self.assertEqual(extras["quoted"], "被引用内容")

    def test_miniprogram_and_channels(self):
        self.assertEqual(classify(49, '<msg><appmsg type="33"><title>小程序</title></appmsg></msg>')[0],
                         "miniprogram")
        self.assertEqual(classify(49, '<msg><appmsg type="51"><title>视频号</title></appmsg></msg>')[0],
                         "channels")

    def test_voice_video_emoji_location_card(self):
        self.assertEqual(classify(0x22, "<msg><voicemsg voicelength=\"1234\"/></msg>")[0], "voice")
        self.assertEqual(classify(0x2B, "<msg><videomsg playlength=\"9\"/></msg>")[0], "video")
        self.assertEqual(classify(0x2F, "<msg><emoji md5=\"e\"/></msg>")[0], "emoji")
        kind, extras = classify(0x30, "<msg><location poiname=\"西湖\" label=\"杭州\"/></msg>")
        self.assertEqual(kind, "location")
        self.assertEqual(extras["poi"], "西湖")
        kind, extras = classify(0x2A, "<msg><nickname>张三</nickname><username>wxid_z</username></msg>")
        self.assertEqual(kind, "card")
        self.assertEqual(extras["nickname"], "张三")

    def test_system_and_revoke(self):
        self.assertEqual(classify(10000, "系统消息")[0], "system")
        self.assertEqual(classify(10000 | 9, "撤回")[0], "system")

    def test_malformed_appmsg_falls_back_to_app(self):
        kind, _ = classify(49, "not xml at all")
        self.assertEqual(kind, "app")


class TestDecodeContent(unittest.TestCase):
    """TC-RDR-09: content blob decoding (plain utf-8 vs zstd)."""

    def test_none_and_str(self):
        self.assertEqual(decode_content(None, 0), "")
        self.assertEqual(decode_content("already text", 0), "already text")

    def test_plain_utf8(self):
        self.assertEqual(decode_content("你好".encode("utf-8"), 0), "你好")

    def test_zstd_compressed(self):
        import zstandard

        payload = "压缩的内容" * 50
        blob = zstandard.ZstdCompressor().compress(payload.encode("utf-8"))
        self.assertEqual(decode_content(blob, 4), payload)

    def test_bad_utf8_is_replaced_not_raised(self):
        out = decode_content(b"\xff\xfe\x00bad", 0)
        self.assertIsInstance(out, str)


class TestMessageDisplayText(unittest.TestCase):
    """TC-RDR-10: display_text rendering for every kind."""

    def mk(self, kind: str, **kw) -> Message:
        msg = Message(local_id=1, server_id=1, local_type=1, sender="s",
                      create_time=1735660800, content=kw.pop("content", "c"), kind=kind)
        msg.extras = kw
        return msg

    def test_all_kinds_non_empty(self):
        cases = {
            "text": {"content": "原文"},
            "file": {"file_name": "a.pdf"},
            "image": {}, "voice": {}, "video": {}, "emoji": {},
            "link": {}, "quote": {}, "location": {"poi": "西湖"},
            "card": {"nickname": "张三"}, "miniprogram": {}, "channels": {},
            "system": {"content": "系统提示"}, "app": {},
        }
        for kind, extras in cases.items():
            msg = self.mk(kind, **extras)
            msg.title = "T"
            msg.url = "u"
            self.assertTrue(msg.display_text.strip(), f"{kind} 的 display_text 为空")

    def test_specific_renders(self):
        self.assertEqual(self.mk("text", content="原文").display_text, "原文")
        self.assertEqual(self.mk("file", file_name="a.pdf").display_text, "[文件] a.pdf")
        self.assertEqual(self.mk("image").display_text, "[图片]")
        self.assertEqual(self.mk("system", content="notice").display_text, "[系统消息] notice")
        m = self.mk("card", nickname="张三")
        self.assertEqual(m.display_text, "[名片] 张三")

    def test_unknown_kind_falls_back_to_title(self):
        m = self.mk("weird")
        m.title = "标题"
        self.assertEqual(m.display_text, "标题")


class TestPackedInfo(unittest.TestCase):
    """TC-RDR-11: packed_info_data (protobuf) minimal parsing."""

    def test_empty(self):
        self.assertEqual(reader_mod._packed_info(None), {})
        self.assertEqual(reader_mod._packed_info(b""), {})

    def test_md5_extracted(self):
        blob = b"\x22\x20" + b"0123456789abcdef0123456789abcdef"
        info = reader_mod._packed_info(blob)
        self.assertEqual(info.get("md5"), "0123456789abcdef0123456789abcdef")

    def test_md5_lowercased(self):
        blob = b"\x22\x20" + b"0123456789ABCDEF0123456789ABCDEF"
        self.assertEqual(reader_mod._packed_info(blob).get("md5"),
                         "0123456789abcdef0123456789abcdef")


if __name__ == "__main__":
    sys.exit(unittest.main())
