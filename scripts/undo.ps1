# Undo a run; an undo run can itself be undone. Repeated undo is idempotent.
param(
    [Parameter(Mandatory)][string]$Journal, [string]$RunId,
    [int]$Seq = 0, [switch]$Last, [switch]$List, [switch]$DryRun,
    [switch]$AllowSystemDrive, [int]$ExactSeq = 0
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
$arguments = @('undo','--journal',$Journal)
$selectionCount = [int][bool]$RunId + [int]($Seq -gt 0) + [int]($ExactSeq -gt 0) + [int][bool]$Last + [int][bool]$List
if ($selectionCount -ne 1) { throw 'Select exactly one of -RunId, -Seq, -ExactSeq, -Last or -List.' }
if ($RunId) { $arguments += @('--run-id',$RunId) }
if ($Seq -gt 0) { $arguments += @('--seq',[string]$Seq) }
if ($ExactSeq -gt 0) { $arguments += @('--exact-seq',[string]$ExactSeq) }
if ($Last) { $arguments += '--last' }
if ($List) { $arguments += '--list' }
if ($DryRun) { $arguments += '--dry-run' }
if ($AllowSystemDrive) { $arguments += '--allow-system-drive' }
Invoke-Organizer -Arguments $arguments
exit $script:OrganizerExitCode
