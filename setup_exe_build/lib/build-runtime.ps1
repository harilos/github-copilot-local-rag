function Prepare-EmbeddedPython {
    param(
        [Parameter(Mandatory = $true)][string]$BuildPython,
        [Parameter(Mandatory = $true)]$PythonInfo,
        [Parameter(Mandatory = $true)][string]$RequestedVersion,
        [Parameter(Mandatory = $true)][string]$RequirementsPath,
        [Parameter(Mandatory = $true)][string]$AppCopilotRoot
    )
    $version = if ([string]::IsNullOrWhiteSpace($RequestedVersion)) {
        [string]$PythonInfo.version
    } else {
        $RequestedVersion.Trim()
    }
    $parts = $version.Split('.')
    if ($parts.Count -lt 2 -or [int]$parts[0] -ne [int]$PythonInfo.major -or [int]$parts[1] -ne [int]$PythonInfo.minor) {
        throw "EmbeddedPythonVersion must use the same major/minor as the build Python."
    }

    $archive = Join-Path $CacheRoot "python-$version-embed-amd64.zip"
    Get-OrDownloadFile `
        -Uri "https://www.python.org/ftp/python/$version/python-$version-embed-amd64.zip" `
        -Destination $archive

    $venvRoot = Join-Path $AppCopilotRoot "rag\query\.venv"
    $runtimeRoot = Join-Path $venvRoot "Scripts"
    if (Test-Path -LiteralPath $venvRoot) {
        Remove-Item -LiteralPath $venvRoot -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $runtimeRoot | Out-Null
    Expand-Archive -LiteralPath $archive -DestinationPath $runtimeRoot -Force

    $pythonZip = Get-ChildItem -LiteralPath $runtimeRoot -Filter "python*.zip" -File |
        Sort-Object Name |
        Select-Object -First 1
    $pth = Get-ChildItem -LiteralPath $runtimeRoot -Filter "python*._pth" -File |
        Select-Object -First 1
    if (-not $pythonZip -or -not $pth) {
        throw "The embedded Python archive is missing its standard library ZIP or _pth file."
    }
    $pthLines = @(
        $pythonZip.Name,
        ".",
        # query-local modules such as setup_contract.py and result_bundle.py
        # live two levels above the embedded executable directory.
        "..\..",
        "Lib\site-packages",
        "import site"
    )
    [System.IO.File]::WriteAllLines($pth.FullName, $pthLines, [System.Text.Encoding]::ASCII)

    $sitePackages = Join-Path $runtimeRoot "Lib\site-packages"
    New-Item -ItemType Directory -Force -Path $sitePackages | Out-Null
    $commonPipArguments = @(
        "-m", "pip", "install",
        "--upgrade",
        "--prefer-binary",
        "--no-compile",
        "--disable-pip-version-check",
        "--cache-dir", $PipCache,
        "--target", $sitePackages
    )
    Invoke-Checked -FilePath $BuildPython `
        -Arguments ($commonPipArguments + @("--requirement", $RequirementsPath)) `
        -FailureMessage "Search runtime dependency installation failed"
    Invoke-Checked -FilePath $BuildPython `
        -Arguments ($commonPipArguments + @("pip>=24")) `
        -FailureMessage "Bundled pip installation failed"

    $prunePatterns = @(
        "torch", "torch-*.dist-info", "torchgen", "functorch",
        "sentence_transformers", "sentence_transformers-*.dist-info",
        "optimum", "optimum-*.dist-info",
        "onnx", "onnx-*.dist-info",
        "pypdf", "pypdf-*.dist-info",
        "docx", "python_docx-*.dist-info",
        "pptx", "python_pptx-*.dist-info",
        "openpyxl", "openpyxl-*.dist-info",
        "cryptography", "cryptography-*.dist-info"
    )
    foreach ($pattern in $prunePatterns) {
        Get-ChildItem -LiteralPath $sitePackages -Filter $pattern -Force -ErrorAction SilentlyContinue |
            ForEach-Object {
                Write-Host "Pruned from Setup.exe runtime: $($_.Name)"
                Remove-Item -LiteralPath $_.FullName -Recurse -Force
            }
    }
    Get-ChildItem -LiteralPath $runtimeRoot -Recurse -Directory -Filter "__pycache__" -Force -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    Get-ChildItem -LiteralPath $runtimeRoot -Recurse -File -Include "*.pyc", "*.pyo" -Force -ErrorAction SilentlyContinue |
        Remove-Item -Force -ErrorAction SilentlyContinue

    return Join-Path $runtimeRoot "python.exe"
}

function Resolve-ModelDirectory {
    param(
        [Parameter(Mandatory = $true)][System.IO.DirectoryInfo]$DictionaryDirectory,
        [Parameter(Mandatory = $true)][string]$AppCopilotRoot
    )
    $dictionaryRagRoot = $DictionaryDirectory.Parent.Parent.FullName
    $candidates = @(
        (Join-Path $dictionaryRagRoot "models\$DefaultModelDirectoryName"),
        (Join-Path $RepoRoot ".copilot\rag\models\$DefaultModelDirectoryName"),
        (Join-Path $HOME ".copilot\rag\models\$DefaultModelDirectoryName")
    )
    $selected = $null
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Container) {
            $selected = (Resolve-Path -LiteralPath $candidate).Path
            break
        }
    }
    if (-not $selected) {
        $selected = Select-Folder `
            -Description "同封する準備済みONNXモデルフォルダを選択してください" `
            -InitialDirectory (Join-Path $HOME ".copilot\rag\models")
        $selected = (Resolve-Path -LiteralPath $selected).Path
    }
    $modelItem = Get-Item -LiteralPath $selected -Force
    if (
        -not $modelItem.PSIsContainer -or
        (($modelItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0)
    ) {
        throw "The selected model must be a real directory: $selected"
    }
    foreach ($required in @("model.onnx", "config.json", "tokenizer_config.json", "MODEL_MANIFEST.json")) {
        if (-not (Test-Path -LiteralPath (Join-Path $selected $required) -PathType Leaf)) {
            throw "The selected model is missing $required."
        }
    }
    if (-not (
        (Test-Path -LiteralPath (Join-Path $selected "tokenizer.json") -PathType Leaf) -or
        (Test-Path -LiteralPath (Join-Path $selected "tokenizer.model") -PathType Leaf)
    )) {
        throw "The selected model is missing tokenizer.json or tokenizer.model."
    }

    $modelsRoot = Join-Path $AppCopilotRoot "rag\models"
    $destination = Join-Path $modelsRoot $DefaultModelDirectoryName
    # Product staging may see ignored local model directories on the build PC.
    # Remove the entire staged models tree so only the explicitly selected
    # prepared model can enter the general-user installer.
    if (Test-Path -LiteralPath $modelsRoot) {
        Remove-Item -LiteralPath $modelsRoot -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $modelsRoot | Out-Null
    Copy-Item -LiteralPath $selected -Destination $destination -Recurse -Force
    return $destination
}

function Copy-InstallerPostInstallHelper {
    param([Parameter(Mandatory = $true)][string]$AppCopilotRoot)
    $source = Join-Path $ScriptRoot "post_install.py"
    $destination = Join-Path $AppCopilotRoot "rag\query\installer_post_install.py"
    Copy-Item -LiteralPath $source -Destination $destination -Force
}

function New-WelcomeBitmap {
    param(
        [string]$SourceImage,
        [Parameter(Mandatory = $true)][string]$Destination
    )
    Add-Type -AssemblyName System.Drawing
    $width = 164
    $height = 314
    $pixelFormat = [System.Drawing.Imaging.PixelFormat]::Format24bppRgb
    $bitmap = [System.Drawing.Bitmap]::new($width, $height, $pixelFormat)
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    $graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $graphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    try {
        $rectangle = [System.Drawing.Rectangle]::new(0, 0, $width, $height)
        $background = [System.Drawing.Drawing2D.LinearGradientBrush]::new(
            $rectangle,
            [System.Drawing.Color]::FromArgb(20, 36, 60),
            [System.Drawing.Color]::FromArgb(37, 99, 235),
            [System.Drawing.Drawing2D.LinearGradientMode]::Vertical
        )
        try {
            $graphics.FillRectangle($background, $rectangle)
        } finally {
            $background.Dispose()
        }

        if ($SourceImage) {
            $image = [System.Drawing.Image]::FromFile($SourceImage)
            try {
                $scale = [Math]::Min($width / [double]$image.Width, $height / [double]$image.Height)
                $drawWidth = [Math]::Max(1, [int][Math]::Round($image.Width * $scale))
                $drawHeight = [Math]::Max(1, [int][Math]::Round($image.Height * $scale))
                $x = [int](($width - $drawWidth) / 2)
                $y = [int](($height - $drawHeight) / 2)
                $graphics.DrawImage($image, $x, $y, $drawWidth, $drawHeight)
            } finally {
                $image.Dispose()
            }
        } else {
            $white = [System.Drawing.SolidBrush]::new([System.Drawing.Color]::FromArgb(245, 248, 255))
            $soft = [System.Drawing.SolidBrush]::new([System.Drawing.Color]::FromArgb(204, 222, 255))
            $pen = [System.Drawing.Pen]::new([System.Drawing.Color]::White, 7)
            $fontLarge = [System.Drawing.Font]::new("Segoe UI", 23, [System.Drawing.FontStyle]::Bold)
            $fontSmall = [System.Drawing.Font]::new("Segoe UI", 9, [System.Drawing.FontStyle]::Regular)
            try {
                $graphics.FillRectangle($white, 35, 55, 94, 126)
                $graphics.FillRectangle($soft, 49, 79, 66, 8)
                $graphics.FillRectangle($soft, 49, 101, 66, 8)
                $graphics.FillRectangle($soft, 49, 123, 48, 8)
                $graphics.DrawEllipse($pen, 67, 142, 62, 62)
                $graphics.DrawLine($pen, 112, 190, 143, 221)
                $graphics.DrawString("LOCAL", $fontSmall, $white, 18, 245)
                $graphics.DrawString("RAG", $fontLarge, $white, 15, 258)
            } finally {
                $white.Dispose()
                $soft.Dispose()
                $pen.Dispose()
                $fontLarge.Dispose()
                $fontSmall.Dispose()
            }
        }
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Destination) | Out-Null
        $bitmap.Save($Destination, [System.Drawing.Imaging.ImageFormat]::Bmp)
    } finally {
        $graphics.Dispose()
        $bitmap.Dispose()
    }
}
