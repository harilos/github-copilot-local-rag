function New-FinishScreenshotBitmap {
    param(
        [string]$SourceImage,
        [Parameter(Mandatory = $true)][string]$Destination,
        [Parameter(Mandatory = $true)][string]$FallbackTitle
    )
    Add-Type -AssemblyName System.Drawing
    $width = 250
    $height = 92
    $pixelFormat = [System.Drawing.Imaging.PixelFormat]::Format24bppRgb
    $bitmap = [System.Drawing.Bitmap]::new($width, $height, $pixelFormat)
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    $graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $graphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    try {
        $graphics.Clear([System.Drawing.Color]::White)
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
            $border = [System.Drawing.Pen]::new([System.Drawing.Color]::FromArgb(180, 190, 205), 1)
            $bar = [System.Drawing.SolidBrush]::new([System.Drawing.Color]::FromArgb(37, 99, 235))
            $text = [System.Drawing.SolidBrush]::new([System.Drawing.Color]::FromArgb(30, 41, 59))
            $subtle = [System.Drawing.SolidBrush]::new([System.Drawing.Color]::FromArgb(226, 232, 240))
            $font = [System.Drawing.Font]::new("Segoe UI", 12, [System.Drawing.FontStyle]::Bold)
            $small = [System.Drawing.Font]::new("Segoe UI", 8, [System.Drawing.FontStyle]::Regular)
            try {
                $graphics.DrawRectangle($border, 0, 0, $width - 1, $height - 1)
                $graphics.FillRectangle($bar, 0, 0, $width, 22)
                $graphics.FillRectangle($subtle, 14, 36, 222, 12)
                $graphics.FillRectangle($subtle, 14, 56, 170, 9)
                $graphics.DrawString($FallbackTitle, $font, [System.Drawing.Brushes]::White, 8, 2)
                $graphics.DrawString("ローカルRAGの質問例を表示する画像に差し替えられます", $small, $text, 14, 70)
            } finally {
                $border.Dispose()
                $bar.Dispose()
                $text.Dispose()
                $subtle.Dispose()
                $font.Dispose()
                $small.Dispose()
            }
        }
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Destination) | Out-Null
        $bitmap.Save($Destination, [System.Drawing.Imaging.ImageFormat]::Bmp)
    } finally {
        $graphics.Dispose()
        $bitmap.Dispose()
    }
}

function Resolve-OptionalInstallerImage {
    param(
        [string]$CommandLineValue,
        [string]$ConfiguredValue,
        [Parameter(Mandatory = $true)][string]$Label
    )
    $value = if (-not [string]::IsNullOrWhiteSpace($CommandLineValue)) {
        $CommandLineValue
    } else {
        $ConfiguredValue
    }
    $resolved = Resolve-ConfiguredPath $value
    if ($resolved -and -not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
        throw "$Label image was not found: $resolved"
    }
    return $resolved
}
