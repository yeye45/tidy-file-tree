import json
import os
import subprocess
import sys
import tempfile
import unittest
from http.client import HTTPConnection
from pathlib import Path
from threading import Thread
from urllib.parse import urlsplit
from urllib.request import urlopen
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'core'))

import executor
import progress
from safety import MARKER_FIELDS, encode_tsv


class ProgressDashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='.organizer-test-progress-', dir=PROJECT)
        self.workspace = Path(self.temp.name)
        self.root = self.workspace / 'disk'
        self.root.mkdir()
        self.addCleanup(self.temp.cleanup)
        environment = patch.dict(os.environ, {'LOCALAPPDATA': str(self.workspace / 'local-state'),
                                              'XDG_STATE_HOME': str(self.workspace / 'local-state')})
        environment.start()
        self.addCleanup(environment.stop)

    def test_fresh_root_is_read_only_and_idle(self):
        (self.root / 'Downloads').mkdir()
        (self.root / 'Photos').mkdir()
        snap = progress.collect_snapshot(self.root)
        self.assertEqual(snap['currentStage'], 'idle')
        self.assertEqual(snap['stages']['s1']['total'], 2)
        self.assertEqual(snap['stages']['s1']['done'], 0)
        self.assertFalse((self.root / '_organize').exists())
        self.assertEqual(snap['nextStep']['id'], 'start_analysis')
        self.assertEqual(snap['nextStep']['actor'], 'auto')

    def test_next_step_reports_empty_range_and_no_changes(self):
        empty = progress.collect_snapshot(self.root)
        self.assertEqual(empty['nextStep']['id'], 'no_data')
        (self.root / '_organize').mkdir()
        (self.root / '_organize' / 'policy.json').write_text(json.dumps({'setup': {'ready': True}}), encoding='utf-8')
        no_changes = progress.collect_snapshot(self.root)
        self.assertEqual(no_changes['nextStep']['id'], 'no_changes')

    def test_next_step_prioritizes_pending_transaction_recovery(self):
        (self.root / 'Inbox').mkdir()
        (self.root / '_organize' / 'transactions').mkdir(parents=True)
        with patch.object(executor.Journal, 'unresolved', return_value=[{'id': 'x'}, {'id': 'y'}]):
            snap = progress.collect_snapshot(self.root)
        self.assertEqual(snap['nextStep']['id'], 'recover_transactions')
        self.assertEqual(snap['nextStep']['actor'], 'auto')
        self.assertEqual(snap['currentStage'], 'S5')
        self.assertEqual(snap['nextStep']['count'], 2)

    def test_plan_page_prompts_review_not_approval_or_apply(self):
        org = self.root / '_organize'
        org.mkdir()
        (org / 'decisions.json').write_text(json.dumps({
            'summary': {'total': 1, 'keep': 0, 'move': 1, 'new': 0, 'needCheck': 0},
            'aiQuestions': [],
        }), encoding='utf-8')
        (org / 'plan.tsv').write_text('group\taction\nA\tmove\n', encoding='utf-8')
        (org / 'ui.html').write_text('<html></html>', encoding='utf-8')
        snap = progress.collect_snapshot(self.root)
        self.assertEqual(snap['nextStep']['id'], 'preview_plan')
        self.assertEqual(snap['nextStep']['actor'], 'auto')
        (org / 'plan.tsv').unlink()
        snap = progress.collect_snapshot(self.root)
        self.assertEqual(snap['nextStep']['id'], 'review_plan')
        self.assertEqual(snap['nextStep']['actor'], 'user')

    def test_tracked_preview_requires_the_same_plan_and_receipt(self):
        org = self.root / '_organize'
        org.mkdir()
        plan = org / 'plan.tsv'
        source = self.root / 'sample.txt'
        source.touch()
        plan.write_bytes(encode_tsv([
            {'op': 'rename', 'src': str(source), 'dst': 'first.txt', 'reason': 'test'},
        ], executor.ACTION_FIELDS))

        def preview(name):
            receipt = self.workspace / f'{name}.json'
            proc = subprocess.run([sys.executable, '-B', str(PROJECT / 'tidy_file_tree.py'), 'apply',
                                   str(self.root), '--actions', str(plan), '--dry-run', '--receipt', str(receipt)],
                                  capture_output=True)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertEqual(progress.collect_snapshot(self.root)['nextStep']['id'], 'confirm_preview')
            return receipt

        receipt = preview('first')
        receipt.write_bytes(receipt.read_bytes() + b'\n')
        self.assertEqual(progress.collect_snapshot(self.root)['nextStep']['id'], 'preview_plan')
        preview('second')
        plan.write_bytes(plan.read_bytes().replace(b'first.txt', b'other.txt'))
        self.assertEqual(progress.collect_snapshot(self.root)['nextStep']['id'], 'preview_plan')
        self.assertTrue(source.is_file())

    def test_partial_execution_does_not_report_completion(self):
        org = self.root / '_organize'
        org.mkdir()
        plan = org / executor.WORKBENCH_PLAN
        rows = []
        for name in ('a', 'b'):
            source = self.root / f'{name}.txt'
            source.touch()
            rows.append({'op': 'rename', 'src': str(source), 'dst': f'{name}-filed.txt', 'reason': 'test'})
        plan.write_bytes(encode_tsv(rows, executor.ACTION_FIELDS))
        receipt = self.workspace / 'partial.json'
        command = [sys.executable, '-B', str(PROJECT / 'tidy_file_tree.py'), 'apply', str(self.root),
                   '--actions', str(plan), '--batch-size', '1']
        no_receipt = subprocess.run(command, input=b'n\n', capture_output=True)
        self.assertNotEqual(no_receipt.returncode, 0)
        self.assertFalse((org / 'journal.tsv').exists())
        self.assertTrue((self.root / 'a.txt').is_file())
        command += ['--receipt', str(receipt)]
        preview = subprocess.run([*command, '--dry-run'], capture_output=True)
        self.assertEqual(preview.returncode, 0, preview.stdout + preview.stderr)
        apply = subprocess.run(command, input=b'n\n', capture_output=True)
        self.assertEqual(apply.returncode, 0, apply.stdout + apply.stderr)
        snap = progress.collect_snapshot(self.root)
        self.assertEqual(snap['nextStep']['id'], 'review_partial')
        self.assertEqual(snap['planReview'], {'status': 'partial', 'count': 2, 'done': 1})
        self.assertTrue((self.root / 'a-filed.txt').is_file())
        self.assertTrue((self.root / 'b.txt').is_file())

    def test_workbench_preview_explains_blocked_actions_without_receipt(self):
        source = self.root / 'source.txt'
        source.touch()
        (self.root / 'existing.txt').touch()
        journal = executor.Journal(self.root)
        progress._submit_workbench(journal, [['rename', str(source), 'existing.txt', 'test']])
        url = progress._workbench_preview(journal, 'zh-CN')
        snap = progress.collect_snapshot(self.root)
        self.assertEqual(snap['nextStep']['id'], 'review_blocked')
        report = Path(snap['planReview']['preview'])
        self.assertEqual(url, f'preview/{report.parent.name}')
        self.assertIn('目标名称已存在', report.read_text(encoding='utf-8'))
        self.assertFalse((report.parent / 'receipt.json').exists())
        self.assertFalse(journal.path.exists())
        self.assertTrue(source.exists())

    def test_full_pipeline_stages_quarantine_and_secret_redaction(self):
        dl = self.root / 'Downloads'
        media = self.root / 'Media'
        secret = self.root / 'PRIVATE_KEYS_TOP_SECRET'
        quarantine = self.root / '_待删除'
        for d in (dl, media, secret, quarantine):
            d.mkdir()
        (quarantine / 'dup1.zip').write_text('a', encoding='utf-8')
        (quarantine / 'dup2.zip').write_text('b', encoding='utf-8')

        org = self.root / '_organize'
        probe = org / 'probe'
        probe.mkdir(parents=True)
        (org / 'policy.json').write_text(json.dumps({
            'setup': {'adult_separation': '分开'},
            'mapping': {'video_movie': '影视'},
            'no_go_zones': [],
        }), encoding='utf-8')
        (org / 'markers.tsv').write_bytes(encode_tsv([
            {'path': str(secret), 'kind': 'SECRET', 'by': 'user', 'note': 'keys', 'ts': '2026-01-01T00:00:00Z'},
        ], MARKER_FIELDS))

        # S1 probes
        (probe / 'probe_dl.json').write_text(json.dumps({'path': str(dl), 'action': 'split'}), encoding='utf-8')
        (probe / 'probe_media.json').write_text(json.dumps({'path': str(media), 'action': 'keep'}), encoding='utf-8')
        # S2 & S3 for Downloads
        members = [f'document-{n}.pdf' for n in range(12)]
        for member in members:
            (dl / member).touch()
        (probe / 'units_dl.json').write_text(json.dumps({'path': str(dl), 'total_units': 12,
                                                        'loose_files_as_units': members}), encoding='utf-8')
        (probe / 'group_dl.json').write_text(json.dumps({'path': str(dl), 'groups': [
            {'name': 'G1', 'units': members[:6]}, {'name': 'G2', 'units': members[6:]},
        ]}), encoding='utf-8')
        # S4 decisions & ui
        (org / 'decisions.json').write_text(json.dumps({
            'summary': {'total': 2, 'keep': 1, 'move': 1, 'new': 0, 'needCheck': 1},
            'aiQuestions': [{'group': 'G2', 'aiJudgement': {'category': 'docs'}}],
        }), encoding='utf-8')
        (org / 'ui.html').write_text('<html></html>', encoding='utf-8')

        # S5 executed move
        journal = executor.Journal(self.root)
        with journal.locked():
            journal.append('run-1', 'move', str(dl / 'a.pdf'), str(media / 'a.pdf'),
                           json.dumps({'tx': 'a' * 32, 'effect': 'move'}), 'done')

        snap = progress.collect_snapshot(self.root)
        serialized = json.dumps(snap, ensure_ascii=False)
        self.assertNotIn('PRIVATE_KEYS_TOP_SECRET', serialized)
        self.assertEqual(snap['secretCount'], 1)
        self.assertEqual(snap['quarantine']['count'], 2)
        self.assertEqual(snap['quarantine']['dir'], '_待删除')
        self.assertTrue(snap['stages']['s0']['done'])
        self.assertEqual(snap['stages']['s1']['done'], 2)
        self.assertEqual(snap['stages']['s2']['units'], 12)
        self.assertEqual(snap['stages']['s3']['groups'], 2)
        self.assertEqual(snap['stages']['s4']['needCheck'], 1)
        self.assertTrue(snap['stages']['s4']['uiReady'])
        self.assertEqual(snap['stages']['s5']['moves'], 1)
        self.assertTrue(snap['stages']['s6']['ok'])
        self.assertEqual(snap['currentStage'], 'S6')
        self.assertEqual(snap['nextStep']['id'], 'completed')

    def test_live_page_opens_before_scan_and_shares_snapshot_reads(self):
        (self.root / 'Inbox').mkdir()
        real_snapshot = progress.collect_snapshot
        with patch.object(progress, 'collect_snapshot', wraps=real_snapshot) as collect:
            server, url = progress.make_server(self.root, interval=10)
            with server:
                worker = Thread(target=server.serve_forever, daemon=True)
                worker.start()
                try:
                    with urlopen(url, timeout=3) as response:
                        html = response.read().decode('utf-8')
                    self.assertIn('id="progress-snapshot" type="application/json">null', html)
                    collect.assert_not_called()
                    for _ in range(2):
                        with urlopen(url + 'snapshot', timeout=3) as response:
                            self.assertEqual(json.load(response)['nextStep']['id'], 'start_analysis')
                    with urlopen(url + 'events', timeout=3) as response:
                        body = response.readline().decode('utf-8')
                        self.assertEqual(json.loads(body.removeprefix('data: '))['nextStep']['id'], 'start_analysis')
                    collect.assert_called_once()
                finally:
                    server.shutdown()
                    worker.join()

    def test_live_server_snapshot_sse_and_offline_export(self):
        (self.root / 'Inbox').mkdir()
        server, url = progress.make_server(self.root, 'zh-CN', interval=0.4)
        with server:
            worker = Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                with urlopen(url, timeout=3) as res:
                    html = res.read().decode('utf-8')
                    self.assertIn('整理实时进度', html)
                with urlopen(url + 'snapshot', timeout=3) as res:
                    data = json.loads(res.read().decode('utf-8'))
                    self.assertEqual(data['stages']['s1']['total'], 1)
                # Reject invalid Host header
                conn = HTTPConnection('127.0.0.1', server.server_port, timeout=3)
                try:
                    conn.request('GET', '/', headers={'Host': 'evil.local'})
                    response = conn.getresponse()
                    self.assertEqual(response.status, 400)
                    response.read()
                    parts = urlsplit(url)
                    source = self.root / 'Inbox'
                    body = json.dumps([['rename', str(source), 'Filed', 'test']])
                    path = parts.path + 'workbench-actions'
                    conn.request('POST', path, body, {'Origin': 'https://example.invalid',
                                                      'Content-Type': 'application/json'})
                    response = conn.getresponse()
                    self.assertEqual(response.status, 403)
                    response.read()
                    self.assertFalse((self.root / '_organize').exists())
                    self.assertTrue(source.is_dir())
                finally:
                    conn.close()
            finally:
                server.shutdown()
                worker.join()

        out_html = self.workspace / 'progress_snapshot.html'
        self.assertEqual(progress.main(self.root, lang='en', out=str(out_html), open_browser=False), 0)
        self.assertTrue(out_html.is_file())
        content = out_html.read_text(encoding='utf-8')
        self.assertIn('"live": false', content)

    def test_empty_decisions_does_not_jump_to_s4(self):
        (self.root / 'Downloads').mkdir()
        org = self.root / '_organize'
        org.mkdir()
        (org / 'policy.json').write_text(json.dumps({'setup': {'k': 'v'}}), encoding='utf-8')
        (org / 'decisions.json').write_text(json.dumps({
            'summary': {'total': 0, 'keep': 0, 'move': 0, 'new': 0, 'needCheck': 0},
            'aiQuestions': [],
        }), encoding='utf-8')
        snap = progress.collect_snapshot(self.root)
        self.assertEqual(snap['currentStage'], 'S1')
        self.assertFalse(snap['stages']['s4']['ready'])

    def test_enrolled_loose_file_with_probe_is_not_stale(self):
        loose = self.root / 'loose-file.txt'
        loose.write_text('content', encoding='utf-8')
        probe = self.root / '_organize' / 'probe'
        probe.mkdir(parents=True)
        (probe / 'probe_loose.json').write_text(json.dumps({
            'path': str(loose), 'action': 'keep',
        }), encoding='utf-8')

        snap = progress.collect_snapshot(self.root)

        row = next(node for node in snap['nodes'] if node['name'] == 'loose-file.txt')
        self.assertEqual(row['stage'], 's1_done')
        self.assertEqual(row['action'], 'keep')
        self.assertEqual(snap['stages']['s1']['done'], 1)
        self.assertEqual(snap['stages']['s1']['total'], 1)

    def test_truncated_root_scan_is_reported_and_never_s6_ok(self):
        for name in ('A', 'B'):
            (self.root / name).mkdir()
        old_limit = progress.NODE_SCAN_LIMIT
        progress.NODE_SCAN_LIMIT = 1
        self.addCleanup(setattr, progress, 'NODE_SCAN_LIMIT', old_limit)
        journal = executor.Journal(self.root)
        with journal.locked():
            journal.append('run-1', 'move', 'A/a', 'B/a', json.dumps({'tx': 'b' * 32}), 'done')
        snap = progress.collect_snapshot(self.root)
        self.assertTrue(snap['scan']['truncated'])
        self.assertTrue(snap['stages']['s6']['truncated'])
        self.assertFalse(snap['stages']['s6']['ok'])
        self.assertEqual(snap['currentStage'], 'S5')
        self.assertIn('Scan limit reached', progress.render_page({'lang': 'en', 'live': False}, snap).decode())

    def test_invalid_decision_counts_are_ignored_and_reported(self):
        org = self.root / '_organize'
        org.mkdir()
        (org / 'decisions.json').write_text(json.dumps({
            'summary': {'total': 'bad', 'needCheck': None, 'move': 2, 'keep': 0, 'new': 0},
            'aiQuestions': [],
        }), encoding='utf-8')
        snap = progress.collect_snapshot(self.root)
        self.assertEqual(snap['stages']['s4']['totalGroups'], 0)
        self.assertEqual(snap['stages']['s4']['needCheck'], 0)
        self.assertEqual(snap['stages']['s4']['invalidArtifactCount'], 2)
        self.assertIn('invalid summary field', progress.render_page({'lang': 'en', 'live': False}, snap).decode())

    def test_system_drive_snapshot_is_read_only(self):
        if os.name != 'nt':
            self.skipTest('Windows-only system drive check')
        sys_drive = os.environ.get('SystemDrive', 'C:').rstrip('\\/') + '\\'
        snap = progress.collect_snapshot(sys_drive)
        self.assertTrue(snap['isSystemDrive'])
        self.assertGreater(snap['protectedSkipped'], 0)
        self.assertFalse((Path(sys_drive) / '_organize').exists())
        names = {n['name'].casefold() for n in snap['nodes']}
        # Active user's KnownFolders (e.g. Downloads / Desktop / Documents) must be visible on C:\
        if (Path.home() / 'Downloads').is_dir():
            expected_dl = f'users/{Path.home().name}/downloads'.casefold()
            self.assertIn(expected_dl, names)


if __name__ == '__main__':
    unittest.main(verbosity=2)
