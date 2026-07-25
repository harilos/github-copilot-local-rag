# GitHub Copilot Local RAG

Local RAG utilities and instructions intended to be copied under your user-level GitHub Copilot folder.

The `.copilot/` directory in this repository is the installable payload. Copy its contents into your real `$HOME/.copilot` folder.

## Install On macOS / Linux

```bash
./install.sh
```

This creates `~/.copilot` if it does not exist. To install to another folder:

```bash
COPILOT_HOME=/path/to/.copilot ./install.sh
```

Manual copy:

```bash
mkdir -p ~/.copilot
cp -R .copilot/. ~/.copilot/
```

## Install On Windows

PowerShell:

```powershell
.\install.ps1
```

This creates `$HOME\.copilot` if it does not exist. To install to another folder:

```powershell
.\install.ps1 -Target "C:\path\to\.copilot"
```

Then add this short reference to your existing Copilot custom instructions when needed:

```text
RAGが必要な場合は ~/.copilot/instructions/rag.instructions.md を参照してください。
```

This repository does not ship a top-level `copilot-instructions.md`, so it will not overwrite existing user-level Copilot instructions.

## Docs

- [RAG pack README](.copilot/rag/README.md)
- [System design](.copilot/rag/docs/local-rag-system-design.md)

## License

MIT
