$ErrorActionPreference = 'Stop'
$workspace = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$exe = Join-Path $workspace '.local\package-build\AgentWorkbench\AgentWorkbench.exe'
$db = Join-Path $workspace '.local\mvp-package-smoke.db'
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
    if ($null -eq $ready -or $ready.app_version -ne '0.3.0') { throw 'Packaged app did not reach v0.3.0 ready state' }
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
    $today = [DateTime]::UtcNow.ToString('yyyy-MM-dd')
    $from = [DateTime]::UtcNow.AddDays(-14).ToString('yyyy-MM-dd')
    $usage = Invoke-RestMethod -Uri "$base/v1/mvp/usage?day=$from&through=$today&tz=Asia%2FHong_Kong" -WebSession $session -TimeoutSec 120
    if ($usage.status -ne 'ready' -or $usage.summary.requests -le 0) { throw 'Packaged app returned no Codex usage' }
    if ($usage.summary.fresh_input_tokens + $usage.summary.cached_input_tokens -ne $usage.summary.input_tokens) {
        throw 'Input and cache totals do not reconcile'
    }
    if ($usage.summary.input_tokens + $usage.summary.output_tokens -ne $usage.summary.total_tokens) {
        throw 'Total tokens do not reconcile'
    }
    if ($usage.sessions.Count -gt 0) {
        $nativeId = [Uri]::EscapeDataString($usage.sessions[0].native_id)
        $detail = Invoke-RestMethod -Uri "$base/v1/mvp/usage/sessions/$nativeId/requests?day=$from&through=$today&tz=Asia%2FHong_Kong&limit=100" -WebSession $session
        if ($detail.count -le 0) { throw 'Session detail returned no requests' }
    }
    [pscustomobject]@{
        app_version = $ready.app_version
        web_setup = $setupStatus.web_setup_available
        files_scanned = $usage.coverage.files_scanned
        requests = $usage.summary.requests
        total_tokens = $usage.summary.total_tokens
        sessions = $usage.session_count
    } | Format-List
    Invoke-RestMethod -Uri "$base/v1/local/shutdown" -Method Post -WebSession $session -Headers @{ 'x-awb-csrf' = $csrf } | Out-Null
} finally {
    if (-not $process.HasExited) { Stop-Process -Id $process.Id -Force }
}
