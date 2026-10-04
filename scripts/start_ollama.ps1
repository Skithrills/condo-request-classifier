param(
    [string]$OllamaExecutable = "$env:LOCALAPPDATA\OllamaClassifier\v0.35.0\ollama.exe"
)

$ErrorActionPreference = 'Stop'
$taskEndpoint = 'http://127.0.0.1:11434'
try {
    $taskVersion = Invoke-RestMethod "$taskEndpoint/api/version" -TimeoutSec 2
    if ($taskVersion.version) {
        Write-Output "Ollama $($taskVersion.version) is already running at $taskEndpoint"
        exit 0
    }
} catch { }

if (-not (Test-Path -LiteralPath $OllamaExecutable -PathType Leaf)) {
    $taskCommand = Get-Command ollama -ErrorAction SilentlyContinue
    if (-not $taskCommand) {
        throw 'Ollama was not found. Install it or pass -OllamaExecutable with its full path.'
    }
    $OllamaExecutable = $taskCommand.Source
}

$taskLogs = Join-Path $env:LOCALAPPDATA 'OllamaClassifier\logs'
New-Item -ItemType Directory -Force -Path $taskLogs | Out-Null
$taskStamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
$taskSaved = @{}
foreach ($taskName in @('OLLAMA_HOST', 'OLLAMA_CONTEXT_LENGTH', 'OLLAMA_NUM_PARALLEL')) {
    $taskSaved[$taskName] = [Environment]::GetEnvironmentVariable($taskName, 'Process')
}
try {
    # This dedicated local server handles one short classifier request at a time.
    $env:OLLAMA_HOST = '127.0.0.1:11434'
    $env:OLLAMA_CONTEXT_LENGTH = '4096'
    $env:OLLAMA_NUM_PARALLEL = '1'
    $taskProcess = Start-Process -FilePath $OllamaExecutable -ArgumentList 'serve' `
        -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $taskLogs "$taskStamp-out.log") `
        -RedirectStandardError (Join-Path $taskLogs "$taskStamp-error.log")
} finally {
    foreach ($taskName in $taskSaved.Keys) {
        [Environment]::SetEnvironmentVariable($taskName, $taskSaved[$taskName], 'Process')
    }
}

$taskDeadline = (Get-Date).AddSeconds(30)
do {
    if ($taskProcess.HasExited) { throw "Ollama stopped. Check logs in $taskLogs" }
    try {
        $taskVersion = Invoke-RestMethod "$taskEndpoint/api/version" -TimeoutSec 2
        if ($taskVersion.version) {
            Write-Output "Ollama $($taskVersion.version) ready at $taskEndpoint (PID $($taskProcess.Id))."
            Write-Output 'Local inference only; this launcher does not download models or configure startup.'
            exit 0
        }
    } catch { }
    Start-Sleep -Milliseconds 500
} while ((Get-Date) -lt $taskDeadline)
throw "Ollama did not become ready within 30 seconds. Check logs in $taskLogs"
