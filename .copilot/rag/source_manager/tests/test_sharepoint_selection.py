from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import manage
from source_manager import SourceManagerError, SourceStore, execution, providers, runner
from source_manager import manager_connections
from source_manager.source_exclusion import parse_include_input
from source_manager.document_filter_counts import count_document_files


class SharePointSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'Documents'
        self.root.mkdir()
        for name in ('docs/guide.txt', 'docs/archive/old.txt', 'api/spec.txt', 'other/private.txt', 'docs/~$owner.docx'):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('fixture', encoding='utf-8')
        self.settings = {'root_env': 'RAG_SHAREPOINT_ROOT', 'relative_path': 'Documents'}

    def test_comma_separated_folders_and_portable_settings(self):
        folders = parse_include_input(r'docs\guide, api, docs, docs')
        self.assertEqual(['api', 'docs'], folders)
        value = providers.validate_provider_config('sharepoint', {
            **self.settings, 'include_paths': folders, 'exclude_paths': ['docs/archive', '**/*.tmp'],
        })
        self.assertEqual(folders, value['include_paths'])
        self.assertEqual(['docs/archive', '**/*.tmp'], value['exclude_paths'])
        legacy = providers.validate_provider_config('sharepoint', self.settings)
        self.assertEqual([], legacy['include_paths'])
        self.assertEqual([], legacy['exclude_paths'])
        for paths in (['../other'], ['/absolute'], ['C:\\private'], ['docs/*'], ['.git']):
            with self.subTest(paths=paths), self.assertRaises(SourceManagerError):
                providers.validate_provider_config('sharepoint', {**self.settings, 'include_paths': paths})

    def test_fetch_prunes_unselected_and_excluded_before_validation_and_count(self):
        # A VCS tree / symlink in an ignored branch must not block selected documents.
        (self.root / 'other' / '.svn').mkdir()
        (self.root / 'docs' / 'archive' / 'link').symlink_to(self.base, target_is_directory=True)
        plan = providers.build_fetch_plan(source_key='fixture', provider='sharepoint',
            settings={**self.settings, 'include_paths': ['docs', 'api'], 'exclude_paths': ['docs/archive']},
            logical_root='work/fixture', work_path='work/fixture')
        (self.base / 'work').mkdir()
        with mock.patch.object(execution, '_is_windows', return_value=True), mock.patch.object(Path, 'open', side_effect=AssertionError('file body read during fetch')):
            value = execution.execute_fetch_plan(plan.to_dict(), self.base / 'work', {}, environment={'RAG_SHAREPOINT_ROOT': str(self.base)})
        self.assertEqual(2, value['documents'])
        self.assertEqual(str(self.root), value['external_add_root'])
        self.assertEqual([], list((self.base / 'work').iterdir()))
        self.assertEqual(2, count_document_files(self.root, include_paths=['docs','api'], exclude_paths=['docs/archive']))
        with self.assertRaises(SourceManagerError):
            execution.validate_external_add_root(self.root, include_paths=['other'])

    def test_indexed_settings_can_change_filters_but_not_root(self):
        db = self.base / 'fixture-rag'
        db.mkdir()
        dto = runner.register_source(db, source_type='sharepoint', display_name='fixture', fetch=self.settings, start=False)
        key = dto['local_source_key']
        store = SourceStore(db)
        saved = store.read_source(key)
        store.confirm_source_id(key, key, expected_revision=saved.revision, expected_etag=saved.etag)
        updated = runner.update_source_configuration(db, key, fetch={**self.settings, 'include_paths': ['docs'], 'exclude_paths': ['docs/archive']})
        self.assertEqual(key, updated['source_id'])
        self.assertEqual(['docs'], store.read_source(key).payload['fetch']['include_paths'])
        with self.assertRaisesRegex(SourceManagerError, 'immutable'):
            runner.update_source_configuration(db, key, fetch={**self.settings, 'relative_path': 'Other'})
        state = store.read_state(key)
        from source_manager.checkpoints import new_run_state
        plan = store.plan(store.read_source(key).payload)
        store.save_state(key, {**new_run_state(plan), 'status': 'partial'}, expected_revision=state.revision, expected_etag=state.etag)
        with self.assertRaisesRegex(SourceManagerError, 'resumed'):
            runner.update_source_configuration(db, key, fetch=self.settings)

    def test_add_command_carries_filters_with_one_stable_external_root(self):
        import json
        from source_manager.subprocess_stream import RESULT_FRAME
        key = 'src_existing-0123456789ab'
        summary = {'operation':'add','source_id':key,'file_count':1,'indexed_files':1,
            'skipped_files':0,'error_files':0,'input_error_files':0,'extract_error_files':0,
            'upserted_records':1,'deleted_records':0,'result_status':'success','error_details':[]}
        command = mock.Mock(return_value=SimpleNamespace(returncode=0, stdout=RESULT_FRAME+json.dumps(summary), stderr=''))
        runner._execute_add(db_root=self.base/'fixture-rag', source={
            'local_source_key':key,'source_type':'sharepoint',
            'fetch':{**self.settings,'include_paths':['docs','api'],'exclude_paths':['docs/archive','**/*.tmp']},
        }, work=self.root, python_executable=Path('python'), rag_root=self.base/'rag', command_runner=command, progress_callback=None)
        command.assert_called_once()
        args = command.call_args.args[0]
        self.assertEqual(str(self.root), args[args.index('--root')+1])
        self.assertIn('--include-path=docs', args)
        self.assertIn('--include-path=api', args)
        self.assertIn('--exclude-path=docs/archive', args)
        self.assertIn('--exclude-path=**/*.tmp', args)
        self.assertIn('--privacy-safe-root', args)
        self.assertNotIn('--scan-subdir', args)

    def make_manager(self):
        rag = self.base / 'rag'
        (rag / 'config').mkdir(parents=True)
        (rag / 'dbs').mkdir()
        return manage.LocalRagManager(rag_root=rag, dbs_root=rag/'dbs', runtime_python=Path('python'), input_fn=lambda _: '', output_fn=lambda _: None, color=False)

    def test_registration_and_existing_edit_use_same_comma_separated_ui(self):
        manager = self.make_manager()
        manager._select_value = mock.Mock(side_effect=['partial', '2'])
        manager._prompt_preserving_value = mock.Mock(side_effect=['Documents','https://example.sharepoint.com/sites/team/Documents','fixture','docs, api','docs/archive, **/*.tmp'])
        windows = SimpleNamespace(**{**vars(os), 'name': 'nt'})
        with mock.patch.object(manager_connections, 'os', windows), mock.patch.object(manager_connections, 'sharepoint_root_status', return_value=SimpleNamespace(configured=True)):
            proposal = manager._prompt_new_sharepoint_source()
        self.assertEqual(['docs','api'], proposal['fetch']['include_paths'])
        self.assertEqual('documents_only', proposal['fetch']['file_selection'])
        self.assertEqual(['docs/archive','**/*.tmp'], proposal['fetch']['exclude_paths'])
        manager._select_value = mock.Mock(side_effect=['1','partial'])
        manager._prompt_preserving_value = mock.Mock(side_effect=['api','**/*.tmp'])
        manager._confirm = mock.Mock(return_value=True)
        source = {'source_type':'sharepoint','source_id':'src_existing-0123456789ab','_local_source_key':'src_existing-0123456789ab','fetch':proposal['fetch']}
        with mock.patch.object(manage, 'os', windows), mock.patch.object(runner, 'update_source_configuration') as save:
            manager._edit_source_fetch_settings('fixture-rag', source)
        save.assert_called_once()
        fetch = save.call_args.kwargs['fetch']
        self.assertEqual(['api'], fetch['include_paths'])
        self.assertEqual('all_supported', fetch['file_selection'])
        self.assertEqual('Documents', fetch['relative_path'])
        self.assertEqual(2, manager._prompt_preserving_value.call_count)

    def test_ui_cancel_and_empty_exclusions(self):
        manager = self.make_manager()
        manager._select_value = mock.Mock(return_value='partial')
        manager._prompt_preserving_value = mock.Mock(return_value=None)
        self.assertIsNone(manager_connections.prompt_sharepoint_selection(manager, {}))
        manager._select_value = mock.Mock(return_value='all')
        manager._prompt_preserving_value = mock.Mock(return_value='')
        self.assertEqual({'include_paths': [], 'exclude_paths': []}, manager_connections.prompt_sharepoint_selection(manager, {'include_paths':['docs'],'exclude_paths':['archive']}))


if __name__ == '__main__':
    unittest.main()
