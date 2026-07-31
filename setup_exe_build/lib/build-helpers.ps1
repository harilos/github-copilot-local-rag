function Write-Step {
    param(
        [Parameter(Mandatory = $true)][int]$Current,
        [Parameter(Mandatory = $true)][int]$Total,
        [Parameter(Mandatory = $true)][string]$Message
    )
    $percent = [Math]::Floor(($Current / [double]$Total) * 100)
    Write-Progress -Activity "Local RAG Setup.exe build" -Status $Message -PercentComplete $percent
    Write-Host ("[{0}/{1}] {2}" -f $Current, $Total, $Message)
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$FailureMessage
    )
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$FailureMessage (exit=$LASTEXITCODE)"
    }
}

function Resolve-ConfiguredPath {
    param([string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value)) {
        return $null
    }
    if ([System.IO.Path]::IsPathRooted($Value)) {
        return [System.IO.Path]::GetFullPath($Value)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $ScriptRoot $Value))
}

function Select-Folder {
    param(
        [Parameter(Mandatory = $true)][string]$Description,
        [string]$InitialDirectory
    )
    try {
        Add-Type -AssemblyName System.Windows.Forms
        $dialog = New-Object System.Windows.Forms.FolderBrowserDialog
        $dialog.Description = $Description
        $dialog.ShowNewFolderButton = $false
        if ($InitialDirectory -and (Test-Path -LiteralPath $InitialDirectory -PathType Container)) {
            $dialog.SelectedPath = $InitialDirectory
        }
        $result = $dialog.ShowDialog()
        if ($result -ne [System.Windows.Forms.DialogResult]::OK) {
            throw "Folder selection was cancelled."
        }
        return $dialog.SelectedPath
    } catch [System.Management.Automation.RuntimeException] {
        throw
    } catch {
        Write-Warning "Folder picker could not be opened: $($_.Exception.Message)"
        $value = Read-Host $Description
        if ([string]::IsNullOrWhiteSpace($value)) {
            throw "Folder selection was cancelled."
        }
        return $value
    }
}

function Resolve-DictionaryDirectory {
    param([string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value)) {
        $preferred = Join-Path $HOME ".copilot\rag\dbs"
        if (-not (Test-Path -LiteralPath $preferred -PathType Container)) {
            $preferred = Join-Path $RepoRoot ".copilot\rag\dbs"
        }
        $Value = Select-Folder `
            -Description "Setup.exeに同封する辞書（xxx-ragフォルダ）を選択してください" `
            -InitialDirectory $preferred
    }
    $resolved = (Resolve-Path -LiteralPath $Value).Path
    $item = Get-Item -LiteralPath $resolved -Force
    if (-not $item.PSIsContainer -or (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0)) {
        throw "The selected dictionary must be a real directory: $resolved"
    }
    if ($item.Parent.Name -ine "dbs") {
        throw "Select the xxx-rag folder directly below a rag\dbs directory."
    }
    if ($item.Name -notmatch '^[A-Za-z0-9][A-Za-z0-9_.-]*-rag$') {
        throw "Dictionary folder names must use ASCII letters, digits, dot, underscore, or hyphen and end in -rag."
    }
    foreach ($required in @("VERSION.json", "db.json", "catalog.sqlite")) {
        if (-not (Test-Path -LiteralPath (Join-Path $resolved $required) -PathType Leaf)) {
            throw "The selected dictionary is missing $required."
        }
    }
    if (-not (Test-Path -LiteralPath (Join-Path $resolved "index") -PathType Container)) {
        throw "The selected dictionary is missing its index directory."
    }
    return $item
}

function Resolve-BuildPythonExecutable {
    param(
        [string]$RequestedPath,
        [Parameter(Mandatory = $true)][System.IO.DirectoryInfo]$DictionaryDirectory
    )
    $candidates = New-Object System.Collections.Generic.List[string]
    if (-not [string]::IsNullOrWhiteSpace($RequestedPath)) {
        $candidates.Add($RequestedPath)
    }
    $dictionaryRagRoot = $DictionaryDirectory.Parent.Parent.FullName
    $candidates.Add((Join-Path $dictionaryRagRoot "query\.venv\Scripts\python.exe"))
    $candidates.Add((Join-Path $RepoRoot ".copilot\rag\query\.venv\Scripts\python.exe"))
    $candidates.Add((Join-Path $HOME ".copilot\rag\query\.venv\Scripts\python.exe"))
    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($pythonCommand) {
        $candidates.Add($pythonCommand.Source)
    }

    foreach ($candidate in $candidates) {
        if ([string]::IsNullOrWhiteSpace($candidate)) {
            continue
        }
        try {
            $resolved = (Resolve-Path -LiteralPath $candidate).Path
        } catch {
            continue
        }
        if (Test-Path -LiteralPath $resolved -PathType Leaf) {
            return $resolved
        }
    }
    throw (
        "A Windows x64 Python with pip is required only on the build PC. " +
        "Run Local RAG setup first or pass -BuildPythonPath."
    )
}

function Get-PythonInformation {
    param([Parameter(Mandatory = $true)][string]$PythonExecutable)
    $code = @'
import json, platform, struct, sys
print(json.dumps({
    "implementation": platform.python_implementation(),
    "machine": platform.machine(),
    "bits": struct.calcsize("P") * 8,
    "version": platform.python_version(),
    "major": sys.version_info.major,
    "minor": sys.version_info.minor,
}))
'@
    $output = & $PythonExecutable -c $code
    if ($LASTEXITCODE -ne 0) {
        throw "Could not inspect the build Python."
    }
    $info = $output | ConvertFrom-Json
    if ($info.implementation -ne "CPython" -or [int]$info.bits -ne 64) {
        throw "The build Python must be 64-bit CPython."
    }
    Invoke-Checked -FilePath $PythonExecutable -Arguments @("-m", "pip", "--version") `
        -FailureMessage "pip is unavailable in the build Python"
    return $info
}

function Get-OrDownloadFile {
    param(
        [Parameter(Mandatory = $true)][string]$Uri,
        [Parameter(Mandatory = $true)][string]$Destination
    )
    if (Test-Path -LiteralPath $Destination -PathType Leaf) {
        return
    }
    $parent = Split-Path -Parent $Destination
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    $temporary = "$Destination.partial"
    Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
    Write-Host "Downloading: $Uri"
    try {
        Invoke-WebRequest -UseBasicParsing -Uri $Uri -OutFile $temporary
        Move-Item -LiteralPath $temporary -Destination $Destination -Force
    } finally {
        Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
    }
}

function Resolve-MakeNsis {
    param([Parameter(Mandatory = $true)][string]$Version)
    $command = Get-Command makensis.exe -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }
    $installedCandidates = New-Object System.Collections.Generic.List[string]
    foreach ($environmentName in @("ProgramFiles(x86)", "ProgramFiles")) {
        $programFiles = [Environment]::GetEnvironmentVariable($environmentName)
        if (-not [string]::IsNullOrWhiteSpace($programFiles)) {
            $installedCandidates.Add((Join-Path $programFiles "NSIS\makensis.exe"))
        }
    }
    foreach ($candidate in $installedCandidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return $candidate
        }
    }

    $archive = Join-Path $CacheRoot "nsis-$Version.zip"
    $extractRoot = Join-Path $CacheRoot "nsis-$Version"
    Get-OrDownloadFile `
        -Uri "https://downloads.sourceforge.net/project/nsis/NSIS%203/$Version/nsis-$Version.zip" `
        -Destination $archive
    if (-not (Test-Path -LiteralPath $extractRoot -PathType Container)) {
        New-Item -ItemType Directory -Force -Path $extractRoot | Out-Null
        Expand-Archive -LiteralPath $archive -DestinationPath $extractRoot -Force
    }
    $resolved = Get-ChildItem -LiteralPath $extractRoot -Filter makensis.exe -File -Recurse |
        Select-Object -First 1
    if (-not $resolved) {
        throw "Downloaded NSIS archive did not contain makensis.exe."
    }
    return $resolved.FullName
}

function Invoke-PayloadStaging {
    param(
        [Parameter(Mandatory = $true)][string]$PythonExecutable,
        [Parameter(Mandatory = $true)][System.IO.DirectoryInfo]$DictionaryDirectory
    )
    $helper = Join-Path $ScriptRoot "stage_payload.py"
    $arguments = @(
        $helper,
        "--repo-root", $RepoRoot,
        "--dictionary", $DictionaryDirectory.FullName,
        "--app-stage", $AppStage,
        "--dictionary-stage", $DictionaryStage
    )
    $lines = & $PythonExecutable @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Search-only product and dictionary staging failed."
    }
    $jsonLine = $lines | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
        Select-Object -Last 1
    if (-not $jsonLine) {
        throw "Payload staging returned no result."
    }
    return $jsonLine | ConvertFrom-Json
}

function Get-NormalizedRequirementName {
    param([string]$Line)
    $match = [regex]::Match($Line, '^\s*([A-Za-z0-9_.-]+)')
    if (-not $match.Success) {
        return ""
    }
    return ([regex]::Replace($match.Groups[1].Value.ToLowerInvariant(), '[-_.]+', '-'))
}

function Write-SearchOnlyRequirements {
    param([Parameter(Mandatory = $true)][string]$AppCopilotRoot)
    $source = Join-Path $RepoRoot ".copilot\rag\gen_db\software_rag_tool\requirements.txt"
    $destination = Join-Path $AppCopilotRoot "rag\gen_db\software_rag_tool\requirements.txt"
    $generated = Join-Path $GeneratedRoot "runtime-requirements.txt"
    $output = New-Object System.Collections.Generic.List[string]
    $output.Add("# Generated by setup_exe_build/build_setup.ps1")
    $output.Add("# Search-only runtime; model-build and administrator ingestion packages are excluded.")
    $output.Add("packaging>=24.0")
    foreach ($raw in Get-Content -LiteralPath $source -Encoding UTF8) {
        $line = $raw.Trim()
        if (-not $line -or $line.StartsWith("#")) {
            continue
        }
        if ($line.StartsWith("-")) {
            throw "Unsupported requirement include or option in $source: $line"
        }
        $name = Get-NormalizedRequirementName $line
        if ($ExcludedDistributionNames -contains $name) {
            Write-Host "Excluded from Setup.exe runtime: $line"
            continue
        }
        if ($name -eq "packaging") {
            continue
        }
        $output.Add($line)
    }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $generated) | Out-Null
    [System.IO.File]::WriteAllLines($generated, $output, [System.Text.UTF8Encoding]::new($false))
    [System.IO.File]::WriteAllLines($destination, $output, [System.Text.UTF8Encoding]::new($false))
    return $generated
}

function Convert-StagedVerificationToSearchOnly {
    param([Parameter(Mandatory = $true)][string]$AppCopilotRoot)
    $path = Join-Path $AppCopilotRoot "rag\query\setup_verification.py"
    $content = [System.IO.File]::ReadAllText($path, [System.Text.Encoding]::UTF8)
    $pattern = '(?s)REQUIRED_IMPORTS = \(\r?\n.*?\r?\n\)\r?\nREQUIRED_MODEL_FILES'
    $replacement = @'
REQUIRED_IMPORTS = (
    "packaging",
    "chromadb",
    "numpy",
    "onnxruntime",
    "transformers",
    "sentencepiece",
    "sudachipy",
)
REQUIRED_MODEL_FILES
'@
    $regex = [regex]::new($pattern)
    if (-not $regex.IsMatch($content)) {
        throw "Could not convert setup_verification.py to the packaged search-only contract."
    }
    $content = $regex.Replace($content, $replacement, 1)
    [System.IO.File]::WriteAllText($path, $content, [System.Text.UTF8Encoding]::new($false))

    $marker = Join-Path $AppCopilotRoot "rag\query\.packaged-runtime"
    $markerPayload = [ordered]@{
        schema = "local-rag.packaged-runtime.v1"
        kind = "general-user-search-only"
        excluded_distributions = $ExcludedDistributionNames
    } | ConvertTo-Json -Depth 4
    [System.IO.File]::WriteAllText(
        $marker,
        $markerPayload + "`n",
        [System.Text.UTF8Encoding]::new($false)
    )
}

function Convert-StagedSetupToPackagedRuntime {
    param([Parameter(Mandatory = $true)][string]$AppCopilotRoot)
    $path = Join-Path $AppCopilotRoot "rag\query\setup.py"
    $content = [System.IO.File]::ReadAllText($path, [System.Text.Encoding]::UTF8)
    $pattern = '    args = parser\.parse_args\(\)\r?\n'
    $replacement = @'
    args = parser.parse_args()
    packaged_runtime = Path(__file__).with_name(".packaged-runtime").is_file()
    if packaged_runtime and not (
        args.verify_only
        or args.migrate_legacy_marker
        or args.refresh_completion_marker
        or args.repair_completion_marker
    ):
        if args.force_model or args.prepare_model or args.no_prepare_model:
            parser.error(
                "packaged search runtime does not support model-build setup flags; "
                "rerun the Windows Setup.exe to repair the installation"
            )
        # Setup.exe owns the embedded interpreter, dependencies, and ONNX model.
        # A packaged user's setup request therefore performs an offline
        # verification/marker refresh instead of downloading build-only tools.
        args.refresh_completion_marker = True
'@
    $replacement += "`n"
    $regex = [regex]::new($pattern)
    if (-not $regex.IsMatch($content)) {
        throw "Could not convert setup.py to the packaged search-only contract."
    }
    $content = $regex.Replace($content, $replacement, 1)
    [System.IO.File]::WriteAllText($path, $content, [System.Text.UTF8Encoding]::new($false))
}
