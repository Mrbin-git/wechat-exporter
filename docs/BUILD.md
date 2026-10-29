# 构建与打包（BUILD）

本文面向**维护者**：如何从源码构建出可交付的绿色版，以及如何验证产物。
终端用户请看 [USAGE.md](USAGE.md)。

- 产物：`dist\WeChatExport\`（onedir 绿色版）+ `dist\WeChatExport-v1.0.0-win64.zip`
- 构建脚本：`tools\build.ps1`（打包）、`tools\make-zip.ps1`（压缩）、`tools\selftest.ps1`（自检）

---

## 一、构建机要求

| 项目 | 要求 | 说明 |
| --- | --- | --- |
| 操作系统 | Windows 10 / 11 x64 | PyInstaller **不能交叉编译**，Windows 包只能在 Windows 上构建 |
| PowerShell | Windows PowerShell 5.1 **或** PowerShell 7+ | 脚本带 UTF-8 BOM，两者都能直接跑；本文命令以 `powershell.exe` 为例 |
| Python | **3.12 x64**（必须） | 需带 `pip`；`vendor\` 内二进制是 cp312 ABI，脚本遇到非 3.12 解释器会直接报错。可用独立安装的 CPython 3.12，或仓库内 `.venv` |
| 磁盘 | ≥ 2 GB 空闲 | `build\` 中间产物约 200 MB，`dist\` 约 47 MB，`vendor\` 约 300 MB |
| 网络 | 首次构建需要 | 仅用于 `pip install` 依赖；之后可离线重建 |
| 其他 | 无需安装微信 | 构建与 `--scan` 都能在没有微信的机器上跑（只是扫不到账号） |

**目标机（用户机）要求**：Windows 10/11 x64，**不需要** Python、不需要 pip、不需要 VC++ 运行库安装包
（`VCRUNTIME140.dll` / `python312.dll` 已随包分发）。解压即用，可放在 U 盘。

依赖版本（当前锁定，`vendor\` 内已就位）：

```
pyinstaller 6.22.3          pyinstaller-hooks-contrib 2026.8
cryptography 50.0.2         zstandard 0.25.0
openpyxl 3.1.5              pillow 13.0.0
cffi 2.1.1 / pycparser 3.11 / et_xmlfile 2.0.0 / altgraph / pefile / pywin32-ctypes
```

---

## 二、`tools\build.ps1`

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\build.ps1
```

### 参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `-Python <path>` | 自动探测 | 指定解释器。探测顺序：`.venv\Scripts\python.exe` → `PATH` 里的 `python.exe`（跳过 WindowsApps 商店别名）→ `py -3.12` → `%APPDATA%\uv\python\cpython-3.12*`。解析后校验 3.12 与 base prefix：宿主运行时（如 `dsh-runtimes`）会被直接拒绝，避免产物不可复现 |
| `-Out <dir>` | `dist` | 产物根目录，最终生成 `<Out>\WeChatExport\` |
| `-OneFile` | 关闭 | 改用 `--onefile` 打成单文件。**不推荐**：每次启动要自解压到 `%TEMP%`，冷启动 2~5 秒，且部分杀软对单文件自解压更敏感 |

### 脚本做了什么

1. 解析 Python 解释器并打印版本；**校验必须是 3.12**（`vendor\` 是 cp312 ABI）。
2. 用 `build\probe_deps.py` 探测 `PyInstaller` / `cryptography` / `zstandard` / `openpyxl` / `PIL`；
   缺哪个就 `pip install --target vendor` 一次补齐（缓存目录指向 `.tmp\pipcache`，不污染用户 `%LOCALAPPDATA%`）。
3. 调 PyInstaller：`--onedir --console --clean`，附加 3 个前端资源（`ui.html` / `app.js` / `style.css`），
   并显式 `--hidden-import` 六个输出模块 + `--collect-submodules` 三个二进制依赖包。
4. 把 `README.md`、`LICENSE`、`微信聊天记录导出工具使用说明.txt`、`THIRD-PARTY-NOTICES.md` 复制到 exe 同级，
   并把 `docs\*.md` 复制到 `dist\...\docs\`（一次构建即得到完整可交付目录）。
5. 扫描 `build\pyinstaller\*.toc`：出现 `dsh-runtimes`，或出现"本项目目录 / 所选 Python 安装目录"之外的绝对路径时打印警告。
   `.toc` 存的是 Python repr，路径里的反斜杠是**双写**的，断言必须用 `-SimpleMatch`（写单反斜杠正则会全部漏报——这是修过的 bug）。
6. 打印产物路径与总体积，并提示用 `--scan` 自检。

> 注意：`--clean` 会清空 `build\pyinstaller\`，因此**上一次的 `warn-*.txt` 会被覆盖**。想对比警告差异请先备份。

### 产物结构

```
dist\WeChatExport\
├─ WeChatExport.exe              3.6 MB   PyInstaller 引导器（含 .pyz 归档与 PKG）
├─ README.md / LICENSE / 微信聊天记录导出工具使用说明.txt
├─ docs\                                  README 之外的文档（构建时从 docs\ 复制）
└─ _internal\                    约 43 MB
   ├─ base_library.zip           1.3 MB   Python 标准库
   ├─ python312.dll             6.7 MB   来自所选 CPython 3.12
   ├─ python3.dll / VCRUNTIME140*.dll / libffi-8.dll
   ├─ libcrypto-3-x64.dll       7.6 MB   OpenSSL（cryptography 依赖）
   ├─ libssl-3-x64.dll          1.5 MB
   ├─ sqlite3.dll               1.5 MB
   ├─ _ssl.pyd / _hashlib.pyd / _sqlite3.pyd / _ctypes.pyd / ...
   ├─ cryptography\hazmat\bindings\_rust.pyd      9.4 MB   签名/密钥派生
   ├─ zstandard\backend_c.cp312-win_amd64.pyd     513 KB  zstd 解压加密数据库页
   ├─ PIL\_imaging*.pyd                           约 10 MB 头像/图片处理（含 _avif）
   └─ wexport\web\{ui.html,app.js,style.css}              前端静态资源（--add-data）
```

`openpyxl` 不落盘为目录，而是编译进 `WeChatExport.exe` 内的 `PYZ` 归档（189 个模块）。
用 `tomllib`/`pyi-archive_viewer` 可查，**不要**因为 `_internal\` 里看不到 `openpyxl\` 目录就以为漏包。
`numpy` / `pandas` / `lxml` 不再打包（源码未使用，openpyxl 只在可选路径 import 它们），这是体积从 100 MB 级降到 47 MB 的主要原因。

### 交付压缩包

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\make-zip.ps1
# 自定义版本号：
powershell -NoProfile -ExecutionPolicy Bypass -File tools\make-zip.ps1 -Version 1.0.1
# 不带外层目录（解压后文件直接散落）：
powershell -NoProfile -ExecutionPolicy Bypass -File tools\make-zip.ps1 -Flat
```

产物：`dist\WeChatExport-v1.0.0-win64.zip`，含一个顶层 `WeChatExport\` 目录；
压缩前会把根目录的 `README.md`/`LICENSE`/`THIRD-PARTY-NOTICES.md` 同步进去（按修改时间，不覆盖更新的副本）。
用 `System.IO.Compression.ZipArchive` 而非 `Compress-Archive`，避开 2 GB 限制且更快（实测 1.6 秒 / 60 个文件，
压缩后 20.1 MB）。脚本结束会打印 SHA256，并把结果写进交付说明（`dist\SHA256SUMS.txt`）。

### 干净复现（从零开始）

```powershell
# 0) 进入项目根
cd D:\wechat-exporter

# 1) 建虚拟环境（已有的跳过）
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip

# 2) 构建（依赖会自动装进 vendor\，不需要预装）
powershell -NoProfile -ExecutionPolicy Bypass -File tools\build.ps1

# 3) 压缩
powershell -NoProfile -ExecutionPolicy Bypass -File tools\make-zip.ps1

# 4) 自检
powershell -NoProfile -ExecutionPolicy Bypass -File tools\selftest.ps1 -CleanEnv
```

离线重建：保留 `vendor\` 与 `build\pyinstaller\`，在断网状态下直接跑第 2 步即可（探测到依赖齐全就不会调 pip）。

> **别用 DSH 运行时/其它宿主环境的 Python 当 base**：这类解释器的 `.venv` 会把宿主目录写进 PyInstaller 的
> `.toc`，导致换机不可复现（历史上曾把 `dsh-runtimes` 下的 DLL 收进包）。用独立安装的 CPython 3.12，
> 或显式指定：`build.ps1 -Python C:\Python312\python.exe`。构建结束会打印 `.toc` 断言结果。

---

## 三、验证：`tools\selftest.ps1`

```powershell
# 标准验收（模拟干净环境）
powershell -NoProfile -ExecutionPolicy Bypass -File tools\selftest.ps1 -AppDir dist\WeChatExport -Port 18901 -CleanEnv

# 只测 --scan，不测 HTTP
... -SkipServer

# 输出机器可读 JSON（CI 用）
... -Json
```

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `-AppDir` | `dist\WeChatExport` | 被测目录（也可以指向解压后的任意路径，含中文/空格） |
| `-Port` | `18901` | 测试端口；被占用会先报 FAIL 并跳过服务测试 |
| `-CleanEnv` | 关闭 | 把 `PATH` 收窄为 `C:\Windows\System32;C:\Windows`，清空 `PYTHONPATH`/`PYTHONHOME`：模拟“没装 Python 的机器” |
| `-SkipServer` | 关闭 | 跳过 HTTP 部分 |
| `-Json` | 关闭 | 汇总后额外打印 JSON 数组 |
| `-StartTimeoutSec` | 20 | 等待服务就绪的上限 |

检查项（共 17 项）：

1. `--scan`：返回码 0 / 输出可 `ConvertFrom-Json` / 含 `accounts` / 含 `version` / 记录耗时
2. HTTP：端口空闲 / 进程启动 / 20 秒内就绪 / `/api/health` 200 且 `ok:true` / 首页 200 /
   首页含标题「微信聊天记录导出」/ 引用了前端脚本 / 声明 UTF-8 / 可重复响应
3. 清理：进程被杀掉、端口释放
4. `Content-Type` **必须带** `charset=utf-8`（已在 `server.py` 修复）；若回退成 `text/html` 只记 **WARN**，不阻断交付。

退出码：`0` = 全 PASS（允许 WARN）；`1` = 有 FAIL。CI 可直接用退出码判定。

---

## 四、常见打包问题

### 1. 中文路径 / 空格路径

现象：`pip` 报 `UnicodeDecodeError`；或 `pyinstaller` 找不到 `--add-data` 的源文件；或 exe 启动即闪退。

- PyInstaller 6.x + Python 3.12 对中文路径已基本可用，但**依赖链里的老工具**（部分 hook、`setuptools` 旧版）
  仍会按系统 ANSI 代码页解码路径。项目路径含中文时，若构建失败，**优先把整个仓库移到纯 ASCII 路径**
  （如 `D:\build\wechat-exporter`）再构建，不要在源码里改路径处理来绕过。
- 运行时路径不受影响：成品绿色版放在 `D:\portable-test\微信导出 便携版\`（含中文与空格）已验证可用。
- `--add-data` 的分隔符必须是 `;`（Windows），写成 `:` 会在 6.x 上被当成路径的一部分。
- `Start-Process`/脚本里调用 exe **必须加引号**：`& "D:\路径 含空格\WeChatExport.exe" --scan`。

### 2. 编码与 BOM

- `tools\*.ps1` 必须存为 **UTF-8 with BOM**。Windows PowerShell 5.1 在无 BOM 时按系统 ANSI（GBK）解码脚本，
  中文注释/字符串会变成乱码，甚至因全角引号被当成字符串定界符而报
  `The string is missing the terminator`。**本仓库三个脚本都带 BOM，改写时请保持。**
  检查：`[IO.File]::ReadAllBytes('tools\selftest.ps1')[0..2] -join ','` 应为 `239,187,191`。
- `docs\*.md`、`README.md` 不带 BOM（与仓库既有约定一致），保持 UTF-8 无 BOM。
- exe 内部输出统一走 `src\wexport\__main__.py` 的 `_fix_streams()`，把 stdout/stderr 重设为 UTF-8，
  因此控制台中文不会 `UnicodeEncodeError`。但在**非 UTF-8 代码页**的终端里看中文仍可能显示为乱码——
  这是终端的问题，不是程序的；用 `chcp 65001` 或 `[Console]::OutputEncoding=[Text.Encoding]::UTF8`。
- PyInstaller 的 `--console` + 重定向：`--scan` 输出是纯 UTF-8 JSON，重定向到文件时请用
  `Out-File -Encoding UTF8`，否则 5.1 会写成 UTF-16。

### 3. `pip --target`（本项目用 `vendor\`）

- `pip install --target vendor` **不会**安装/升级依赖的依赖？会安装，但**不会去重**：重复执行会在 `vendor\`
  里留下多个版本的 `*.dist-info`，PyInstaller 可能挑到旧的那个。加 `--upgrade` 或在干净目录重装。
- `--target` 装出来的东西**不能直接 `import`**，除非把目录加到 `sys.path`。构建脚本通过
  `$env:PYTHONPATH = "$root\src;$vendor"` 和 `pyinstaller --paths` 解决；手工调试时也要设。
- `--target` 里的 `.pth` 文件不会被处理，`pip` 的 console-scripts 也不会生成 —— 本项目用
  `python -m PyInstaller` 而非 `pyinstaller.exe`，正是为了避开这一点。
- 包含二进制扩展的包（`cryptography`、`zstandard`、`PIL`）从 `--target` 目录加载时，
  **DLL 搜索路径**不会自动带上该目录；PyInstaller 打包后会由引导器处理，但**直接运行源码**时要
  `os.add_dll_directory(vendor)`，否则 `ImportError: DLL load failed`。
- 别把 `vendor\` 加进 git（已在 `.gitignore`），它约 300 MB 且与 Python 小版本绑定。

### 4. 杀毒软件误报

现象：用户下载后被 Defender/360/火绒 报 `Trojan:Win32/Wacatac` 之类，或文件被静默删除。

- 成因：PyInstaller 引导器是“自解压 + 内存加载”，与加壳木马的行为特征重合；**不是**代码有问题。
- 缓解：
  - 保持 **onedir**（本项目默认）。单文件 `--onefile` 误报率高得多。
  - 不要再用 UPX 压缩（本项目的 `build.ps1` 未启用 UPX，**请勿**加 `--upx-dir`）。
  - 交付 zip 并附 SHA256（`make-zip.ps1` 会打印），让用户核对完整性。
  - 有预算就买代码签名证书，对 `WeChatExport.exe` 签名；无签名时在 README 里写明“可能被误报，请加信任”。
  - 可把产物提交微软 [误报申诉](https://www.microsoft.com/en-us/wdsi/filesubmission) 做白名单。
- 构建机上如遇 `PermissionError`/文件被占用，多半是杀软正在扫描刚生成的 DLL，等几秒重跑即可。

### 5. 其他

- **缺 `openpyxl` 症状**：`--formats xlsx` 时抛 `ImportError`，程序集成了 `try/except`，会退化为“不导出 xlsx”。
  验证方法见 [PACKAGING-REPORT.md](PACKAGING-REPORT.md) 第三节。
- **`--scan` 扫不到账号**：程序不读注册表里的微信路径，而是遍历常见根目录（`Documents\WeChat Files`、
  `xwechat_files` 等）。数据放在非默认盘（如 `F:\微信文件`）时，只要目录名匹配就能扫到；否则用
  `--account <folder>` 手动指定。
- **端口占用**：默认 `--port 0` 由系统分配，几乎不会冲突；固定端口调试时若 `OSError: [WinError 10048]`，
  换个端口或先 `Get-NetTCPConnection -LocalPort 18901` 找占用者。
- **`--clean` 后构建变慢**：首次会重新分析全部依赖（约 1~2 分钟）；日常迭代可临时去掉 `--clean`。
- **受限沙箱内重建报 `Access denied`**：`vendor\` 里个别目录（`openpyxl`、`PIL`、`et_xmlfile` 及其 dist-info）
  的 DACL 关闭了继承，受限令牌读不到，PyInstaller 在收集模块阶段就会失败。普通用户/管理员构建不受影响
  （目录所有者就是当前用户）；在受限环境里，用完整权限跑一次构建，或删除并重装这几个依赖
  （新目录会继承正常权限）。详见 [LIMITATIONS.md](LIMITATIONS.md)。
