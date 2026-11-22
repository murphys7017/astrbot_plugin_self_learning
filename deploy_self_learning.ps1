param(
    [string]$PluginsRoot = "C:\Users\Administrator\Downloads\AstrBot\data\plugins",
    [switch]$DryRun,
    [string[]]$OnlyFiles,
    [string]$SourceRef,
    [switch]$PreserveExtraFiles,
    [string]$SourceRoot
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-MetadataValue {
    param(
        [string]$MetadataPath,
        [string]$Key
    )

    $pattern = "^\s*" + [Regex]::Escape($Key) + ":\s*(.+?)\s*$"
    foreach ($line in Get-Content -LiteralPath $MetadataPath -Encoding UTF8) {
        if ($line -match $pattern) {
            return $Matches[1].Trim().Trim('"')
        }
    }
    return $null
}

function Get-RelativePathCompat {
    param(
        [string]$BasePath,
        [string]$TargetPath
    )

    $baseUri = New-Object System.Uri(($BasePath.TrimEnd('\') + '\'))
    $targetUri = New-Object System.Uri($TargetPath)
    $relativeUri = $baseUri.MakeRelativeUri($targetUri)
    return [System.Uri]::UnescapeDataString($relativeUri.ToString()).Replace('/', '\')
}

function Test-PathWithin {
    param(
        [string]$RootPath,
        [string]$CandidatePath
    )

    $root = ([System.IO.Path]::GetFullPath($RootPath)).TrimEnd('\') + '\'
    $candidate = ([System.IO.Path]::GetFullPath($CandidatePath)).TrimEnd('\') + '\'
    return $candidate.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)
}

function Test-RelativePathInList {
    param(
        [string]$RelativePath,
        [string[]]$Prefixes
    )

    $normalized = $RelativePath.Replace('/', '\').Trim('\')
    foreach ($prefix in $Prefixes) {
        $cleanPrefix = $prefix.Replace('/', '\').Trim('\')
        if ($normalized -eq $cleanPrefix -or $normalized.StartsWith($cleanPrefix + '\')) {
            return $true
        }
    }
    return $false
}

function Test-SkippedRelativePath {
    param([string]$RelativePath)

    $segments = $RelativePath.Replace('/', '\') -split "[\\/]+"
    foreach ($segment in $segments) {
        if ($segment -in @(".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", "tests")) {
            return $true
        }
    }
    return $false
}

function Invoke-LoggedAction {
    param(
        [string]$Description,
        [scriptblock]$Action,
        [switch]$DryRun
    )

    if ($DryRun) {
        Write-Host "[DryRun] $Description"
        return
    }

    Write-Host $Description
    & $Action
}

$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

if ($SourceRef) {
    $commit = git -C $RepoRoot rev-parse --verify --end-of-options "${SourceRef}^{commit}"
    if ($LASTEXITCODE -ne 0 -or $commit -notmatch '^[0-9a-f]{40,64}$') {
        throw "Cannot resolve Self-Learning deployment revision: $SourceRef"
    }

    $tempRoot = (Resolve-Path -LiteralPath ([System.IO.Path]::GetTempPath())).Path.TrimEnd('\')
    $stage = Join-Path $tempRoot ("self-learning-deploy-" + [Guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $stage | Out-Null
    try {
        $archive = Join-Path $stage "source.tar"
        git -C $RepoRoot archive --format=tar "-o$archive" $commit
        if ($LASTEXITCODE -ne 0) {
            throw "Could not archive Self-Learning at $commit"
        }
        $windowsTar = Join-Path ([Environment]::SystemDirectory) "tar.exe"
        & $windowsTar -xf $archive -C $stage
        if ($LASTEXITCODE -ne 0) {
            throw "Could not extract Self-Learning at $commit"
        }

        $stagedScript = Join-Path $stage "deploy_self_learning.ps1"
        Copy-Item -LiteralPath $PSCommandPath -Destination $stagedScript -Force
        $stagedArguments = @{
            PluginsRoot = $PluginsRoot
            DryRun = $DryRun
            PreserveExtraFiles = $PreserveExtraFiles
            SourceRoot = $stage
        }
        if ($OnlyFiles) {
            $stagedArguments["OnlyFiles"] = $OnlyFiles
        }
        Write-Host "Deploy revision : $commit"
        & $stagedScript @stagedArguments
    } finally {
        if (Test-Path -LiteralPath $stage) {
            $resolvedStage = (Resolve-Path -LiteralPath $stage).Path
            if (-not (Test-PathWithin -RootPath $tempRoot -CandidatePath $resolvedStage)) {
                throw "Unexpected Self-Learning staging directory: $resolvedStage"
            }
            Remove-Item -LiteralPath $resolvedStage -Recurse -Force
        }
    }
    return
}

$SourcePluginRoot = if ($SourceRoot) {
    [System.IO.Path]::GetFullPath($SourceRoot)
} else {
    $RepoRoot
}
$MetadataPath = Join-Path $SourcePluginRoot "metadata.yaml"

if (-not (Test-Path -LiteralPath $SourcePluginRoot -PathType Container)) {
    throw "Source plugin directory not found: $SourcePluginRoot"
}
if (-not (Test-Path -LiteralPath $MetadataPath -PathType Leaf)) {
    throw "metadata.yaml not found: $MetadataPath"
}

$PluginName = Get-MetadataValue -MetadataPath $MetadataPath -Key "name"
if ([string]::IsNullOrWhiteSpace($PluginName) -or $PluginName -notlike "astrbot_plugin_*") {
    throw "Invalid plugin name in metadata.yaml: $PluginName"
}

$ResolvedPluginsRoot = (Resolve-Path -LiteralPath $PluginsRoot).Path
$TargetPluginRoot = Join-Path $ResolvedPluginsRoot $PluginName
$ResolvedTargetPluginRoot = [System.IO.Path]::GetFullPath($TargetPluginRoot)
if (-not (Test-PathWithin -RootPath $ResolvedPluginsRoot -CandidatePath $ResolvedTargetPluginRoot)) {
    throw "Resolved target path escapes plugins root: $ResolvedTargetPluginRoot"
}
if (Test-Path -LiteralPath $ResolvedTargetPluginRoot -PathType Container) {
    $targetItem = Get-Item -LiteralPath $ResolvedTargetPluginRoot -Force
    if ($targetItem.Attributes.ToString().Contains("ReparsePoint")) {
        throw "Refusing to deploy through a reparse-point target: $ResolvedTargetPluginRoot"
    }
}

# These paths are present in the current runtime package but are absent from
# this source checkout. Keep them until their current contents are imported
# into the repository. Once imported, source files automatically become managed.
$RuntimeCompatibilityDirectories = @(".astrbot-plugin", "docs", "pages", "web_src")
$RuntimeCompatibilityFiles = @(".gitattributes", "ruff.toml")
$SkipFilePatterns = @("*.pyc", "pytest.ini", "deploy_self_learning.ps1", "source.tar")

$ManagedRelativeFiles = [System.Collections.Generic.HashSet[string]]::new(
    [System.StringComparer]::OrdinalIgnoreCase
)
$ManagedRelativeDirectories = [System.Collections.Generic.HashSet[string]]::new(
    [System.StringComparer]::OrdinalIgnoreCase
)

if ($OnlyFiles) {
    if (-not (Test-Path -LiteralPath $ResolvedTargetPluginRoot -PathType Container)) {
        throw "A full deploy is required before deploying selected files."
    }
    $OnlyFiles = @($OnlyFiles | ForEach-Object { $_.Replace('/', '\').Trim('\') } | Sort-Object -Unique)
}

Invoke-LoggedAction -Description "Ensure target plugin directory exists: $ResolvedTargetPluginRoot" -DryRun:$DryRun -Action {
    New-Item -ItemType Directory -Path $ResolvedTargetPluginRoot -Force | Out-Null
}

$sourceDirectories = Get-ChildItem -LiteralPath $SourcePluginRoot -Recurse -Directory | Where-Object {
    $relativePath = Get-RelativePathCompat -BasePath $SourcePluginRoot -TargetPath $_.FullName
    if ([string]::IsNullOrWhiteSpace($relativePath) -or (Test-SkippedRelativePath $relativePath)) {
        return $false
    }
    if ($OnlyFiles) {
        return $false
    }
    return $true
}

foreach ($directory in $sourceDirectories) {
    $relativePath = Get-RelativePathCompat -BasePath $SourcePluginRoot -TargetPath $directory.FullName
    [void]$ManagedRelativeDirectories.Add($relativePath)
    $targetDirectory = Join-Path $ResolvedTargetPluginRoot $relativePath
    Invoke-LoggedAction -Description "Ensure directory: $targetDirectory" -DryRun:$DryRun -Action {
        New-Item -ItemType Directory -Path $targetDirectory -Force | Out-Null
    }
}

$sourceFiles = @(Get-ChildItem -LiteralPath $SourcePluginRoot -Recurse -File | Where-Object {
    $relativePath = Get-RelativePathCompat -BasePath $SourcePluginRoot -TargetPath $_.FullName
    if ($OnlyFiles -and $relativePath -notin $OnlyFiles) {
        return $false
    }
    if (Test-SkippedRelativePath $relativePath) {
        return $false
    }
    foreach ($pattern in $SkipFilePatterns) {
        if ($_.Name -like $pattern) {
            return $false
        }
    }
    return $true
})

if ($OnlyFiles -and $sourceFiles.Count -ne $OnlyFiles.Count) {
    throw "One or more selected paths are not managed source files: $($OnlyFiles -join ', ')"
}

foreach ($file in $sourceFiles) {
    $relativePath = Get-RelativePathCompat -BasePath $SourcePluginRoot -TargetPath $file.FullName
    [void]$ManagedRelativeFiles.Add($relativePath)
    $targetPath = Join-Path $ResolvedTargetPluginRoot $relativePath
    $targetParent = Split-Path -Parent $targetPath
    Invoke-LoggedAction -Description "Ensure directory: $targetParent" -DryRun:$DryRun -Action {
        New-Item -ItemType Directory -Path $targetParent -Force | Out-Null
    }
    Invoke-LoggedAction -Description "Copy file: $relativePath" -DryRun:$DryRun -Action {
        Copy-Item -LiteralPath $file.FullName -Destination $targetPath -Force
    }
}

if (-not $OnlyFiles -and (Test-Path -LiteralPath $ResolvedTargetPluginRoot -PathType Container)) {
    $targetFiles = Get-ChildItem -LiteralPath $ResolvedTargetPluginRoot -Recurse -File
    foreach ($file in $targetFiles) {
        $relativePath = Get-RelativePathCompat -BasePath $ResolvedTargetPluginRoot -TargetPath $file.FullName
        if ($ManagedRelativeFiles.Contains($relativePath) -or (Test-SkippedRelativePath $relativePath)) {
            continue
        }
        $isCompatibilityPath = ((Test-RelativePathInList -RelativePath $relativePath -Prefixes $RuntimeCompatibilityDirectories) -or ($RuntimeCompatibilityFiles -contains $relativePath))
        if ($PreserveExtraFiles -or $isCompatibilityPath) {
            continue
        }
        Invoke-LoggedAction -Description "Remove stale file: $relativePath" -DryRun:$DryRun -Action {
            Remove-Item -LiteralPath $file.FullName -Force
        }
    }

    $targetDirectories = Get-ChildItem -LiteralPath $ResolvedTargetPluginRoot -Recurse -Directory |
        Sort-Object FullName -Descending
    foreach ($directory in $targetDirectories) {
        $relativePath = Get-RelativePathCompat -BasePath $ResolvedTargetPluginRoot -TargetPath $directory.FullName
        if (Test-SkippedRelativePath $relativePath) {
            continue
        }
        if (Test-RelativePathInList -RelativePath $relativePath -Prefixes $RuntimeCompatibilityDirectories) {
            continue
        }
        if ($PreserveExtraFiles) {
            continue
        }
        if (-not $ManagedRelativeDirectories.Contains($relativePath)) {
            Invoke-LoggedAction -Description "Remove stale directory: $relativePath" -DryRun:$DryRun -Action {
                Remove-Item -LiteralPath $directory.FullName -Recurse -Force
            }
        }
    }
}

Write-Host ""
Write-Host "Deploy source : $SourcePluginRoot"
Write-Host "Deploy target : $ResolvedTargetPluginRoot"
Write-Host "Plugin name   : $PluginName"
if ($DryRun) {
    Write-Host "Mode          : DryRun"
} else {
    Write-Host "Mode          : Apply"
    Write-Host "Runtime       : Reload or restart AstrBot before testing."
}
