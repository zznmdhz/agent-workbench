$ErrorActionPreference = 'Stop'
$workspace = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$exe = Join-Path $workspace '.local\package-build\AgentWorkbench\AgentWorkbench.exe'
$db = Join-Path $workspace ('.local\multi-package-smoke-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.db')
$sync = Join-Path $workspace '.local\windows-package-smoke-sync'
New-Item -ItemType Directory -Force -Path (Join-Path $sync '.stfolder') | Out-Null
$previousSyncRoot = $env:AWB_SYNC_ROOT
$env:AWB_SYNC_ROOT = $sync
$port = 8767
if (-not (Test-Path -LiteralPath $exe)) { throw "Packaged executable missing: $exe" }
if (-not $db.StartsWith($workspace + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Smoke database escaped workspace'
}
$process = Start-Process -FilePath $exe -ArgumentList @('--db', $db, '--port', "$port", '--no-browser', '--no-tray') -PassThru -WindowStyle Hidden
try {
    $base = "http://127.0.0.1:$port"
    $ready = $null
    for ($i = 0; $i -lt 100; $i++) {
        try { $ready = Invoke-RestMethod -Uri "$base/health/ready" -TimeoutSec 2; break }
        catch { Start-Sleep -Milliseconds 200 }
    }
    if ($null -eq $ready -or $ready.app_version -ne '0.8.0') { throw 'Packaged app did not reach v0.8.0 ready state' }
    $homePage = Invoke-WebRequest -Uri "$base/" -TimeoutSec 10
    if ($homePage.StatusCode -ne 200 -or $homePage.Content -notmatch '(/assets/index-[^" ]+\.js)') {
        throw 'Packaged app did not serve the compiled dashboard'
    }
    $asset = $Matches[1]
    $script = Invoke-WebRequest -Uri "$base$asset" -TimeoutSec 10
    if ($script.StatusCode -ne 200 -or $script.Content -notmatch '全部历史') {
        throw 'Packaged dashboard asset does not include the custom range control'
    }
    $setupStatus = Invoke-RestMethod -Uri "$base/auth/setup-status"
    if ($setupStatus.needs_setup -or $setupStatus.web_setup_available) { throw 'Desktop still requests password setup' }
    $me = Invoke-RestMethod -Uri "$base/auth/me"
    if (-not $me.authenticated -or -not $me.csrf) { throw 'Passwordless local access unavailable' }
    $csrf = $me.csrf
    $today = (Get-Date).ToString('yyyy-MM-dd')
    $from = '2026-01-01'
    $usage = Invoke-RestMethod -Uri "$base/v1/mvp/usage?day=$from&through=$today&tz=Asia%2FHong_Kong" -TimeoutSec 120
    $activity = Invoke-RestMethod -Uri "$base/v1/mvp/activity?day=$today&through=$today&tz=Asia%2FHong_Kong&heatmap_view=day&focus_day=$today" -TimeoutSec 120
    if ($activity.heatmap.Count -ne 24 -or $null -eq $activity.summary.wall_ms) {
        throw 'Packaged activity endpoint did not return the 24-hour view'
    }
    if ($usage.status -ne 'ready' -or $usage.summary.requests -le 0) { throw 'Packaged app returned no usage' }
    if ($usage.summary.fresh_input_tokens + $usage.summary.cached_input_tokens + $usage.summary.cache_creation_tokens -ne $usage.summary.input_tokens) {
        throw 'Input and cache totals do not reconcile across agents'
    }
    if ($usage.summary.input_tokens + $usage.summary.output_tokens -ne $usage.summary.total_tokens) {
        throw 'Total tokens do not reconcile'
    }
    $trendTotal = ($usage.trend | Measure-Object -Property total_tokens -Sum).Sum
    if ($trendTotal + $usage.unattributed_tokens -ne $usage.summary.total_tokens) {
        throw 'Trend and unallocated Hermes totals do not reconcile'
    }
    foreach ($agent in @('codex', 'claude', 'hermes')) {
        if ($usage.sources.$agent.status -ne 'archived' -or $usage.sources.$agent.total_tokens -le 0) {
            throw "Expected local $agent usage is missing"
        }
    }
    if ($usage.sessions.Count -gt 0) {
        $nativeId = [Uri]::EscapeDataString($usage.sessions[0].native_id)
        $agent = $usage.sessions[0].agent
        $detail = Invoke-RestMethod -Uri "$base/v1/mvp/usage/sessions/$agent/$nativeId/requests?day=$from&through=$today&tz=Asia%2FHong_Kong&limit=100"
        if ($detail.count -le 0) { throw 'Session detail returned no requests' }
    }
    [pscustomobject]@{
        app_version = $ready.app_version
        passwordless = $me.authenticated
        codex_files = $usage.sources.codex.files
        claude_files = $usage.sources.claude.files
        hermes_status = $usage.sources.hermes.status
        requests = $usage.summary.requests
        total_tokens = $usage.summary.total_tokens
        sessions = $usage.session_count
    } | Format-List
    Invoke-RestMethod -Uri "$base/v1/local/shutdown" -Method Post -Headers @{ 'x-awb-csrf' = $csrf } | Out-Null
} finally {
    if (-not $process.HasExited) { Stop-Process -Id $process.Id -Force }
    $env:AWB_SYNC_ROOT = $previousSyncRoot
}
