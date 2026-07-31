@{
    # Installer text. Edit XXX and the contact before building a company package.
    ProductName = "Local RAG"
    Publisher = "XXX"
    WelcomeTitle = "XXXの社内文書検索用RAGです"
    WelcomeTextLines = @(
        "GitHub Copilotから、同封された社内文書を検索できるようにします。"
        "インストール後はCopilotへ『使えるローカルRAGのDBを教えて』と依頼してください。"
    )
    ContactText = "連絡先：XXX"
    FinishTextLines = @(
        "Local RAGのインストールが完了しました。"
        "同封した初期辞書は、同名の辞書がまだ存在しない場合だけ配置されています。"
    )

    # Empty means that build_setup.ps1 generates the bundled default image.
    # PNG, JPEG, GIF, or BMP can be specified. Relative paths are resolved from this folder.
    WelcomeImagePath = ""

    # Empty means that the exact patch version of the detected build Python is used.
    EmbeddedPythonVersion = ""

    # A portable NSIS ZIP is downloaded into the ignored work/cache folder when makensis.exe is absent.
    NsisVersion = "3.12"

    # {version} is replaced by .copilot/rag/VERSION.
    OutputFileName = "CopilotLocalRAG-Setup-{version}.exe"
}
