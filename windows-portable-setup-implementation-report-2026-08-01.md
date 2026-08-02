# Windows portable setup implementation note

This historical implementation report is superseded by LRR-REL-002.

The supported Windows x64 portable flow keeps:

- copy-ready ZIP creation with an embedded runtime and model;
- explicit database selection and explicit same-name replacement;
- preservation of unselected databases and machine-local configuration;
- staged publication and rollback of changed product, runtime, model, and
  selected database paths;
- optional, explicitly requested VS Code/Copilot settings;
- direct AMD64 PE checking before target changes.

The installer and builder no longer create or consume package-wide checksum
manifests, closed-set file inventories, runtime distribution inventories, model
fingerprints, database fingerprints, or portable completion-marker state. The
installer does not run setup verification, list_dbs, or search smoke tests.
Acceptance tests invoke public commands from outside the installer.

GitHub Release creation, tags, published ZIP uploads, and issue closure remain
separate release-gate operations.
