# Reconcile interrupted operations using durable transaction records.
param([Parameter(Mandatory)][string]$Root, [switch]$AllowSystemDrive)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
$arguments = @('recover','--root',$Root)
if ($AllowSystemDrive) { $arguments += '--allow-system-drive' }
Invoke-Organizer -Arguments $arguments
exit $script:OrganizerExitCode
