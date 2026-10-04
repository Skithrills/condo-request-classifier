param(
    [switch]$SkipOllama,
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskPython = Join-Path $taskRoot '.venv\Scripts\python.exe'
$taskApp = Join-Path $taskRoot 'app.py'
$taskUrl = 'http://localhost:8501'

if (-not (Test-Path -LiteralPath $taskPython -PathType Leaf)) {
    throw "Python environment missing. In $taskRoot, run: uv sync --locked"
}

if (-not $SkipOllama) {
    & powershell.exe -NoProfile -File (Join-Path $PSScriptRoot 'start_ollama.ps1')
    if ($LASTEXITCODE -ne 0) {
        throw 'Ollama could not start. Use -SkipOllama to run the offline classifier.'
    }
    $taskTags = Invoke-RestMethod 'http://127.0.0.1:11434/api/tags' -TimeoutSec 5
    if (-not ($taskTags.models | Where-Object { $_.name -eq 'gemma3:4b' })) {
        Write-Warning 'gemma3:4b is not installed. The offline classifier is ready; download the model before selecting Ollama.'
    }
}

$taskListener = Get-NetTCPConnection -LocalPort 8501 -State Listen -ErrorAction SilentlyContinue |
    Select-Object -First 1
if ($taskListener) {
    $taskOwner = Get-CimInstance Win32_Process -Filter "ProcessId=$($taskListener.OwningProcess)"
    if (-not $taskOwner.CommandLine -or
        $taskOwner.CommandLine.IndexOf($taskApp, [StringComparison]::OrdinalIgnoreCase) -lt 0) {
        throw 'Port 8501 is in use by another process. No process was stopped. Check the existing server before starting this project.'
    }
    $taskHealth = Invoke-RestMethod "$taskUrl/_stcore/health" -TimeoutSec 5
    if ($taskHealth -ne 'ok') { throw 'The existing app is not ready yet. Try again shortly.' }
    Write-Output "This project's website is already running at $taskUrl"
    if (-not $NoBrowser) { Start-Process $taskUrl }
    exit 0
}

Write-Output "Starting the classifier website at $taskUrl"
Write-Output 'The offline classifier is the default. Select Ollama in the sidebar to use Gemma.'
Write-Output 'Keep this window open. Ctrl+C stops the website; Ollama remains available in the background.'
Push-Location $taskRoot
try {
    $taskHeadless = if ($NoBrowser) { 'true' } else { 'false' }
    & $taskPython -m streamlit run $taskApp --server.address 127.0.0.1 --server.port 8501 --server.headless $taskHeadless
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
