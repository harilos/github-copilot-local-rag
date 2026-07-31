[CmdletBinding()]
param(
    [string]$DictionaryPath,
    [string]$WelcomeImagePath,
    [string]$BuildPythonPath,
    [switch]$KeepWork
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "Continue"

$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptRoot
$ConfigPath = Join-Path $ScriptRoot "installer-config.psd1"
$WorkRoot = Join-Path $ScriptRoot "work"
$CacheRoot = Join-Path $WorkRoot "cache"
$AppStage = Join-Path $WorkRoot "app-stage"
$DictionaryStage = Join-Path $WorkRoot "dictionary-stage"
$GeneratedRoot = Join-Path $WorkRoot "generated"
$PipCache = Join-Path $CacheRoot "pip"
$DefaultModelDirectoryName = "ruri-v3-30m-onnx-int8"
# These packages are useful only for document ingestion, PyTorch fallback, or
# ONNX model creation. The general-user installer ships a prepared ONNX model
# and search-only product files, so none of them belongs in the EXE runtime.
$ExcludedDistributionNames = @(
    "sentence-transformers",
    "onnx",
    "optimum",
    "pypdf",
    "cryptography",
    "python-docx",
    "python-pptx",
    "openpyxl"
)

$LibraryRoot = Join-Path $ScriptRoot "lib"
. (Join-Path $LibraryRoot "build-helpers.ps1")
. (Join-Path $LibraryRoot "build-runtime.ps1")
. (Join-Path $LibraryRoot "build-nsis.ps1")

if ($env:OS -ne "Windows_NT") {
    throw "This builder must be run on Windows."
}
if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
    throw "Missing installer config: $ConfigPath"
}

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$Config = Import-PowerShellDataFile -LiteralPath $ConfigPath
$TotalSteps = 10

try {
    New-Item -ItemType Directory -Force -Path $WorkRoot, $CacheRoot, $GeneratedRoot, $PipCache | Out-Null

    Write-Step 1 $TotalSteps "同封する初期辞書を選択"
    $DictionaryDirectory = Resolve-DictionaryDirectory $DictionaryPath
    Write-Host "Dictionary: $($DictionaryDirectory.FullName)"

    Write-Step 2 $TotalSteps "ビルド用PythonとNSISを準備"
    $BuildPython = Resolve-BuildPythonExecutable `
        -RequestedPath $BuildPythonPath `
        -DictionaryDirectory $DictionaryDirectory
    $PythonInfo = Get-PythonInformation $BuildPython
    $MakeNsis = Resolve-MakeNsis ([string]$Config.NsisVersion)
    Write-Host "Build Python: $BuildPython ($($PythonInfo.version), $($PythonInfo.bits)-bit)"
    Write-Host "NSIS: $MakeNsis"

    Write-Step 3 $TotalSteps "一般利用者向け検索ファイルと辞書を安全にステージング"
    $Stage = Invoke-PayloadStaging -PythonExecutable $BuildPython -DictionaryDirectory $DictionaryDirectory
    $AppCopilotRoot = [string]$Stage.app_root
    $DictionaryRoot = [string]$Stage.dictionary_root
    $Version = [string]$Stage.version

    Write-Step 4 $TotalSteps "管理・モデル生成用の重い依存を除外"
    $RuntimeRequirements = Write-SearchOnlyRequirements -AppCopilotRoot $AppCopilotRoot
    Convert-StagedVerificationToSearchOnly -AppCopilotRoot $AppCopilotRoot
    Convert-StagedSetupToPackagedRuntime -AppCopilotRoot $AppCopilotRoot
    Copy-InstallerPostInstallHelper -AppCopilotRoot $AppCopilotRoot

    Write-Step 5 $TotalSteps "Windows用の埋め込みPythonと検索依存だけを配置"
    $EmbeddedPython = Prepare-EmbeddedPython `
        -BuildPython $BuildPython `
        -PythonInfo $PythonInfo `
        -RequestedVersion ([string]$Config.EmbeddedPythonVersion) `
        -RequirementsPath $RuntimeRequirements `
        -AppCopilotRoot $AppCopilotRoot

    Write-Step 6 $TotalSteps "準備済みONNXモデルを同封"
    $ModelDestination = Resolve-ModelDirectory `
        -DictionaryDirectory $DictionaryDirectory `
        -AppCopilotRoot $AppCopilotRoot
    Write-Host "Model: $ModelDestination"

    Write-Step 7 $TotalSteps "検索利用判定マーカーを生成"
    $previousDbsRoot = $env:RAG_DBS_ROOT
    try {
        $env:RAG_DBS_ROOT = Split-Path -Parent $DictionaryRoot
        Invoke-Checked `
            -FilePath $EmbeddedPython `
            -Arguments @(
                (Join-Path $AppCopilotRoot "rag\query\setup.py"),
                "--refresh-completion-marker",
                "--format", "json"
            ) `
            -FailureMessage "Packaged search runtime verification failed"
    } finally {
        if ($null -eq $previousDbsRoot) {
            Remove-Item Env:RAG_DBS_ROOT -ErrorAction SilentlyContinue
        } else {
            $env:RAG_DBS_ROOT = $previousDbsRoot
        }
    }

    Write-Step 8 $TotalSteps "初期画面の画像と文言を生成"
    $configuredImage = if (-not [string]::IsNullOrWhiteSpace($WelcomeImagePath)) {
        Resolve-ConfiguredPath $WelcomeImagePath
    } else {
        Resolve-ConfiguredPath ([string]$Config.WelcomeImagePath)
    }
    if ($configuredImage -and -not (Test-Path -LiteralPath $configuredImage -PathType Leaf)) {
        throw "Welcome image was not found: $configuredImage"
    }
    $WelcomeBitmap = Join-Path $GeneratedRoot "welcome.bmp"
    New-WelcomeBitmap -SourceImage $configuredImage -Destination $WelcomeBitmap

    $outputName = ([string]$Config.OutputFileName).Replace("{version}", $Version)
    if ([string]::IsNullOrWhiteSpace($outputName) -or [System.IO.Path]::GetFileName($outputName) -ne $outputName) {
        throw "OutputFileName must be a file name, not a path."
    }
    if (-not $outputName.EndsWith(".exe", [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "OutputFileName must end in .exe."
    }
    $OutputPath = Join-Path $ScriptRoot $outputName
    Remove-Item -LiteralPath $OutputPath -Force -ErrorAction SilentlyContinue
    $NsisScript = Write-NsisScript `
        -Config $Config `
        -Version $Version `
        -OutputPath $OutputPath `
        -AppCopilotRoot $AppCopilotRoot `
        -DictionaryRoot $DictionaryRoot `
        -DictionaryName $DictionaryDirectory.Name `
        -WelcomeBitmap $WelcomeBitmap

    Write-Step 9 $TotalSteps "NSISで単一Setup.exeを作成"
    $sourceBytes = (Get-DirectoryBytes $AppCopilotRoot) + (Get-DirectoryBytes $DictionaryRoot)
    if ($sourceBytes -gt 1500MB) {
        Write-Warning (
            "The uncompressed payload is {0:N1} GB. NSIS has a practical single-EXE size limit, " +
            "so this build may need a smaller dictionary." -f ($sourceBytes / 1GB)
        )
    }
    Invoke-Checked -FilePath $MakeNsis -Arguments @("/V4", $NsisScript) `
        -FailureMessage "NSIS compilation failed"
    if (-not (Test-Path -LiteralPath $OutputPath -PathType Leaf)) {
        throw "NSIS completed without creating the expected EXE: $OutputPath"
    }

    Write-Step 10 $TotalSteps "成果物を確認"
    $output = Get-Item -LiteralPath $OutputPath
    if ($output.Length -gt 1900MB) {
        Write-Warning "The generated EXE is close to the NSIS single-file size ceiling."
    }
    Write-Progress -Activity "Local RAG Setup.exe build" -Completed
    Write-Host ""
    Write-Host "Created: $($output.FullName)"
    Write-Host ("Size: {0:N1} MB" -f ($output.Length / 1MB))
    Write-Host "Dictionary: $($DictionaryDirectory.Name)"
    Write-Host "Heavy model-build and administrator ingestion dependencies were excluded."

    if (-not $KeepWork) {
        Remove-Item -LiteralPath $AppStage, $DictionaryStage -Recurse -Force -ErrorAction SilentlyContinue
    }
} catch {
    Write-Progress -Activity "Local RAG Setup.exe build" -Completed
    Write-Error $_
    exit 1
}
