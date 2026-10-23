# タスク BarNullOps を止めて登録を外す
$ErrorActionPreference = "Stop"
Stop-ScheduledTask -TaskName "BarNullOps" -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName "BarNullOps" -Confirm:$false
Write-Host "タスク BarNullOps を削除しました（data/ のデータはそのまま残ります）"
