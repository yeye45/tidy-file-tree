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
import history as history_mod
from history import make_server, trace_path, write_preview
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

    def test_explorer_present_overlay_excludes_secrets_and_tracks_destinations(self):
        dl = self.root / 'Downloads'
        dl.mkdir()
        keep_file = dl / 'keep-me.txt'
        keep_file.write_text('stay', encoding='utf-8')
        moved_dir = dl / 'Toolkit'
        (moved_dir / 'sub').mkdir(parents=True)
        (moved_dir / 'sub' / 'tool.exe').write_bytes(b'bin')
        secret_file = dl / 'secret-token.pem'
        secret_file.write_text('do-not-leak', encoding='utf-8')
        dest = self.root / 'Apps'
        dest.mkdir()

        self.journal_path.parent.mkdir(exist_ok=True)
        (self.journal_path.parent / 'markers.tsv').write_bytes(encode_tsv([
            {'path': str(secret_file), 'kind': 'SECRET', 'by': 'user', 'note': '', 'ts': ''},
        ], MARKER_FIELDS))

        actions = self.root / 'actions.tsv'
        actions.write_text(f'op\tsrc\tdst\treason\nmove\t{moved_dir}\t{dest}\tfixture\n', encoding='utf-8')
        self.cli('apply', self.root, '--actions', actions)

        html, payload = self.snapshot()
        self.assertNotIn('secret-token.pem', html)
        self.assertIn('Downloads', payload['present'])
        dl_names = [entry[0] for entry in payload['present']['Downloads']]
        self.assertIn('keep-me.txt', dl_names)
        self.assertNotIn('secret-token.pem', dl_names)
        # Moved folder's destination metadata records it as an existing folder
        dest_key = str(dest / 'Toolkit')
        self.assertTrue(payload['destinationMeta'][dest_key]['exists'])
        self.assertTrue(payload['destinationMeta'][dest_key]['isDir'])

        # Trace a nested child inside the moved folder using its pre-move path
        traced = trace_path(payload['operations'], self.root, dl / 'Toolkit' / 'sub' / 'tool.exe')
        self.assertEqual(traced['status'], 'move')
        self.assertEqual(Path(traced['current']), dest / 'Toolkit' / 'sub' / 'tool.exe')
        self.assertEqual(len(traced['steps']), 1)
        self.assertTrue(traced['steps'][0]['inherited'])

    def test_history_skips_marked_and_policy_excluded_directories(self):
        protected = {'Others': 'FOREIGN', 'HandsOff': 'NO_TOUCH', 'Vault': 'SECRET'}
        for name in [*protected, 'PolicyExcluded', 'ProbeExcluded', 'Public']:
            directory = self.root / name
            directory.mkdir()
            (directory / (name + '-file.txt')).touch()
        self.journal_path.parent.mkdir()
        (self.journal_path.parent / 'markers.tsv').write_bytes(encode_tsv([
            {'path': str(self.root / name), 'kind': kind, 'by': 'user', 'note': '', 'ts': ''}
            for name, kind in protected.items()
        ], MARKER_FIELDS))
        (self.journal_path.parent / 'policy.json').write_text(
            json.dumps({'no_go_zones': ['PolicyExcluded']}), encoding='utf-8',
        )
        probe = self.journal_path.parent / 'probe'
        probe.mkdir()
        (probe / 'skip.json').write_text(json.dumps({
            'path': str(self.root / 'ProbeExcluded'), 'action': 'skip',
        }), encoding='utf-8')
        html, data = self.snapshot()
        self.assertEqual([entry[0] for entry in data['present']['.']], ['Public'])
        self.assertIn('Public-file.txt', html)
        for name in [*protected, 'PolicyExcluded', 'ProbeExcluded']:
            self.assertNotIn(name + '-file.txt', html)
            self.assertNotIn(name, data['present'])

    def test_trace_path_handles_multihop_rename_and_undo(self):
        dl = self.root / 'Downloads'
        dl.mkdir()
        target_dir = self.root / 'Archive'
        target_dir.mkdir()
        item = dl / 'clip.mp4'
        item.write_bytes(b'vid')

        actions = self.root / 'actions.tsv'
        actions.write_text(f'op\tsrc\tdst\treason\nmove\t{item}\t{target_dir}\tstep1\n', encoding='utf-8')
        self.cli('apply', self.root, '--actions', actions)
        moved = target_dir / 'clip.mp4'
        actions.write_text(f'op\tsrc\tdst\treason\nrename\t{moved}\tclip-final.mp4\tstep2\n', encoding='utf-8')
        self.cli('apply', self.root, '--actions', actions)

        _, payload = self.snapshot()
        traced = trace_path(payload['operations'], self.root, item)
        self.assertEqual(traced['status'], 'move')
        self.assertEqual(Path(traced['current']), target_dir / 'clip-final.mp4')
        self.assertEqual(len(traced['steps']), 2)

        # Undo both batches in reverse order -> traces back to original location with status 'undone'
        run1 = payload['operations'][0]['run']
        run2 = payload['operations'][1]['run']
        self.cli('undo', '--journal', self.journal_path, '--run-id', run2)
        self.cli('undo', '--journal', self.journal_path, '--run-id', run1)
        _, after_undo = self.snapshot()
        traced_undone = trace_path(after_undo['operations'], self.root, item)
        self.assertEqual(traced_undone['status'], 'undone')
        self.assertEqual(Path(traced_undone['current']), item)

    def test_live_history_server_ls_stat_and_reveal_guards(self):
        import threading
        import urllib.error
        import urllib.parse
        import urllib.request

        dl = self.root / 'Downloads'
        dl.mkdir()
        item = dl / 'mod.zip'
        item.write_bytes(b'zip')
        secret = dl / 'private.key'
        secret.write_text('secret', encoding='utf-8')
        dest = self.root / 'Mods'
        dest.mkdir()

        self.journal_path.parent.mkdir(exist_ok=True)
        (self.journal_path.parent / 'markers.tsv').write_bytes(encode_tsv([
            {'path': str(secret), 'kind': 'SECRET', 'by': 'user', 'note': '', 'ts': ''},
        ], MARKER_FIELDS))

        actions = self.root / 'actions.tsv'
        actions.write_text(f'op\tsrc\tdst\treason\nmove\t{item}\t{dest}\tfixture\n', encoding='utf-8')
        self.cli('apply', self.root, '--actions', actions)

        with patch.dict(os.environ, self.env):
            journal = executor.Journal(self.root)
            server, url = make_server(journal, journal.protection(), lang='zh-CN')
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                # 1. Main page loads with live=true and connect-src 'self'
                with urllib.request.urlopen(url) as resp:
                    html = resp.read().decode('utf-8')
                    self.assertIn("connect-src 'self'", resp.headers.get('Content-Security-Policy', ''))
                self.assertIn('"live": true', html)
                self.assertNotIn('private.key', html)

                # 2. /ls lists directory and excludes SECRET
                ls_url = url + 'ls?path=' + urllib.parse.quote(str(dl))
                with urllib.request.urlopen(ls_url) as resp:
                    ls_data = json.loads(resp.read().decode('utf-8'))
                self.assertTrue(ls_data['ok'])
                self.assertNotIn('private.key', [e[0] for e in ls_data['entries']])

                # 3. /stat checks the requested path without rebuilding the snapshot.
                dest_file = dest / 'mod.zip'
                stat_url = url + 'stat?path=' + urllib.parse.quote(str(dest_file))
                with patch.object(history_mod, 'collect_history') as collect:
                    with urllib.request.urlopen(stat_url) as resp:
                        st_data = json.loads(resp.read().decode('utf-8'))
                    collect.assert_not_called()
                self.assertTrue(st_data['ok'] and st_data['exists'] and not st_data['isDir'])

                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(url + 'stat?path=' + urllib.parse.quote(str(secret)))
                self.assertEqual(ctx.exception.code, 400)

                with patch.object(history_mod, 'LIVE_LS_LIMIT', 2):
                    for index in range(5):
                        (dest / f'file-{index}.txt').touch()
                    endpoint = url + 'ls?path=' + urllib.parse.quote(str(dest))
                    first = json.load(urllib.request.urlopen(endpoint))
                    self.assertTrue(first['truncated'])
                    query = urllib.parse.urlencode({
                        'offset': first['nextOffset'], 'revision': first['revision'],
                    })
                    second = json.load(urllib.request.urlopen(endpoint + '&' + query))
                    self.assertEqual(len(first['entries'] + second['entries']), 4)
                    self.assertEqual(len({row[0] for row in first['entries'] + second['entries']}), 4)
                    with self.assertRaises(urllib.error.HTTPError) as ctx:
                        urllib.request.urlopen(endpoint + '&offset=2&revision=old')
                    self.assertEqual(ctx.exception.code, 409)

                # 4. /reveal requires X-Tidy-Action header, blocks out-of-scope/SECRET, allows valid dest
                req_no_hdr = urllib.request.Request(
                    url + 'reveal',
                    data=json.dumps({'path': str(dest_file)}).encode('utf-8'),
                    headers={'Content-Type': 'application/json'},
                    method='POST',
                )
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(req_no_hdr)
                self.assertEqual(ctx.exception.code, 403)

                revealed = []
                with patch.object(history_mod, '_reveal_in_file_manager', side_effect=lambda p: revealed.append(str(p))):
                    req_ok = urllib.request.Request(
                        url + 'reveal',
                        data=json.dumps({'path': str(dest_file)}).encode('utf-8'),
                        headers={'Content-Type': 'application/json', 'X-Tidy-Action': 'reveal'},
                        method='POST',
                    )
                    with urllib.request.urlopen(req_ok) as resp:
                        rv = json.loads(resp.read().decode('utf-8'))
                    self.assertTrue(rv['ok'])
                    self.assertEqual(len(revealed), 1)
                    self.assertEqual(Path(revealed[0]), dest_file.resolve())

                    req_outside = urllib.request.Request(
                        url + 'reveal',
                        data=json.dumps({'path': str(self.root.parent)}).encode('utf-8'),
                        headers={'Content-Type': 'application/json', 'X-Tidy-Action': 'reveal'},
                        method='POST',
                    )
                    with self.assertRaises(urllib.error.HTTPError) as ctx:
                        urllib.request.urlopen(req_outside)
                    self.assertEqual(ctx.exception.code, 400)

                (self.journal_path.parent / 'markers.tsv').write_bytes(encode_tsv([
                    {'path': str(dest), 'kind': 'NO_TOUCH', 'by': 'user', 'note': '', 'ts': ''},
                ], MARKER_FIELDS))
                for endpoint in ('ls', 'stat'):
                    with self.assertRaises(urllib.error.HTTPError) as ctx:
                        urllib.request.urlopen(url + endpoint + '?path=' + urllib.parse.quote(str(dest)))
                    self.assertEqual(ctx.exception.code, 400)
            finally:
                server.shutdown()
                server.server_close()
