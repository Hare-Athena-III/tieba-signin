<#
.SYNOPSIS
    贴吧签到的运行包装：写日志、日志轮转，然后调用 tieba_sign.py。

.DESCRIPTION
    计划任务实际执行的是这个脚本，而不是直接跑 Python。
    这么做的两个原因：
      1) 计划任务在 0 号会话里运行，控制台输出没人看得见，所以结果必须落盘；
      2) 日志需要轮转，否则长年累月会一直变大。
    退出码原样透传 tieba_sign.py 的退出码（0 全部成功 / 1 有失败 / 2 没 cookie）。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\run-sign.ps1
    powershell -ExecutionPolicy Bypass -File scripts\run-sign.ps1 -ExtraArgs "--retry-failed" -MaxLogKB 512
#>
[CmdletBinding()]
param(
    # 追加参数，原样传给 tieba_sign.py，例如 "--retry-failed --delay 3"
    [string]$ExtraArgs = '',

    # 日志文件，默认 <项目根>\sign.log
    [string]$LogPath,

    # 日志超过这个大小就轮转，单位 KB，默认 1024（1MB），设 0 表示不轮转
    [int]$MaxLogKB = 1024,

    # 保留几个历史日志，默认 5
    [int]$KeepFiles = 5,

    # 指定 python 解释器；默认依次尝试 项目根\python-path.txt => PATH 上的 python
    [string]$Python,

    # 开机/登录触发时等网络：最多等这么多秒，期间每 5 秒 ping 一次。
    # 0 表示不等。通了就立刻开始，不会白等。
    [int]$WaitForNetwork = 0
)

$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root      = Split-Path -Parent $ScriptDir
$Entry     = Join-Path $Root 'tieba_sign.py'
if (-not $LogPath) { $LogPath = Join-Path $Root 'sign.log' }

function Write-Wrap {
    param([string]$Text)
    $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    Add-Content -LiteralPath $LogPath -Value "[$stamp] [wrapper] $Text" -Encoding utf8
}

# 日志要能和 python 的输出拼成一篇干净 UTF-8 文本：
#   - $OutputEncoding 决定 PowerShell 怎么把字符串编码给子进程
#   - [Console]::OutputEncoding 决定它怎么解码子进程吐回来的字节
#   - python 那边统一用 -X utf8 输出 UTF-8
# 三者不一致时，中文日志就会变乱码（这正是最早踩到的坑）。
$utf8 = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = $utf8
try { [Console]::OutputEncoding = $utf8 } catch { }

if (-not (Test-Path -LiteralPath $Entry)) {
    Write-Wrap "找不到主脚本 $Entry，已放弃。"
    exit 3
}

# --- 找 python --------------------------------------------------------------
$pyFile = Join-Path $Root 'python-path.txt'
if (-not $Python -and (Test-Path -LiteralPath $pyFile)) {
    $Python = (Get-Content -LiteralPath $pyFile -First 1).Trim()
}
if (-not $Python) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $cmd) { $cmd = Get-Command py -ErrorAction SilentlyContinue }
    if (-not $cmd) {
        Write-Wrap 'PATH 上找不到 python，请装好 Python 或用 install-task.ps1 -Python <完整路径> 重新注册。'
        exit 3
    }
    $Python = $cmd.Source
}

# --- 等网络就绪（开机/登录触发时用）-----------------------------------------
# Windows 没有「网络就绪」触发器，只能先触发、再等一等。
# 冷启动时「以太网/Wi-Fi 已连接」往往早就亮了，但 DNS 和默认路由还没好，
# 所以必须真的往外发一个包，而不是看网卡状态。
#
# 判定以 ping 为主，TCP 兜底：
#   ① ping tieba.baidu.com     —— 通 = DNS 正常且路由可达，最理想
#   ② TCP 连 tieba.baidu.com:443 —— 有些网络封 ICMP，ping 永远失败但网是好的
#   ③ ping 223.5.5.5           —— 只在超时失败时记一行，用来区分「完全没网」和「只是贴吧不可达」
# ①②任一通就开始签到；都不同则等到超时也照常执行（脚本自己有重试）。
function Test-Ping {
    param([string]$Target)
    try {
        # PS 5.1 的 Test-Connection 没有 -TimeoutSeconds；-Quiet 失败时只返回 $false，不抛错
        return [bool](Test-Connection -ComputerName $Target -Count 1 -Quiet -ErrorAction Stop)
    } catch {
        return $false
    }
}

function Test-Tcp443 {
    param([string]$HostName = 'tieba.baidu.com')
    # 用 TcpClient 异步连：Windows PowerShell 5.1 没有 -AsJob，同步连会一直卡住
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        $async = $client.BeginConnect($HostName, 443, $null, $null)
        while (-not $async.IsCompleted -and $sw.Elapsed.TotalSeconds -lt 5) {
            Start-Sleep -Milliseconds 200
        }
        if (-not $async.IsCompleted) { return $false }
        $client.EndConnect($async)   # DNS 解析失败会在这里抛异常，正是要判定的情况
        return $true
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

if ($WaitForNetwork -gt 0) {
    Write-Wrap "等待网络就绪（最多 $WaitForNetwork 秒）..."
    $deadline   = (Get-Date).AddSeconds($WaitForNetwork)
    $startedAt  = Get-Date
    $ready      = $false
    $via        = ''
    $lastSignal = ''
    while ($true) {
        $pingDns = Test-Ping 'tieba.baidu.com'
        $tcp     = if ($pingDns) { $true } else { Test-Tcp443 }
        $ready   = $pingDns -or $tcp
        if ($pingDns)     { $via = 'ping tieba.baidu.com 通' }
        elseif ($tcp)     { $via = 'ping 不通但 TCP 443 能连（ICMP 被封）' }

        # 只在探测结果变化时记一行，免得日志被每 5 秒一次的记录刷屏
        $signal = "ping=$pingDns tcp443=$tcp"
        if ($signal -ne $lastSignal) {
            Write-Wrap "网络探测: $signal"
            $lastSignal = $signal
        }
        if ($ready) { break }
        if ((Get-Date) -ge $deadline) { break }
        Start-Sleep -Seconds 5
    }
    $waited = [int]((Get-Date) - $startedAt).TotalSeconds
    if ($ready) {
        Write-Wrap "网络就绪（$via，等了约 $waited 秒），开始签到。"
    } else {
        # 故意不跳过：签到脚本自己会重试，硬跳过等于白白漏掉一天
        $ipOk = Test-Ping '223.5.5.5'
        Write-Wrap "等了 $WaitForNetwork 秒仍连不上贴吧（公网 IP ping=$ipOk）。照常执行，交给脚本自己重试。"
    }
}

# --- 日志轮转：sign.log -> sign.log.1 -> sign.log.2 ... ---------------------
if ($MaxLogKB -gt 0 -and (Test-Path -LiteralPath $LogPath)) {
    $sizeKB = [math]::Round((Get-Item -LiteralPath $LogPath).Length / 1KB, 1)
    if ($sizeKB -ge $MaxLogKB) {
        for ($i = $KeepFiles - 1; $i -ge 1; $i--) {
            $older = "$($LogPath).$i"
            $newer = "$($LogPath).$($i + 1)"
            if (Test-Path -LiteralPath $older) {
                Move-Item -LiteralPath $older -Destination $newer -Force
            }
        }
        Move-Item -LiteralPath $LogPath -Destination "$($LogPath).1" -Force
        Write-Wrap "日志已达 $sizeKB KB，已轮转为 $($LogPath).1"
    }
}

# --- 组装参数并运行 ---------------------------------------------------------
# -X utf8：强制 python 以 UTF-8 输出，否则中文在 GBK 控制台下会变乱码再进日志
$pyArgs = @('-X', 'utf8', $Entry, '--log-path', $LogPath)
if ($ExtraArgs) {
    $pyArgs += ($ExtraArgs -split '\s+' | Where-Object { $_ -ne '' })
}

Write-Wrap "开始执行: $Python $($pyArgs -join ' ')"
$started = Get-Date
# python 的 stderr 在 ErrorActionPreference=Stop 下会变成终止性错误，这里临时放开
$eap = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
try {
    $output = & $Python @pyArgs 2>&1
    if ($null -ne $LASTEXITCODE) { $code = $LASTEXITCODE }
} catch {
    $output = @("包装脚本调用 python 失败: $($_.Exception.Message)")
    $code = 1
} finally {
    $ErrorActionPreference = $eap
}
$seconds = [math]::Round(((Get-Date) - $started).TotalSeconds, 1)

# 把子进程的输出也留一份（出错时的堆栈最有用）
if ($output) {
    foreach ($line in $output) {
        Add-Content -LiteralPath $LogPath -Value "    $line" -Encoding utf8
    }
}
Write-Wrap "结束，退出码 $code，耗时 ${seconds}s"

exit $code
