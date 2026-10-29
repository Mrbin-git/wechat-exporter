# Build the standalone WeChat exporter (one-folder, no Python required on target).
#
# Usage:
#   pwsh -File tools\build.ps1 [-Python <python.exe>] [-Out dist]
#
# Requirements on the BUILD machine only:
#   Python 3.11+ with pip, and these packages (installed into .\vendor when missing):
#   pyinstaller, cryptography, zstandard, openpyxl, pillow
param(
    [string]$Python = "",
    [string]$Out = "dist",
    [switch]$OneFile
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Resolve-Python {
    if ($Python) { return $Python }
    foreach ($candidate in @(
            (Join-Path $root ".venv\Scripts\python.exe"),
            "python.exe")) {
        try {
            if (Test-Path $candidate) { return (Resolve-Path $candidate).Path }
            $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
            if ($cmd -and $cmd.Source -notlike "*WindowsApps*") { return $cmd.Source }
        } catch { }
    }
    # Prefer the 3.12 line: the wheels already vendored under vendor\ are cp312 ABI.
    try {
        $p = & py -3.12 -c "import sys;print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $p) { return $p.Trim() }
    } catch { }
    # uv-managed CPython 3.12: independent of any host runtime, which is exactly
    # what reproducibility needs (a host-runtime base leaks its DLL paths into .toc).
    $uvRoot = Join-Path $env:APPDATA "uv\python"
    if (Test-Path $uvRoot) {
        $uvDir = Get-ChildItem $uvRoot -Directory -Filter "cpython-3.12*" -ErrorAction SilentlyContinue |
            Sort-Object Name -Descending | Select-Object -First 1
        if ($uvDir) {
            $uvExe = Join-Path $uvDir.FullName "python.exe"
            if (Test-Path $uvExe) { return $uvExe }
        }
    }
    throw "未找到 Python，请用 -Python 指定解释器路径（推荐独立安装的 CPython 3.12 x64）"
}

$py = Resolve-Python
Write-Host "使用 Python: $py"
$version = & $py -c "import sys;print(f'{sys.version_info.major}.{sys.version_info.minor}')"
Write-Host "Python 版本: $version"

# Reproducibility: the pre-populated vendor\ binaries are cp312 (Python 3.12) ABI.
$vendorPyInstaller = Join-Path $root "vendor\PyInstaller"
if ($version -ne "3.12" -and (Test-Path $vendorPyInstaller)) {
    throw ("vendor\ 中已安装的二进制依赖是 cp312（Python 3.12）ABI，当前解释器为 {0}；" -f $version) +
        "请用 -Python 指定独立的 CPython 3.12，或清空 vendor\ 后重新安装依赖。"
}

# A host runtime (e.g. DSH) as base prefix leaks its DLL paths into .toc, so the
# build cannot be reproduced elsewhere. Refuse early with an actionable message.
$basePrefix = (& $py -c "import sys;print(sys.base_prefix)" 2>$null)
if ($basePrefix -and $basePrefix -match 'dsh-runtimes') {
    throw ("当前解释器基于宿主运行时（{0}），打包会把宿主 DLL 收进产物、导致换机不可复现；" -f $basePrefix) +
        "请用独立安装的 CPython 3.12（可用 -Python 指定）。"
}

$vendor = Join-Path $root "vendor"
New-Item -ItemType Directory -Force -Path $vendor | Out-Null

$needed = @("pyinstaller", "cryptography", "zstandard", "openpyxl", "PIL")
$probe = @'
import importlib, sys
missing = []
for name in ("PyInstaller", "cryptography", "zstandard", "openpyxl", "PIL"):
    try:
        importlib.import_module(name)
    except Exception:
        missing.append(name)
print(",".join(missing))
'@
$env:PYTHONPATH = "$root\src;$vendor"
$probeFile = Join-Path $root "build\probe_deps.py"
New-Item -ItemType Directory -Force -Path (Split-Path $probeFile) | Out-Null
Set-Content -Path $probeFile -Value $probe -Encoding UTF8
$missing = (& $py $probeFile).Trim()
if ($missing) {
    Write-Host "安装缺失依赖: $missing"
    $env:TMPDIR = Join-Path $root ".tmp"
    $env:PIP_CACHE_DIR = Join-Path $root ".tmp\pipcache"
    New-Item -ItemType Directory -Force -Path $env:TMPDIR | Out-Null
    & $py -m pip install --disable-pip-version-check --no-cache-dir --target $vendor `
        pyinstaller cryptography zstandard openpyxl pillow
}

$icon = Join-Path $root "assets\app.ico"
$iconArgs = @()
if (Test-Path $icon) { $iconArgs = @("--icon", $icon) }

$dataArgs = @(
    "--add-data", "$root\src\wexport\web\ui.html;wexport\web",
    "--add-data", "$root\src\wexport\web\app.js;wexport\web",
    "--add-data", "$root\src\wexport\web\style.css;wexport\web"
)
$mode = if ($OneFile) { "--onefile" } else { "--onedir" }
$dist = Join-Path $root $Out
$work = Join-Path $root "build\pyinstaller"

Write-Host "开始打包 ($mode) ..."
& $py -m PyInstaller `
    --noconfirm --clean $mode --console `
    --name WeChatExport `
    --paths "$root\src" --paths $vendor `
    --hidden-import wexport.web.server `
    --hidden-import wexport.export.runner `
    --hidden-import wexport.export.html_export `
    --hidden-import wexport.export.csv_export `
    --hidden-import wexport.export.json_export `
    --hidden-import wexport.export.txt_export `
    --hidden-import wexport.export.xlsx_export `
    --collect-submodules cryptography `
    --collect-submodules zstandard `
    --collect-submodules openpyxl `
    --distpath $dist --workpath $work --specpath (Join-Path $root "build") `
    @iconArgs @dataArgs `
    (Join-Path $root "src\wexport\__main__.py")

if ($LASTEXITCODE -ne 0) { throw "PyInstaller 打包失败" }

$target = Join-Path $dist "WeChatExport"
$exe = Join-Path $target "WeChatExport.exe"
if (-not (Test-Path $exe)) { throw "未找到生成的 exe: $exe" }

# copy license / notices next to the exe
foreach ($doc in @("README.md", "LICENSE", "微信聊天记录导出工具使用说明.txt")) {
    $p = Join-Path $root $doc
    if (Test-Path $p) { Copy-Item $p (Join-Path $target $doc) -Force }
}
$notices = Join-Path $root "THIRD-PARTY-NOTICES.md"
if (Test-Path $notices) {
    $docsDir = Join-Path $target "docs"
    New-Item -ItemType Directory -Force -Path $docsDir | Out-Null
    Copy-Item $notices (Join-Path $docsDir "THIRD-PARTY-NOTICES.md") -Force
}

# Ship the user documentation (so `build.ps1` alone already yields a complete
# folder; make-zip.ps1 re-copies the same files before zipping).
$docsSrc = Join-Path $root "docs"
if (Test-Path $docsSrc) {
    $docsDir = Join-Path $target "docs"
    New-Item -ItemType Directory -Force -Path $docsDir | Out-Null
    $docFiles = @(Get-ChildItem $docsSrc -File -Filter *.md)
    foreach ($doc in $docFiles) {
        Copy-Item $doc.FullName (Join-Path $docsDir $doc.Name) -Force
    }
    Write-Host ("已复制文档: {0} 个 -> docs\" -f $docFiles.Count)
}

# --- reproducibility guard ----------------------------------------------------
# PyInstaller .toc files record where every collected DLL came from. Absolute
# build-machine paths (C:\Users\..., especially the DSH runtime) do not break the
# finished exe but make rebuilds on another machine fragile: add a loud warning
# instead of failing, and document the result in docs\PACKAGING-REPORT.md.
$tocDir = Join-Path $work "WeChatExport"
if (Test-Path $tocDir) {
    $tocFiles = @(Get-ChildItem $tocDir -Filter *.toc -File)
    # .toc files are Python reprs: Windows paths inside them carry DOUBLED
    # backslashes ("C:\\Users\\..."). A regex written for single backslashes
    # silently matches nothing, so these checks use -SimpleMatch or a pattern
    # that also accepts the doubled form.
    $dshHits = @()
    $absHits = @()
    foreach ($toc in $tocFiles) {
        $dshHits += @(Select-String -Path $toc.FullName -SimpleMatch 'dsh-runtimes' -ErrorAction SilentlyContinue)
        $absHits += @(Select-String -Path $toc.FullName -Pattern '[A-Za-z]:\\' -ErrorAction SilentlyContinue)
    }
    $pyPrefix = (Split-Path -Parent $py).Replace('\', '\\')
    $rootPrefix = $root.Replace('\', '\\')
    $foreign = @($absHits | Where-Object { $_.Line -notlike "*$pyPrefix*" -and $_.Line -notlike "*$rootPrefix*" })
    if ($dshHits.Count -gt 0) {
        Write-Warning ("构建可复现性警告：.toc 中有 {0} 行引用 DSH 运行时 (dsh-runtimes)" -f $dshHits.Count)
        Write-Warning ("  受影响文件: {0}" -f (($dshHits | ForEach-Object { Split-Path -Leaf $_.Path } | Sort-Object -Unique) -join ', '))
        Write-Warning ("  示例: {0}" -f $dshHits[0].Line.Trim())
        Write-Warning "  -> 产物可运行，但 DLL 来源不受控；请用独立安装的 CPython 3.12 重建（-Python 指定）。"
    }
    if ($foreign.Count -gt 0) {
        Write-Warning ("构建可复现性警告：.toc 中有 {0} 行引用本次构建目录/所选 Python 之外的位置" -f $foreign.Count)
        $sample = $foreign[0].Line.Trim()
        if ($sample.Length -gt 160) { $sample = $sample.Substring(0, 160) + '...' }
        Write-Warning ("  示例: {0}" -f $sample)
        Write-Warning "  -> 产物可运行；换机重建前请确认这些依赖的来源（见 docs\BUILD.md）。"
    }
    if ($dshHits.Count -eq 0 -and $foreign.Count -eq 0) {
        Write-Host ("可复现性检查: 绝对路径 {0} 条，均指向所选 Python 安装目录与本次构建目录；无 dsh-runtimes 引用" -f $absHits.Count)
    }
}

$size = (Get-ChildItem $target -Recurse -File | Measure-Object -Property Length -Sum).Sum / 1MB
Write-Host ("打包完成: {0}  总大小 {1:N1} MB" -f $exe, $size)
Write-Host "自检: $exe --scan"
Write-Host "自检脚本: pwsh -File tools\selftest.ps1"
