# 测试报告（v1.0.0）

测试环境：Windows 10 Pro 26H2（build 26300，x64），微信 Weixin 4.1.15.51，
数据目录 `D:\WeChat\xwechat_files`（2 个账号），Python 3.12.14。
测试日期：2026-10-20。负责人执行的验收测试，全部结果来自真实运行输出。

## 1. 自动化单元/集成测试

命令：

```powershell
$env:PYTHONPATH="src;vendor;tests"
python -m unittest discover -s tests -p "test_*.py"
```

结果：**Ran 53 tests — OK（53/53 通过，1.8 秒）**

| 测试文件 | 覆盖内容 | 结果 |
|---|---|---|
| `test_sqlcipher.py` | 真实 `session.db` 用正确密钥解密、SQLite 可打开、`SessionTable` 有数据；错误密钥必须失败；源文件不被修改 | PASS |
| `test_reader.py` | 文本/图片/文件/链接/引用/小程序/视频号/语音/视频/表情/位置/名片/系统消息分类；zstd 解码；真实解密库的会话、联系人、消息读取 | PASS |
| `test_export.py` | HTML/CSV/JSON/TXT/XLSX 五种导出；UTF-8、BOM、表头、可反序列化、可被 openpyxl 打开；日期过滤与系统消息开关 | PASS |

## 2. 媒体解密测试（真实 .dat）

命令：

```powershell
python tests\test_media_real.py
```

结果：**22/22 个 `.dat` 全部成功还原**（19 个消息图片 + 3 个新增样本）。
其中 JPEG/PNG 直接可打开，HEVC（`wxgf`）原样保留待外部转码。

## 3. 真实数据端到端（源码模式）

| 账号 | 会话数 | 消息数 | 媒体 | 结果 |
|---|---|---|---|---|
| `wxid_example_9f3a` | 4 | 115 | 21 图片 + 5 文件 | 成功，HTML/CSV/JSON/TXT/XLSX 全部生成 |
| `wxid_sample_2b7c` | — | — | — | 8 个消息库全部解密成功 |

- 自动取密钥：内存扫描 552 个候选，8 进程并行校验，**约 20 秒**命中。
- 解密性能：30MB `message_0.db` 约 0.3 秒；`session.db`/`contact.db` < 0.1 秒。
- 导出后自动清理临时明文库；设置 `WEXPORT_KEEP_WORK=1` 可保留用于排查。

## 4. 打包版（免环境 EXE）验证

产物：`dist\WeChatExport\WeChatExport.exe`（onedir，**46.55 MB / 60 文件**，2026-10-26 重建），
`dist\WeChatExport-v1.0.0-win64.zip`（约 20 MB，SHA256 见 `dist\SHA256SUMS.txt`）。

| 用例 | 结果 |
|---|---|
| 干净环境变量（PATH 仅系统目录）下 `--scan` | PASS，返回 2 个账号，退出码 0 |
| 复制到含中文与空格的路径后运行 | PASS |
| 本地 HTTP 服务 `/api/health`、首页、`app.js` | PASS（200） |
| 携带密钥缓存时 `--export`（HTML/CSV/JSON/TXT/XLSX） | PASS，5 会话（115 条）+ 21 张图片 + 5 个文件 |
| 手动密钥 `--key` 模式 `--export` | PASS，4 会话 93 条消息 |
| 冷启动 `--scan` | 约 170–350 ms；HTTP 就绪约 0.6 s |
| dist 内是否引用构建机路径 | 0 命中（位置无关） |

## 5. 未能在自动化环境验证的项（重要）

**打包版首次运行的内存取密钥**：在本次自动化沙箱中，打包后的 exe 被限制为
低权限令牌（`EnumProcesses` 只能看到 14/233 个进程，`OpenProcess` 返回错误 5），
因此无法读取微信进程内存。同一份代码在源码模式（普通用户令牌）下已实测成功
（552 个候选 → 20 秒命中）。用户双击运行时是普通用户令牌，与源码模式一致。
为覆盖这种极端情况，产品提供了两条兜底路径：

1. 手动密钥输入（界面「密钥」输入框 / CLI `--key`），已在打包版实测通过；
2. 取到一次密钥后用 DPAPI 缓存，后续启动直接复用。

## 6. 已修复的问题

| 问题 | 修复 |
|---|---|
| 打包版进程枚举返回空（Toolhelp 句柄被截断） | 改用 `EnumProcesses + QueryFullProcessImageNameW`，并以微信窗口 PID 兜底 |
| 临时目录不可写导致导出失败 | 工作目录改为多级回退，并支持 `WEXPORT_WORK_DIR` 指定 |
| `appmsg` 类型写在属性上时识别失败 | 同时读取属性与子元素 |
| 首页 `Content-Type` 缺 charset | 文本类型统一追加 `; charset=utf-8` |
| 预分配空白页 HMAC 误报 | 仅当解密后 `quick_check` 失败才提示 |

## 7. 未覆盖 / 已知限制

- 微信 3.x、企业微信、macOS 未支持。
- 语音：桌面端通常没有本地语音文件，识别 SILK 但不转码。
- HEVC 图片（`wxgf`）需要外部 ffmpeg 才能转成普通图片。
- 微信升级可能改变内存特征，导致自动取密钥失效（可手动输入密钥）。
- 超大数据量（单账号数十 GB 消息）未做压力测试；当前按会话流式导出。
