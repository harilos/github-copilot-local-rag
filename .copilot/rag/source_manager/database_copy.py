from __future__ import annotations

import functools
import re
from typing import Any, Mapping

from .database_copy_core import DatabaseCopyError, copy_database
from multi_select import SelectionRow, toggle_selection

_RUNTIME_PATCH_MARKER = "_local_rag_database_copy_runtime_installed"
_CLASS_PATCH_MARKER = "_local_rag_database_copy_ui_installed"
_DB_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*-rag$")


def install_database_copy_runtime() -> None:
    from . import manager_connections

    if bool(getattr(manager_connections, _RUNTIME_PATCH_MARKER, False)):
        return
    original = manager_connections.install_manager_connection_ui

    @functools.wraps(original)
    def install_manager_connection_ui(manager_class: type[Any]) -> None:
        original(manager_class)
        install_manager_database_copy(manager_class)

    manager_connections.install_manager_connection_ui = install_manager_connection_ui
    setattr(manager_connections, _RUNTIME_PATCH_MARKER, True)


def install_manager_database_copy(manager_class: type[Any]) -> None:
    if bool(getattr(manager_class, _CLASS_PATCH_MARKER, False)):
        return

    def database_screen(self: Any, db_name: str) -> None:
        if not self._guard_valid_database_target(db_name):
            return
        while self._database_root(db_name).is_dir():
            self._print_screen_header("DB操作", db_name=db_name)
            self._show_database_overview(db_name)
            self._print_menu(
                "操作",
                (
                    ("1", "Sourceを見る・更新する"),
                    ("2", "新しいSourceを追加する"),
                    ("3", "このDBの全Sourceを更新・再開する"),
                    ("4", "DBの名前・説明を変更する"),
                    ("5", "このDBのコピーを作る"),
                    ("6", "問題があるとき"),
                    ("7", "このDBを削除する【危険】"),
                    ("0", "戻る"),
                ),
            )
            choice = self._ask("番号を入力してください: ")
            if choice in (None, "0"):
                return
            if choice == "1":
                self._sources_screen(db_name)
            elif choice == "2":
                self._add_source_screen(db_name)
            elif choice == "3":
                self._update_database_sources(db_name)
            elif choice == "4":
                self._edit_database_metadata(db_name)
            elif choice == "5":
                self._copy_database_screen(db_name)
            elif choice == "6":
                self._problem_screen(db_name)
            elif choice == "7":
                if self._delete_database_interactive(db_name):
                    return
            else:
                self._invalid_selection("0～7")

    def copy_database_screen(self: Any, db_name: str) -> None:
        if not self._guard_valid_database_target(db_name):
            return
        source_root = self._validated_database_root(db_name)
        inventory = self._load_source_inventory(db_name)
        catalog_sources = (
            self._inventory_sources(inventory) if inventory is not None else []
        )
        sources = self._combined_source_records(db_name, catalog_sources)
        excluded = choose_excluded_sources(self, db_name, sources)
        if excluded is None:
            return

        new_name = self._prompt_preserving_value(
            "コピー先DB名",
            proposed_copy_name(db_name),
            required=True,
            description=(
                "半角英数字で始まり、末尾が -rag の新しいDB名です。"
                "元DBとは独立して更新・削除できます。"
            ),
            examples=self._examples("database_name"),
        )
        if new_name is None:
            self._print_info("DBコピーを中止しました。")
            return
        new_name = str(new_name).strip()
        if not _DB_NAME.fullmatch(new_name):
            self._print_error(
                "コピー先DB名は半角英数字で始め、末尾を -rag にしてください。"
            )
            return
        destination = self._database_root(new_name)
        if destination.exists() or destination.is_symlink():
            self._print_error(f"DB「{new_name}」は既に存在します。")
            return

        metadata = self._read_database_metadata(db_name)
        title = self._prompt_preserving_value(
            "コピー先の表示名",
            f"{metadata['title']}（コピー）",
            required=False,
            description="空欄ならコピー先DB名を表示名として使います。",
            examples=self._examples("database_title"),
            empty_help="コピー先DB名を利用",
        )
        if title is None:
            self._print_info("DBコピーを中止しました。")
            return
        query_hint = self._prompt_preserving_value(
            "コピー先の検索ヒント",
            str(metadata.get("query_hint") or ""),
            required=False,
            description="元DBの検索ヒントをそのまま利用できます。",
            examples=self._examples("database_query_hint"),
            empty_help="検索ヒントなし",
        )
        if query_hint is None:
            self._print_info("DBコピーを中止しました。")
            return

        excluded_records = [
            dict(source)
            for index, source in enumerate(sources, start=1)
            if choice_key(source, index) in excluded
        ]
        copied_count = len(sources) - len(excluded_records)
        self.output("\nコピー内容")
        self.output(f"  元DB: {db_name}")
        self.output(f"  コピー先DB: {new_name}")
        self.output(f"  コピーするSource: {copied_count:,}件")
        self.output(f"  コピーしないSource: {len(excluded_records):,}件")
        for source in excluded_records:
            self.output("    - " + strike_text(self, source_name(source)))
        self.output(
            "  Sourceの取得先: 元DBと同じ設定をコピー\n"
            "  元DB: 変更しない\n"
            "  除外Source: コピー先のベクトル・catalog・clean・取得設定から削除"
        )
        if not self._confirm("この内容でDBコピーを作成しますか？"):
            self._print_info("DBコピーを中止しました。")
            return

        try:
            result = copy_database(
                source_root,
                destination,
                destination_name=new_name,
                title=str(title).strip() or new_name,
                query_hint=str(query_hint).strip(),
                excluded_sources=excluded_records,
                rag_root=self.rag_root,
                progress_callback=self._progress_callback("DBコピー"),
            )
        except Exception as exc:
            self._print_internal_diagnostic(
                exc,
                operation="DBコピー",
                stage="database.copy",
                db_name=db_name,
                can_resume=False,
            )
            self.output(
                "元DBは変更されていません。未完成のコピー先DBも残しません。"
            )
            return
        self._print_success(f"DB「{new_name}」を作成しました。")
        self.output(
            "コピーしなかったSource: "
            f"{int(result.get('excluded_source_count') or 0):,}件"
        )
        self.output(
            "コピー先は元DBとは独立しています。"
            "以後のSource更新・削除は別々です。"
        )

    manager_class._database_screen = database_screen
    manager_class._copy_database_screen = copy_database_screen
    setattr(manager_class, _CLASS_PATCH_MARKER, True)


def choose_excluded_sources(
    manager: Any,
    db_name: str,
    sources: list[dict[str, Any]],
) -> set[str] | None:
    if not sources:
        return set()
    rows = tuple(
        SelectionRow(
            choice_key(source, index),
            (
                f"{source_name(source)} "
                f"[{str(source.get('source_type') or 'other')}] "
                f"{int(source.get('document_count') or 0):,} documents"
            ),
        )
        for index, source in enumerate(sources, start=1)
    )
    result = toggle_selection(
        rows,
        ask=manager._ask,
        output=manager.output,
        invalid=manager._invalid_selection,
        title=f"Source selection for {db_name}",
        selected_text="copy",
        excluded_text="do not copy",
    )
    if result.mode == "cancelled":
        manager._print_info("Database copy was cancelled.")
        return None
    selected = set(result.keys)
    return {row.key for row in rows if row.key not in selected}

def choice_key(source: Mapping[str, Any], index: int) -> str:
    return str(
        source.get("_local_source_key")
        or source.get("source_id")
        or f"row-{index}"
    )


def source_name(source: Mapping[str, Any]) -> str:
    return str(
        source.get("display_name")
        or source.get("source_id")
        or source.get("_local_source_key")
        or "Source"
    )


def strike_text(manager: Any, text: str) -> str:
    value = str(text)
    if bool(getattr(manager, "use_color", False)):
        return f"\033[9m{value}\033[0m"
    return f"~~{value}~~"


def proposed_copy_name(db_name: str) -> str:
    stem = db_name[:-4] if db_name.endswith("-rag") else db_name
    return f"{stem}-copy-rag"
