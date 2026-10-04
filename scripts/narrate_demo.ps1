$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$demoRoot = Split-Path -Parent $PSScriptRoot
$demoScenes = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'demo_storyboard.json') -Raw | ConvertFrom-Json
$demoAudioDir = Join-Path $demoRoot 'output\demo\audio'
New-Item -ItemType Directory -Path $demoAudioDir -Force | Out-Null
$demoSpeech = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $demoSpeech.SelectVoice('Microsoft Hazel Desktop')
    $demoSpeech.Rate = 1
    $demoSpeech.Volume = 90
    foreach ($demoScene in $demoScenes) {
        $demoIndex = 0
        foreach ($demoSentence in $demoScene.narration) {
            $demoIndex++
            $demoAudioPath = Join-Path $demoAudioDir ("{0}-{1}.wav" -f $demoScene.id, $demoIndex)
            $demoSpeech.SetOutputToWaveFile($demoAudioPath)
            $demoSpeech.Speak($demoSentence)
            $demoSpeech.SetOutputToNull()
        }
    }
} finally {
    $demoSpeech.Dispose()
}
Write-Output 'Generated local narration for seven demo scenes.'
