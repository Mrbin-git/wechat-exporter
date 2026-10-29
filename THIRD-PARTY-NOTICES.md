# 第三方组件声明（THIRD-PARTY-NOTICES）

**微信聊天记录导出（WeChatExport）1.0.0** 本身以 MIT 许可发布（见 [LICENSE](LICENSE)）。但它的运行与打包依赖若干第三方开源组件，这些组件各自保留自己的版权与许可证，本文件逐一列出。

> 发行包中的 `_internal\` 目录（或 `vendor\` 目录）包含这些组件的实际文件与完整许可证文本（通常在 `<包名>.dist-info\LICENSE*` 或包目录下的 `LICENSE` 文件中）。本文件是摘要，若摘要与随包许可证原文冲突，以原文为准。

---

## 一、总览

| 组件 | 版本（参考） | 许可证 | 在本项目中的用途 | 是否随 exe 分发 |
| --- | --- | --- | --- | --- |
| cryptography | 4x | **Apache-2.0**（或 BSD-3-Clause，双许可） | AES-256-CBC / AES-128-ECB 解密，SQLCipher 页解密与 `.dat` 媒体还原 | 是 |
| zstandard | 0.2x | **BSD-3-Clause** | 解压微信消息内容（`WCDB_CT_message_content = 4` 时内容为 zstd 压缩） | 是 |
| openpyxl | 3.1.x | **MIT** | 生成 XLSX 导出文件 | 是 |
| Pillow | 13.x | **HPND**（Historical Permission Notice and Disclaimer） | 打包链依赖（图像处理库）；媒体还原以字节级处理为主，未做有损转码 | 是 |
| PyInstaller | 6.x | **GPL-2.0-or-later，附 bootloader 例外条款** | 把 Python 程序与依赖打包成免安装 exe | 打包工具 + 其 bootloader 运行时 |
| et_xmlfile | 2.0.0 | **MIT** | openpyxl 的 XML 读写依赖 | 是（随 openpyxl 引入） |
| Python 运行时 | 3.12.x | **PSF-2.0** | PyInstaller 打包进发行包的解释器与标准库（含 `sqlite3`、`hashlib`、`http.server`） | 是 |
| pyinstaller-hooks-contrib | 2025.x | 见包内 `LICENSE`（PyInstaller 生态的 hook 集合） | PyInstaller 打包时用到的第三方库 hook | 打包阶段 |

许可证兼容性说明：MIT（本项目）与上表中的 Apache-2.0、BSD-3-Clause、MIT、HPND、PSF-2.0 均兼容；PyInstaller 的 GPL 例外条款明确允许将**打包产物**以任意许可分发（见第三节），因此本项目可以 MIT 发布二进制包。

---

## 二、逐项说明

### 1. cryptography — Apache-2.0

- **用途**：所有密码学运算的唯一来源。具体包括
  - SQLCipher 4 数据库逐页解密：`AES-256-CBC`，IV 取自页尾，见 `src/wexport/core/sqlcipher.py`；
  - 微信图片 V2 容器解密：`AES-128-ECB`，见 `src/wexport/core/media.py`。
- **说明**：本项目**不自行实现** AES 或任何密码学原语，全部委托给 cryptography。密钥派生使用 Python 标准库的 `hashlib.pbkdf2_hmac` 与 `hmac`。
- **许可义务**：Apache-2.0 要求保留版权声明、许可证副本，并对修改过的文件作出说明。本项目未修改 cryptography 源码，仅以库的形式调用；`vendor\cryptography\` 与 `*.dist-info\` 中的原始声明应随分发保留。
- **上游**：https://github.com/pyca/cryptography

### 2. zstandard — BSD-3-Clause

- **用途**：微信 4.x 的消息内容列在某些情况下以 zstd 压缩存储（内容类型标记为 4）。`src/wexport/core/reader.py` 的 `decode_content()` 用 `ZstdDecompressor().decompress()` 还原为文本，单条上限 128 MB。
- **许可义务**：BSD-3-Clause 要求保留版权声明、条件列表与免责声明，且**不得使用作者名称为衍生产品背书**。本项目未修改其源码。
- **上游**：https://github.com/facebook/zstd （Python 绑定：https://github.com/indygreg/python-zstandard）

### 3. openpyxl — MIT

- **用途**：生成 `.xlsx` 导出文件，见 `src/wexport/export/xlsx_export.py`（表头加粗、列宽、冻结首行）。若打包环境中缺少 openpyxl，`write_chat()` 会返回 `None` 而不会让整个导出失败。
- **许可义务**：MIT，保留版权与许可声明即可。
- **上游**：https://foss.heptapod.net/openpyxl/openpyxl

### 4. Pillow — HPND

- **用途**：本项目对图片**不做重新编码**——解密出来的原始字节按魔术数字判定扩展名后直接落盘（`src/wexport/core/media.py` 的 `_sniff_ext`）。Pillow 属于构建期依赖列表的一部分（`tools/build.ps1` 会安装并打包它），以保证依赖链完整、便于后续扩展。
- **许可义务**：HPND 是一种宽松许可，允许使用、复制、修改、分发，条件是保留版权声明与许可声明，且**不得使用作者名称背书**。Pillow 的二进制轮子还捆绑了若干图像编解码库（如 libjpeg-turbo、zlib、libtiff、FreeType），它们各自带有 IJG、zlib、libtiff、FTL 等许可；完整清单见 Pillow 发行包中的 `LICENSE` 文件与 Pillow 官方文档的 "License" 章节。
- **上游**：https://python-pillow.org/

### 5. PyInstaller — GPL-2.0-or-later with bootloader exception

- **用途**：`tools/build.ps1` 调用 PyInstaller 把 `src\wexport\__main__.py` 及其依赖打包成 `WeChatExport.exe`（默认 `--onedir`，可选 `--onefile`）。
- **为什么 MIT 项目可以用 GPL 工具打包**：PyInstaller 的许可证包含一条**明确的例外条款**（Bootloader Exception）。该例外允许将 PyInstaller 的 bootloader 与打包出的应用一起分发，且**不要求打包产物本身采用 GPL**。换言之，GPL 约束的是 PyInstaller 自身的修改与分发，而不是你的程序。这是 PyInstaller 被大量商业与闭源项目使用的原因。
- **仍需履行的义务**：分发产物时应一并提供 PyInstaller 的许可证文本或其获取方式（本文件与随包 `LICENSE` 即为履行方式之一）。如你修改了 PyInstaller 的 bootloader 源码，则修改部分需按 GPL-2.0-or-later 开放。
- **上游**：https://pyinstaller.org/

### 6. et_xmlfile — MIT

- **用途**：openpyxl 的底层 XML 写入依赖，随 openpyxl 自动引入。
- **备注**：无独立运行时行为，仅在生成 xlsx 时被间接调用。

### 7. Python 运行时 — PSF-2.0

- **用途**：PyInstaller 会把 Python 解释器与标准库打包进发行包（`_internal\` 下的 `python312.dll`、`base_library.zip` 等），因此目标机器无需安装 Python。本项目重度依赖的标准库模块包括：`sqlite3`（读取解密后的数据库）、`hashlib`/`hmac`（PBKDF2 与页校验）、`http.server`（本地界面服务）、`ctypes`（调用 Windows API：注册表、DPAPI、进程内存、Toolhelp 快照）、`csv`/`json`（导出）。
- **许可义务**：PSF License Agreement 要求保留版权声明与许可文本，随包附带即可。
- **上游**：https://www.python.org/

### 8. pyinstaller-hooks-contrib

- **用途**：为 `cryptography`、`openpyxl` 等库提供 PyInstaller 的收集规则（哪些隐藏导入、数据文件、二进制要一起打包）。仅在**构建阶段**起作用，本文不臆测其具体许可条款，请以 `vendor\_pyinstaller_hooks_contrib\` 内随附的 `LICENSE` 文件为准。
- **上游**：https://github.com/pyinstaller/pyinstaller-hooks-contrib

---

## 三、没有使用的东西（澄清）

为避免误解，明确列出**本工具不包含**的组件与能力：

- 没有使用任何 SQLCipher 官方库或 WCDB 库——页格式由本项目用 cryptography + 标准库自行实现；
- 没有使用 `pycryptodome`、`pycrypto`、`openssl` 命令行工具；
- 没有使用 `pywin32`、`comtypes` ——所有 Windows API 调用通过标准库 `ctypes` 直接完成；
- 没有使用任何网络库（`requests`、`urllib3`、`aiohttp`）、遥测 SDK、崩溃上报 SDK；
- 没有使用任何 UI 框架（Qt、Electron、Tkinter）——界面是标准库 `http.server` 提供的本地网页；
- 没有内置 ffmpeg、SILK 解码器或 HEVC 解码器。

---

## 四、合规提示

如果你是**再次分发**本工具（例如放在公司内网、打包进你自己的产品、或作为服务的一部分提供），请注意：

1. 保留本文件与 [LICENSE](LICENSE)，以及 `_internal\`/`vendor\` 中各组件自带的许可证文本；
2. Apache-2.0 组件（cryptography）若被你修改，需在修改的文件中注明；
3. BSD-3-Clause 与 HPND 组件不得用其作者名义为你的分发背书；
4. 不得移除任何版权声明；
5. 若你以**收费**方式分发，请自行复核各许可证对商业使用的要求（上述许可证均允许商业使用，但保留声明义务不变）。

如对某一条款有疑问，请查阅对应项目的官方许可证原文，或咨询法律专业人士。本文件不构成法律意见。

---

*本文件最后更新：2026-10-20 · 对应 WeChatExport 1.0.0*
