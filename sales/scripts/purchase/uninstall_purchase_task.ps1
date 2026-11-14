# uninstall_purchase_task.ps1 - タスク「BarNullPurchase」を外す（仕入の自動取り込みを止める）。
param([string]$TaskName = "BarNullPurchase")
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "外しました: $TaskName"
