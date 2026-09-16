# Daily driver: run each data pull, commit whatever new data landed, push to GitHub.
# Intended to be triggered by Windows Task Scheduler from a residential connection
# (data.gov.in/CPCB reject datacenter/cloud IPs outright; FIRMS doesn't, but this
# runs from the same machine for simplicity).
#
# Task Scheduler setup:
#   Program/script:  powershell.exe
#   Arguments:        -NoProfile -ExecutionPolicy Bypass -File "D:\Projects\AQI NIS\scripts\run_and_push.ps1"
#   Start in:         D:\Projects\AQI NIS
#   Trigger:          Daily, at a time your machine is normally on and online
#
# Each fetch script is independent -- one failing (e.g. CPCB blocked, FIRMS quota)
# must not stop the other from running or from committing/pushing what it got.

Set-Location "D:\Projects\AQI NIS"

$python = "python"
$log = "logs\run_and_push_$(Get-Date -Format 'yyyy-MM-dd').log"

function Log($msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg"
    Write-Output $line
    Add-Content -Path $log -Value $line
}

$failures = @()

Log "Starting CPCB fetch"
& $python scripts\fetch_cpcb.py
if ($LASTEXITCODE -ne 0) {
    Log "fetch_cpcb.py exited with code $LASTEXITCODE"
    $failures += "cpcb"
}

Log "Starting FIRMS fetch"
& $python scripts\fetch_firms.py
if ($LASTEXITCODE -ne 0) {
    Log "fetch_firms.py exited with code $LASTEXITCODE"
    $failures += "firms"
}

git add data/raw logs
$status = git status --porcelain
if ([string]::IsNullOrWhiteSpace($status)) {
    Log "No changes to commit"
} else {
    git commit -m "Daily data pull: $(Get-Date -Format 'yyyy-MM-dd')"
    git push
    Log "Committed and pushed"
}

if ($failures.Count -gt 0) {
    Log "Completed with failures: $($failures -join ', ')"
    exit 1
}

exit 0
