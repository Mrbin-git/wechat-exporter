# 媒体格式说明（V2 容器 / 密钥派生 / wxgf）

本文讲清楚三件事：微信 4.x 的图片 `.dat` 文件长什么样、密钥是怎么从本机推出来的、为什么有些图导出来是 `.wxgf` 而浏览器打不开。文末给了可复现的验证步骤。

> 面向**想自己核对**的用户或开发者。只想导出的普通用户可以跳过本文。
> 文中出现的账号名、密钥一律为**占位示例**，不包含任何真实数据。

---

## 一、V2 容器结构

微信在 `msg\attach\` 下按 MD5 命名的 `.dat` 文件，是一条消息图片的**加密容器**。实测（微信 4.1.15.51，Windows）结构如下：

```
偏移      长度      内容
0x00      6        魔术字节  07 08 56 32 08 07     ← "V2"
0x06      4        int32 小端  aes_size             ← 实测 1024
0x0A      4        int32 小端  xor_size             ← file_size - 0x0F - aes_size
0x0E      1        保留（0x00）
0x0F      ...      payload
```

`payload` 由三段拼成：

```
┌─────────────────────────┬──────────────────────┬────────────────┐
│  aes_block（按 16 对齐） │  raw_middle（明文）  │ xor_tail       │
│  AES-128-ECB 加密        │  原样未加密          │ 逐字节 XOR     │
└─────────────────────────┴──────────────────────┴────────────────┘
^                         ^                      ^
0                         aligned                len - xor_size
```

其中：

- `aligned = ceil(aes_size / 16) * 16`，即把 `aes_size` 向上取整到 16 的倍数；
- `raw_middle` 的长度 = `len(payload) - aligned - xor_size`，通常为 0（图片主要数据都在 AES 段）；
- `xor_size` 一般就是文件尾部被 XOR 的那一小段。

### 解密步骤

1. 校验魔术字节为 `07 08 56 32 08 07`；
2. 从偏移 6 和 10 读出 `aes_size`、`xor_size`，任一为负或两者之和超过 `payload` 长度则判定头部异常、放弃；
3. 对 `payload[0 : aligned]` 做 **AES-128-ECB 解密**；
4. 手动去掉 PKCS#7 填充（若末尾是 `n` 个 `n` 字节且 `1 ≤ n ≤ 16`），再截断到 `aes_size` 字节；
5. 拼上 `raw_middle`；
6. 把 `xor_tail` 的每个字节与 `xor_key` 异或，拼到末尾。

结果就是一张标准图片。扩展名不靠猜，靠**魔术数字嗅探**：

| 文件头 | 扩展名 |
| --- | --- |
| `FF D8 FF` | `.jpg` |
| `89 50 4E 47` | `.png` |
| `47 49 46 38`（`GIF8`） | `.gif` |
| `52 49 46 46`（`RIFF`） | `.webp` |
| `42 4D`（`BM`） | `.bmp` |
| `77 78 67 66`（`wxgf`） | `.wxgf`（见第五节） |
| 其他 | `.bin` |

只有嗅探出上述图片签名，才认为解密成功；否则换下一个候选密钥重试。这条"输出必须是合法图片"的约束，本身就排除了绝大多数错误的密钥。

---

## 二、密钥派生（不需要读进程内存）

媒体密钥和数据库密钥是**两套独立**的机制。媒体这一套完全来自磁盘上的一个文件名，不碰进程内存，也不碰 `keys.dat`。

### 2.1 code 从哪来

```
%APPDATA%\Tencent\xwechat\net\kvcomm\key_<code>_*_input.statistic
```

例如文件名是 `key_1234567890_abcdef_input.statistic`，那么 `code = 1234567890`。

- 作用范围：**每个安装/每个用户**一份，通常只有一个；
- 目录里可能同时存在多个不同 `code` 的文件（历史遗留），所以实现上会把**所有**候选 code 都收集起来（`media.find_codes()`）；
- 兼容路径：同时也会看 `%APPDATA%\Tencent\WeChat\net\kvcomm`（老目录名）。

### 2.2 两个密钥怎么算

设账号的 wxid 为 `wxid_example_9f3a`。**先去掉目录名尾部的 4 位十六进制后缀**（`_[0-9a-f]{4}`），得到 `wxid_example`。

```python
aes_key = md5( f"{code}{wxid}" )[:16]      # 取十六进制摘要的前 16 个字符
aes_key = aes_key.encode("ascii")          # → 16 字节 ASCII，正好是 AES-128 的密钥长度
xor_key = code & 0xFF                      # 只取最低一个字节，0..255
```

两个细节容易踩坑：

- `aes_key` 是**十六进制字符串的前 16 个字符**（例如 `"3f9a1c7e5b2d8046"`），直接当 ASCII 字节用，**不是**把十六进制解码成 8 字节再补零；
- 参与拼接的 `wxid` 是**去掉后缀的纯 wxid**，不含 `_9f3a` 这类安装后缀。

### 2.3 时机与稳定性

- 这些文件是微信客户端自己写入的统计文件，工具只读不写；
- `code` 在同一个客户端安装下通常长期不变，所以媒体密钥可以稳定复用；
- 换电脑、重装微信后 `code` 可能变化；旧 `code` 解不出新图片，此时把所有候选都试一遍仍失败，就说明 `code` 确实变了。

---

## 三、文件怎么对应到消息

一条图片消息在数据库里带两个 md5：

1. XML 里 `<img md5="..." />` 的 md5（可能为空）；
2. `packed_info_data` 里的 32 字符 md5 —— **这个才是磁盘上的资源名**。

读取时优先用第 2 个（`reader.py` 里写入 `extras["md5"]`），再到 `msg\attach\` 下按 md5 找文件：

```
msg\attach\<md5>.dat       原图
msg\attach\<md5>_h.dat     高清/大图变体
msg\attach\<md5>_t.dat     缩略图
```

工具会把这些变体都解出来放进导出目录的 `media\<会话名>\`，HTML 引用其中**体积最大**的那张（通常是原图）。这就是为什么导出的图片文件名是一长串十六进制——那是资源 md5，不含任何聊天内容。

---

## 四、V1 容器（旧格式）

微信 3.x/早期 4.x 的容器魔术字节是：

```
07 08 56 31 08 07      ← "... V1 ..."
```

本工具能**识别**它（分类为 `v1`），但**不做解密**——V1 的密钥派生与分段方式不同，且不在 1.0.0 的支持范围内。遇到 V1 文件会被跳过，计入统计里的 `skipped`，不会产生损坏的图片文件。判断依据就是文件头 4 字节是 `07 08 56 32`（V2）还是 `07 08 56 31`（V1）。

---

## 五、`.wxgf` 与 HEVC 限制

### 5.1 现象

部分图片解密、XOR 还原都成功后，文件头既不是 JPEG 也不是 PNG，而是 ASCII 的 **`wxgf`**。这是微信自有的封装容器，内部通常是 **HEVC（H.265）** 编码的图像数据。

### 5.2 本工具的处理

- **原样保留**：把解密后的完整字节写成 `<md5>.wxgf` 放到 `media\<会话名>\`；
- **不谎报成功**：不把它改名成 `.jpg`，避免用户以为"图片坏了"；
- **HTML 中给出可点击链接**，而不是尝试内嵌显示。因为主流浏览器（Chrome/Edge/Firefox）都不支持在 `<img>` 里直接渲染 HEVC，内嵌会得到一个破图；
- **不内置解码器**：项目没有捆绑 HEVC 解码库，也没有调用任何外部转码器。

### 5.3 你能做什么

- 用系统里支持 HEVC 的方案打开：Windows 上的「HEVC 视频扩展」+ 支持该编码的看图/播放软件，或 PotPlayer、VLC 一类自带解码器的播放器；
- 或者自己用 **ffmpeg** 转码后查看，例如：

  ```powershell
  ffmpeg -i "<md5>.wxgf" -frames:v 1 "output.png"
  ```

  这属于工具之外的自行加工，本工具不会代你执行，也不对 `wxgf` 内部究竟是否为标准 HEVC 码流作保证。若 ffmpeg 报"无法识别格式"，说明该文件的封装还需要额外的解包步骤。

> 实测样本中，19 张图片里能解出标准图片的 **19/19 全部还原成功**；`.wxgf` 属于少数情况，多见于高效率压缩的大图。

---

## 六、语音与视频

### 语音

- 微信桌面端**默认不把语音落地**，`msg\voice\` 往往是空的；这是客户端行为，不是导出失败；
- 若目录里按 md5 找到了文件，工具会复制原始文件（扩展名保持原样）；
- 内容通常是 **SILK v3**（文件头 `#!SILK_V3`）。工具只做了**格式识别**，**不做音频解码**，因此不会产出 `.wav`/`.mp3`；
- 页面在这种情况下显示"未找到本地文件"或给出原始文件链接，不会伪造一条空音频。

### 视频

- `msg\video\` 下若已有 `.mp4`，直接按原样复制；
- 若是加密容器（`.dat`），按媒体流程尝试解密，失败则跳过；
- HTML 里视频以链接形式给出，不做在线播放器。

---

## 七、复现与自查步骤

### 7.1 用工具自带函数验证（推荐）

在项目根目录执行（需要 Python 3.11+ 与 `cryptography`；或用打包机上的 `.venv`）：

```python
# check_media.py
import sys
from pathlib import Path

sys.path.insert(0, "src")
sys.path.insert(0, "vendor")
from wexport.core import media

ACC  = Path(r"D:\WeChat\xwechat_files\wxid_example_9f3a")   # ← 换成你自己的账号目录
WXID = "wxid_example"                                          # ← 去掉 _9f3a 后缀
OUT  = Path("media_check")

print("kvcomm codes:", media.find_codes())
keys = media.resolve_keys(ACC, WXID)
for k in keys:
    print(f"code={k.code} aes_key={k.aes_key!r} xor_key=0x{k.xor_key:02x}")

ok = bad = 0
for p in sorted((ACC / "msg" / "attach").rglob("*.dat")):
    fmt = media.detect_format(p)
    out = media.decrypt_media(p, OUT, keys)
    print(("OK  " if out else "BAD "), p.name[:40], fmt, "->", out.name if out else "-")
    ok, bad = (ok + 1, bad) if out else (ok, bad + 1)
print(f"summary: {ok} ok / {bad} failed")
```

判断标准很直观：`OK` 的行会写出真正的图片文件，直接双击能打开。

### 7.2 手工验证一个文件（不依赖本项目代码）

想独立复核格式的话，下面这段只用了标准库 + `cryptography`：

```python
import hashlib, struct, sys
from pathlib import Path
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

dat  = Path(sys.argv[1])
code = int(sys.argv[2])       # kvcomm 文件名里的数字
wxid = sys.argv[3]            # 去掉 _xxxx 后缀

data = dat.read_bytes()
assert data[:6] == bytes.fromhex("070856320807"), "不是 V2 容器"

aes_size = struct.unpack_from("<i", data, 6)[0]
xor_size = struct.unpack_from("<i", data, 10)[0]
aligned  = (aes_size + 15) // 16 * 16
payload  = data[0x0F:]

aes_key = hashlib.md5(f"{code}{wxid}".encode()).hexdigest()[:16].encode("ascii")
xor_key = code & 0xFF

dec = Cipher(algorithms.AES(aes_key), modes.ECB()).decryptor()
plain = dec.update(payload[:aligned]) + dec.finalize()

pad = plain[-1]
if 0 < pad <= 16 and plain[-pad:] == bytes([pad]) * pad:
    plain = plain[:-pad]
plain = plain[:aes_size]

raw = payload[aligned: len(payload) - xor_size]
tail = bytes(b ^ xor_key for b in payload[len(payload) - xor_size:])

out = plain + raw + tail
print("first16:", out[:16].hex())
Path("out.bin").write_bytes(out)
```

打开 `out.bin` 看头几个字节：`ffd8ff` 是 JPEG，`89504e47` 是 PNG。能对上就说明抄对了。

### 7.3 常见失败原因对照

| 现象 | 原因 |
| --- | --- |
| 所有文件都 `BAD`，code 有值 | `code` 与账号不匹配，或 wxid 后缀没去掉 |
| 只有一部分 `BAD` | 该图本机只有缩略图/未落地，或原文件在 `_t`/`_h` 变体之外 |
| `aes_size` 为负或超长 | 文件头损坏，或根本不是 V2（可能是 V1） |
| 解密后头是 `wxgf` | 正常，HEVC 容器，见第五节 |
| 解密后头是乱码 | `aes_key` 算错了（常见：把 hex 解码成 8 字节而不是用 16 个 ASCII 字符） |
| `xor_size` 大于文件长度 | 文件被截断（下载未完成/被抓取工具改写过） |

---

## 八、隐私提醒

复现过程中会产出**明文图片**和**明文密钥**：

- `code` 与 `aes_key` 等同于你账号的媒体解密能力，**不要截图发到任何地方**（包括工单、群聊、论坛）；
- 复现脚本请在自己电脑上跑，用完删掉生成的 `media_check\`、`out.bin`；
- 任何"帮你远程看日志"的人，都不需要你的真实 wxid、code 或密钥。
