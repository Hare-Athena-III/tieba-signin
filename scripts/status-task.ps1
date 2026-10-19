<#
.SYNOPSIS
    查看签到计划任务的状态、最近几次运行和日志尾部。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\status-task.ps1
    powershell -ExecutionPolicy Bypass -File scripts\status-task.ps1 -Tail 30 -TaskName TiebaSign
#>
[CmdletBinding()]
param(
    [string]$TaskName = 'TiebaSign',
    [int]$Tail = 15
)

$ErrorActionPreference = 'Stop'
$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root       = Split-Path -Parent $ScriptDir
$ConfigFile = Join-Path $Root 'task-config.json'
$LogPath    = Join-Path $Root 'sign.log'

if (Test-Path -LiteralPath $ConfigFile) {
    $cfg = Get-Content -LiteralPath $ConfigFile -Raw | ConvertFrom-Json
    if ($cfg.taskName) { $TaskName = $cfg.taskName }
}

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Host "没有找到计划任务 $TaskName。" -ForegroundColor Yellow
    Write-Host "注册: powershell -ExecutionPolicy Bypass -File scripts\install-task.ps1 -Time 08:05" -ForegroundColor DarkGray
    return
}

$info = Get-ScheduledTaskInfo -TaskName $TaskName

Write-Host "=== 计划任务 $TaskName ===" -ForegroundColor Cyan
Write-Host ("状态        : {0}" -f $task.State)
Write-Host ("下次运行    : {0}" -f $info.NextRunTime)
Write-Host ("上次运行    : {0}" -f $info.LastRunTime)
Write-Host ("上次结果    : 0x{0:X} ({1})" -f $info.LastTaskResult, $(if ($info.LastTaskResult -eq 0) { '成功' } else { '非零，见下方日志' }))
Write-Host ("错过次数    : {0}" -f $info.NumberOfMissedRuns)

# 命令行里逗号是数组分隔符，直接输出更易读
Write-Host "执行内容    :" -ForegroundColor DarkGray
Write-Host ("  " + ($task.Actions | ForEach-Object { "$($_.Execute) $($_.Arguments)" }))

Write-Host "触发时间    :" -ForegroundColor DarkGray
foreach ($t in $task.Triggers) {
    $kind = $t.CimClass.CimClassName
    $at   = if ($t.StartBoundary) { ([datetime]$t.StartBoundary).ToString('HH:mm') } else { '' }
    Write-Host "  $kind $at"
}

if (Test-Path -LiteralPath $LogPath) {
    Write-Host ""
    Write-Host "=== 日志尾部（$LogPath）===" -ForegroundColor Cyan
    Get-Content -LiteralPath $LogPath -Tail $Tail -Encoding utf8
} else {
    Write-Host ""
    Write-Host "还没有日志文件，任务可能还没跑过。" -ForegroundColor Yellow
    Write-Host "手动试跑: Start-ScheduledTask -TaskName $TaskName" -ForegroundColor DarkGray
}
