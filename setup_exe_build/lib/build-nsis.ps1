function ConvertTo-NsisLiteral {
    param([AllowEmptyString()][string]$Value)
    if ($null -eq $Value) { return "" }
    $normalized = $Value.Replace("`r`n", "`n").Replace("`r", "`n")
    $normalized = $normalized.Replace('$', '$$')
    $normalized = $normalized.Replace('"', '$\"')
    return $normalized.Replace("`n", '$\r$\n')
}

function Get-NsisVersionQuad {
    param([Parameter(Mandatory = $true)][string]$Version)
    $numbers = @()
    foreach ($part in $Version.Split('.')) {
        $match = [regex]::Match($part, '^\d+')
        if ($match.Success) { $numbers += [int]$match.Value }
    }
    while ($numbers.Count -lt 4) { $numbers += 0 }
    return (($numbers | Select-Object -First 4) -join '.')
}

function Write-NsisScript {
    param(
        [Parameter(Mandatory = $true)]$Config,
        [Parameter(Mandatory = $true)][string]$Version,
        [Parameter(Mandatory = $true)][string]$OutputPath,
        [Parameter(Mandatory = $true)][string]$AppCopilotRoot,
        [Parameter(Mandatory = $true)][string]$DictionaryRoot,
        [Parameter(Mandatory = $true)][string]$DictionaryName,
        [Parameter(Mandatory = $true)][string]$WelcomeBitmap,
        [Parameter(Mandatory = $true)][string]$VSCodeBitmap,
        [Parameter(Mandatory = $true)][string]$CopilotCliBitmap
    )
    $welcomeText = (@($Config.WelcomeTextLines) -join "`r`n`r`n")
    if (-not [string]::IsNullOrWhiteSpace([string]$Config.ContactText)) {
        $welcomeText += "`r`n`r`n" + [string]$Config.ContactText
    }
    $finishText = @($Config.FinishTextLines) -join "`r`n`r`n"
    $customText = @($Config.FinishCustomTextLines) -join "`r`n"
    $permissionHelper = Join-Path $ScriptRoot "configure_vscode_autoapprove.py"
    if (-not (Test-Path -LiteralPath $permissionHelper -PathType Leaf)) {
        throw "Missing VS Code permission helper: $permissionHelper"
    }

    $template = @'
Unicode true
ManifestDPIAware true
RequestExecutionLevel user
SetCompressor /SOLID lzma
SetCompressorDictSize 64
SetDatablockOptimize on
ShowInstDetails show
AutoCloseWindow false

!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "nsDialogs.nsh"
!include "WinMessages.nsh"

Name "@@PRODUCT_NAME@@"
Caption "@@PRODUCT_NAME@@ セットアップ"
OutFile "@@OUTPUT_PATH@@"
InstallDir "$PROFILE\.copilot"
BrandingText "@@PUBLISHER@@"

VIProductVersion "@@VERSION_QUAD@@"
VIAddVersionKey /LANG=1041 "ProductName" "@@PRODUCT_NAME@@"
VIAddVersionKey /LANG=1041 "CompanyName" "@@PUBLISHER@@"
VIAddVersionKey /LANG=1041 "FileDescription" "@@PRODUCT_NAME@@ general-user installer"
VIAddVersionKey /LANG=1041 "FileVersion" "@@VERSION@@"
VIAddVersionKey /LANG=1041 "ProductVersion" "@@VERSION@@"

!define MUI_ABORTWARNING
!define MUI_WELCOMEFINISHPAGE_BITMAP "@@WELCOME_BITMAP@@"
!define MUI_WELCOMEPAGE_TITLE "@@WELCOME_TITLE@@"
!define MUI_WELCOMEPAGE_TEXT "@@WELCOME_TEXT@@"

Var AutoApproveCheckbox
Var VSCodeImageControl
Var CopilotCliImageControl
Var VSCodeImageHandle
Var CopilotCliImageHandle

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_INSTFILES
Page custom AutoApprovePageCreate AutoApprovePageLeave
Page custom FinishGuidanceCreate
!insertmacro MUI_LANGUAGE "Japanese"

Function AutoApprovePageCreate
    nsDialogs::Create 1018
    Pop $0
    ${If} $0 == error
        Abort
    ${EndIf}
    !insertmacro MUI_HEADER_TEXT "VS Code連携" "Local RAG専用Pythonの実行許可を設定できます。"
    ${NSD_CreateLabel} 0 0 100% 34u "この設定はVS Codeが未インストールでも先に配置できます。既存のsettings.jsonは上書きせず、対象キーだけをマージします。"
    Pop $0
    ${NSD_CreateCheckbox} 0 48u 100% 18u "VS Codeの自動許可リストにこのツールを登録"
    Pop $AutoApproveCheckbox
    ${NSD_Check} $AutoApproveCheckbox
    ${NSD_CreateLabel} 14u 72u 92% 44u "対象: 標準版のユーザー設定、検出済みのVS Code Insiders、既存プロファイル。変更前のJSONCは同じフォルダへバックアップします。"
    Pop $0
    nsDialogs::Show
FunctionEnd

Function AutoApprovePageLeave
    ${NSD_GetState} $AutoApproveCheckbox $0
    ${If} $0 == ${BST_CHECKED}
        nsExec::ExecToLog '\"$INSTDIR\rag\query\.venv\Scripts\python.exe\" \"$INSTDIR\rag\query\installer_vscode_autoapprove.py\" --copilot-home \"$INSTDIR\"'
        Pop $1
        ${If} $1 != "0"
            MessageBox MB_OK|MB_ICONSTOP "VS Code自動許可設定のマージに失敗しました。終了コード: $1"
            Abort
        ${EndIf}
    ${EndIf}
FunctionEnd

Function OpenManual
    ExecShell "open" "@@MANUAL_URL@@"
FunctionEnd

Function FinishGuidanceCreate
    nsDialogs::Create 1018
    Pop $0
    ${If} $0 == error
        Abort
    ${EndIf}
    !insertmacro MUI_HEADER_TEXT "インストールが完了しました" "VS CodeまたはCopilot CLIからLocal RAGを利用できます。"
    GetDlgItem $0 $HWNDPARENT 1
    SendMessage $0 ${WM_SETTEXT} 0 "STR:完了"
    ${NSD_CreateLabel} 0 0 100% 26u "@@FINISH_TEXT@@"
    Pop $0
    ${NSD_CreateLabel} 0 30u 48% 12u "VS Codeでの実行例"
    Pop $0
    ${NSD_CreateBitmap} 0 44u 48% 74u ""
    Pop $VSCodeImageControl
    ${NSD_SetImage} $VSCodeImageControl "@@VSCODE_BITMAP@@" $VSCodeImageHandle
    ${NSD_CreateLabel} 52% 30u 48% 12u "Copilot CLIでの実行例"
    Pop $0
    ${NSD_CreateBitmap} 52% 44u 48% 74u ""
    Pop $CopilotCliImageControl
    ${NSD_SetImage} $CopilotCliImageControl "@@COPILOT_CLI_BITMAP@@" $CopilotCliImageHandle
    ${NSD_CreateLabel} 0 124u 100% 40u "@@FINISH_CUSTOM_TEXT@@"
    Pop $0
    ${NSD_CreateLink} 0 168u 100% 14u "@@MANUAL_LINK_TEXT@@"
    Pop $0
    ${NSD_OnClick} $0 OpenManual
    nsDialogs::Show
    ${NSD_FreeImage} $VSCodeImageHandle
    ${NSD_FreeImage} $CopilotCliImageHandle
FunctionEnd

Section "Local RAG" SEC_MAIN
    SectionIn RO
    SetShellVarContext current
    SetOverwrite on

    DetailPrint "既存の検索ランタイムを整理しています..."
    runtime_cleanup:
        IfFileExists "$INSTDIR\rag\query\.venv\*.*" 0 runtime_model_cleanup
        ClearErrors
        RMDir /r "$INSTDIR\rag\query\.venv"
        IfErrors runtime_cleanup_failed runtime_model_cleanup
    runtime_model_cleanup:
        IfFileExists "$INSTDIR\rag\models\@@MODEL_DIRECTORY_NAME@@\*.*" 0 runtime_cleanup_done
        ClearErrors
        RMDir /r "$INSTDIR\rag\models\@@MODEL_DIRECTORY_NAME@@"
        IfErrors runtime_cleanup_failed runtime_cleanup_done
    runtime_cleanup_failed:
        MessageBox MB_RETRYCANCEL|MB_ICONEXCLAMATION "Local RAGの検索処理が動作中のため、既存環境を更新できません。GitHub Copilotの検索を終了してから［再試行］を押してください。" IDRETRY runtime_cleanup IDCANCEL runtime_cleanup_cancel
    runtime_cleanup_cancel:
        Abort
    runtime_cleanup_done:
        RMDir /r "$INSTDIR\rag\query\run"

    DetailPrint "Local RAG検索プログラムを配置しています..."
    SetOutPath "$INSTDIR"
    File /r "@@APP_ROOT@@\*.*"
    SetOutPath "$INSTDIR\rag\query"
    File /oname=installer_vscode_autoapprove.py "@@PERMISSION_HELPER@@"

    IfFileExists "$INSTDIR\rag\dbs\@@DICTIONARY_NAME@@\*.*" dictionary_exists 0
        DetailPrint "初期辞書 @@DICTIONARY_NAME@@ を配置しています..."
        SetOutPath "$INSTDIR\rag\dbs\@@DICTIONARY_NAME@@"
        File /r "@@DICTIONARY_ROOT@@\*.*"
        Goto dictionary_done
    dictionary_exists:
        DetailPrint "既存の @@DICTIONARY_NAME@@ を保持しました。"
    dictionary_done:

    DetailPrint "GitHub Copilotの参照設定を確認しています..."
    nsExec::ExecToLog '\"$INSTDIR\rag\query\.venv\Scripts\python.exe\" \"$INSTDIR\rag\query\installer_post_install.py\" --copilot-home \"$INSTDIR\"'
    Pop $0
    StrCmp $0 "0" post_install_ok
        MessageBox MB_OK|MB_ICONSTOP "Copilot設定の更新に失敗しました。終了コード: $0"
        Abort
    post_install_ok:
    DetailPrint "インストールが完了しました。"
SectionEnd
'@
    $values = @{
        '@@PRODUCT_NAME@@' = ConvertTo-NsisLiteral ([string]$Config.ProductName)
        '@@PUBLISHER@@' = ConvertTo-NsisLiteral ([string]$Config.Publisher)
        '@@WELCOME_TITLE@@' = ConvertTo-NsisLiteral ([string]$Config.WelcomeTitle)
        '@@WELCOME_TEXT@@' = ConvertTo-NsisLiteral $welcomeText
        '@@FINISH_TEXT@@' = ConvertTo-NsisLiteral $finishText
        '@@FINISH_CUSTOM_TEXT@@' = ConvertTo-NsisLiteral $customText
        '@@MANUAL_URL@@' = ConvertTo-NsisLiteral ([string]$Config.ManualUrl)
        '@@MANUAL_LINK_TEXT@@' = ConvertTo-NsisLiteral ([string]$Config.ManualLinkText)
        '@@VERSION@@' = ConvertTo-NsisLiteral $Version
        '@@VERSION_QUAD@@' = Get-NsisVersionQuad $Version
        '@@OUTPUT_PATH@@' = ConvertTo-NsisLiteral $OutputPath
        '@@APP_ROOT@@' = ConvertTo-NsisLiteral $AppCopilotRoot
        '@@DICTIONARY_ROOT@@' = ConvertTo-NsisLiteral $DictionaryRoot
        '@@DICTIONARY_NAME@@' = ConvertTo-NsisLiteral $DictionaryName
        '@@MODEL_DIRECTORY_NAME@@' = ConvertTo-NsisLiteral $DefaultModelDirectoryName
        '@@WELCOME_BITMAP@@' = ConvertTo-NsisLiteral $WelcomeBitmap
        '@@VSCODE_BITMAP@@' = ConvertTo-NsisLiteral $VSCodeBitmap
        '@@COPILOT_CLI_BITMAP@@' = ConvertTo-NsisLiteral $CopilotCliBitmap
        '@@PERMISSION_HELPER@@' = ConvertTo-NsisLiteral $permissionHelper
    }
    foreach ($entry in $values.GetEnumerator()) { $template = $template.Replace($entry.Key, $entry.Value) }
    $scriptPath = Join-Path $GeneratedRoot "installer.nsi"
    [System.IO.File]::WriteAllText($scriptPath, $template, [System.Text.UTF8Encoding]::new($true))
    return $scriptPath
}

function Get-DirectoryBytes {
    param([Parameter(Mandatory = $true)][string]$Path)
    $sum = (Get-ChildItem -LiteralPath $Path -File -Recurse -Force -ErrorAction Stop | Measure-Object -Property Length -Sum).Sum
    if ($null -eq $sum) { return [int64]0 }
    return [int64]$sum
}
