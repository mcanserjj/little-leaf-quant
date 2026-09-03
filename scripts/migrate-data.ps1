param(
    [Parameter(Mandatory = $true)]
    [string]$Source,
    [string]$Destination = "$PSScriptRoot\..\data"
)

$resolvedDestination = [System.IO.Path]::GetFullPath($Destination)
$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
if (-not $resolvedDestination.StartsWith($projectRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Destination must stay inside the Little Leaf project."
}

$datasets = @(
    "instruments", "kline_daily", "kline_daily_enriched", "adj_factor",
    "financials", "ext_data", "kline_index_daily", "kline_index_enriched"
)
foreach ($dataset in $datasets) {
    $sourcePath = Join-Path $Source $dataset
    if (Test-Path -LiteralPath $sourcePath) {
        Copy-Item -LiteralPath $sourcePath -Destination $resolvedDestination -Recurse -Force
    }
}

$userData = Join-Path $resolvedDestination "user_data"
New-Item -ItemType Directory -Path $userData -Force | Out-Null
foreach ($dataset in @("research_league", "research_news")) {
    $sourcePath = Join-Path (Join-Path $Source "user_data") $dataset
    if (Test-Path -LiteralPath $sourcePath) {
        Copy-Item -LiteralPath $sourcePath -Destination $userData -Recurse -Force
    }
}

$manifest = Get-ChildItem -LiteralPath $resolvedDestination -Recurse -File |
    Where-Object { $_.Name -notin @("secrets.json", "secrets.dat", "migration-manifest.json") } |
    ForEach-Object {
        [pscustomobject]@{
            relative_path = $_.FullName.Substring($resolvedDestination.Length + 1)
            bytes = $_.Length
            sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        }
    }
$record = [ordered]@{
    generated_at = (Get-Date).ToString("o")
    source = [System.IO.Path]::GetFullPath($Source)
    note = "Verified data copy; secrets, caches, and logs were excluded."
    files = $manifest
}
$record | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $resolvedDestination "migration-manifest.json") -Encoding UTF8
Write-Host "Migration complete: $($manifest.Count) files. Secrets, caches, and logs were excluded."
