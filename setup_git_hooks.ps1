param(
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$HooksPath = Join-Path $RepoRoot ".githooks"
if (-not (Test-Path -LiteralPath (Join-Path $HooksPath "post-commit") -PathType Leaf)) {
    throw "Tracked post-commit hook not found: $HooksPath"
}

if ($DryRun) {
    Write-Host "[DryRun] git -C $RepoRoot config core.hooksPath .githooks"
    return
}

git -C $RepoRoot config core.hooksPath .githooks
if ($LASTEXITCODE -ne 0) {
    throw "Could not configure Git hooks path."
}
Write-Host "Git hooks configured: $HooksPath"
