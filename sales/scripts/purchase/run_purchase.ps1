# run_purchase.ps1 - 仕入の取り込み（NOS の仕入実績一覧 → Asana「Bar null 仕入」）を1回実行する。
# タスクスケジューラ（install_purchase_task.ps1 で登録）が毎週月曜 9:30 に呼ぶ。手で試すとき:
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\sales\scripts\purchase\run_purchase.ps1
# 失敗・要確認（終了コード 0 以外）のときは PC に通知を出す。Slack には知らせない（2026-11-15 本人）。
param([string[]]$Extra = @())

$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"

$Sales = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $Sales
$LogDir = Join-Path $Sales "samples\purchases\logs"     # git に入れない
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Log = Join-Path $LogDir ("run_" + (Get-Date -Format "yyyyMMdd_HHmmss") + ".log")
function Write-Log([string]$msg) { "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $msg" | Out-File -FilePath $Log -Append -Encoding utf8 }

function Show-Notice([string]$title, [string]$text) {
    try {
        Add-Type -AssemblyName System.Windows.Forms
        Add-Type -AssemblyName System.Drawing
        $n = New-Object System.Windows.Forms.NotifyIcon
        $n.Icon = [System.Drawing.SystemIcons]::Warning
        $n.BalloonTipTitle = $title
        if ($text.Length -gt 240) { $text = $text.Substring(0, 240) + "..." }
        $n.BalloonTipText = $text
        $n.Visible = $true
        $n.ShowBalloonTip(15000)
        Start-Sleep -Seconds 15
        $n.Dispose()
    } catch { Write-Log "通知に失敗: $_" }
}

# 二重起動の防止
$Lock = Join-Path $LogDir "run.lock"
if (Test-Path $Lock) {
    if (((Get-Date) - (Get-Item $Lock).LastWriteTime).TotalMinutes -lt 30) { Write-Log "実行中のため飛ばしました"; exit 0 }
}
New-Item -ItemType File -Force -Path $Lock | Out-Null

$code = 1
try {
    $LocalBin = Join-Path $env:USERPROFILE ".local\bin"
    if ((Test-Path $LocalBin) -and (($env:Path -split ';') -notcontains $LocalBin)) { $env:Path = "$env:Path;$LocalBin" }
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if (-not $uv) {
        Write-Log "uv が見つかりません"
        Show-Notice "Bar null 仕入の取り込み" "uv が見つからないため実行できませんでした。"
        exit 1
    }
    $ArgsList = @("run", "--group", "purchase", "python", "-m", "barnull_sales.cli", "purchase-fetch") + $Extra
    Write-Log "開始: uv $($ArgsList -join ' ')"
    & uv @ArgsList 2>&1 | Out-File -FilePath $Log -Append -Encoding utf8
    $code = $LASTEXITCODE
    Write-Log "終了コード: $code"
}
finally {
    Remove-Item $Lock -Force -ErrorAction SilentlyContinue
}
# Slack には知らせない（2026-11-15 本人）。0 以外なら PC に通知を出す（3 = 取り込めたが要確認、それ以外 = 失敗）
if ($code -ne 0) {
    $Result = Join-Path $Sales "samples\purchases\last_result.txt"
    $detail = ""
    if (Test-Path $Result) { $detail = ((Get-Content -Path $Result -Encoding UTF8 | Select-Object -Skip 1) -join " ") }
    if ($code -eq 3) { Show-Notice "Bar null 仕入: 要確認" "取り込みましたが確認が必要です。$detail" }
    else { Show-Notice "Bar null 仕入の取り込みに失敗" "$detail（ログ: $Log）" }
}
exit $code
