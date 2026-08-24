[CmdletBinding(PositionalBinding = $false)]
param(
    [Parameter()]
    [ValidateSet("savings", "standard", "thorough")]
    [string]$Tier = "standard",

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$CopilotArguments
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Test-PathInside {
    param([string]$Path, [string]$Root)
    $candidate = [System.IO.Path]::GetFullPath($Path)
    $boundary = [System.IO.Path]::GetFullPath($Root).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
    return $candidate.Equals($boundary, [System.StringComparison]::OrdinalIgnoreCase) -or
        $candidate.StartsWith(
            $boundary + [System.IO.Path]::DirectorySeparatorChar,
            [System.StringComparison]::OrdinalIgnoreCase
        )
}

function Get-ProjectRoot {
    param([string]$Start)
    $startPath = [System.IO.Path]::GetFullPath($Start)
    $current = [System.IO.DirectoryInfo]::new($startPath)
    while ($null -ne $current) {
        if (Test-Path -LiteralPath (Join-Path $current.FullName ".git")) {
            return $current.FullName
        }
        $current = if ($current -is [System.IO.DirectoryInfo]) {
            $current.Parent
        }
        else {
            $current.Directory
        }
    }
    return $startPath
}

function Test-JsonObject {
    param([object]$Value)
    return $null -ne $Value -and
        $Value -is [System.Management.Automation.PSCustomObject]
}

function ConvertFrom-ProjectJsonc {
    param([string]$Path)
    try {
        $strictUtf8 = [System.Text.UTF8Encoding]::new($false, $true)
        $raw = [System.IO.File]::ReadAllText($Path, $strictUtf8)
        $withoutComments = [System.Text.StringBuilder]::new($raw.Length)
        $inString = $false
        $escaped = $false
        $inLineComment = $false
        $inBlockComment = $false
        for ($index = 0; $index -lt $raw.Length; $index++) {
            $character = $raw[$index]
            $next = if ($index + 1 -lt $raw.Length) { $raw[$index + 1] } else { [char]0 }
            if ($inLineComment) {
                if ($character -eq "`r" -or $character -eq "`n") {
                    $inLineComment = $false
                    [void]$withoutComments.Append($character)
                }
                else {
                    [void]$withoutComments.Append(" ")
                }
                continue
            }
            if ($inBlockComment) {
                if ($character -eq "*" -and $next -eq "/") {
                    [void]$withoutComments.Append("  ")
                    $index++
                    $inBlockComment = $false
                }
                elseif ($character -eq "`r" -or $character -eq "`n") {
                    [void]$withoutComments.Append($character)
                }
                else {
                    [void]$withoutComments.Append(" ")
                }
                continue
            }
            if ($inString) {
                [void]$withoutComments.Append($character)
                if ($escaped) {
                    $escaped = $false
                }
                elseif ($character -eq "\") {
                    $escaped = $true
                }
                elseif ($character -eq '"') {
                    $inString = $false
                }
                continue
            }
            if ($character -eq '"') {
                $inString = $true
                [void]$withoutComments.Append($character)
            }
            elseif ($character -eq "/" -and $next -eq "/") {
                [void]$withoutComments.Append("  ")
                $index++
                $inLineComment = $true
            }
            elseif ($character -eq "/" -and $next -eq "*") {
                [void]$withoutComments.Append("  ")
                $index++
                $inBlockComment = $true
            }
            else {
                [void]$withoutComments.Append($character)
            }
        }
        if ($inBlockComment) {
            throw "Unterminated block comment"
        }

        $commentFree = $withoutComments.ToString()
        $withoutTrailingCommas = [System.Text.StringBuilder]::new($commentFree.Length)
        $inString = $false
        $escaped = $false
        for ($index = 0; $index -lt $commentFree.Length; $index++) {
            $character = $commentFree[$index]
            if ($inString) {
                [void]$withoutTrailingCommas.Append($character)
                if ($escaped) {
                    $escaped = $false
                }
                elseif ($character -eq "\") {
                    $escaped = $true
                }
                elseif ($character -eq '"') {
                    $inString = $false
                }
                continue
            }
            if ($character -eq '"') {
                $inString = $true
                [void]$withoutTrailingCommas.Append($character)
                continue
            }
            if ($character -eq ",") {
                $lookahead = $index + 1
                while ($lookahead -lt $commentFree.Length -and
                    [char]::IsWhiteSpace($commentFree[$lookahead])) {
                    $lookahead++
                }
                if ($lookahead -lt $commentFree.Length -and
                    ($commentFree[$lookahead] -eq "}" -or $commentFree[$lookahead] -eq "]")) {
                    [void]$withoutTrailingCommas.Append(" ")
                    continue
                }
            }
            [void]$withoutTrailingCommas.Append($character)
        }
        $document = $withoutTrailingCommas.ToString() | ConvertFrom-Json -ErrorAction Stop
        if (-not (Test-JsonObject -Value $document)) {
            throw "Root must be a JSON object"
        }
        return $document
    }
    catch {
        throw "Invalid project MCP config: $Path ($($_.Exception.Message))"
    }
}

function Get-JsonObjectPropertiesNamed {
    param([object]$Value, [string]$Name)
    if (-not (Test-JsonObject -Value $Value)) { return @() }
    return @($Value.PSObject.Properties | Where-Object {
        $_.Name.Equals($Name, [System.StringComparison]::OrdinalIgnoreCase)
    })
}

function Assert-NoMcpServerName {
    param([object]$ServerMap, [string]$Path, [string]$ServerName)
    if (-not (Test-JsonObject -Value $ServerMap)) {
        throw "Invalid project MCP config: $Path (server container must be a JSON object)"
    }
    foreach ($property in @($ServerMap.PSObject.Properties)) {
        if ($property.Name.Equals($ServerName, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing a project-shadowed MCP server: $Path"
        }
    }
}

function Assert-NoMcpShadowInConfig {
    param([string]$Path, [string]$RelativePath, [string]$ServerName)
    $document = ConvertFrom-ProjectJsonc -Path $Path
    if ($RelativePath -eq ".copilot/mcp-config.json") {
        $containers = @(Get-JsonObjectPropertiesNamed -Value $document -Name "mcpServers")
        if ($containers.Count -ne 1) {
            throw "Invalid project MCP config: $Path (expected one mcpServers object)"
        }
        Assert-NoMcpServerName -ServerMap $containers[0].Value -Path $Path -ServerName $ServerName
        return
    }
    if ($RelativePath -eq ".vscode/mcp.json") {
        $containers = @(Get-JsonObjectPropertiesNamed -Value $document -Name "servers")
        if ($containers.Count -ne 1) {
            throw "Invalid project MCP config: $Path (expected one servers object)"
        }
        Assert-NoMcpServerName -ServerMap $containers[0].Value -Path $Path -ServerName $ServerName
        return
    }

    Assert-NoMcpServerName -ServerMap $document -Path $Path -ServerName $ServerName
    $wrappedContainers = @(
        @(Get-JsonObjectPropertiesNamed -Value $document -Name "mcpServers")
        @(Get-JsonObjectPropertiesNamed -Value $document -Name "servers")
    )
    foreach ($container in $wrappedContainers) {
        Assert-NoMcpServerName -ServerMap $container.Value -Path $Path -ServerName $ServerName
    }
}

function Assert-NoReparseChain {
    param([string]$Path, [string]$Root)
    if (-not (Test-PathInside -Path $Path -Root $Root)) {
        throw "Project shadow candidate escapes the project root: $Path"
    }
    $current = if (Test-Path -LiteralPath $Path) {
        Get-Item -LiteralPath $Path -Force
    }
    else {
        Get-Item -LiteralPath ([System.IO.Path]::GetDirectoryName($Path)) -Force
    }
    while ($null -ne $current -and (Test-PathInside -Path $current.FullName -Root $Root)) {
        if ($current.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
            throw "Project shadow path crosses a reparse point: $($current.FullName)"
        }
        if ($current.FullName.Equals($Root, [System.StringComparison]::OrdinalIgnoreCase)) {
            break
        }
        $current = if ($current -is [System.IO.DirectoryInfo]) {
            $current.Parent
        }
        else {
            $current.Directory
        }
    }
}

function Assert-NoProjectAgentOrMcpShadow {
    param([string]$ProjectRoot, [string]$StartDirectory, [string]$AgentId)
    $current = [System.IO.DirectoryInfo]::new(
        [System.IO.Path]::GetFullPath($StartDirectory)
    )
    while ($null -ne $current -and (Test-PathInside -Path $current.FullName -Root $ProjectRoot)) {
        $agentRelativePaths = @(
            ".github/agents/$AgentId.agent.md",
            ".github/agents/$AgentId.md",
            ".claude/agents/$AgentId.agent.md",
            ".claude/agents/$AgentId.md"
        )
        foreach ($relative in $agentRelativePaths) {
            $candidate = Join-Path $current.FullName $relative
            $parent = [System.IO.Path]::GetDirectoryName($candidate)
            if ((Test-Path -LiteralPath $parent) -or (Test-Path -LiteralPath $candidate)) {
                Assert-NoReparseChain -Path $candidate -Root $ProjectRoot
            }
            if (Test-Path -LiteralPath $candidate) {
                throw "Refusing a project-shadowed Agent definition: $candidate"
            }
        }
        foreach ($relative in @(
            ".copilot/mcp-config.json",
            ".vscode/mcp.json",
            ".mcp.json",
            ".github/mcp.json"
        )) {
            $candidate = Join-Path $current.FullName $relative
            if (-not (Test-Path -LiteralPath $candidate)) { continue }
            Assert-NoReparseChain -Path $candidate -Root $ProjectRoot
            $item = Get-Item -LiteralPath $candidate -Force
            if ($item.PSIsContainer) { throw "Project MCP config is not a regular file: $candidate" }
            Assert-NoMcpShadowInConfig `
                -Path $candidate `
                -RelativePath $relative `
                -ServerName "localragagent003"
        }
        if ($current.FullName.Equals($ProjectRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            break
        }
        $current = $current.Parent
    }
}

function Get-Sha256 {
    param([string]$Path)
    $stream = $null
    $sha = $null
    try {
        $stream = [System.IO.File]::OpenRead($Path)
        $sha = [System.Security.Cryptography.SHA256]::Create()
        $bytes = $sha.ComputeHash($stream)
        return ([System.BitConverter]::ToString($bytes)).Replace("-", "").ToLowerInvariant()
    }
    finally {
        if ($null -ne $stream) { $stream.Dispose() }
        if ($null -ne $sha) { $sha.Dispose() }
    }
}

function Assert-RegularOwnedFile {
    param([string]$Path, [string]$Boundary)
    if (-not (Test-PathInside -Path $Path -Root $Boundary)) {
        throw "Owned artifact escapes Copilot home: $Path"
    }
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if ($item.PSIsContainer -or ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
        throw "Owned artifact is not a regular file: $Path"
    }
    return $item
}

function Ensure-SafeDirectory {
    param([string]$Path, [string]$Boundary)
    $candidate = [System.IO.Path]::GetFullPath($Path)
    $boundaryPath = [System.IO.Path]::GetFullPath($Boundary).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
    if (-not (Test-PathInside -Path $candidate -Root $boundaryPath)) {
        throw "Expected temporary directory escapes the install root: $candidate"
    }
    $boundaryItem = Get-Item -LiteralPath $boundaryPath -Force -ErrorAction Stop
    if (-not $boundaryItem.PSIsContainer -or
        ($boundaryItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
        throw "Expected temporary directory root is not a regular directory: $boundaryPath"
    }
    $separatorCharacters = [char[]]@(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
    $relative = $candidate.Substring($boundaryPath.Length).TrimStart(
        $separatorCharacters
    )
    $current = $boundaryPath
    foreach ($segment in $relative.Split(
        $separatorCharacters,
        [System.StringSplitOptions]::RemoveEmptyEntries
    )) {
        $current = Join-Path $current $segment
        $item = Get-Item -LiteralPath $current -Force -ErrorAction SilentlyContinue
        if ($null -eq $item) {
            try {
                [void][System.IO.Directory]::CreateDirectory($current)
            }
            catch {
                throw "Expected temporary directory could not be created safely: $current"
            }
            $item = Get-Item -LiteralPath $current -Force -ErrorAction Stop
        }
        if (-not $item.PSIsContainer) {
            throw "Expected temporary path is not a directory: $current"
        }
        if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
            throw "Expected temporary path crosses a reparse point: $current"
        }
    }
    return $candidate
}

$ReservedFlags = @(
    "--acp", "--attachment", "--autopilot", "--mode", "--plan",
    "--agent", "--model", "--additional-mcp-config", "--available-tools",
    "--excluded-tools", "--allow-tool", "--deny-tool", "--allow-all", "--yolo",
    "--allow-all-tools", "--allow-all-paths", "--allow-all-urls", "--allow-url",
    "--deny-url", "--config-dir", "-c", "--add-dir", "--enable-mcp-server",
    "--disable-mcp-server", "--no-custom-instructions", "--custom-instructions",
    "--github-mcp-tool", "--add-github-mcp-tool", "--add-github-mcp-toolset",
    "--enable-all-github-mcp-tools", "--disable-github-mcp-server",
    "--allow-all-mcp-server-instructions", "--plugin-dir", "--extension-sdk-path",
    "--disable-builtin-mcps", "--enable-builtin-mcps",
    "--enable-memory", "--experimental", "--remote", "--remote-export",
    "--share", "--share-gist", "--secret-env-vars",
    "--connect", "--session-id", "--resume", "-r", "--continue",
    "--worktree", "-w", "--prefer-version"
)
foreach ($argument in @($CopilotArguments)) {
    if ($null -eq $argument) { continue }
    if ($argument -match '^--(?:allow|deny)-') {
        throw "Reserved Copilot CLI flag is controlled by the launcher: $argument"
    }
    foreach ($reserved in $ReservedFlags) {
        if ($argument.Equals($reserved, [System.StringComparison]::OrdinalIgnoreCase) -or
            $argument.StartsWith($reserved + "=", [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Reserved Copilot CLI flag is controlled by the launcher: $argument"
        }
    }
    if ($argument -match '(?i)^-(?:c|r|w).+') {
        throw "Reserved Copilot CLI short flag is controlled by the launcher: $argument"
    }
}

$BundleRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$InstallRoot = [System.IO.Directory]::GetParent($BundleRoot).FullName
$ManifestPath = Join-Path $BundleRoot "owned-manifest.json"
$PinnedConfig = Join-Path $BundleRoot "local-rag-agent003.pinned-mcp.json"
$ManifestItem = Assert-RegularOwnedFile -Path $ManifestPath -Boundary $InstallRoot
$manifest = Get-Content -LiteralPath $ManifestItem.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
if ($manifest.schema -ne 1 -or $manifest.server -ne "localragagent003") {
    throw "Unsupported or invalid owned manifest"
}
$ManifestInstallRoot = [System.IO.Path]::GetFullPath([string]$manifest.install_root)
if (-not $ManifestInstallRoot.Equals($InstallRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Owned manifest install root does not match the launcher location"
}
if (-not [System.IO.Path]::IsPathRooted([string]$manifest.copilot_home)) {
    throw "Owned manifest Copilot home is not absolute"
}
$CopilotHome = [System.IO.Path]::GetFullPath([string]$manifest.copilot_home)
$ExpectedArtifacts = @(
    @{ Root = "copilot_home"; Path = "agents/local-rag-agent003-savings.agent.md" },
    @{ Root = "copilot_home"; Path = "agents/local-rag-agent003-standard.agent.md" },
    @{ Root = "copilot_home"; Path = "agents/local-rag-agent003-thorough.agent.md" },
    @{ Root = "install_root"; Path = "copilot-cli/local-rag-agent003.ps1" },
    @{ Root = "install_root"; Path = "copilot-cli/local-rag-agent003.pinned-mcp.json" }
)
if (@($manifest.artifacts).Count -ne $ExpectedArtifacts.Count) {
    throw "Owned manifest artifact set is not exact"
}
foreach ($expected in $ExpectedArtifacts) {
    $matches = @($manifest.artifacts | Where-Object {
        $_.root -ceq $expected.Root -and $_.path -ceq $expected.Path
    })
    if ($matches.Count -ne 1) {
        throw "Owned manifest is missing an exact artifact: $($expected.Root):$($expected.Path)"
    }
    $relative = $expected.Path
    $nativeRelative = $relative.Replace("/", [System.IO.Path]::DirectorySeparatorChar)
    if ([System.IO.Path]::IsPathRooted($nativeRelative)) { throw "Owned manifest path is rooted" }
    $boundary = if ($expected.Root -ceq "copilot_home") { $CopilotHome } else { $InstallRoot }
    $path = [System.IO.Path]::GetFullPath((Join-Path $boundary $nativeRelative))
    $item = Assert-RegularOwnedFile -Path $path -Boundary $boundary
    if ([int64]$item.Length -ne [int64]$matches[0].bytes) {
        throw "Owned artifact size mismatch: $($expected.Root):$relative"
    }
    if ((Get-Sha256 -Path $item.FullName) -cne [string]$matches[0].sha256) {
        throw "Owned artifact hash mismatch: $($expected.Root):$relative"
    }
}

$pinned = Get-Content -LiteralPath $PinnedConfig -Raw -Encoding UTF8 | ConvertFrom-Json
$pinnedRootProperties = @($pinned.PSObject.Properties)
if ($pinnedRootProperties.Count -ne 1 -or $pinnedRootProperties[0].Name -cne "mcpServers") {
    throw "Pinned MCP configuration root is not exact"
}
$serverProperties = @($pinned.mcpServers.PSObject.Properties)
if ($serverProperties.Count -ne 1 -or $serverProperties[0].Name -cne "localragagent003") {
    throw "Pinned MCP configuration does not contain the exact owned server"
}
$server = $serverProperties[0].Value
$ExpectedServerFields = @("type", "command", "args", "env", "tools", "timeout")
$actualServerFields = @($server.PSObject.Properties.Name)
if ($actualServerFields.Count -ne $ExpectedServerFields.Count -or
    @($ExpectedServerFields | Where-Object { $_ -cnotin $actualServerFields }).Count -ne 0) {
    throw "Pinned MCP server fields are not exact"
}
$ExpectedTools = @("local_rag_search", "local_rag_get_evidence")
$ExpectedRagRoot = [System.IO.Path]::GetFullPath((Join-Path $InstallRoot "rag"))
$ExpectedPython = [System.IO.Path]::GetFullPath((Join-Path $ExpectedRagRoot "query/.venv/Scripts/python.exe"))
$ExpectedServer = [System.IO.Path]::GetFullPath((Join-Path $ExpectedRagRoot "query/mcp_server.py"))
$ExpectedTemporary = [System.IO.Path]::GetFullPath((Join-Path $ExpectedRagRoot "query/run/tmp"))
$ExpectedSpool = [System.IO.Path]::GetFullPath((Join-Path $ExpectedTemporary "GitHubCopilotLocalRAG/results"))
$ExpectedServerArgs = @(
    "-B", $ExpectedServer, "--rag-root", $ExpectedRagRoot,
    "--python", $ExpectedPython, "--spool-root", $ExpectedSpool
)
$envProperties = @($server.env.PSObject.Properties)
if ($server.type -cne "local" -or [int]$server.timeout -ne 180000 -or
    @($server.tools).Count -ne 2 -or $server.tools[0] -cne $ExpectedTools[0] -or
    $server.tools[1] -cne $ExpectedTools[1] -or
    $envProperties.Count -ne 2 -or
    $server.env.TEMP -cne $ExpectedTemporary -or $server.env.TMP -cne $ExpectedTemporary -or
    -not ([System.IO.Path]::GetFullPath([string]$server.command)).Equals(
        $ExpectedPython, [System.StringComparison]::OrdinalIgnoreCase
    ) -or @($server.args).Count -ne $ExpectedServerArgs.Count) {
    throw "Pinned MCP configuration violates the CLI contract"
}
for ($index = 0; $index -lt $ExpectedServerArgs.Count; $index++) {
    $actual = [string]$server.args[$index]
    $expected = $ExpectedServerArgs[$index]
    if ($index -in @(1, 3, 5, 7)) {
        $actual = [System.IO.Path]::GetFullPath($actual)
    }
    if (-not $actual.Equals($expected, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Pinned MCP server arguments are not exact"
    }
}
[void](Ensure-SafeDirectory -Path $ExpectedTemporary -Boundary $InstallRoot)

$TierMap = @{
    savings = @{ Agent = "local-rag-agent003-savings"; Model = "claude-haiku-4.5" }
    standard = @{ Agent = "local-rag-agent003-standard"; Model = "auto" }
    thorough = @{ Agent = "local-rag-agent003-thorough"; Model = "auto" }
}
$selection = $TierMap[$Tier]
$ToolList = "localragagent003-local_rag_search,localragagent003-local_rag_get_evidence"
$AllowList = "localragagent003(local_rag_search),localragagent003(local_rag_get_evidence)"
$FixedArguments = @(
    "--agent=$($selection.Agent)",
    "--model=$($selection.Model)",
    "--additional-mcp-config=@$PinnedConfig",
    "--available-tools=$ToolList",
    "--allow-tool=$AllowList",
    "--no-custom-instructions"
)

$commands = @(Get-Command "copilot" -CommandType Application -All -ErrorAction Stop)
if ($commands.Count -lt 1) { throw "Copilot CLI executable was not found" }
$CopilotPath = [System.IO.Path]::GetFullPath($commands[0].Source)
$projectRoot = Get-ProjectRoot -Start ([System.Environment]::CurrentDirectory)
if (Test-PathInside -Path $CopilotPath -Root $projectRoot) {
    throw "Refusing a project-shadowed Copilot CLI executable: $CopilotPath"
}
Assert-NoProjectAgentOrMcpShadow `
    -ProjectRoot $projectRoot `
    -StartDirectory ([System.Environment]::CurrentDirectory) `
    -AgentId $selection.Agent

$EnvironmentNames = @(
    "COPILOT_HOME", "COPILOT_LARGE_OUTPUT_THRESHOLD_BYTES", "COPILOT_MCP_TOOL_CACHE",
    "PYTHONUTF8", "PYTHONIOENCODING"
)
$SavedEnvironment = @{}
foreach ($name in $EnvironmentNames) {
    $SavedEnvironment[$name] = [System.Environment]::GetEnvironmentVariable($name, "Process")
}
$SavedDirectory = [System.Environment]::CurrentDirectory
$SavedOutputEncoding = $OutputEncoding
$SavedConsoleOutputEncoding = [Console]::OutputEncoding
$SavedConsoleInputEncoding = [Console]::InputEncoding
$ExitCode = 1
try {
    [System.Environment]::SetEnvironmentVariable("COPILOT_HOME", $CopilotHome, "Process")
    [System.Environment]::SetEnvironmentVariable("COPILOT_LARGE_OUTPUT_THRESHOLD_BYTES", "1310720", "Process")
    [System.Environment]::SetEnvironmentVariable("COPILOT_MCP_TOOL_CACHE", "false", "Process")
    [System.Environment]::SetEnvironmentVariable("PYTHONUTF8", "1", "Process")
    [System.Environment]::SetEnvironmentVariable("PYTHONIOENCODING", "utf-8", "Process")
    $utf8 = [System.Text.UTF8Encoding]::new($false)
    $OutputEncoding = $utf8
    [Console]::OutputEncoding = $utf8
    [Console]::InputEncoding = $utf8
    & $CopilotPath @FixedArguments @CopilotArguments
    $ExitCode = $LASTEXITCODE
}
finally {
    [System.Environment]::CurrentDirectory = $SavedDirectory
    $OutputEncoding = $SavedOutputEncoding
    [Console]::OutputEncoding = $SavedConsoleOutputEncoding
    [Console]::InputEncoding = $SavedConsoleInputEncoding
    foreach ($name in $EnvironmentNames) {
        [System.Environment]::SetEnvironmentVariable($name, $SavedEnvironment[$name], "Process")
    }
}
exit $ExitCode
