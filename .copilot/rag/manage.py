from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Iterable


RAG_ROOT = Path(__file__).resolve().parent
TOOL_ROOT = RAG_ROOT / "gen_db" / "software_rag_tool"
GEN_DB_ROOT = RAG_ROOT / "gen_db"

TOP_MENU = (
    ("1", "Initial setup and setup verification"),
    ("2", "List and select a database"),
    ("3", "Create a new database"),
    ("0", "Exit"),
)
DATABASE_MENU = (
    ("1", "Search or run a search test"),
    ("2", "List Sources"),
    ("3", "Build / resume"),
    ("4", "Add or update documents"),
    ("5", "Show detailed status"),
    ("6", "Repair or recreate search indexes"),
    ("7", "Delete this database"),
    ("0", "Back"),
)
SOURCE_MENU = (
    ("1", "List / select Source"),
    ("2", "Inspect unmatched Source-Link settings"),
    ("0", "Back"),
)
SOURCE_DETAIL_MENU = (
    ("1", "Sample documents"),
    ("2", "Ingestion scopes"),
    ("3", "Configure Source Link"),
    ("4", "Preview generated URLs"),
    ("0", "Back"),
)
SOURCE_LINK_MENU = (
    ("1", "Show configuration"),
    ("2", "Configure or replace"),
    ("3", "Enable / disable"),
    ("4", "Remove configuration"),
    ("5", "Preview generated URLs"),
    ("0", "Back"),
)
REPAIR_COMPONENTS = {
    "1": "lexical",
    "2": "vector",
    "3": "all",
}
ALLOWED_SCRIPTS = frozenset(
    {
        "query/setup.py",
        "query/list_dbs.py",
        "query/search.py",
        "gen_db/create_db.py",
        "gen_db/build_db.py",
        "gen_db/add_data.py",
        "gen_db/status.py",
        "gen_db/rebuild_component.py",
    }
)
DATABASE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*-rag$")
_STATUS_UNSET = object()


class ManagerError(RuntimeError):
    pass


class LocalRagManager:
    """Small human-facing orchestrator for the existing Local RAG CLIs."""

    def __init__(
        self,
        *,
        rag_root: Path = RAG_ROOT,
        dbs_root: Path | None = None,
        runtime_python: Path | None = None,
        input_fn: Callable[[str], str] = input,
        output_fn: Callable[[str], None] = print,
        runner: Callable[..., Any] = subprocess.run,
    ) -> None:
        self.rag_root = Path(rag_root).expanduser().resolve()
        configured_dbs = os.getenv("RAG_DBS_ROOT", "").strip()
        self.dbs_root = (
            Path(dbs_root).expanduser().resolve()
            if dbs_root is not None
            else (
                Path(configured_dbs).expanduser().resolve()
                if configured_dbs
                else self.rag_root / "dbs"
            )
        )
        self._runtime_override = (
            Path(runtime_python).expanduser().resolve()
            if runtime_python is not None
            else None
        )
        self.input = input_fn
        self.output = output_fn
        self.runner = runner
        self._sidecar_etags: dict[str, str] = {}
        self._sidecar_migrations: dict[str, bool] = {}
        self._sidecar_source_statuses: dict[str, dict[str, str]] = {}

    def run(self) -> int:
        while True:
            self._print_menu("Local RAG Manager", TOP_MENU)
            choice = self._ask("Select: ")
            if choice is None or choice == "0":
                return 0
            if choice == "1":
                self._setup_or_verify()
            elif choice == "2":
                selected = self._select_database()
                if selected:
                    self._database_screen(selected)
            elif choice == "3":
                self._create_database()
            else:
                self.output("Unknown selection.")

    def _setup_or_verify(self) -> None:
        self._print_menu(
            "Setup / verify",
            (("1", "Verify only"), ("2", "Run setup"), ("0", "Back")),
        )
        choice = self._ask("Select: ")
        if choice in (None, "0"):
            return
        if choice == "1":
            if not self._runtime_python().is_file():
                self.output("setup_required: the Local RAG venv is missing.")
                return
            result = self._invoke(
                "query/setup.py",
                ["--verify-only", "--format", "json"],
                capture_output=True,
            )
            self._show_setup_result(result)
            return
        if choice == "2":
            if not self._confirm("Setup may install packages and prepare the model. Continue?"):
                return
            python = (
                self._runtime_python()
                if self._runtime_python().is_file()
                else Path(sys.executable).resolve()
            )
            result = self._invoke(
                "query/setup.py",
                ["--format", "json"],
                python=python,
                capture_output=True,
            )
            self._show_setup_result(result)
            return
        self.output("Unknown selection.")

    def _show_setup_result(self, result: Any | None) -> None:
        if result is None:
            return
        try:
            payload = json.loads(str(result.stdout or ""))
        except json.JSONDecodeError:
            self.output("Setup returned invalid JSON.")
            return
        if not isinstance(payload, dict):
            self.output("Setup returned an unexpected result.")
            return
        self.output(
            "Setup complete: "
            f"{'yes' if payload.get('setup_complete') else 'no'}"
        )
        self.output(
            "Lookup ready: "
            f"{'yes' if payload.get('lookup_ready') else 'no'}"
        )
        self.output(f"Status: {payload.get('status') or 'unknown'}")
        if payload.get("next_action"):
            self.output(f"Next action: {payload['next_action']}")

    def _select_database(self) -> str | None:
        databases = self._database_summaries()
        if not databases:
            self.output("No local RAG databases are installed.")
            return None
        self.output("\nDatabases")
        for index, item in enumerate(databases, start=1):
            name = str(item.get("name") or "")
            title = str(item.get("title") or name)
            status = self._status_json(name) or {}
            inventory = self._load_source_inventory(name)
            sources = (
                self._inventory_sources(inventory)
                if inventory is not None
                else []
            )
            documents = status.get("document_count")
            chunks = status.get("chunk_count")
            if documents is None:
                documents = sum(
                    int(value.get("document_count") or 0)
                    for value in sources
                )
            if chunks is None:
                chunks = sum(
                    int(value.get("chunk_count") or 0)
                    for value in sources
                )
            state = str(status.get("status") or "unknown")
            readiness = (
                "build interrupted"
                if status.get("can_resume")
                and state not in {"completed", "ready"}
                else (
                    "ready"
                    if state in {"completed", "ready"} or documents or chunks
                    else state
                )
            )
            self.output(
                f"{index}. {name} — {title} | {readiness} | "
                f"documents={documents}, chunks={chunks}"
            )
        choice = self._ask("Select a database, or 0 to go back: ")
        if choice in (None, "0"):
            return None
        try:
            index = int(choice) - 1
        except ValueError:
            self.output("Invalid database selection.")
            return None
        if index < 0 or index >= len(databases):
            self.output("Invalid database selection.")
            return None
        return str(databases[index]["name"])

    def _database_screen(self, db_name: str) -> None:
        while self._database_root(db_name).is_dir():
            self._show_database_overview(db_name)
            self._print_menu(f"Database: {db_name}", DATABASE_MENU)
            choice = self._ask("Select: ")
            if choice in (None, "0"):
                return
            if choice == "1":
                self._search(db_name)
            elif choice == "2":
                self._sources_screen(db_name)
            elif choice == "3":
                self._build_or_resume(db_name)
            elif choice == "4":
                self._add_or_update(db_name)
            elif choice == "5":
                self._show_status(db_name)
            elif choice == "6":
                self._repair_index(db_name)
            elif choice == "7":
                if self._delete_database_interactive(db_name):
                    return
            else:
                self.output("Unknown selection.")

    def _show_database_overview(self, db_name: str) -> None:
        status = self._status_json(db_name) or {}
        inventory = self._load_source_inventory(db_name)
        sources = self._inventory_sources(inventory) if inventory is not None else []
        documents = status.get("document_count")
        chunks = status.get("chunk_count")
        if documents is None:
            documents = sum(int(value.get("document_count") or 0) for value in sources)
        if chunks is None:
            chunks = sum(int(value.get("chunk_count") or 0) for value in sources)
        updated = (
            status.get("updated_at")
            or status.get("last_updated")
            or status.get("completed_at")
            or "unknown"
        )
        configured_count = 0
        sidecar_status = "unconfigured"
        try:
            loaded = self._import_source_links().load_source_links(
                self._database_root(db_name), db_name
            )
            sidecar_status = loaded.status
            indexed_source_ids = {
                str(value.get("source_id") or "")
                for value in sources
            }
            configured_count = len(
                {
                    str(source.get("source_id") or "")
                    for source in (loaded.payload or {}).get("sources") or []
                    if isinstance(source, dict)
                    and source.get("source_id") in indexed_source_ids
                    and bool(source.get("provider"))
                }
            )
        except Exception:
            sidecar_status = "invalid"
        self.output(
            f"\nStatus: {status.get('status') or 'unknown'} | "
            f"documents={documents} | chunks={chunks} | "
            f"sources={len(sources)} | updated={updated} | "
            f"source links={configured_count}/{len(sources)} "
            f"({sidecar_status})"
        )

    def _search(self, db_name: str) -> None:
        self._print_menu(
            "Search",
            (
                ("1", "Compact normal search"),
                ("2", "Compact diagnostic search"),
                ("0", "Back"),
            ),
        )
        mode = self._ask("Select: ")
        if mode in (None, "0"):
            return
        if mode not in {"1", "2"}:
            self.output("Unknown selection.")
            return
        question = self._ask("Question: ")
        if question is None or not question.strip():
            self.output("A question is required.")
            return
        arguments = ["--db", db_name, "--compact-json"]
        if mode == "2":
            arguments.append("--explain")
        arguments.append(question)
        # Exactly one search process is started for either presentation mode.
        result = self._invoke(
            "query/search.py",
            arguments,
            capture_output=True,
        )
        if result is None or int(result.returncode) != 0:
            return
        self._show_search_result(str(result.stdout or ""))

    def _show_search_result(self, raw_output: str) -> None:
        try:
            payload = json.loads(raw_output)
        except json.JSONDecodeError:
            self.output(raw_output.strip())
            return
        if not isinstance(payload, dict):
            self.output(raw_output.strip())
            return
        if (
            payload.get("schema_version") == "rag-result-pointer-v1"
            and payload.get("summary_file")
        ):
            try:
                summary_path = Path(str(payload["summary_file"]))
                payload = json.loads(summary_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                self.output(json.dumps(payload, ensure_ascii=False, indent=2))
                return
        self.output(json.dumps(payload, ensure_ascii=False, indent=2))
        preferred: list[str] = []
        for key in ("evidence", "background_context", "document_results"):
            for item in payload.get(key) or []:
                if not isinstance(item, dict):
                    continue
                source = item.get("source")
                path = (
                    str(source.get("path") or "")
                    if isinstance(source, dict)
                    else str(item.get("path") or "")
                )
                value = str(
                    item.get("source_permalink")
                    or item.get("source_url")
                    or path
                )
                if value and value not in preferred:
                    preferred.append(value)
        if preferred:
            self.output("\nPreferred document links")
            for value in preferred[:10]:
                self.output(f"- {value}")

    def _create_database(self) -> None:
        name = self._ask("Database name (must end in -rag): ")
        if name is None or not name.strip():
            return
        name = name.strip()
        if not self._valid_database_name(name):
            self.output("Invalid database name.")
            return
        if self._database_root(name).exists():
            self.output("That database already exists.")
            return
        title = self._ask("Title (optional): ")
        query_hint = self._ask("Query hint (optional): ")
        arguments = ["--db", name]
        if title and title.strip():
            arguments.extend(["--title", title.strip()])
        if query_hint and query_hint.strip():
            arguments.extend(["--query-hint", query_hint.strip()])
        self._invoke("gen_db/create_db.py", arguments)

    def _build_or_resume(self, db_name: str) -> None:
        if not self._guard_valid_database_target(db_name):
            return
        status = self._status_json(db_name)
        if not self._guard_inactive(db_name, status):
            return
        entries = [("1", "Build")]
        if status and status.get("can_resume"):
            entries.append(("2", "Resume saved operation"))
        entries.extend((("3", "Force rebuild"), ("0", "Back")))
        self._print_menu(f"Build / resume: {db_name}", entries)
        choice = self._ask("Select: ")
        if choice in (None, "0"):
            return
        if choice == "2" and status and status.get("can_resume"):
            if self._confirm(f"Resume the saved operation for {db_name}?"):
                self._resume_saved_operation(db_name, status)
            return
        if choice not in {"1", "3"}:
            self.output("Unknown selection.")
            return
        values = self._prompt_ingestion_values()
        if values is None:
            return
        root, source_id, scan_subdir = values
        arguments = [
            "--db",
            db_name,
            "--root",
            root,
            "--source-id",
            source_id,
            "--include-root-name-in-path",
        ]
        if scan_subdir:
            arguments.extend(["--scan-subdir", scan_subdir])
        if choice == "3":
            confirmation = self._ask(
                f"Type the selected database name ({db_name}) to force rebuild: "
            )
            if confirmation != db_name:
                self.output("Force rebuild cancelled.")
                return
            arguments.append("--force-rebuild")
        elif not self._confirm(f"Start build for selected database {db_name}?"):
            return
        self._invoke("gen_db/build_db.py", arguments)

    def _resume_saved_operation(
        self,
        db_name: str,
        status: dict[str, Any],
    ) -> None:
        # Never execute the resume_command stored in progress. Reconstruct the
        # allowlisted argv from individually validated status fields.
        root = str(status.get("root") or "")
        source_id = str(status.get("source_id") or "")
        scan_subdir = str(status.get("scan_subdir") or ".")
        if not root or not source_id:
            self.output("Saved resume values are incomplete.")
            return
        operation = str(status.get("operation") or "build")
        script = (
            "gen_db/add_data.py"
            if operation == "add"
            else "gen_db/build_db.py"
        )
        arguments = [
            "--db",
            db_name,
            "--root",
            root,
            "--source-id",
            source_id,
            "--include-root-name-in-path",
            "--resume",
        ]
        if scan_subdir and scan_subdir != ".":
            arguments.extend(["--scan-subdir", scan_subdir])
        self._invoke(script, arguments)

    def _add_or_update(self, db_name: str) -> None:
        if not self._guard_valid_database_target(db_name):
            return
        status = self._status_json(db_name)
        if not self._guard_inactive(db_name, status):
            return
        source_id = self._select_ingestion_source_id(db_name)
        if source_id is None:
            return
        values = self._prompt_ingestion_values(source_id=source_id)
        if values is None:
            return
        root, source_id, scan_subdir = values
        arguments = [
            "--db",
            db_name,
            "--root",
            root,
            "--source-id",
            source_id,
            "--include-root-name-in-path",
        ]
        if scan_subdir:
            arguments.extend(["--scan-subdir", scan_subdir])
        if self._confirm(
            f"Retry prior extraction errors for selected database {db_name}?"
        ):
            arguments.append("--retry-errors")
        if not self._confirm(
            f"Start add/update for selected database {db_name}?"
        ):
            return
        self._invoke("gen_db/add_data.py", arguments)

    def _prompt_ingestion_values(
        self,
        *,
        source_id: str | None = None,
    ) -> tuple[str, str, str] | None:
        root = self._ask("Logical root directory: ")
        if root is None or not root.strip():
            self.output("A logical root is required.")
            return None
        if source_id is None:
            self.output(
                "Use one Source ID per ingestion provider, for example "
                "sharepoint-docs, redmine-issues, github-repository, or "
                "filesystem-docs."
            )
            source_id = self._ask("Source ID: ")
            if source_id is None or not source_id.strip():
                self.output("A Source ID is required.")
                return None
        scan_subdir = self._ask(
            "Scan subdirectory (blank for the whole root): "
        )
        return root.strip(), source_id.strip(), str(scan_subdir or "").strip()

    def _select_ingestion_source_id(self, db_name: str) -> str | None:
        inventory = self._load_source_inventory(db_name)
        source_ids = (
            [
                str(source["source_id"])
                for source in self._inventory_sources(inventory)
            ]
            if inventory is not None
            else []
        )
        self.output("\nSelect Source ID")
        for index, source_id in enumerate(source_ids, start=1):
            self.output(f"{index}. Existing: {source_id}")
        new_index = len(source_ids) + 1
        self.output(
            f"{new_index}. New Source ID "
            "(examples: sharepoint-docs, redmine-issues, "
            "github-repository, filesystem-docs)"
        )
        choice = self._ask("Select, or 0 to cancel: ")
        if choice in (None, "0"):
            return None
        try:
            index = int(choice)
        except ValueError:
            return None
        if 1 <= index <= len(source_ids):
            return source_ids[index - 1]
        if index != new_index:
            return None
        value = self._ask(
            "New Source ID (keep each provider in a separate Source): "
        )
        if value is None or not value.strip():
            self.output("A Source ID is required.")
            return None
        return value.strip()

    def _repair_index(self, db_name: str) -> None:
        if not self._guard_valid_database_target(db_name):
            return
        if not self._guard_inactive(db_name):
            return
        self._print_menu(
            "Index repair",
            (
                ("1", "Full-text / identifier"),
                ("2", "Vector"),
                ("3", "All"),
                ("0", "Back"),
            ),
        )
        choice = self._ask("Select: ")
        component = REPAIR_COMPONENTS.get(str(choice or ""))
        if component is None:
            return
        if not self._confirm(
            f"Repair {component} index data for selected database {db_name}?"
        ):
            return
        self._invoke(
            "gen_db/rebuild_component.py",
            ["--db", db_name, "--component", component],
        )

    def _sources_screen(self, db_name: str) -> None:
        while self._database_root(db_name).is_dir():
            self._print_menu(f"Sources: {db_name}", SOURCE_MENU)
            choice = self._ask("Select: ")
            if choice in (None, "0"):
                return
            if choice == "1":
                inventory = self._load_source_inventory(db_name)
                if inventory is None:
                    continue
                source = self._select_source(inventory)
                if source is not None:
                    self._source_detail_screen(db_name, inventory, source)
            elif choice == "2":
                self._unmatched_source_settings(db_name)
            else:
                self.output("Unknown selection.")

    def _load_source_inventory(self, db_name: str) -> Any | None:
        try:
            module = self._import_source_inventory()
            return module.build_source_inventory(
                self._database_root(db_name),
                db_name,
            )
        except Exception as exc:
            self.output(
                "Could not read the Source inventory: "
                f"{type(exc).__name__}: {exc}"
            )
            return None

    @staticmethod
    def _inventory_sources(inventory: Any) -> list[dict[str, Any]]:
        payload = inventory.to_dict()
        return [
            dict(value)
            for value in payload.get("sources") or []
            if isinstance(value, dict) and value.get("source_id")
        ]

    def _select_source(self, inventory: Any) -> dict[str, Any] | None:
        sources = self._inventory_sources(inventory)
        inventory_payload = inventory.to_dict()
        self.output("\nRead-only Source inventory")
        missing = int(
            inventory_payload.get("documents_without_source_id")
            or inventory_payload.get("missing_source_document_count")
            or 0
        )
        if missing:
            self.output(
                f"Diagnostic: {missing} indexed document(s) have no Source ID."
            )
        if not sources:
            self.output("No indexed Sources were found.")
            return None
        for index, source in enumerate(sources, start=1):
            source_id = str(source["source_id"])
            label = str(source.get("display_name") or source_id)
            providers = ", ".join(source.get("link_providers") or []) or "none"
            link_setting = source.get("source_link_setting") or {}
            self.output(
                f"{index}. {label} ({source_id}) | "
                f"documents={int(source.get('document_count') or 0)}, "
                f"chunks={int(source.get('chunk_count') or 0)} | "
                f"provider={providers} | "
                f"configuration={link_setting.get('configuration') or 'not_configured'} | "
                f"status={source.get('link_status') or 'not_configured'}"
            )
        self.output(
            "Indexed Sources are read-only; this manager cannot create, "
            "rename, or delete them."
        )
        choice = self._ask("Select a Source, or 0 to go back: ")
        if choice in (None, "0"):
            return None
        try:
            index = int(choice) - 1
        except ValueError:
            return None
        return sources[index] if 0 <= index < len(sources) else None

    def _source_detail_screen(
        self,
        db_name: str,
        inventory: Any,
        source: dict[str, Any],
    ) -> None:
        source_id = str(source["source_id"])
        while True:
            self.output(
                f"\nSource: {source.get('display_name') or source_id} "
                f"({source_id})"
            )
            link_setting = source.get("source_link_setting") or {}
            self.output(
                f"Documents: {int(source.get('document_count') or 0)} | "
                f"Chunks: {int(source.get('chunk_count') or 0)}\n"
                f"Source ID (read-only): {source_id}\n"
                f"Last indexed: {source.get('last_updated_at') or 'unknown'}\n"
                f"Extraction errors: {source.get('error_file_count') or 0}\n"
                f"Observed stored roots: "
                f"{', '.join(source.get('observed_stored_roots') or []) or 'none'}\n"
                f"Observed root status: "
                f"{source.get('observed_root_status') or 'no_observed_root'}\n"
                f"Provider: {link_setting.get('provider') or 'not configured'}\n"
                f"Configuration: {link_setting.get('configuration') or 'not_configured'}\n"
                f"Status: {source.get('link_status') or 'not_configured'}"
            )
            self._print_menu("Source detail", SOURCE_DETAIL_MENU)
            choice = self._ask("Select: ")
            if choice in (None, "0"):
                return
            if choice == "1":
                values = (
                    source.get("sample_documents")
                    or source.get("document_samples")
                    or source.get("paths")
                    or []
                )
                self._print_values("Sample documents", values)
            elif choice == "2":
                values = (
                    source.get("ingestion_scopes")
                    or source.get("scopes")
                    or source.get("observed_roots")
                    or []
                )
                self._print_values("Ingestion scopes", values)
            elif choice == "3":
                self._source_link_screen(db_name, inventory, source_id)
            elif choice == "4":
                self._preview_source_link(db_name, inventory, source_id)
            else:
                self.output("Unknown selection.")

    def _source_link_screen(
        self,
        db_name: str,
        inventory: Any,
        source_id: str,
    ) -> None:
        while True:
            self._print_menu(
                f"Source Link: {source_id}",
                SOURCE_LINK_MENU,
            )
            choice = self._ask("Select: ")
            if choice in (None, "0"):
                return
            if choice == "1":
                self._show_source_link(db_name, source_id)
            elif choice == "2":
                self._configure_source_link(db_name, inventory, source_id)
            elif choice == "3":
                self._toggle_source_link(db_name, inventory, source_id)
            elif choice == "4":
                self._remove_source_link(db_name, inventory, source_id)
            elif choice == "5":
                self._preview_source_link(db_name, inventory, source_id)
            else:
                self.output("Unknown selection.")

    def _load_sidecar_payload(
        self,
        db_name: str,
    ) -> tuple[Any, dict[str, Any]] | None:
        source_links = self._import_source_links()
        loaded = source_links.load_source_links(
            self._database_root(db_name), db_name
        )
        if loaded.status == "invalid":
            self.output("The Source-Link sidecar is invalid and was not modified.")
            return None
        self._sidecar_etags[db_name] = str(
            getattr(loaded, "etag", "missing")
        )
        self._sidecar_migrations[db_name] = bool(
            getattr(loaded, "migration_required", False)
        )
        self._sidecar_source_statuses[db_name] = dict(
            getattr(loaded, "source_statuses", ())
        )
        if self._sidecar_migrations[db_name]:
            statuses = self._sidecar_source_statuses[db_name]
            summary = ", ".join(
                f"{source_id}={status}"
                for source_id, status in sorted(statuses.items())
            )
            self.output(
                "Legacy Source-Link settings were loaded read-only. "
                "They will not be rewritten without an explicit migration."
                + (f" Status: {summary}" if summary else "")
            )
        if loaded.status == "configured" and loaded.payload is not None:
            return source_links, copy.deepcopy(loaded.payload)
        return source_links, {
            "schema_version": source_links.SCHEMA_VERSION,
            "revision": 0,
            "sources": [],
        }

    @staticmethod
    def _source_entry(
        payload: dict[str, Any],
        source_id: str,
        *,
        create: bool,
    ) -> dict[str, Any] | None:
        sources = payload.setdefault("sources", [])
        for source in sources:
            if isinstance(source, dict) and source.get("source_id") == source_id:
                return source
        if not create:
            return None
        source = {"source_id": source_id}
        sources.append(source)
        return source

    def _inventory_ids_paths(
        self,
        inventory: Any,
    ) -> tuple[list[str], dict[str, list[str]]]:
        sources = self._inventory_sources(inventory)
        ids = [str(value["source_id"]) for value in sources]
        if hasattr(inventory, "observed_paths_by_source"):
            observed = inventory.observed_paths_by_source()
            return ids, {
                str(key): [str(path) for path in value]
                for key, value in observed.items()
            }
        observed: dict[str, list[str]] = {}
        for source in sources:
            source_id = str(source["source_id"])
            values = (
                source.get("observed_paths")
                or source.get("sample_documents")
                or source.get("document_samples")
                or source.get("paths")
                or []
            )
            observed[source_id] = [
                str(value.get("path") if isinstance(value, dict) else value)
                for value in values
                if value
            ]
        return ids, observed

    def _save_sidecar(
        self,
        db_name: str,
        inventory: Any,
        source_links: Any,
        payload: dict[str, Any],
    ) -> bool:
        ids, _observed = self._inventory_ids_paths(inventory)
        previous_revision = int(payload.get("revision") or 0)
        payload["revision"] = previous_revision + 1
        kwargs: dict[str, Any] = {
            "db_name": db_name,
            "existing_sources": ids,
            "allow_unmatched_sources": True,
            "expected_revision": previous_revision,
            "expected_etag": self._sidecar_etags.get(db_name, "missing"),
        }
        if self._sidecar_migrations.get(db_name, False):
            statuses = self._sidecar_source_statuses.get(db_name, {})
            self.output(
                "The next save will explicitly migrate the legacy sidecar "
                "to rag-source-links-v2. The current primary will be retained "
                "as source-links.json.bak."
            )
            if statuses:
                self.output(
                    "Migration status: "
                    + ", ".join(
                        f"{source_id}={status}"
                        for source_id, status in sorted(statuses.items())
                    )
                )
            if not self._confirm(
                f"Migrate Source-Link settings for database {db_name}?"
            ):
                payload["revision"] = previous_revision
                self.output("Legacy Source-Link migration cancelled.")
                return False
        try:
            source_links.save_source_links(
                self._database_root(db_name), payload, **kwargs
            )
        except Exception as exc:
            self.output(
                "Source-Link settings were not saved: "
                f"{type(exc).__name__}: {exc}"
            )
            return False
        self._sidecar_migrations[db_name] = False
        return True

    def _source_link(
        self,
        db_name: str,
        source_id: str,
    ) -> tuple[Any, dict[str, Any], dict[str, Any] | None] | None:
        loaded = self._load_sidecar_payload(db_name)
        if loaded is None:
            return None
        source_links, payload = loaded
        source = self._source_entry(payload, source_id, create=False)
        return source_links, payload, source

    def _show_source_link(self, db_name: str, source_id: str) -> None:
        loaded = self._source_link(db_name, source_id)
        if loaded is None:
            return
        source = loaded[2]
        if source is None or not source.get("provider"):
            self.output("No Source Link is configured for this Source.")
            return
        self.output(
            json.dumps(
                {
                    "source_id": source_id,
                    "display_name": source.get("display_name"),
                    "provider": source.get("provider"),
                    "strategy": source.get("strategy"),
                    "enabled": bool(source.get("enabled")),
                    "settings": source.get("settings") or {},
                },
                ensure_ascii=False,
                indent=2,
            )
        )

    def _configure_source_link(
        self,
        db_name: str,
        inventory: Any,
        source_id: str,
    ) -> None:
        loaded = self._source_link(db_name, source_id)
        if loaded is None:
            return
        source_links, payload, source = loaded
        current = (
            {
                key: copy.deepcopy(source[key])
                for key in (
                    "display_name",
                    "provider",
                    "enabled",
                    "strategy",
                    "settings",
                )
                if source is not None and key in source
            }
            if source is not None
            else {}
        )
        link = self._prompt_source_link(existing=current or None)
        if link is None:
            return
        display_name = str(link.pop("display_name", "") or "").strip()
        try:
            link = source_links.validate_source_link(link)
        except Exception as exc:
            self.output(
                f"Invalid Source Link: {type(exc).__name__}: {exc}"
            )
            return
        source_payload = next(
            (
                value
                for value in self._inventory_sources(inventory)
                if value.get("source_id") == source_id
            ),
            {},
        )
        root_status = str(
            source_payload.get("observed_root_status") or "no_observed_root"
        )
        if link.get("strategy") != "home-only" and root_status != "ready":
            self.output(
                "Per-file Source Links require exactly one observed stored "
                f"root; current status is {root_status}. Split ingestion "
                "providers into separate Source IDs and add them again."
            )
            return
        _, observed = self._inventory_ids_paths(inventory)
        self._show_representative_preview(
            source_links,
            link,
            observed.get(source_id, []),
        )
        if not self._confirm(
            f"Save this Source Link for Source {source_id} in "
            f"database {db_name}?"
        ):
            return
        target = self._source_entry(payload, source_id, create=True)
        assert target is not None
        if display_name:
            target["display_name"] = display_name
        else:
            target.pop("display_name", None)
        for key in ("provider", "enabled", "strategy", "settings"):
            target[key] = copy.deepcopy(link[key])
        if self._save_sidecar(
            db_name,
            inventory,
            source_links,
            payload,
        ):
            self.output("Source Link saved.")

    def _toggle_source_link(
        self,
        db_name: str,
        inventory: Any,
        source_id: str,
    ) -> None:
        loaded = self._source_link(db_name, source_id)
        if loaded is None:
            return
        source_links, payload, source = loaded
        if source is None or not source.get("provider"):
            self.output("No Source Link is configured for this Source.")
            return
        new_state = not bool(source.get("enabled"))
        label = "Enable" if new_state else "Disable"
        if not self._confirm(
            f"{label} the Source Link for Source {source_id} in "
            f"database {db_name}?"
        ):
            return
        source["enabled"] = new_state
        if self._save_sidecar(
            db_name,
            inventory,
            source_links,
            payload,
        ):
            self.output("Source Link state changed.")

    def _remove_source_link(
        self,
        db_name: str,
        inventory: Any,
        source_id: str,
    ) -> None:
        loaded = self._source_link(db_name, source_id)
        if loaded is None:
            return
        source_links, payload, source = loaded
        if source is None or not source.get("provider"):
            self.output("No Source Link is configured for this Source.")
            return
        if not self._confirm(
            f"Remove the Source Link for Source {source_id} from "
            f"database {db_name}? Indexed Source records are unchanged."
        ):
            return
        for key in ("provider", "enabled", "strategy", "settings"):
            source.pop(key, None)
        if not source.get("display_name"):
            payload["sources"] = [
                value
                for value in payload.get("sources") or []
                if value.get("source_id") != source_id
            ]
        if self._save_sidecar(
            db_name,
            inventory,
            source_links,
            payload,
        ):
            self.output("Source Link removed.")

    def _preview_source_link(
        self,
        db_name: str,
        inventory: Any,
        source_id: str,
    ) -> None:
        loaded = self._source_link(db_name, source_id)
        if loaded is None:
            return
        source_links, _payload, source = loaded
        if source is None or not source.get("provider"):
            self.output("No Source Link is configured for this Source.")
            return
        _, observed = self._inventory_ids_paths(inventory)
        preview = source_links.resolve_mapping_preview(
            source,
            observed.get(source_id, []),
        )
        self.output(json.dumps(preview, ensure_ascii=False, indent=2))

    def _show_representative_preview(
        self,
        source_links: Any,
        source_link: dict[str, Any],
        paths: list[str],
    ) -> None:
        representative = list(paths[:5])
        self.output("\nRepresentative stored paths and generated URLs")
        if not representative:
            self.output("(no representative paths are available)")
            return
        preview = source_links.resolve_mapping_preview(
            source_link,
            representative,
        )
        self.output(json.dumps(preview, ensure_ascii=False, indent=2))

    def _unmatched_source_settings(self, db_name: str) -> None:
        inventory = self._load_source_inventory(db_name)
        if inventory is None:
            return
        ids, _ = self._inventory_ids_paths(inventory)
        loaded = self._load_sidecar_payload(db_name)
        if loaded is None:
            return
        source_links, payload = loaded
        unmatched = [
            dict(value)
            for value in payload.get("sources") or []
            if isinstance(value, dict) and value.get("source_id") not in ids
        ]
        if not unmatched:
            self.output("No unmatched Source-Link settings.")
            return
        for index, source in enumerate(unmatched, start=1):
            self.output(
                f"{index}. {source.get('source_id')} | "
                f"provider={source.get('provider') or 'not_configured'} | "
                f"enabled={bool(source.get('enabled'))}"
            )
        choice = self._ask("Inspect a setting, or 0 to go back: ")
        if choice in (None, "0"):
            return
        try:
            selected = unmatched[int(choice) - 1]
        except (ValueError, IndexError):
            return
        self.output(json.dumps(selected, ensure_ascii=False, indent=2))
        if not self._confirm(
            f"Delete this unmatched sidecar setting from selected database "
            f"{db_name}?"
        ):
            return
        payload["sources"] = [
            value
            for value in payload.get("sources") or []
            if value.get("source_id") != selected.get("source_id")
        ]
        # Saving after removing unmatched settings is validated against the
        # current read-only inventory.
        if self._save_sidecar(db_name, inventory, source_links, payload):
            self.output("Unmatched Source-Link setting removed.")

    def _print_values(self, title: str, values: Iterable[Any]) -> None:
        self.output(f"\n{title}")
        printed = False
        for value in values:
            if isinstance(value, dict):
                text = value.get("path") or value.get("scan_root") or value
            else:
                text = value
            self.output(f"- {text}")
            printed = True
        if not printed:
            self.output("(none recorded)")

    @staticmethod
    def _compact_values(values: Iterable[Any]) -> str:
        output: list[str] = []
        for value in values:
            if isinstance(value, dict):
                text = value.get("prefix") or value.get("path") or str(value)
            else:
                text = str(value)
            if text:
                output.append(str(text))
        return ", ".join(output[:4]) if output else "none"

    def _prompt_source_link(
        self,
        *,
        existing: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        current = dict(existing or {})
        current_settings = dict(current.get("settings") or {})
        display_name = self._prompt_preserving_value(
            "Optional Source display name",
            str(current.get("display_name") or ""),
            required=False,
        )
        if display_name is None:
            return None
        if existing is not None:
            provider = self._prompt_choice_preserving(
                "Provider",
                ["sharepoint", "github", "redmine", "other"],
                str(current.get("provider") or ""),
            )
        else:
            provider = self._select_value(
                "Provider",
                ["sharepoint", "github", "redmine", "other"],
            )
        if provider is None:
            return None
        if provider == "sharepoint":
            choices = ["home-only", "append-relative-path"]
        elif provider == "github":
            choices = ["github-blob", "append-relative-path"]
        else:
            choices = ["home-only", "append-relative-path", "regex-template"]
        if existing is not None:
            current_strategy = self._infer_source_link_strategy(
                str(current.get("provider") or ""),
                current_settings,
            )
            strategy = self._prompt_choice_preserving(
                "Link strategy",
                choices,
                current_strategy,
            )
        else:
            strategy = self._select_value("Link strategy", choices)
        if strategy is None:
            return None
        settings: dict[str, Any] = {}
        same_shape = (
            provider == current.get("provider")
            and strategy
            == self._infer_source_link_strategy(
                str(current.get("provider") or ""),
                current_settings,
            )
        )
        prior = current_settings if same_shape else {}
        if provider == "sharepoint":
            home = self._prompt_preserving_value(
                "SharePoint Source home URL",
                str(prior.get("source_home_url") or ""),
                required=strategy == "home-only",
            )
            if home is None:
                return None
            if home:
                settings["source_home_url"] = home
            if strategy == "append-relative-path":
                root = self._prompt_preserving_value(
                    "SharePoint web root URL",
                    str(prior.get("source_web_root") or ""),
                    required=True,
                )
                if root is None:
                    return None
                settings["source_web_root"] = root
        elif provider == "github":
            repository = self._prompt_preserving_value(
                "Repository URL",
                str(prior.get("repository_url") or ""),
                required=True,
            )
            ref = self._prompt_preserving_value(
                "Ref (entered manually)",
                str(prior.get("ref") or ""),
                required=True,
            )
            if repository is None or ref is None:
                return None
            settings = {
                "repository_url": repository,
                "ref": ref,
                "permalink_enabled": False,
            }
            repository_prefix = self._prompt_preserving_value(
                "Repository path prefix",
                str(prior.get("repository_path_prefix") or ""),
                required=False,
            )
            commit = self._prompt_preserving_value(
                "Commit for permalink",
                str(prior.get("commit") or ""),
                required=False,
            )
            if repository_prefix is None or commit is None:
                return None
            if repository_prefix:
                settings["repository_path_prefix"] = repository_prefix
            if commit:
                settings["commit"] = commit
                settings["permalink_enabled"] = True
        elif strategy == "home-only":
            value = self._prompt_preserving_value(
                "Source home URL",
                str(prior.get("source_home_url") or ""),
                required=True,
            )
            if value is None:
                return None
            settings = {"source_home_url": value}
        elif strategy == "append-relative-path":
            value = self._prompt_preserving_value(
                "Source web root URL",
                str(prior.get("source_web_root") or ""),
                required=True,
            )
            if value is None:
                return None
            settings = {"source_web_root": value}
        else:
            pattern = self._prompt_preserving_value(
                "Safe path regex with named groups",
                str(prior.get("path_pattern") or ""),
                required=True,
            )
            template = self._prompt_preserving_value(
                "URL template",
                str(prior.get("url_template") or ""),
                required=True,
            )
            if pattern is None or template is None:
                return None
            settings = {"path_pattern": pattern, "url_template": template}
        return {
            "display_name": display_name,
            "enabled": bool(current.get("enabled", True)),
            "provider": provider,
            "strategy": strategy,
            "settings": settings,
        }

    @staticmethod
    def _infer_source_link_strategy(
        provider: str,
        settings: dict[str, Any],
    ) -> str:
        if provider == "github":
            return "github-blob"
        if settings.get("path_pattern") or settings.get("url_template"):
            return "regex-template"
        if settings.get("source_web_root"):
            return "append-relative-path"
        return "home-only"

    def _prompt_preserving_value(
        self,
        label: str,
        current: str,
        *,
        required: bool,
    ) -> str | None:
        suffix = f" [{current}]" if current else ""
        value = self._ask(
            f"{label}{suffix} "
            "(Enter keeps current; '-' clears optional; Ctrl-C cancels): "
        )
        if value is None:
            return None
        if not value:
            if current:
                return current
            if required:
                self.output(f"{label} is required.")
                return None
            return ""
        if value == "-":
            if required:
                self.output(f"{label} cannot be cleared.")
                return None
            return ""
        return value.strip()

    def _prompt_choice_preserving(
        self,
        label: str,
        choices: list[str],
        current: str,
    ) -> str | None:
        value = self._ask(
            f"{label} [{current}] ({', '.join(choices)}; Enter keeps current): "
        )
        if value is None:
            return None
        selected = value.strip() or current
        if selected not in choices:
            self.output(f"Unsupported {label.lower()}.")
            return None
        return selected

    def _delete_database_interactive(self, db_name: str) -> bool:
        if not self._guard_valid_database_target(db_name):
            return False
        if not self._guard_inactive(db_name):
            return False
        try:
            root = self._validated_database_root(db_name)
            self._ensure_no_active_mutation(root)
        except Exception as exc:
            self.output(
                f"Database cannot be deleted: {type(exc).__name__}: {exc}"
            )
            return False
        documents = 0
        chunks = 0
        inventory = self._load_source_inventory(db_name)
        if inventory is not None:
            for source in self._inventory_sources(inventory):
                documents += int(source.get("document_count") or 0)
                chunks += int(source.get("chunk_count") or 0)
        size = self._directory_size_without_following_links(root)
        self.output(
            "This permanently deletes the selected database directory.\n"
            f"Resolved path: {root}\n"
            f"Documents: {documents}\n"
            f"Chunks: {chunks}\n"
            f"Size: {size} bytes"
        )
        confirmation = self._ask(
            f"Type the exact database name ({db_name}) to continue: "
        )
        if confirmation is None:
            return False
        try:
            self._delete_database(db_name, confirmation)
        except Exception as exc:
            self.output(
                f"Database was not deleted: {type(exc).__name__}: {exc}"
            )
            return False
        self.output(f"Deleted database: {db_name}")
        return True

    @staticmethod
    def _directory_size_without_following_links(root: Path) -> int:
        total = 0
        for current, directory_names, file_names in os.walk(
            root, followlinks=False
        ):
            current_path = Path(current)
            directory_names[:] = [
                name
                for name in directory_names
                if not (current_path / name).is_symlink()
            ]
            for name in file_names:
                path = current_path / name
                if path.is_symlink():
                    continue
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
        return total

    def _delete_database(
        self,
        db_name: str,
        typed_name: str,
    ) -> None:
        if typed_name != db_name:
            raise ManagerError("typed confirmation did not match")
        root = self._validated_database_root(db_name)
        self._ensure_no_active_mutation(root)
        daemon = self._import_daemon_control()
        try:
            with daemon.database_mutation_guard(
                db_name,
                operation="delete",
                timeout_seconds=10.0,
                rag_root=self.rag_root,
                dbs_root=self.dbs_root,
            ):
                # Revalidate after daemon coordination, immediately before
                # the destructive operation.
                root = self._validated_database_root(db_name)
                self._ensure_no_active_mutation(root)
                shutil.rmtree(root)
        except RuntimeError as exc:
            raise ManagerError(str(exc)) from exc

    def _validated_database_root(self, db_name: str) -> Path:
        if not self._valid_database_name(db_name):
            raise ManagerError("invalid database name")
        dbs_root = self.dbs_root.resolve(strict=True)
        candidate = self.dbs_root / db_name
        if candidate.parent != self.dbs_root:
            raise ManagerError("database is not a direct child")
        if candidate.is_symlink():
            raise ManagerError("database root cannot be a symlink")
        resolved = candidate.resolve(strict=True)
        if resolved.parent != dbs_root or resolved.name != db_name:
            raise ManagerError("database target is outside the DB root")
        if not resolved.is_dir():
            raise ManagerError("database target is not a directory")
        return resolved

    @staticmethod
    def _valid_database_name(db_name: str) -> bool:
        return bool(DATABASE_NAME_PATTERN.fullmatch(str(db_name)))

    @staticmethod
    def _ensure_no_active_mutation(db_root: Path) -> None:
        progress = db_root / "logs" / "progress.json"
        if progress.is_symlink():
            raise ManagerError("database progress state cannot be a symlink")
        try:
            payload = json.loads(
                progress.read_text(encoding="utf-8", errors="replace")
            )
        except FileNotFoundError:
            return
        except json.JSONDecodeError as exc:
            raise ManagerError("database progress state is invalid") from exc
        if str(payload.get("status") or "").casefold() == "running":
            raise ManagerError("a database mutation is active")

    def _status_json(self, db_name: str) -> dict[str, Any] | None:
        result = self._invoke(
            "gen_db/status.py",
            ["--db", db_name, "--json"],
            capture_output=True,
        )
        if result is None or int(result.returncode) != 0:
            return None
        try:
            payload = json.loads(str(result.stdout or ""))
        except json.JSONDecodeError:
            self.output("Status returned invalid JSON.")
            return None
        return payload if isinstance(payload, dict) else None

    def _guard_inactive(
        self,
        db_name: str,
        status: dict[str, Any] | None | object = _STATUS_UNSET,
    ) -> bool:
        current = (
            self._status_json(db_name)
            if status is _STATUS_UNSET
            else status
        )
        if current is None:
            self.output(
                f"Operation refused: status for selected database {db_name} "
                "could not be verified."
            )
            return False
        if current and bool(current.get("appears_active")):
            self.output(
                f"Operation refused: selected database {db_name} has an "
                "active ingestion or repair operation."
            )
            return False
        return True

    def _guard_valid_database_target(self, db_name: str) -> bool:
        try:
            self._validated_database_root(db_name)
        except Exception as exc:
            self.output(
                f"Operation refused for selected database {db_name}: "
                f"{type(exc).__name__}: {exc}"
            )
            return False
        return True

    def _show_status(self, db_name: str) -> None:
        status = self._status_json(db_name)
        if status is None:
            return
        catalog = (
            status.get("catalog")
            if isinstance(status.get("catalog"), dict)
            else {}
        )
        documents = status.get("document_count")
        chunks = status.get("chunk_count")
        if documents is None:
            documents = catalog.get("documents") or 0
        if chunks is None:
            chunks = catalog.get("chunks") or 0
        self.output(
            f"Status for {db_name}: {status.get('status') or 'unknown'} | "
            f"phase={status.get('phase') or '-'} | "
            f"documents={documents} | "
            f"chunks={chunks} | "
            f"updated={status.get('updated_at') or 'unknown'}"
        )
        if self._confirm(f"Show detailed status for selected database {db_name}?"):
            self.output(
                json.dumps(
                    status,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )

    def _database_summaries(self) -> list[dict[str, Any]]:
        result = self._invoke(
            "query/list_dbs.py",
            ["--format", "json"],
            capture_output=True,
        )
        if result is None or int(result.returncode) != 0:
            return []
        try:
            payload = json.loads(str(result.stdout or ""))
        except json.JSONDecodeError:
            self.output("Database listing returned invalid JSON.")
            return []
        databases = payload.get("databases") if isinstance(payload, dict) else []
        if not isinstance(databases, list):
            return []
        return [
            dict(item)
            for item in databases
            if isinstance(item, dict) and item.get("name")
        ]

    def _invoke(
        self,
        relative_script: str,
        arguments: Iterable[str],
        *,
        capture_output: bool = False,
        python: Path | None = None,
    ) -> Any | None:
        normalized = Path(relative_script).as_posix()
        if normalized not in ALLOWED_SCRIPTS:
            raise ManagerError("script is not allowlisted")
        script = (self.rag_root / normalized).resolve(strict=False)
        try:
            script.relative_to(self.rag_root)
        except ValueError as exc:
            raise ManagerError("script is outside the RAG root") from exc
        runtime = Path(python or self._runtime_python())
        if not runtime.is_file():
            self.output("setup_required: the Local RAG venv is missing.")
            return None
        argv = [str(runtime), str(script), *[str(value) for value in arguments]]
        kwargs: dict[str, Any] = {
            "shell": False,
            "check": False,
            "cwd": str(self.rag_root),
        }
        if capture_output:
            kwargs.update(
                {
                    "stdout": subprocess.PIPE,
                    "stderr": subprocess.PIPE,
                    "text": True,
                    "encoding": "utf-8",
                    "errors": "replace",
                }
            )
        try:
            completed = self.runner(argv, **kwargs)
        except OSError as exc:
            self.output(f"Command failed to start: {type(exc).__name__}: {exc}")
            return None
        if int(completed.returncode) != 0:
            self.output(
                f"Command exited with status {int(completed.returncode)}."
            )
            if capture_output and completed.stderr:
                self.output(str(completed.stderr).strip())
        return completed

    def _runtime_python(self) -> Path:
        if self._runtime_override is not None:
            return self._runtime_override
        relative = (
            Path("query/.venv/Scripts/python.exe")
            if sys.platform.startswith("win")
            else Path("query/.venv/bin/python")
        )
        return self.rag_root / relative

    def _database_root(self, db_name: str) -> Path:
        return self.dbs_root / db_name

    def _import_source_inventory(self) -> Any:
        tool_root = self.rag_root / "gen_db" / "software_rag_tool"
        if str(tool_root) not in sys.path:
            sys.path.insert(0, str(tool_root))
        from software_rag_tool import source_inventory

        return source_inventory

    def _import_source_links(self) -> Any:
        tool_root = self.rag_root / "gen_db" / "software_rag_tool"
        if str(tool_root) not in sys.path:
            sys.path.insert(0, str(tool_root))
        from software_rag_tool import source_links

        return source_links

    def _import_daemon_control(self) -> Any:
        tool_root = self.rag_root / "gen_db" / "software_rag_tool"
        if str(tool_root) not in sys.path:
            sys.path.insert(0, str(tool_root))
        from software_rag_tool import daemon_control

        return daemon_control

    def _select_value(
        self,
        title: str,
        values: Iterable[str],
    ) -> str | None:
        choices = [str(value) for value in values]
        self.output(f"\n{title}")
        for index, value in enumerate(choices, start=1):
            self.output(f"{index}. {value}")
        choice = self._ask("Select, or 0 to cancel: ")
        if choice in (None, "0"):
            return None
        try:
            index = int(choice) - 1
        except ValueError:
            self.output("Invalid selection.")
            return None
        if index < 0 or index >= len(choices):
            self.output("Invalid selection.")
            return None
        return choices[index]

    def _confirm(self, question: str) -> bool:
        answer = self._ask(f"{question} [y/N]: ")
        return bool(answer and answer.casefold() in {"y", "yes"})

    def _ask(self, prompt: str) -> str | None:
        try:
            return self.input(prompt)
        except (EOFError, KeyboardInterrupt):
            self.output("")
            return None

    def _print_menu(
        self,
        title: str,
        entries: Iterable[tuple[str, str]],
    ) -> None:
        self.output(f"\n{title}")
        for key, label in entries:
            self.output(f"{key}. {label}")


def main() -> int:
    manager = LocalRagManager()
    return manager.run()


if __name__ == "__main__":
    raise SystemExit(main())
