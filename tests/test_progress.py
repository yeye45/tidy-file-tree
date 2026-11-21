import json
import os
import sys
import tempfile
import unittest
from http.client import HTTPConnection
from pathlib import Path
from threading import Thread
from urllib.request import urlopen

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

    def test_fresh_root_is_read_only_and_idle(self):
        (self.root / 'Downloads').mkdir()
        (self.root / 'Photos').mkdir()
        snap = progress.collect_snapshot(self.root)
        self.assertEqual(snap['currentStage'], 'idle')
        self.assertEqual(snap['stages']['s1']['total'], 2)
        self.assertEqual(snap['stages']['s1']['done'], 0)
        self.assertFalse((self.root / '_organize').exists())

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
        (probe / 'units_dl.json').write_text(json.dumps({'path': str(dl), 'total_units': 12}), encoding='utf-8')
        (probe / 'group_dl.json').write_text(json.dumps({'path': str(dl), 'groups': [{'name': 'G1'}, {'name': 'G2'}]}), encoding='utf-8')
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
                    self.assertEqual(conn.getresponse().status, 400)
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

    def test_system_drive_snapshot_is_read_only(self):
        if os.name != 'nt':
            self.skipTest('Windows-only system drive check')
        sys_drive = os.environ.get('SystemDrive', 'C:').rstrip('\\/') + '\\'
        snap = progress.collect_snapshot(sys_drive)
        self.assertTrue(snap['isSystemDrive'])
        self.assertGreater(snap['protectedSkipped'], 0)
        self.assertFalse((Path(sys_drive) / '_organize').exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
