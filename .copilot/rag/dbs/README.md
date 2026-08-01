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

Database directories are ignored by git except for the public `fizzbuzz-planet-rag` sample.

## Public FizzBuzz sample

`fizzbuzz-planet-rag` is ready to search after the normal Local RAG setup. It contains the public [FizzBuzz Planet repository](https://github.com/harilos/fizzbuzz-planet-docs) and that repository's GitHub Issues. GitHub Wiki content is not included.

Use the root `list_dbs.py` and `search.py` entry points shown in the main README. Administrators can select `fizzbuzz-planet-rag` in Local RAG Manager and update its Repository and Issues Sources normally.

Source `work` directories are intentionally not tracked. Manager recreates them from the public Sources during an update.
