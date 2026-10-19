<#
.SYNOPSIS
    把贴吧签到注册成 Windows 计划任务。

.DESCRIPTION
    默认：每天指定时间跑一次，且「错过就补跑」（StartWhenAvailable）。
    加 -IntervalHours 可以在一天内跑多次；加 -AtLogon 可以在登录时补跑一次。

    加 -AtStartup 时默认以 **SYSTEM** 身份运行，因为「开机触发」必须能在
    「还没有任何用户登录」的时候启动。用当前用户身份（Interactive）注册的话，
    开机那一刻没有人登录，任务会被 Task Scheduler 直接忽略——任务列得出来、
    触发器也显示已启用，但开机后 LastRunTime 一点没变，这就是踩过的坑。
    想改回当前用户加 -RunAsUser。

    重复注册同一个任务名会直接覆盖，所以改时间只要再跑一次本脚本。

.EXAMPLE
    # 先看看会做什么
    powershell -ExecutionPolicy Bypass -File scripts\install-task.ps1 -Time 11:00 -AtStartup -WhatIf

    # 真注册：每天 11:00 + 开机（SYSTEM 身份，开机后等 120 秒让网络就绪）
    powershell -ExecutionPolicy Bypass -File scripts\install-task.ps1 -Time 11:00 -AtStartup -NetworkWait 120
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    # 每天第一次运行的时间，HH:mm。加 -NoDaily 时可以不填。
    [ValidatePattern('^([01]\d|2[0-3]):[0-5]\d$')]
    [string]$Time,

    # 每隔几小时再跑一次，默认 0 表示一天只跑一次
    [ValidateRange(0, 23)]
    [int]$IntervalHours = 0,

    # 登录时补跑一次
    [switch]$AtLogon,

    # 开机（系统启动）时触发，不等任何人登录。默认配 SYSTEM 身份。
    [switch]$AtStartup,

    # 开机/登录触发后最多等多少秒让网络就绪，默认 120
    [ValidateRange(0, 3600)]
    [int]$NetworkWait = 120,

    # 只开机触发时不登录也能跑，所以默认用 SYSTEM；
    # 加这个开关改回「当前用户身份」（那就变回只在登录后才跑）
    [switch]$RunAsUser,

    # 只要开机触发，不要每天定时跑
    [switch]$NoDaily,

    # 传给 tieba_sign.py 的额外参数，例如 "--retry-failed --delay 3"
    [string]$Args = '',

    # 任务名
    [string]$TaskName = 'TiebaSign',

    # 指定 python 解释器完整路径，默认用 PATH 上的 python
    [string]$Python = ''
)

$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root      = Split-Path -Parent $ScriptDir
$Wrapper   = Join-Path $ScriptDir 'run-sign.ps1'
$Entry     = Join-Path $Root 'tieba_sign.py'
$ConfigFile = Join-Path $Root 'task-config.json'

if (-not (Test-Path -LiteralPath $Entry))   { throw "找不到主脚本 $Entry" }
if (-not (Test-Path -LiteralPath $Wrapper)) { throw "找不到包装脚本 $Wrapper" }

# PowerShell 7 优先，退回 Windows PowerShell
$pwshExe = (Get-Command pwsh -ErrorAction SilentlyContinue).Source
if (-not $pwshExe) { $pwshExe = (Get-Command powershell -ErrorAction Stop).Source }

# 没显式给 -Python 时，把 PATH 上找到的 python 解析成绝对路径写进任务。
# 必须在这里定死：开机触发是以 SYSTEM 身份跑的，而 SYSTEM 的 PATH 是机器级 PATH，
# 用户自己装进用户 PATH 的 python 它看不到。
if (-not $Python) {
    $py = Get-Command python -ErrorAction SilentlyContinue
    if (-not $py) { $py = Get-Command py -ErrorAction SilentlyContinue }
    if ($py) {
        $Python = $py.Source
    } else {
        Write-Host '警告: PATH 上找不到 python，计划任务很可能跑不起来。' -ForegroundColor Yellow
        Write-Host '      用 -Python <解释器完整路径> 指定，或先把 python 加进 PATH。' -ForegroundColor Yellow
    }
}


# --- 计算所有触发时间点 -----------------------------------------------------
$points = New-Object System.Collections.Generic.List[datetime]
if (-not $NoDaily) {
    if (-not $Time) {
        throw "需要 -Time HH:mm 指定每天几点跑；只想开机触发就加 -NoDaily。"
    }
    $start = [datetime]::ParseExact($Time, 'HH:mm', $null)
    $points.Add($start)
    if ($IntervalHours -gt 0) {
        $next = $start.AddHours($IntervalHours)
        while ($next.Day -eq $start.Day -and $next -gt $start) {
            $points.Add($next)
            $next = $next.AddHours($IntervalHours)
        }
    }
} elseif ($IntervalHours -gt 0) {
    throw "-IntervalHours 只在每天定时跑时才有意义，不能和 -NoDaily 一起用。"
}
if ($points.Count -eq 0 -and -not $AtStartup -and -not $AtLogon) {
    throw "没有配任何触发器：至少要有 -Time、-AtStartup、-AtLogon 之一。"
}

# --- 组装计划任务 -----------------------------------------------------------
$wrapperArg = "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Wrapper`""
if ($Python)     { $wrapperArg += " -Python `"$Python`"" }
if ($Args)       { $wrapperArg += " -ExtraArgs `"$Args`"" }
# 只有开机/登录这类「网络可能还没好」的触发器才需要等网络
if (($AtStartup -or $AtLogon) -and $NetworkWait -gt 0) {
    $wrapperArg += " -WaitForNetwork $NetworkWait"
}

$action = New-ScheduledTaskAction -Execute $pwshExe -Argument $wrapperArg -WorkingDirectory $Root

$triggers = @()
foreach ($point in $points) {
    $triggers += New-ScheduledTaskTrigger -Daily -At $point
}
if ($AtStartup) {
    # 三层保险：
    #   1) 触发器自带 Delay：开机后先等 30 秒，给网络栈留出时间；
    #   2) run-sign.ps1 的 -WaitForNetwork：再按 ping 实际可达与否等最多 $NetworkWait 秒；
    #   3) 身份用 SYSTEM：开机时还没人登录，Interactive 身份的任务根本起不来。
    if ($NetworkWait -gt 0) {
        $triggers += New-ScheduledTaskTrigger -AtStartup -RandomDelay (New-TimeSpan -Seconds 30)
    } else {
        $triggers += New-ScheduledTaskTrigger -AtStartup
    }
}
if ($AtLogon) {
    $triggers += New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
}

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -MultipleInstances IgnoreNew

# 开机触发时身份必须是 SYSTEM：见文件头的说明。
if ($AtStartup -and -not $RunAsUser) {
    $principal     = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    $principalText = 'SYSTEM（开机即跑，不需要任何人登录）'
} else {
    $principal     = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
    $principalText = "$env:USERDOMAIN\$env:USERNAME（只在该用户已登录时才会跑）"
}

$task = New-ScheduledTask -Action $action -Trigger $triggers -Settings $settings -Principal $principal

$timesText = if ($points.Count) { ($points | ForEach-Object { $_.ToString('HH:mm') }) -join ' / ' } else { '(不定时)' }
Write-Host "任务名     : $TaskName" -ForegroundColor Cyan
Write-Host "执行时间   : $timesText" -ForegroundColor Cyan
$extraTriggers = @()
if ($AtStartup) { $extraTriggers += '开机时' }
if ($AtLogon)   { $extraTriggers += '登录时' }
if ($extraTriggers.Count) {
    $waitText = if ($NetworkWait -gt 0) { "（启动后最多等 $NetworkWait 秒让网络就绪）" } else { '' }
    Write-Host "额外触发   : $($extraTriggers -join ' / ')$waitText" -ForegroundColor Cyan
}
Write-Host "运行身份   : $principalText" -ForegroundColor Cyan
Write-Host "解释器     : $pwshExe" -ForegroundColor Cyan
Write-Host "命令行     : `"$Wrapper`" $Args" -ForegroundColor Cyan
Write-Host "工作目录   : $Root" -ForegroundColor Cyan

if (-not $PSCmdlet.ShouldProcess($TaskName, '注册计划任务')) { return }

# 直接用 -Force 覆盖注册，**不要先 Unregister**：
# 曾经因为「先卸载旧的、再注册新的」，而注册又需要管理员权限，
# 非管理员窗口里跑就变成「旧任务没了、新任务也没建上」——一次操作丢失全部配置。
# 注册失败必须抛错，绝不能静默地把旧任务留在被删状态。
try {
    Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force -ErrorAction Stop | Out-Null
} catch {
    Write-Host ""
    Write-Host "注册失败：$($_.Exception.Message)" -ForegroundColor Red
    Write-Host "原有任务未被改动（本脚本不再先卸载旧任务）。" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "多数情况是权限不够。用管理员身份重开 PowerShell 再试，或直接双击：" -ForegroundColor Yellow
    Write-Host "    install-task.cmd" -ForegroundColor Cyan
    throw
}
Write-Host "已注册计划任务 $TaskName" -ForegroundColor Green

# 记录配置，供 status / uninstall 使用
@{
    taskName    = $TaskName
    python      = $Python
    wrapper     = $Wrapper
    args        = $Args
    times       = ($points | ForEach-Object { $_.ToString('HH:mm') })
    atLogon     = [bool]$AtLogon
    atStartup   = [bool]$AtStartup
    runAsUser   = [bool]$RunAsUser
    principal   = $principalText
    networkWait = $NetworkWait
    installedAt = (Get-Date).ToString('s')
} | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $ConfigFile -Encoding utf8

$info = Get-ScheduledTask -TaskName $TaskName
Write-Host ("当前状态   : {0}" -f $info.State) -ForegroundColor Cyan
$next = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Host ("下次运行   : {0}" -f $next.NextRunTime) -ForegroundColor Cyan
Write-Host ""
Write-Host "马上试跑一次: Start-ScheduledTask -TaskName $TaskName" -ForegroundColor DarkGray
Write-Host "看执行结果  : powershell -ExecutionPolicy Bypass -File scripts\status-task.ps1" -ForegroundColor DarkGray
