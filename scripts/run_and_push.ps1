# Daily driver for the CPCB fetch: run the pull, commit new data, push to GitHub.
# Intended to be triggered by Windows Task Scheduler from a residential connection
# (these government endpoints reject datacenter/cloud IPs outright).
#
# Task Scheduler setup:
#   Program/script:  powershell.exe
#   Arguments:        -NoProfile -ExecutionPolicy Bypass -File "D:\Projects\AQI NIS\scripts\run_and_push.ps1"
#   Start in:         D:\Projects\AQI NIS
#   Trigger:          Daily, at a time your machine is normally on and online

$ErrorActionPreference = "Stop"
Set-Location "D:\Projects\AQI NIS"

$python = "python"
$log = "logs\run_and_push_$(Get-Date -Format 'yyyy-MM-dd').log"

function Log($msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg"
    Write-Output $line
    Add-Content -Path $log -Value $line
}

Log "Starting CPCB fetch"
& $python scripts\fetch_cpcb.py
if ($LASTEXITCODE -ne 0) {
    Log "fetch_cpcb.py exited with code $LASTEXITCODE, skipping commit/push"
    exit $LASTEXITCODE
}

git add data/raw logs
$status = git status --porcelain
if ([string]::IsNullOrWhiteSpace($status)) {
    Log "No changes to commit"
    exit 0
}

git commit -m "Daily CPCB NCR data pull: $(Get-Date -Format 'yyyy-MM-dd')"
git push
Log "Committed and pushed"
