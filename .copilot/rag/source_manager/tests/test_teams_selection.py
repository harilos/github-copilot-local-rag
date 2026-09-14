from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import test_sharepoint_selection as shared
from source_manager import SourceStore, execution, providers, runner, teams_source
from source_manager.subprocess_stream import RESULT_FRAME


class TeamsSelectionTests(shared.SharePointSelectionTests):
    provider = 'teams'

    def test_registration_and_existing_edit_use_same_comma_separated_ui(self):
        manager = self.make_manager()
        manager._select_value = mock.Mock(side_effect=['partial', '2'])
        manager._prompt_preserving_value = mock.Mock(side_effect=[
            'Documents', 'fixture', 'docs, api', 'docs/archive, **/*.tmp',
        ])
        windows = SimpleNamespace(**{**vars(os), 'name': 'nt'})
        with mock.patch.object(teams_source, 'os', windows), mock.patch.object(
            teams_source, 'sharepoint_root_status', return_value=SimpleNamespace(configured=True),
        ):
            proposal = manager._prompt_new_teams_source()
        self.assertEqual('teams', proposal['source_type'])
        self.assertEqual(['docs', 'api'], proposal['fetch']['include_paths'])
        self.assertEqual(['docs/archive', '**/*.tmp'], proposal['fetch']['exclude_paths'])
        self.assertEqual('documents_only', proposal['fetch']['file_selection'])
        self.assertNotIn('link', proposal)

        manager._select_value = mock.Mock(side_effect=['1', 'partial'])
        manager._prompt_preserving_value = mock.Mock(side_effect=['api', '**/*.tmp'])
        manager._confirm = mock.Mock(return_value=True)
        source = {
            'source_type': 'teams', 'source_id': 'src_existing-0123456789ab',
            '_local_source_key': 'src_existing-0123456789ab', 'fetch': proposal['fetch'],
        }
        with mock.patch.object(teams_source, 'os', windows), mock.patch.object(runner, 'update_source_configuration') as save:
            manager._edit_source_fetch_settings('fixture-rag', source)
        save.assert_called_once()
        fetch = save.call_args.kwargs['fetch']
        self.assertEqual(['api'], fetch['include_paths'])
        self.assertEqual(['**/*.tmp'], fetch['exclude_paths'])
        self.assertEqual('all_supported', fetch['file_selection'])
        self.assertEqual('Documents', fetch['relative_path'])
        self.assertEqual(2, manager._prompt_preserving_value.call_count)

        # Cancel after choosing a different file type must not save only that choice.
        manager._select_value = mock.Mock(side_effect=['1', None])
        with mock.patch.object(teams_source, 'os', windows), mock.patch.object(runner, 'update_source_configuration') as save:
            manager._edit_source_fetch_settings('fixture-rag', source)
        save.assert_not_called()

    def test_document_only_fetch_counts_only_selected_nonexcluded_files(self):
        (self.root / 'other' / '.svn').mkdir()
        (self.root / 'docs' / 'archive' / 'link').symlink_to(self.base, target_is_directory=True)
        (self.root / 'docs' / 'code.py').write_text('code', encoding='utf-8')
        plan = providers.build_fetch_plan(
            source_key='fixture', provider='teams', logical_root='work/fixture', work_path='work/fixture',
            settings={**self.settings, 'include_paths': ['docs'], 'exclude_paths': ['docs/archive'],
                      'file_selection': 'documents_only'},
        )
        self.assertEqual('teams', plan.provider)
        events = []
        (self.base / 'work').mkdir()
        with mock.patch.object(execution, '_is_windows', return_value=True):
            result = execution.execute_fetch_plan(
                plan.to_dict(), self.base / 'work', {},
                environment={'RAG_SHAREPOINT_ROOT': str(self.base)}, progress_callback=events.append,
            )
        self.assertEqual(1, result['documents'])
        self.assertEqual(str(self.root), result['external_add_root'])
        self.assertTrue(events)
        self.assertFalse(any(event.get('provider') == 'sharepoint' for event in events))

    def test_reflection_uses_external_selection_including_all_excluded(self):
        (self.root / 'other' / '.svn').mkdir()
        (self.root / 'docs' / 'archive' / 'link').symlink_to(self.base, target_is_directory=True)
        for selection, count in [('all_supported', 1), ('documents_only', 0)]:
            with self.subTest(selection=selection):
                db = self.base / (selection + '-rag')
                db.mkdir()
                store = SourceStore(db)
                exclusions = ['docs/archive'] if count else ['docs']
                source = store.create_source(
                    source_type='teams', display_name='fixture', local_source_key='src_teams-0123456789ab',
                    fetch={**self.settings, 'include_paths': ['docs'], 'exclude_paths': exclusions,
                           'file_selection': selection},
                )
                key = source.payload['local_source_key']
                state = store.save_state(key, {
                    **runner.new_run_state(store.plan(source.payload)), 'status': 'fetched',
                    'phase': 'reflect', 'fetched_count': count, 'pending_count': count,
                }, expected_revision=0, expected_etag=runner.MISSING_ETAG)
                summary = {
                    'operation': 'add', 'source_id': key, 'file_count': count, 'indexed_files': count,
                    'skipped_files': 0, 'error_files': 0, 'input_error_files': 0, 'extract_error_files': 0,
                    'upserted_records': count, 'deleted_records': 0, 'result_status': 'success', 'error_details': [],
                }
                command = mock.Mock(return_value=SimpleNamespace(
                    returncode=0, stdout=RESULT_FRAME + json.dumps(summary), stderr='',
                ))
                runner._reflect_and_sync(
                    store, source, state, add_root=self.root, command_runner=command,
                    python_executable=Path('python'), rag_root=self.base / 'rag',
                    metadata_publisher=lambda *_args: None, progress_callback=None,
                )
                command.assert_called_once()
                args = command.call_args.args[0]
                entry = 'add_data.py' if selection == 'all_supported' else 'add_data_documents_only.py'
                self.assertTrue(any(Path(value).name == entry for value in args))
                self.assertIn('--privacy-safe-root', args)
                self.assertIn('--include-path=docs', args)
                self.assertIn('--exclude-path=' + exclusions[0], args)
                self.assertNotIn('--scan-subdir', args)
                self.assertEqual(str(self.root), args[args.index('--root') + 1])
                self.assertEqual('teams', store.read_source(key).payload['source_type'])
                self.assertEqual('complete', store.read_state(key).payload['status'])
