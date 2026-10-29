# 打包验证报告（PACKAGING-REPORT）

- 版本：`WeChatExport 1.0.0`（`src\wexport\__init__.py`）
- 首次验证：2026-10-20；**本次重建复核：2026-10-26**；**默认解释器重建 + 退出码复核：2026-10-27**
- 验证人：A4-打包工程师（构建脚本与文档修订后，由自动化流程重建并复核）
- 被测产物：`dist\WeChatExport\`（onedir，**60 个文件 / 46.55 MB**，exe 3,767,327 B）
- 交付压缩包：`dist\WeChatExport-v1.0.0-win64.zip`；**大小与 SHA256 见同目录 `dist\SHA256SUMS.txt`**
  （报告本身在压缩包内，写死自身哈希会自引用，故不在正文记录）
- 结论：**通过**（自检 17 项：PASS 17 / WARN 0 / FAIL 0）。前次遗留的构建可复现性问题（DSH 运行时依赖）与 `Content-Type` 缺 charset 问题均已修复，见第五节。

> 与 2026-10-20 版的差异摘要：改用独立安装的 CPython 3.12.14 构建，产物从 100.7 MB / 751 文件降到
> 46.55 MB / 60 文件（不再把未使用的 numpy/pandas/lxml 收进包）；`build.ps1` 新增解释器版本守卫与
> `.toc` 绝对路径断言；文档随构建自动复制进 `dist\...\docs\`。

---

## 一、环境与路径说明

| 项目 | 实际情况 |
| --- | --- |
| 项目根 | `D:\wechat-exporter` |
| 构建解释器 | 独立 CPython **3.12.14**（uv 管理，脚本默认探测到 `%APPDATA%\uv\python\cpython-3.12-windows-x86_64-none\python.exe`），非 DSH 运行时 |
| 构建依赖 | 仓库内 `vendor\`（PyInstaller 6.22.3、cryptography 50.0.2、zstandard 0.25.0、openpyxl 3.1.5、Pillow 13.0.0），cp312 ABI |
| 便携测试目录 | 任务指定的 `D:\portable-test\` 会被沙箱拒绝写入，按预案回退到 `dist\portable-test\微信导出 便携版\`（含中文+空格，难度等价） |
| Shell | 本机只有 Windows PowerShell **5.1**（`5.1.26100.9711`）；脚本按 5.1 兼容编写，且必须带 UTF-8 BOM |
| 目标机依赖 | 无 Python、无 VC++ 运行库安装包（`VCRUNTIME140.dll`、`python312.dll` 随包分发） |

干净环境启动方式（模拟"没装 Python 的机器"）：

```powershell
$env:PATH='C:\Windows\System32;C:\Windows'
$env:PYTHONPATH=''
$env:PYTHONHOME=''
& 'D:\wechat-exporter\dist\WeChatExport\WeChatExport.exe' --scan
```

---

## 二、干净环境功能验证

### 2.1 `--scan`

| 断言 | 结果 | 证据 |
| --- | --- | --- |
| 返回码 = 0 | **PASS** | `exit=0`（3 次实测均为 0） |
| 输出是合法 JSON | **PASS** | `ConvertFrom-Json` 成功，804 字符 |
| 含 `accounts` 字段 | **PASS** | 2 个账号（`wxid_example_9f3a`、`wxid_sample_2b7c`） |
| 含 `version` 字段 | **PASS** | `4.1.15.51`（微信版本，非程序版本） |

输出摘要（已脱敏；只探测目录/数据库文件，不读取任何聊天内容）：

```json
{
  "version": "4.1.15.51",
  "install_dir": "E:\\Program Files (x86)\\Tencent\\Weixin",
  "running": false,
  "roots": ["F:\\微信文件\\xwechat_files", "F:\\微信文件\\WeChat Files", "F:\\微信文件"],
  "accounts": [
    {"wxid": "wxid_example", "folder": "wxid_example_9f3a", "message_dbs": 3},
    {"wxid": "wxid_sample", "folder": "wxid_sample_2b7c", "message_dbs": 8}
  ],
  "warnings": []
}
```

附加验证：把 `C:\Windows` 当工作目录、干净环境变量下运行 `--scan`，结果同为 `EXIT=0 / accounts=2`，
证明 `_internal` 按 exe 相对路径定位，与当前目录无关。

### 2.2 HTTP 服务

| 检查 | 结果 | 证据 |
| --- | --- | --- |
| 服务就绪（18912 端口） | **PASS** | 启动到 `/api/health` 可访问 **0.569 s** |
| `GET /api/health` = 200 | **PASS** | `status=200`，`{"ok": true}` |
| `GET /` = 200 | **PASS** | `status=200`，4018 字节 |
| 首页含「微信聊天记录导出」 | **PASS** | `<title>微信聊天记录导出</title>` |
| 首页引用前端脚本 | **PASS** | `<script ... app.js>` |
| 页面声明 UTF-8 | **PASS** | `<meta charset="utf-8">` |
| 响应头 `Content-Type` | **PASS** | `text/html; charset=utf-8`（前次为 WARN，已在 `server.py` 修复） |
| 连续请求不崩 | **PASS** | 二次 health 仍 200 |
| 进程清理 / 端口释放 | **PASS** | `Stop-Process` 后 `Get-NetTCPConnection -LocalPort 18912` 为 0 条 |

---

## 三、冷启动耗时（2026-10-26 实测）

测量口径：`--scan` 完整进程生命周期，同机、文件缓存已预热（首次运行会额外加 50~100 ms）。

| # | 墙钟耗时 | 返回码 |
| --- | --- | --- |
| 1 | 244 ms | 0 |
| 2 | 151 ms | 0 |
| 3 | 160 ms | 0 |
| **中位数** | **160 ms** | — |

- HTTP 服务从进程启动到 `/api/health` 可访问：**0.57 s**。
- 用户体感：双击到浏览器出界面约 0.6~1.0 s（含浏览器自身启动）。
- 前次 100.7 MB 产物的中位数是 173 ms，本次精简包略快，量级不变。

---

## 四、依赖完整性与构建路径

### 4.1 关键二进制

| 依赖 | 关键文件 | 大小 | 结论 |
| --- | --- | --- | --- |
| cryptography | `_internal\cryptography\hazmat\bindings\_rust.pyd` | 9,897,472 B | 在包内 |
| （同上，OpenSSL） | `_internal\libcrypto-3-x64.dll` / `libssl-3-x64.dll` | 7,987,712 / 1,583,616 B | 在包内 |
| Python 运行时 | `_internal\python312.dll` / `VCRUNTIME140(_1).dll` | 6,969,856 / 124,544 / 49,792 B | 在包内，来自所选 CPython 3.12.14 |
| zstandard | `zstandard\backend_c.cp312-win_amd64.pyd`、`zstandard\_cffi.cp312-win_amd64.pyd`、`_cffi_backend.cp312-win_amd64.pyd` | 约 513 KB / 652 KB / 177 KB | 在包内 |
| Pillow | `PIL\_imaging*.pyd`（imaging / avif / webp / imagingcms / imagingtk） | 约 2.6 MB / 7.9 MB / 406 KB … | 在包内 |
| openpyxl | **不在 `_internal\`**，编译进 exe 内 `PYZ` 归档，**189 个模块条目** | — | 在包内 |
| 前端资源 | `_internal\wexport\web\{ui.html 4678, app.js 10573, style.css 3701}` | — | 在包内（`--add-data` 生效） |
| numpy / pandas / lxml | **不再打包** | — | 源码未 `import numpy`，openpyxl 仅在可选路径用；去掉后体积减半 |

`openpyxl` 的核实方法（避免"目录不存在 = 漏包"的误判）：

```powershell
$env:PYTHONPATH="$PWD\vendor"
# 用构建时同一个 Python：
python -m PyInstaller.utils.cliutils.archive_viewer -r -b dist\WeChatExport\WeChatExport.exe
# → PYZ-00.toc 中 openpyxl.* 189 条、cryptography.* 77 条、PIL.* 74 条、wexport.* 18 条
```

`PYZ-00.toc` 实测模块数：openpyxl **189**、cryptography 77、PIL 74、zstandard 1、sqlite3 3、wexport 18。
`warn-WeChatExport.txt` 共 34 条 `missing module named ...`，全部是可选依赖（numpy/pandas/lxml/defusedxml），
与运行时行为无关。

### 4.2 构建机绝对路径 / `dsh-runtimes` 残留

| 检查 | 结果 |
| --- | --- |
| `build\pyinstaller\WeChatExport\*.toc` 中的 `dsh-runtimes` | **0 处**（前次 3174 处，根因已消除） |
| `.toc` 中的绝对路径总数 | **1397 条**，全部指向本次所用的 CPython 3.12.14 安装目录（575 条）或项目根（822 条） |
| exe 内 `dsh-runtimes` / `C:\Users\user` | 各 **0 处**（二进制字符串检索） |
| `_internal` 文本文件中的构建机路径 | **0 处** |
| 文档文本中的 `dsh-runtimes` 字样 | 仅本报告自述此检查项时出现，非产物残留 |

> **勘误**：2026-10-20 版报告称"toc 中未发现构建机绝对路径"，实际是断言正则的 bug——
> `.toc` 存的是 Python repr，Windows 路径里的反斜杠是**双写**的（`C:\\Users\\...`），
> 而当时用的正则只匹配单反斜杠，于是 1397 条全部漏报。`build.ps1` 已改为 `-SimpleMatch` +
> 按"是否属于所选解释器/构建目录"分类的判定，本次输出：
> `可复现性检查: 绝对路径 1397 条，均指向所选 Python 安装目录与本次构建目录；无 dsh-runtimes 引用`。

---

## 五、发现的问题与处理状态

> 本次修订未改动 `src\` 业务逻辑；两处源码级修复（`server.py` 的 charset、`selftest.ps1` 的提示语）在本次之前已完成。

### 问题 1（已解决）构建链隐式依赖 DSH 运行时目录

- **前情**：`.venv` 以 DSH 运行时为 base，`build\pyinstaller\*.toc` 引用 `C:\Users\user\.dsh\dsh-runtimes\…`
  下的 `VCRUNTIME140.dll`、`python312.dll`、numpy hook。
- **处理**：
  1. `build.ps1` 的 `Resolve-Python` 增加 `py -3.12` 兜底与"解释器 ≠ 3.12 且 `vendor\` 存在则直接报错"的守卫；
  2. 本次用独立 CPython 3.12.14 构建（`-Python` 显式传入），toc 中的 `dsh-runtimes` 归零；
  3. 构建结束后自动扫描 `.toc`：出现 `dsh-runtimes` 或"构建目录/Python 之外的绝对路径"时打印警告并列出来源。
- **残留风险**：`.toc` 仍记录 1397 条构建机绝对路径（解释器安装目录 + 项目根），这是 PyInstaller 的正常行为，
  不影响产物运行；换机重建只要用同版本 CPython 3.12 即可复现。

### 问题 2（已解决）首页响应头缺 `charset=utf-8`

- `server.py` 的 `_file()` 对 `text/*` 追加 `; charset=utf-8`；`selftest.ps1` 相应改为条件化判定
  （PASS 时只显示实际值，不再无条件提示"建议补 charset"）。本次实测 `text/html; charset=utf-8`。

### 问题 3（提示，仍成立）`openpyxl` 不在 `_internal\`

- 189 个模块在 exe 的 PYZ 归档里，`_internal\` 看不到属正常。`docs\BUILD.md` 已写核查命令。

### 问题 4（环境/流程，非产品缺陷）

- **`D:\portable-test` 不可写**：沙箱只允许写会话工作区，已回退到 `dist\portable-test\`。
- **`taskkill /F` 在本会话偶发 `Access denied`**：`selftest.ps1` 已改为优先 `Stop-Process`。
- **PowerShell 5.1 的两个坑**：脚本必须带 UTF-8 BOM（本次用 `edit` 改完 `build.ps1` 后已补回并用
  `Parser::ParseFile` 验证 `parseErrors=0`）；全角引号会被当字符串定界符，脚本里一律用单引号包裹。
- **`vendor\` 六个目录权限异常**：`openpyxl`、`openpyxl-3.1.5.dist-info`、`PIL`、`pillow-13.0.0.dist-info`、
  `et_xmlfile`、`et_xmlfile-2.0.0.dist-info` 的 DACL 关闭了继承、且没有沙箱令牌对应的 ACE，
  受限模式下读取报 `Access denied`（普通用户构建不受影响，因为目录所有者就是当前用户）。
  本次用完整权限执行打包命令完成重建；在受限沙箱里重建时，重装一遍 `vendor\`（新目录会继承正常权限）即可。
  已把该现象记入 `docs\LIMITATIONS.md`，避免后续维护者误判为打包脚本 bug。

---

## 六、交付脚本

| 脚本 | 用途 | 本次验证 |
| --- | --- | --- |
| `tools\build.ps1` | 构建 onedir 产物；自动复制 docs；`.toc` 断言 | 已跑通，**退出码 0**，输出 `46.55 MB / 60 文件`，断言输出见 4.2 |
| `tools\make-zip.ps1` | 打 `dist\WeChatExport-v1.0.0-win64.zip`（自动同步最新 docs） | 已跑通，见 `dist\SHA256SUMS.txt` |
| `tools\selftest.ps1` | 17 项自检，PASS/WARN/FAIL 汇总，非零退出码表示失败 | 已跑通：PASS 17 / WARN 0 / FAIL 0 |

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\build.ps1 -Python <CPython 3.12 路径>
powershell -NoProfile -ExecutionPolicy Bypass -File tools\make-zip.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File tools\selftest.ps1 -AppDir "dist\portable-test\微信导出 便携版" -Port 18901 -CleanEnv
```

`selftest.ps1` 本次输出（节选）：

```
[PASS] scan 返回码为 0 - exit=0
[PASS] scan 输出为合法 JSON - 长度 804 字符
[PASS] scan 结果含 accounts 字段 - 共 2 个账号
[PASS] scan 结果含 version 字段 - version=4.1.15.51
[PASS] /api/health 返回 200 - status=200
[PASS] 首页返回 200 - status=200
[PASS] 首页含标题 微信聊天记录导出 - 长度 4018 字符
[PASS] 首页 Content-Type 含 charset - text/html; charset=utf-8
[PASS] 进程已清理、端口已释放
共 17 项：PASS 17 / WARN 0 / FAIL 0
结论: 全部通过
```

---

## 七、交付物清单

| 文件 | 大小 | 说明 |
| --- | --- | --- |
| `dist\WeChatExport\` | **46.55 MB / 60 文件** | onedir 绿色版（exe 3,767,327 B + `_internal`） |
| `dist\WeChatExport-v1.0.0-win64.zip` | 见 `dist\SHA256SUMS.txt` | 顶层为 `WeChatExport\`，含 README/LICENSE/第三方声明/docs |
| `dist\SHA256SUMS.txt` | — | 压缩包 SHA256（发布时附在 zip 同目录） |
| `docs\BUILD.md` | — | 构建机要求、参数、产物结构、复现、常见问题 |
| `docs\LIMITATIONS.md` | — | 已知限制的取舍与后续条件 |
| `tools\{build,make-zip,selftest}.ps1` | — | 构建 / 打包 / 自检脚本 |

---

## 八、验证覆盖范围与未覆盖项

**已覆盖**：产物完整性、干净环境运行、中文+空格路径、无 Python 环境、非项目 cwd、`--scan` JSON 契约、
HTTP 服务与首页、响应头 charset、冷启动耗时、依赖二进制存在性、构建路径残留、ZIP 解压可运行。

**未覆盖 / 说明**：

1. **本次重建没有重跑 `--export` 全链路**：它会解密真实聊天数据，按本次任务约束（不动真实微信数据）未执行。
   该链路上一次已在 [TESTREPORT](TESTREPORT.md) 第 4 节实测通过（打包版 5 会话 115 条 + 21 图 + 5 文件；
   手动 `--key` 模式 4 会话 93 条），本次只重建、未改变导出代码。
2. **未做"重启后/无文件缓存"的真实冷机测量**；按首次 IO 惩罚估算双击冷启动在 300~500 ms。
3. **未做杀软环境实测**（本机 Defender 未拦截）；误报缓解措施写在 `BUILD.md` 与 `FAQ`。
4. **未做 Windows 10 实机验证**（本机为 Windows 11 26100）。
