param([ValidateSet("start", "stop", "status")][string]$Action = "start")

$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$runtime = Join-Path $projectRoot "data\runtime"
$pidFile = Join-Path $runtime "web.pid"
$logFile = Join-Path $runtime "web.log"
$errorLogFile = Join-Path $runtime "web-error.log"
New-Item -ItemType Directory -Path $runtime -Force | Out-Null

function Get-ListenerProcessId {
    $listener = Get-NetTCPConnection -LocalPort 8011 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($listener) { return [int]$listener.OwningProcess }
    $line = netstat -ano -p TCP | Select-String -Pattern '^\s*TCP\s+\S+:8011\s+\S+\s+LISTENING\s+(\d+)\s*$' | Select-Object -First 1
    if ($line -and $line.Matches.Count) { return [int]$line.Matches[0].Groups[1].Value }
    return $null
}

function Get-LittleLeafServiceProcess {
    $listenerProcessId = Get-ListenerProcessId
    if (-not $listenerProcessId) { return $null }
    try {
        $identity = Invoke-RestMethod -Uri "http://127.0.0.1:8011/api/health" -TimeoutSec 1
        if ($identity.independent -eq $true) { return [pscustomobject]@{ ProcessId = $listenerProcessId } }
    } catch {}
    $candidate = Get-CimInstance Win32_Process -Filter "ProcessId=$listenerProcessId" -ErrorAction SilentlyContinue
    if ($candidate -and $candidate.CommandLine -like "*$projectRoot*" -and $candidate.CommandLine -like "*uvicorn*app.main:app*") { return $candidate }
    $recordedPid = if (Test-Path -LiteralPath $pidFile) { [int](Get-Content -LiteralPath $pidFile -Raw) } else { 0 }
    if ($recordedPid -eq $listenerProcessId) { return [pscustomobject]@{ ProcessId = $listenerProcessId } }
    return $null
}

if ($Action -eq "stop") {
    $service = Get-LittleLeafServiceProcess
    if ($service) {
        Stop-Process -Id $service.ProcessId -Force -ErrorAction Stop
    }
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
    Write-Host "Little Leaf service stopped."
    exit 0
}

if ($Action -eq "status") {
    $service = Get-LittleLeafServiceProcess
    if ($service) { Write-Host "PID $($service.ProcessId)" } else { Write-Host "Not running." }
    exit 0
}

$service = Get-LittleLeafServiceProcess
if ($service) { Set-Content -LiteralPath $pidFile -Value $service.ProcessId; Write-Host "Service is running: http://127.0.0.1:8011"; exit 0 }
Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue

$backend = Join-Path $projectRoot "backend"
$python = Join-Path $backend ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "Backend environment is missing. Run: cd backend; uv sync --extra dev"
}
$pathValue = $env:Path
Remove-Item Env:Path -ErrorAction SilentlyContinue
Remove-Item Env:PATH -ErrorAction SilentlyContinue
$env:Path = $pathValue
$process = Start-Process -FilePath $python -ArgumentList @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8011") -WorkingDirectory $backend -RedirectStandardOutput $logFile -RedirectStandardError $errorLogFile -WindowStyle Hidden -PassThru -ErrorAction Stop
for ($attempt = 0; $attempt -lt 100; $attempt++) {
    Start-Sleep -Milliseconds 100
    $listenerProcessId = Get-ListenerProcessId
    $service = if ($listenerProcessId -eq $process.Id) { [pscustomobject]@{ ProcessId = $listenerProcessId } } else { Get-LittleLeafServiceProcess }
    if ($service) { break }
    if ($process.HasExited) { break }
}
if (-not $service) {
    $detail = if (Test-Path -LiteralPath $errorLogFile) { ([string](Get-Content -LiteralPath $errorLogFile -Raw)).Trim() } else { "No error log was produced." }
    throw "Little Leaf service failed to listen on 127.0.0.1:8011. $detail"
}
Set-Content -LiteralPath $pidFile -Value $service.ProcessId
Write-Host "Little Leaf service started: http://127.0.0.1:8011"
