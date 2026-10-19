param(
    [string]$TaskName = "SmartHomeCameraRecording"
)

$ErrorActionPreference = "Stop"
$serviceDir = $PSScriptRoot
$projectDir = Split-Path -Parent $serviceDir
$pythonCommand = Get-Command python.exe -ErrorAction Stop
$pythonExe = $pythonCommand.Source
$pythonwExe = Join-Path (Split-Path -Parent $pythonExe) "pythonw.exe"
if (Test-Path -LiteralPath $pythonwExe) {
    $pythonExe = $pythonwExe
}

$arguments = "-m recording_service --config `"$serviceDir\config.json`""
$action = New-ScheduledTaskAction -Execute $pythonExe -Argument $arguments -WorkingDirectory $projectDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Write-Host "Installed '$TaskName'. It starts at user logon and records independently of AppDemo."
Write-Host "Start it now with: Start-ScheduledTask -TaskName '$TaskName'"
