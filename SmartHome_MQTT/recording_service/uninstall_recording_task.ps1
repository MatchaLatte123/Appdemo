param(
    [string]$TaskName = "SmartHomeCameraRecording"
)

$ErrorActionPreference = "Stop"
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Write-Host "Removed '$TaskName'. Any unfinished segment remains marked .partial.mp4."
