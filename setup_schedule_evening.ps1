# 加一個傍晚排程：每週一到週五 18:30 執行 run_market_corr.bat（傳導鏈／開盤前訊號＋恐慌儀表，更新後自動 push）
# 只新增這一個工作，不會動到原本早上的排程
$ErrorActionPreference = 'Stop'
$name = 'MarketCorrEvening'
$dir  = $PSScriptRoot
$bat  = Join-Path $dir 'run_market_corr.bat'
if (-not (Test-Path $bat)) { Write-Host "找不到 run_market_corr.bat，請把本檔放在 股市監控 資料夾" -ForegroundColor Red; exit 1 }

$action   = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument "/c `"$bat`"" -WorkingDirectory $dir
$trigger  = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At '18:30'
# StartWhenAvailable：錯過時間（關機）時，開機後補跑；WakeToRun：睡眠中到點自動喚醒
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

Register-ScheduledTask -TaskName $name -Description '股市監控：傍晚更新傳導鏈（台股收盤、外資現貨與期貨）＋恐慌儀表並推上 GitHub' `
    -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null

$info = Get-ScheduledTaskInfo -TaskName $name
Write-Host ""
Write-Host "設定完成：工作名稱 $name" -ForegroundColor Green
Write-Host "執行時間：每週一到週五 18:30（睡眠中會自動喚醒）"
Write-Host "下次執行：$($info.NextRunTime)"

# 列出所有會跑 run_market_corr.bat 的排程，確認早上那一個還在
Write-Host ""
Write-Host "目前會執行 run_market_corr.bat 的排程：" -ForegroundColor Cyan
Get-ScheduledTask | Where-Object { ($_.Actions | ForEach-Object { "$($_.Execute) $($_.Arguments)" }) -match 'run_market_corr' } | ForEach-Object {
    $t = $_
    $times = ($t.Triggers | ForEach-Object { if ($_.StartBoundary) { ([datetime]$_.StartBoundary).ToString('HH:mm') } }) -join ', '
    $days  = ($t.Triggers | ForEach-Object { if ($_.DaysOfWeek) { $_.DaysOfWeek } }) -join ', '
    Write-Host ("  {0,-24} {1,-8} {2}  狀態：{3}" -f $t.TaskName, $times, $days, $t.State)
}
