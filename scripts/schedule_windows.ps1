# Registers (or re-registers) a Windows Task Scheduler job that runs quote_probe.py at 11:00, 16:00 and 21:00
# local time. Output of each run is appended to data\probe_runs.log.
#   powershell -ExecutionPolicy Bypass -File scripts\schedule_windows.ps1            # register / update
#   powershell -ExecutionPolicy Bypass -File scripts\schedule_windows.ps1 -Remove    # unregister
# Runs as the current user, only while logged on (inherits user env vars such as ONEINCH_API_KEY, and sees
# mapped drives such as Google Drive). A run missed while the PC was off/asleep starts as soon as it is back.
param(
    [string]$Python = (Get-Command python -ErrorAction Stop).Source,
    [string[]]$Times = @("11:00", "16:00", "21:00"),
    [switch]$Remove
)
$TaskName = "Tokenlon quote_probe"

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    "removed: $TaskName"
    return
}

$Repo = Split-Path -Parent $PSScriptRoot
$launcher = Join-Path $PSScriptRoot "run_quote_probe_hidden.vbs"
$action = New-ScheduledTaskAction -Execute "wscript.exe" -WorkingDirectory $Repo `
    -Argument "//B //Nologo `"$launcher`" `"$Python`""
$triggers = $Times | ForEach-Object { New-ScheduledTaskTrigger -Daily -At $_ }
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers -Settings $settings `
    -Description "quote_probe.py live run; output -> data\probe_runs.log" -Force | Out-Null
Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo | Select-Object TaskName, NextRunTime
