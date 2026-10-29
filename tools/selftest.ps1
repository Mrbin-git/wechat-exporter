# Post-build self-test for the packaged WeChat exporter.
#
# Usage:
#   pwsh -File tools\selftest.ps1 [-AppDir dist\WeChatExport] [-Port 18901] [-CleanEnv] [-SkipServer]
#
# What it does:
#   1. runs `WeChatExport.exe --scan` and validates the JSON + accounts field
#   2. starts `WeChatExport.exe --port <Port> --no-browser`
#   3. curls /api/health and / (expects 200 and the Chinese page title)
#   4. always kills the process it started and frees the port
#   5. prints a PASS/FAIL summary and exits non-zero when anything failed
#
# -CleanEnv wipes PATH/PYTHONPATH/PYTHONHOME for the child process, emulating a
# machine without Python installed. Nothing under the user's real WeChat data is
# touched: --scan only reads folder metadata.
param(
    [string]$AppDir = "",
    [int]$Port = 18901,
    [switch]$CleanEnv,
    [switch]$SkipServer,
    [switch]$Json,
    [int]$StartTimeoutSec = 20
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
$OutputEncoding = [System.Text.Encoding]::UTF8

if (-not $AppDir) { $AppDir = Join-Path $root "dist\WeChatExport" }
$AppDir = (Resolve-Path $AppDir).Path
$exe = Join-Path $AppDir "WeChatExport.exe"
if (-not (Test-Path $exe)) { throw "未找到 $exe，请先运行 tools\build.ps1" }

$script:results = New-Object System.Collections.Generic.List[object]

function Add-Result {
    param([string]$Name, [bool]$Ok, [string]$Detail = "", [switch]$Warn)
    $status = if ($Ok) { "PASS" } elseif ($Warn) { "WARN" } else { "FAIL" }
    $script:results.Add([pscustomobject]@{ Test = $Name; Result = $status; Detail = $Detail })
    Write-Host ("[{0}] {1} {2}" -f $status, $Name, $(if ($Detail) { "- $Detail" } else { "" }))
}

# --- environment hygiene -----------------------------------------------------
if ($CleanEnv) {
    $env:PATH = 'C:\Windows\System32;C:\Windows'
    $env:PYTHONPATH = ''
    $env:PYTHONHOME = ''
    Write-Host "已清空 PATH/PYTHONPATH/PYTHONHOME（模拟干净环境）"
}
Write-Host "被测程序: $exe"
Write-Host ""

# --- 1. --scan ---------------------------------------------------------------
$scanOut = ""
$scanCode = -1
try {
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $scanOut = (& $exe --scan 2>&1 | Out-String)
    $scanCode = $LASTEXITCODE
    $sw.Stop()
} catch {
    $scanOut = "$_"
}
$scanMs = if ($sw) { [math]::Round($sw.Elapsed.TotalMilliseconds, 0) } else { -1 }

Add-Result "scan 返回码为 0" ($scanCode -eq 0) "exit=$scanCode"

$scanJson = $null
try {
    $scanJson = $scanOut | ConvertFrom-Json
    Add-Result "scan 输出为合法 JSON" $true "长度 $($scanOut.Trim().Length) 字符"
} catch {
    Add-Result "scan 输出为合法 JSON" $false $_.Exception.Message
}

if ($scanJson) {
    $hasAccounts = $null -ne $scanJson.PSObject.Properties['accounts']
    Add-Result "scan 结果含 accounts 字段" $hasAccounts $(if ($hasAccounts) { "共 $($scanJson.accounts.Count) 个账号" } else { "" })
    $verProp = $scanJson.PSObject.Properties['version']
    Add-Result "scan 结果含 version 字段" ($null -ne $verProp) $(if ($verProp) { "version=$($scanJson.version)" } else { "" })
} else {
    Add-Result "scan 结果含 accounts 字段" $false "JSON 解析失败"
}
Add-Result "scan 耗时记录" $true "$scanMs ms"
Write-Host ""

# --- 2. HTTP service --------------------------------------------------------
if (-not $SkipServer) {
    $proc = $null
    try {
        # Fail early if the port is already taken by someone else.
        $busy = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
        Add-Result "端口 $Port 启动前空闲" (-not $busy) $(if ($busy) { "被 PID $($busy[0].OwningProcess) 占用" } else { "" })
        if ($busy) { throw "端口 $Port 已被占用，跳过服务测试" }

        $stdout = Join-Path ([System.IO.Path]::GetTempPath()) "wexport-selftest-$PID.out"
        $stderr = Join-Path ([System.IO.Path]::GetTempPath()) "wexport-selftest-$PID.err"
        $proc = Start-Process -FilePath $exe -ArgumentList @('--port', "$Port", '--no-browser') `
            -PassThru -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr
        Add-Result "服务进程已启动" ($null -ne $proc) "PID $($proc.Id)"

        $health = $null
        $deadline = (Get-Date).AddSeconds($StartTimeoutSec)
        $reachable = $false
        while ((Get-Date) -lt $deadline) {
            if ($proc.HasExited) { break }
            try {
                $health = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/api/health" -UseBasicParsing -TimeoutSec 2
                $reachable = $true
                break
            } catch { Start-Sleep -Milliseconds 250 }
        }
        Add-Result "服务在 ${StartTimeoutSec}s 内就绪" $reachable "http://127.0.0.1:$Port"

        if ($reachable) {
            Add-Result "/api/health 返回 200" ($health.StatusCode -eq 200) "status=$($health.StatusCode)"
            $hJson = $null
            try { $hJson = $health.Content | ConvertFrom-Json } catch { }
            $okFlag = ($hJson -and $hJson.ok -eq $true)
            Add-Result "/api/health 返回 ok=true" $okFlag $health.Content.Trim()

            $index = $null
            try {
                $index = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/" -UseBasicParsing -TimeoutSec 5
                Add-Result "首页返回 200" ($index.StatusCode -eq 200) "status=$($index.StatusCode)"
            } catch {
                Add-Result "首页返回 200" $false $_.Exception.Message
            }

            if ($index) {
                # The server serves ui.html through _file(), which now appends
                # `; charset=utf-8` (fixed in src\wexport\web\server.py, shipped
                # since the 2026-10-21 build). Decode the title from .Content and
                # keep a raw-UTF-8 fallback for hosts that ignore the header.
                $html = [string]$index.Content
                $hasTitle = $html.Contains('微信聊天记录导出')
                if (-not $hasTitle) {
                    $raw = $index.RawContentStream
                    if ($raw) {
                        $bytes = $raw.ToArray()
                        $hasTitle = ([System.Text.Encoding]::UTF8.GetString($bytes)).Contains('微信聊天记录导出')
                    }
                }
                if (-not $hasTitle) {
                    $bytes = [System.Text.Encoding]::GetEncoding(936).GetBytes($html)
                    $hasTitle = ([System.Text.Encoding]::UTF8.GetString($bytes)).Contains('微信聊天记录导出')
                }
                # NOTE: keep the expected title in a single-quoted string. Windows PowerShell
                # treats full-width quotes (U+201C/U+201D) as string delimiters, which silently
                # shifts positional parameters.
                Add-Result '首页含标题 微信聊天记录导出' $hasTitle "长度 $($html.Length) 字符"
                $hasScript = $html -match '<script|app\.js'
                Add-Result "首页引用了前端脚本" $hasScript ""
                Add-Result "首页声明 UTF-8" ($html -match 'charset=["'']?utf-8') ""
                # WARN, not FAIL: browsers honour <meta charset>; this is a hardening item.
                # Only print the remediation hint when the header is actually missing.
                $ctHeader = [string]$index.Headers['Content-Type']
                $ctHasCharset = $ctHeader -match 'charset'
                $ctDetail = if ($ctHasCharset) { $ctHeader } else { 'text/html（建议 src 中 _file() 补 charset=utf-8）' }
                Add-Result "首页 Content-Type 含 charset" $ctHasCharset $ctDetail -Warn
            }

            # A second call must also work (server stays alive across requests).
            try {
                $again = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/api/health" -UseBasicParsing -TimeoutSec 3
                Add-Result "服务可重复响应" ($again.StatusCode -eq 200) ""
            } catch {
                Add-Result "服务可重复响应" $false $_.Exception.Message
            }
        }
    } catch {
        Add-Result "HTTP 服务测试" $false $_.Exception.Message
    } finally {
        if ($proc) {
            # Primary path: Stop-Process on the PID we started (no elevation needed).
            # taskkill.exe can return "Access denied" for redirected/console-less GUI
            # children; keep it as a last resort with a polite (non-forced) attempt.
            if (-not $proc.HasExited) {
                try { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue } catch { }
                Start-Sleep -Milliseconds 400
            }
            if (-not $proc.HasExited) {
                try { & taskkill.exe /PID $proc.Id /T /F 2>&1 | Out-Null } catch { }
                Start-Sleep -Milliseconds 400
            }
            try { $proc.Dispose() } catch { }
            Start-Sleep -Milliseconds 200
        }
        $still = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
        Add-Result "进程已清理、端口已释放" (-not $still) $(if ($still) { "端口仍被占用（可能是其他程序的进程）" } else { "" })
    }
}

# --- summary ----------------------------------------------------------------
Write-Host ""
Write-Host "================ 自检汇总 ================"
$script:results | Format-Table -AutoSize
$failed = @($script:results | Where-Object { $_.Result -eq "FAIL" })
$passed = @($script:results | Where-Object { $_.Result -eq "PASS" })
$warned = @($script:results | Where-Object { $_.Result -eq "WARN" })
Write-Host ("共 {0} 项：PASS {1} / WARN {2} / FAIL {3}" -f $script:results.Count, $passed.Count, $warned.Count, $failed.Count)
if ($warned.Count -gt 0) {
    Write-Host "警告项（不阻断交付，需在报告中记录）:"
    $warned | ForEach-Object { Write-Host ("  ! {0}: {1}" -f $_.Test, $_.Detail) }
}
if ($failed.Count -gt 0) {
    Write-Host "失败项:"
    $failed | ForEach-Object { Write-Host ("  - {0}: {1}" -f $_.Test, $_.Detail) }
    exit 1
}
Write-Host "结论: 全部通过"
# Optional machine-readable output for CI: tools\selftest.ps1 -Json | ConvertFrom-Json
if ($Json) { $script:results | ConvertTo-Json -Depth 4 }
exit 0
