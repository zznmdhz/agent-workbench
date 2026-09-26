$ErrorActionPreference = 'Stop'
$workspace = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$exe = Join-Path $workspace '.local\package-build\AgentWorkbench\AgentWorkbench.exe'
$db = Join-Path $workspace ('.local\multi-package-smoke-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.db')
$port = 8767
if (-not (Test-Path -LiteralPath $exe)) { throw "Packaged executable missing: $exe" }
if (-not $db.StartsWith($workspace + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Smoke database escaped workspace'
}
$process = Start-Process -FilePath $exe -ArgumentList @('--db', $db, '--port', "$port", '--no-browser') -PassThru -WindowStyle Hidden
try {
    $base = "http://127.0.0.1:$port"
    $ready = $null
    for ($i = 0; $i -lt 100; $i++) {
        try { $ready = Invoke-RestMethod -Uri "$base/health/ready" -TimeoutSec 2; break }
        catch { Start-Sleep -Milliseconds 200 }
    }
    if ($null -eq $ready -or $ready.app_version -ne '0.4.0') { throw 'Packaged app did not reach v0.4.0 ready state' }
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
    if (-not $setupStatus.web_setup_available) { throw 'Browser setup unavailable in packaged desktop mode' }
    if ($setupStatus.needs_setup) {
        $password = 'MvpSmoke-' + [Guid]::NewGuid().ToString('N')
        $body = @{ password = $password; confirmation = $password } | ConvertTo-Json -Compress
        $setup = Invoke-RestMethod -Uri "$base/auth/setup" -Method Post -ContentType 'application/json' -Body $body -SessionVariable session
        $csrf = $setup.csrf
    } else {
        throw 'Smoke database was already initialized; use a fresh private database'
    }
    $today = (Get-Date).ToString('yyyy-MM-dd')
    $from = '2026-01-01'
    $usage = Invoke-RestMethod -Uri "$base/v1/mvp/usage?day=$from&through=$today&tz=Asia%2FHong_Kong" -WebSession $session -TimeoutSec 120
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
        if ($usage.sources.$agent.status -ne 'ready' -or $usage.sources.$agent.total_tokens -le 0) {
            throw "Expected local $agent usage is missing"
        }
    }
    if ($usage.sessions.Count -gt 0) {
        $nativeId = [Uri]::EscapeDataString($usage.sessions[0].native_id)
        $agent = $usage.sessions[0].agent
        $detail = Invoke-RestMethod -Uri "$base/v1/mvp/usage/sessions/$agent/$nativeId/requests?day=$from&through=$today&tz=Asia%2FHong_Kong&limit=100" -WebSession $session
        if ($detail.count -le 0) { throw 'Session detail returned no requests' }
    }
    [pscustomobject]@{
        app_version = $ready.app_version
        web_setup = $setupStatus.web_setup_available
        codex_files = $usage.sources.codex.files
        claude_files = $usage.sources.claude.files
        hermes_status = $usage.sources.hermes.status
        requests = $usage.summary.requests
        total_tokens = $usage.summary.total_tokens
        sessions = $usage.session_count
    } | Format-List
    Invoke-RestMethod -Uri "$base/v1/local/shutdown" -Method Post -WebSession $session -Headers @{ 'x-awb-csrf' = $csrf } | Out-Null
} finally {
    if (-not $process.HasExited) { Stop-Process -Id $process.Id -Force }
}
