import contextlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'core'))
import executor
from history import write_preview
from safety import MARKER_FIELDS, SafetyError, encode_tsv


class ExecutionHistoryTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='.organizer-test-history-', dir=PROJECT)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / 'root'
        self.root.mkdir()
        self.env = os.environ | {'LOCALAPPDATA': str(Path(temp.name) / 'state'), 'PYTHONIOENCODING': 'utf-8'}
        self.journal_path = self.root / '_organize/journal.tsv'
        self.report = self.root / '_organize/history.html'

    def cli(self, *args, check=True):
        return subprocess.run([sys.executable, '-B', str(PROJECT / 'tidy_file_tree.py'), *map(str, args)],
                              env=self.env, capture_output=True, encoding='utf-8', check=check)

    def snapshot(self):
        self.cli('history', self.root, '--lang', 'en')
        return self.read_report(self.report)

    @staticmethod
    def read_report(path):
        html = path.read_text(encoding='utf-8')
        data = re.search(r'id="historyData">(.*?)</script>', html).group(1)
        return html, json.loads(data)

    def test_preview_uses_receipt_destinations_and_shows_sanitized_blockers(self):
        source = self.root / 'sample.txt'
        source.write_text('fixture', encoding='utf-8')
        destination = self.root / 'new' / 'archive'
        actions = self.root / 'actions.tsv'
        preview = self.root.parent / 'preview.html'
        receipt = self.root.parent / 'preview.json'
        command = ('apply', self.root, '--actions', actions, '--preview-html', preview)
        tsv = f'op\tsrc\tdst\treason\nmove\t{source}\t{destination}\tfixture\n'
        actions.write_text(tsv, encoding='utf-8')
        self.assertNotEqual(self.cli(*command, check=False).returncode, 0)
        self.assertFalse(preview.exists())
        self.cli(*command, '--dry-run', '--receipt', receipt)
        self.assertFalse(self.journal_path.exists())
        self.assertTrue(source.exists())
        self.assertFalse(destination.exists())
        _, plan = self.read_report(preview)
        self.assertEqual(plan['mode'], 'preview')
        self.assertEqual(plan['operations'][0]['after'], json.loads(receipt.read_text(encoding='utf-8'))['steps'][0]['dst'])
        self.assertEqual(plan['operations'][0]['parents'], [str(destination.parent), str(destination)])

        private = self.root / 'secret-preview.txt'
        private.write_text('fixture', encoding='utf-8')
        self.journal_path.parent.mkdir(exist_ok=True)
        (self.journal_path.parent / 'markers.tsv').write_bytes(encode_tsv([
            {'path': str(private), 'kind': 'SECRET', 'by': 'fixture', 'note': '', 'ts': ''},
        ], MARKER_FIELDS))
        actions.write_text(tsv + f'move\t{private}\t{destination}\tfixture\n', encoding='utf-8')
        result = self.cli(*command, '--dry-run', '--receipt', self.root.parent / 'blocked.json', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root.parent / 'blocked.json').exists())
        html, blocked_plan = self.read_report(preview)
        self.assertEqual(len(blocked_plan['blocked']), 1)
        self.assertTrue(blocked_plan['receiptBlocked'])
        self.assertNotIn(private.name, html)
        missing_source = self.root / 'missing-source'
        actions.write_text(tsv + f'move\t{missing_source}\t{destination}\tfixture\n', encoding='utf-8')
        result = self.cli('apply', self.root, '--actions', actions, '--dry-run', '--preview-html',
                          missing_source / 'preview.html', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(missing_source.exists())

    def test_move_undo_redo_and_recovery_are_traceable(self):
        archive = self.root / 'archive'
        archive.mkdir()
        source = self.root / 'recording.bin'
        source.write_bytes(b'fixture')
        actions = self.root / 'actions.tsv'
        actions.write_text(f'op\tsrc\tdst\treason\nmove\t{source}\t{archive}\tfixture\n', encoding='utf-8')
        self.cli('apply', self.root, '--actions', actions)
        self.cli('undo', '--journal', self.journal_path, '--last')
        self.cli('undo', '--journal', self.journal_path, '--last')
        self.assertEqual((archive / source.name).read_bytes(), b'fixture')

        interrupted = self.root / 'interrupted.bin'
        interrupted.write_bytes(b'interrupted fixture')
        actions.write_text(f'op\tsrc\tdst\treason\nmove\t{interrupted}\t{archive}\tfixture\n', encoding='utf-8')
        with patch.dict(os.environ, self.env), patch.object(executor, 'rename_noreplace', side_effect=OSError):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(executor.main(['apply', '--root', str(self.root), '--actions', str(actions)]), 1)

        before = self.journal_path.read_bytes()
        _, history = self.snapshot()
        self.assertEqual(self.journal_path.read_bytes(), before)
        move, undo, redo, pending = history['operations']
        self.assertEqual([op['direction'] for op in (move, undo, redo)], ['', 'undo', 'redo'])
        self.assertEqual(undo['inverseOf'], move['events'][-1]['seq'])
        self.assertEqual(redo['inverseOf'], undo['events'][-1]['seq'])
        self.assertEqual([event['status'] for event in pending['events']], ['pending'])

        self.cli('recover', self.root)
        _, refreshed = self.snapshot()
        events = refreshed['operations'][-1]['events']
        self.assertEqual([event['status'] for event in events], ['pending', 'aborted'])
        self.assertTrue(events[-1]['recovered'])
        self.assertEqual(interrupted.read_bytes(), b'interrupted fixture')

    def test_secret_paths_and_raw_reasons_are_excluded_and_bad_mirror_is_reported(self):
        private = self.root / 'private.txt'
        with patch.dict(os.environ, self.env):
            journal = executor.Journal(self.root)
            with journal.locked():
                journal.append('legacy', 'move', private.name, 'previous.txt', 'legacy note')
                journal.append('legacy', 'rename', 'public.txt', 'new.txt', '</script><img src=x>')
            (journal.path.parent / 'markers.tsv').write_bytes(encode_tsv([
                {'path': str(private), 'kind': 'SECRET', 'by': 'fixture', 'note': '', 'ts': ''},
            ], MARKER_FIELDS))
            journal.mirror.write_text('invalid mirror', encoding='utf-8')

        html, history = self.snapshot()
        self.assertTrue(history['mirrorDiffers'])
        self.assertEqual(history['hidden'], 1)
        self.assertEqual(history['operations'][0]['before'], 'public.txt')
        for text in ('private.txt', 'previous.txt', '<img src=x>'):
            self.assertNotIn(text, html)

    def test_history_preserves_unrelated_output_and_rejects_a_secret_root(self):
        self.report.parent.mkdir()
        self.report.write_text('unrelated page', encoding='utf-8')
        self.assertNotEqual(self.cli('history', self.root, check=False).returncode, 0)
        self.assertEqual(self.report.read_text(encoding='utf-8'), 'unrelated page')
        (self.report.parent / 'markers.tsv').write_bytes(encode_tsv([
            {'path': str(self.root), 'kind': 'SECRET', 'by': 'fixture', 'note': '', 'ts': ''},
        ], MARKER_FIELDS))
        outside = self.root.parent / 'outside.html'
        self.assertNotEqual(self.cli('history', self.root, '--out', outside, check=False).returncode, 0)
        self.assertFalse(outside.exists())

    def test_preview_respects_protection_in_an_authorized_external_root(self):
        external = self.root.parent / 'external'
        private = external / 'private'
        private.mkdir(parents=True)
        state = external / '_organize'
        state.mkdir()
        (state / 'markers.tsv').write_bytes(encode_tsv([
            {'path': str(private), 'kind': 'SECRET', 'by': 'fixture', 'note': '', 'ts': ''},
        ], MARKER_FIELDS))
        report = private / 'preview.html'
        with patch.dict(os.environ, self.env), self.assertRaises(SafetyError):
            write_preview(executor.Journal(self.root), report, [], [], [{'root': str(external)}], False, 'en')
        self.assertFalse(report.exists())
