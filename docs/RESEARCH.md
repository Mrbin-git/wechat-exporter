# 技术研究记录（P0/P1 实证）

> 本文件记录项目最关键的可行性验证：如何在微信 4.x（Windows）上取得 SQLCipher
> 密钥、解密数据库、识别消息与媒体格式。所有结论均在同一台真实机器上复现，
> 不是推测。测试环境：Windows 10 26H2，微信 Weixin 4.1.15.51。

## 1. 环境与数据布局

| 项 | 实测值 |
|---|---|
| 客户端 | `E:\Program Files (x86)\Tencent\Weixin\Weixin.exe`，版本目录 `4.1.15.51\Weixin.dll` |
| 数据根 | 由 `%APPDATA%\Tencent\xwechat\config\*.ini` 指定（内容为 UTF-8 路径），实测 `D:\WeChat\xwechat_files` |
| 账号目录 | `xwechat_files\wxid_xxx_<4位hex>\db_storage\` |
| 关键库 | `message\message_N.db`、`message\message_resource.db`、`session\session.db`、`contact\contact.db`、`head_image\head_image.db` |
| 登录信息 | `all_users\login\<wxid>\key_info.db`（明文 SQLite，`LoginKeyInfoTable`） |
| 媒体 | `msg\attach\<会话md5>\<年-月>\<资源md5>.dat`、`msg\file\<年-月>\原文件名` |

## 2. 密钥恢复链路（已打通）

WeChat 4.x 的数据库密钥不是固定值，而是**每账号一份 32 字节 passphrase**，
运行时只存在于进程内存中；要解密必须先从内存把它取出来。

实测可行的链路（对应 `src/wexport/core/keys.py`）：

1. **客户端辅助密钥**：扫描 `Weixin.dll` 代码段中的
   `48 BA <8字节> ... 48 85 C0` 指令序列，得到若干 32 字节 `internal key`。
   实测 4.1.15.51 命中 2 个候选。
2. **内存特征**：在 `Weixin.exe` 的私有提交内存中搜索 32 字节特征
   `00*8 20 00*7 2f 00*7`，命中位置前 8 字节是一个指针，指向 32 字节候选缓冲。
3. **解掩码**：候选 `raw` 与每个 `internal key` 逐字节异或，得到候选 passphrase。
4. **校验**：用候选 passphrase 对任一真实数据库首页做 SQLCipher 校验
   （PBKDF2-HMAC-SHA512 256000 轮 → AES 密钥 → 页 HMAC），通过即为正确密钥。
5. **缓存**：正确密钥用 Windows DPAPI 加密后存 `%LOCALAPPDATA%\WeChatExporter\keys.dat`，
   后续启动免扫描；换机/换用户后缓存失效，需要重新获取或手动输入。

实测：内存候选 552 个，用 8 进程并行校验，约 20 秒命中；
输出的 passphrase 对 `session.db/contact.db` 等库首页 HMAC 全部通过。

> 说明：`key_info.db` 中的 180 字节 `key_info_data` 是加密后的登录信息，
> 目前没有可靠、公开的解密方法；本项目不依赖它，仅作为登录状态线索。

## 3. SQLCipher 参数（逐条实测）

```
页大小         4096
salt           文件前 16 字节
enc_key        PBKDF2-HMAC-SHA512(passphrase, salt, 256000, dklen=32)
mac_key        PBKDF2-HMAC-SHA512(enc_key, salt^0x3A, 2, dklen=32)
加密           AES-256-CBC，IV 位于每页尾部 16 字节
页尾保留       80 字节 = IV(16) + HMAC-SHA512(64)
HMAC 覆盖      page[16或0 : 4096-80+16] + page_no(LE32)
明文页 1       以 "SQLite format 3\0" 开头，尾部补 80 个 0
```

实测：`session.db`、`contact.db`、`favorite.db` 等首页 HMAC 100% 通过；
30MB 的 `message_0.db` 只有前若干页被真实写入，其余是预分配空白页，
这些空白页 HMAC 不通过属于正常现象，解密后 SQLite 仍能正常打开与查询。

## 4. 消息模型（真实库验证）

- 每个会话一张表：`Msg_<md5(会话username)>`；`Name2Id(rowid → user_name)` 用于
  `real_sender_id` 反查发送者。
- `local_type` 高 32 位是标志位，**低 32 位才是消息类型**：
  `1` 文本、`3` 图片、`34` 语音、`43` 视频、`47` 表情、`48` 位置、`42` 名片、
  `49` 应用消息（`<appmsg><type>` 再细分：6 文件、5 链接、57 引用、33 小程序、51 视频号）。
- `WCDB_CT_message_content == 4` 表示内容为 **zstd** 压缩（魔数 `28 B5 2F FD`），
  其余为 UTF-8 明文。
- 图片/文件的资源 md5 在 `packed_info_data`（protobuf）里，而不是 XML 属性里；
  例如图片消息 `1a 28 08 .. 10 .. 22 20 <32位hex md5>`。据此在 attach 树中定位 `.dat`。

## 5. 媒体格式（V2 容器，已破解并验证）

```
魔数        07 08 56 32 08 07
偏移 6      int32le aes_size（实测 1024）
偏移 10     int32le xor_size（= 文件长 - 0x0F - aes_size）
载荷        aes 段（补齐 16 字节）+ 明文中段 + XOR 尾段
AES         AES-128-ECB，去 PKCS#7
AES key     md5(f"{code}{wxid}")[:16] 的 ASCII 字节
code        来自 %APPDATA%\Tencent\xwechat\net\kvcomm\key_<code>_*_input.statistic
XOR key     code & 0xFF
```

实测 19/19 个 `.dat` 成功还原（16 个直接得到 JPEG/PNG，3 个是 HEVC `wxgf`，
需要 ffmpeg 才能出图；工具会保留原文件并在页面提供链接）。

## 6. 复现命令

```powershell
# 环境扫描
python src\wexport\app.py --scan
# 诊断（进程/窗口/候选数量）
python src\wexport\app.py --diag
# 端到端导出（首次需微信已登录）
python src\wexport\app.py --export --account <wxid_xxx_abcd> --out D:\out --formats html,csv
# 手动密钥
python src\wexport\app.py --export --account <wxid_xxx_abcd> --out D:\out --key <64位hex>
```

## 7. 已知边界

- 只在 4.1.15.51 上完整实测；4.0–4.1 的布局一致，但内存特征可能随版本变化。
- 微信必须处于登录运行状态；否则只能用手动密钥或已有缓存。
- 企业微信、3.x 客户端、macOS 不在支持范围。
