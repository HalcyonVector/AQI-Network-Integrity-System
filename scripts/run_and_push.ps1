# Daily driver: run each data pull, commit whatever new data landed, push to GitHub.
# Intended to be triggered by Windows Task Scheduler from a residential connection
# (data.gov.in/CPCB reject datacenter/cloud IPs outright; FIRMS/GEE/CDS don't, but
# this runs from the same machine for simplicity).
#
# Task Scheduler setup:
#   Program/script:  powershell.exe
#   Arguments:        -NoProfile -ExecutionPolicy Bypass -File "D:\Projects\AQI NIS\scripts\run_and_push.ps1"
#   Start in:         D:\Projects\AQI NIS
#   Trigger:          Daily, at a time your machine is normally on and online
#   Execution time limit: 6 hours. ERA5 requests can queue on CDS's compute backend for hours;
#   a 2 h limit killed the task before it ever reached the commit step (29-31 Oct 2026).
#
# Order matters, and it is built so a slow or failing step cannot block publishing:
#   1. the fast fetches (CPCB, OpenAQ, FIRMS, AOD), then the derived steps and the dashboard,
#      then COMMIT AND PUSH. This part takes minutes.
#   2. ERA5 last (its CDS queue time is the least predictable), then the steps that use it
#      (neighbours, stubble, dashboard), then a second commit and push.
# Each step is independent: one failing (e.g. CPCB blocked, FIRMS quota) must not stop the others.

Set-Location "D:\Projects\AQI NIS"

$python = "python"
$log = "logs\run_and_push_$(Get-Date -Format 'yyyy-MM-dd').log"

function Log($msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg"
    Write-Output $line
    Add-Content -Path $log -Value $line
}

$failures = @()

function Run-Step($script, $label) {
    Log "Starting $script"
    & $python "scripts\$script.py"
    if ($LASTEXITCODE -ne 0) {
        Log "$script.py exited with code $LASTEXITCODE"
        $script:failures += $label
    }
}

function Commit-And-Push($what) {
    git add data/raw data/processed data/reference dashboard logs
    $status = git status --porcelain
    if ([string]::IsNullOrWhiteSpace($status)) {
        Log "No changes to commit ($what)"
    } else {
        git commit -m "Daily data pull: $(Get-Date -Format 'yyyy-MM-dd') ($what)"
        git push
        Log "Committed and pushed ($what)"
    }
}

# --- Part 1: fast fetches, derived products, publish ---
foreach ($step in @(
    @("fetch_cpcb", "cpcb"),
    @("fetch_openaq", "openaq"),
    @("fetch_firms", "firms"),
    @("fetch_aod", "aod"),
    @("convert_feed", "feed-conversion"),
    @("analyze_neighbor_consistency", "neighbours"),
    @("build_filled_series", "filled"),
    @("analyze_station_bias", "satellite-consistency"),
    @("analyze_stubble_transport", "stubble"),
    @("build_dashboard", "dashboard")
)) {
    Run-Step $step[0] $step[1]
}
Commit-And-Push "fetches and analyses"

# --- Part 2: weather (slow), then what depends on it, then publish again ---
Run-Step "fetch_era5_wind" "era5"
foreach ($step in @(
    @("analyze_neighbor_consistency", "neighbours"),
    @("analyze_stubble_transport", "stubble"),
    @("build_dashboard", "dashboard")
)) {
    Run-Step $step[0] $step[1]
}
Commit-And-Push "weather and re-analysis"

if ($failures.Count -gt 0) {
    Log "Completed with failures: $($failures -join ', ')"
    exit 1
}

exit 0
