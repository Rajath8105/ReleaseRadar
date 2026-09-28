param(
    [string]$Context = ""
)

$ErrorActionPreference = "Stop"

if (-not (Get-Command dotnet -ErrorAction SilentlyContinue)) {
    throw ".NET SDK not found. Install the .NET 8 SDK."
}

if (-not (Get-Command kubectl -ErrorAction SilentlyContinue)) {
    throw "kubectl not found in PATH."
}

$previousMode = $env:Mode
$previousToken = $env:GitHub__Token
$previousContext = $env:Kubernetes__Context

if ([string]::IsNullOrWhiteSpace($Context)) {
    $output = & kubectl config current-context

    if ($LASTEXITCODE -ne 0) {
        throw "Could not read the current Kubernetes context."
    }

    $Context = ($output -join "").Trim()
}

Write-Host ""
Write-Host "Release Radar - Live mode" -ForegroundColor Cyan
Write-Host "Kubernetes context: $Context"
Write-Host "Namespace and services: backend/appsettings.json"
Write-Host "Read-only: no deployment or repository changes."
Write-Host ""

$confirm = Read-Host "Use this context? Type YES to continue"

if ($confirm -cne "YES") {
    Write-Host "Cancelled."
    exit
}

$secureToken = $null
$plainToken = $null

try {
    if ([string]::IsNullOrWhiteSpace($env:GitHub__Token)) {
        $secureToken = Read-Host "Read-only GitHub token" -AsSecureString
        $plainToken = [System.Net.NetworkCredential]::new("", $secureToken).Password
        $env:GitHub__Token = $plainToken
    }

    $env:Mode = "Live"
    $env:Kubernetes__Context = $Context

    Push-Location (Join-Path $PSScriptRoot "backend")

    try {
        Write-Host ""
        Write-Host "Open http://localhost:5080 after startup." -ForegroundColor Green
        Write-Host "Press Ctrl+C to stop."
        Write-Host ""

        & dotnet run
    }
    finally {
        Pop-Location
    }
}
finally {
    $env:Mode = $previousMode
    $env:GitHub__Token = $previousToken
    $env:Kubernetes__Context = $previousContext

    $plainToken = $null
    $secureToken = $null
}
