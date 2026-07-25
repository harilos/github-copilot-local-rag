# RAG Databases

Put generated databases under this directory. Database names should end with `-rag`.

Example:

```text
dbs/
  project-rag/
    VERSION.json
    db.json
    DB_PROFILE.md
    data/
    index/
    logs/
```

`VERSION.json` is created with the DB layout and records the creation timestamp plus a sha256 DB hash.

Database directories are ignored by git. Keep only this README and `.gitkeep`.
