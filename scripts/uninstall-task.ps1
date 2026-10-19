<#
.SYNOPSIS
    卸载贴吧签到的计划任务。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\uninstall-task.ps1                      # 只删任务
    powershell -ExecutionPolicy Bypass -File scripts\uninstall-task.ps1 -RemoveLogs          # 连日志和 task-config.json 一起删
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$TaskName = 'TiebaSign',
    [switch]$RemoveLogs
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
    Write-Host "没有找到计划任务 $TaskName，无需卸载。" -ForegroundColor Yellow
} elseif ($PSCmdlet.ShouldProcess($TaskName, '删除计划任务')) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "已删除计划任务 $TaskName" -ForegroundColor Green
}

if ($RemoveLogs -and $PSCmdlet.ShouldProcess($LogPath, '删除日志与配置文件')) {
    $targets = @($LogPath) + (Get-ChildItem -LiteralPath $Root -Filter 'sign.log.*' -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName })
    $targets += (Join-Path $Root 'result.json')
    $targets += $ConfigFile
    foreach ($f in $targets) {
        if ($f -and (Test-Path -LiteralPath $f)) { Remove-Item -LiteralPath $f -Force }
    }
    Write-Host "已清理日志与 task-config.json（cookie 配置 config.json 未动）" -ForegroundColor Green
}
