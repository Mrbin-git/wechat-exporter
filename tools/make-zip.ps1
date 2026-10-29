# Build the portable release zip from the packaged one-folder output.
#
# Usage:
#   pwsh -File tools\make-zip.ps1 [-AppDir dist\WeChatExport] [-Out dist\WeChatExport-v1.0.0-win64.zip] [-Version 1.0.0] [-Flat]
#
# The produced archive contains a single top-level folder (WeChatExport/) so that
# users can extract it anywhere, including paths with Chinese characters and spaces.
# Use -Flat to zip the files directly without the parent folder.
param(
    [string]$AppDir = "",
    [string]$Out = "",
    [string]$Version = "1.0.0",
    [switch]$Flat
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not $AppDir) { $AppDir = Join-Path $root "dist\WeChatExport" }
if (-not $Out) { $Out = Join-Path $root ("dist\WeChatExport-v{0}-win64.zip" -f $Version) }

if (-not (Test-Path $AppDir)) { throw "未找到打包目录: $AppDir（请先运行 tools\build.ps1）" }
$AppDir = (Resolve-Path $AppDir).Path
$exe = Join-Path $AppDir "WeChatExport.exe"
if (-not (Test-Path $exe)) { throw "目录中缺少 WeChatExport.exe: $AppDir" }

# Make sure the docs that ship with the binary are up to date.
foreach ($doc in @("README.md", "LICENSE", "微信聊天记录导出工具使用说明.txt")) {
    $src = Join-Path $root $doc
    if (Test-Path $src) {
        $dst = Join-Path $AppDir $doc
        if (-not (Test-Path $dst) -or (Get-Item $src).LastWriteTimeUtc -gt (Get-Item $dst).LastWriteTimeUtc) {
            Copy-Item $src $dst -Force
            Write-Host "已更新 $doc"
        }
    }
}

# Third-party notices are kept under docs/ so the top level stays simple.
$notices = Join-Path $root "THIRD-PARTY-NOTICES.md"
if (Test-Path $notices) {
    $docsDir = Join-Path $AppDir "docs"
    New-Item -ItemType Directory -Force -Path $docsDir | Out-Null
    Copy-Item $notices (Join-Path $docsDir "THIRD-PARTY-NOTICES.md") -Force
}
$staleNotices = Join-Path $AppDir "THIRD-PARTY-NOTICES.md"
if (Test-Path $staleNotices) { Remove-Item $staleNotices -Force }

# Ship the user documentation inside the archive as well.
$docsSrc = Join-Path $root "docs"
if (Test-Path $docsSrc) {
    $docsDst = Join-Path $AppDir "docs"
    New-Item -ItemType Directory -Force -Path $docsDst | Out-Null
    Get-ChildItem $docsSrc -File -Filter *.md | ForEach-Object {
        Copy-Item $_.FullName (Join-Path $docsDst $_.Name) -Force
    }
    Write-Host "已包含 docs\ 目录"
}

$outDir = Split-Path -Parent $Out
if ($outDir -and -not (Test-Path $outDir)) { New-Item -ItemType Directory -Force -Path $outDir | Out-Null }
if (Test-Path $Out) { Remove-Item $Out -Force }

Write-Host "打包目录: $AppDir"
Write-Host "输出文件: $Out"

# Use the .NET ZipFile API (fast, no 2GB Compress-Archive limit).
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

$baseName = Split-Path -Leaf $AppDir
$prefix = if ($Flat) { "" } else { $baseName + "/" }
$files = Get-ChildItem $AppDir -Recurse -File
Write-Host ("待压缩文件数: {0}" -f $files.Count)

$sw = [System.Diagnostics.Stopwatch]::StartNew()
$fs = [System.IO.File]::Open($Out, [System.IO.FileMode]::CreateNew)
try {
    $zip = New-Object System.IO.Compression.ZipArchive($fs, [System.IO.Compression.ZipArchiveMode]::Create)
    try {
        foreach ($f in $files) {
            $rel = $f.FullName.Substring($AppDir.Length).TrimStart('\', '/').Replace('\', '/')
            $entryName = $prefix + $rel
            # NoCompression for already-compressed binaries keeps the build fast and
            # the archive roughly the size of the dist folder.
            $entry = $zip.CreateEntry($entryName, [System.IO.Compression.CompressionLevel]::Optimal)
            $entry.LastWriteTime = $f.LastWriteTime
            $es = $entry.Open()
            try {
                $src = [System.IO.File]::OpenRead($f.FullName)
                try { $src.CopyTo($es) } finally { $src.Dispose() }
            } finally { $es.Dispose() }
        }
    } finally { $zip.Dispose() }
} finally { $fs.Dispose() }
$sw.Stop()

$zipItem = Get-Item $Out
$sum = (Get-FileHash $Out -Algorithm SHA256).Hash
$mb = [math]::Round($zipItem.Length / 1MB, 1)
Write-Host ("压缩完成: {0}  {1} MB  用时 {2:N1}s" -f $Out, $mb, $sw.Elapsed.TotalSeconds)
Write-Host ("SHA256: {0}" -f $sum)

# Sidecar checksums: shipped next to the archive (not inside it) so users can
# verify the download. Generated here so docs don't have to hand-maintain it.
$exeSum = (Get-FileHash (Join-Path $AppDir "WeChatExport.exe") -Algorithm SHA256).Hash
$sumFile = Join-Path $outDir "SHA256SUMS.txt"
$sumLines = @(
    ("{0}  {1}" -f $sum, (Split-Path -Leaf $Out))
    ("{0}  {1}" -f $exeSum, "WeChatExport\WeChatExport.exe")
)
[IO.File]::WriteAllLines($sumFile, $sumLines, (New-Object System.Text.UTF8Encoding($false)))
Write-Host ("校验文件: {0}" -f $sumFile)

# Sanity check: the archive must contain the exe.
$check = [System.IO.Compression.ZipFile]::OpenRead($Out)
try {
    $hasExe = ($check.Entries | Where-Object { $_.FullName -like "*WeChatExport.exe" } | Measure-Object).Count -gt 0
    Write-Host ("校验: 包含 WeChatExport.exe = {0}, 条目数 = {1}" -f $hasExe, $check.Entries.Count)
    if (-not $hasExe) { throw "压缩包中未找到 WeChatExport.exe" }
} finally { $check.Dispose() }

[pscustomobject]@{
    Zip    = $Out
    Bytes  = $zipItem.Length
    MB     = $mb
    SHA256 = $sum
    Entries = $files.Count
} | Format-List
