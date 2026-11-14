# install_purchase_task.ps1 - Windows のタスク「BarNullPurchase」を登録する（毎週月曜 9:30 に run_purchase.ps1。2026-11-15 本人指定）。
# 管理者権限は不要。普通の PowerShell で:
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\sales\scripts\purchase\install_purchase_task.ps1
# 止めるとき: uninstall_purchase_task.ps1
param(
    [string]$At = "09:30",
    [string]$Day = "Monday",
    [string]$TaskName = "BarNullPurchase"
)

$Sales = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Script = Join-Path $PSScriptRoot "run_purchase.ps1"

$Action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Script`"" `
    -WorkingDirectory $Sales
$Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $Day -At $At
# StartWhenAvailable: その時刻に PC が止まっていたら、次に起動したときに実行する（当月1日から、1〜7日は前月1日から読むので取りこぼさない）
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$Principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger `
    -Settings $Settings -Principal $Principal -Force `
    -Description "Bar null: NOS の仕入実績を Asana に取り込む（毎週 $Day $At）" | Out-Null

Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo | Format-List TaskName, NextRunTime, LastRunTime, LastTaskResult
Write-Host "登録しました: $TaskName（毎週 $Day $At）。今すぐ試すとき: Start-ScheduledTask -TaskName $TaskName"
