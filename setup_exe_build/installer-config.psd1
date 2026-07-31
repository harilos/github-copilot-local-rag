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
        "次の画面例を参考に、VS CodeまたはCopilot CLIから社内文書を検索してください。"
    )

    # The finish guidance page can show arbitrary explanatory text and two screenshots.
    FinishCustomTextLines = @(
        "質問例：ローカルRAGで、○○の仕様と関連資料を根拠付きで教えて"
        "利用できる辞書が分からない場合は、先に『使えるローカルRAGのDBを教えて』と依頼してください。"
    )
    ManualUrl = "https://example.invalid/local-rag-manual"
    ManualLinkText = "使い方マニュアルを開く"

    # Empty image paths generate placeholder screenshots. PNG, JPEG, GIF, or BMP can be specified.
    # Relative paths are resolved from this folder.
    VSCodeImagePath = ""
    CopilotCliImagePath = ""

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
