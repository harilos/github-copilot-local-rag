# GitHub Copilot Local RAG

Local RAG utilities and instructions intended to be copied under `$HOME/.copilot`.

## Copy

```bash
cp -R copilot_pack/instructions ~/.copilot/
cp -R copilot_pack/rag ~/.copilot/
```

Then add this short reference to your existing Copilot custom instructions when needed:

```text
RAGが必要な場合は ~/.copilot/instructions/rag.instructions.md を参照してください。
```

This repository does not ship a top-level `copilot-instructions.md`, so it will not overwrite existing user-level Copilot instructions.

## Docs

- [RAG pack README](copilot_pack/rag/README.md)
- [System design](docs/local-rag-system-design.md)

## License

MIT
