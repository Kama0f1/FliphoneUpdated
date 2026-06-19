param(
    [string]$PythonPath = "C:\Users\ADMIN\AppData\Local\Programs\Python\Python312\python.exe"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$BackupScript = Join-Path $PSScriptRoot "backup_postgres_to_sqlite.py"

& $PythonPath $BackupScript --init-key

$Action = New-ScheduledTaskAction `
    -Execute $PythonPath `
    -Argument ('"{0}"' -f $BackupScript) `
    -WorkingDirectory $ProjectRoot
$Trigger = New-ScheduledTaskTrigger -Daily -At "12:00 PM"
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName "Fliphone Weekly Encrypted Backup" `
    -Description "Creates a verified encrypted PostgreSQL-to-SQLite backup when seven days have elapsed." `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Force

Write-Host "Backup task installed. It checks daily at noon and catches up after missed runs."
