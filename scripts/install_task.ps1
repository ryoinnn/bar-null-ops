# Bar null 予約対応システムを Windows のタスクスケジューラに登録する（常駐化）
#
#   既定:        ログオン時に起動（PC にログインしたままロック画面でも動く）
#   -AtStartup:  PC 起動時に起動（ログオンしていなくても実行。Windows のパスワード入力が必要）
#
# どちらも 5 分ごとに起動を確認し、落ちていれば再起動する（多重起動はアプリ側のロックで防ぐ）。
# 実行例:  powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1
param([switch]$AtStartup)
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$pyw = Join-Path $root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pyw)) { throw "見つかりません: $pyw （先に uv sync を実行してください）" }

$user = "$env:USERDOMAIN\$env:USERNAME"
$action = New-ScheduledTaskAction -Execute $pyw -Argument "-m barnull run" -WorkingDirectory $root
$triggers = @()
if ($AtStartup) {
    # 起動直後はネットワークがつながっていないことがあるので、1分待ってから起動する
    $boot = New-ScheduledTaskTrigger -AtStartup
    $boot.Delay = "PT1M"
    $triggers += $boot
}
$triggers += New-ScheduledTaskTrigger -AtLogOn -User $user
$triggers += New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew

if ($AtStartup) {
    $cred = Get-Credential -UserName $user -Message "ログオンしていなくても実行するため、Windows のパスワードを入力してください"
    Register-ScheduledTask -TaskName "BarNullOps" -Action $action -Trigger $triggers -Settings $settings `
        -User $cred.UserName -Password $cred.GetNetworkCredential().Password -RunLevel Limited -Force | Out-Null
} else {
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName "BarNullOps" -Action $action -Trigger $triggers -Settings $settings `
        -Principal $principal -Force | Out-Null
}
Write-Host "タスク BarNullOps を登録しました。今すぐ起動するには: Start-ScheduledTask -TaskName BarNullOps"
Write-Host "ログ: $root\data\logs\barnull.log"
