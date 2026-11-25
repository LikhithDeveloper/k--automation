#Requires -Version 7.4
param([Parameter(Mandatory)][ValidateSet('stop', 'start')][string]$Action)

. "$PSScriptRoot/Maintenance.ps1"
exit (Invoke-MaintenanceStage -Role webapp -Action $Action)
