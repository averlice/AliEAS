Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$v = $synth.GetInstalledVoices()
foreach ($voice in $v) {
    if ($voice.Enabled) {
        Write-Output ($voice.VoiceInfo.Name + "|" + $voice.VoiceInfo.Description)
    }
}
$synth.Dispose()
