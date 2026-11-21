from http.client import HTTPConnection
import json
from pathlib import Path
import sys
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch
from urllib.request import urlopen

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'core'))
import scan_tree
from safety import MARKER_FIELDS, SafetyError, encode_tsv


class ScanTreeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='.organizer-test-tree-', dir=PROJECT)
        self.addCleanup(temp.cleanup)
        self.workspace = Path(temp.name)
        env_patch = patch.dict('os.environ', {'LOCALAPPDATA': str(self.workspace / 'local-state')})
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.root = self.workspace / 'library'
        self.root.mkdir()

    def markers(self, root, *paths_and_kinds):
        state = root / '_organize'
        state.mkdir(exist_ok=True)
        records = [{'path': str(path), 'kind': kind, 'by': 'fixture', 'note': '', 'ts': ''}
                   for path, kind in paths_and_kinds]
        (state / 'markers.tsv').write_bytes(encode_tsv(records, MARKER_FIELDS))

    def stream(self, depth=3):
        root, protection = scan_tree.scan_protection(self.root)
        return list(scan_tree.observations(root, protection, depth))

    @staticmethod
    def by_name(events):
        nodes = {}
        for event in events:
            if event['event'] == 'node':
                nodes.setdefault(event['id'], {}).update(event)
        return {node['name']: node for node in nodes.values()}

    def fixture_file(self, *parts):
        path = self.root.joinpath(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        return path

    def test_split_season_draft_lifts_files_and_paired_subtitles(self):
        first = self.fixture_file('Star Train', 'A', 'Star.Train.S01E1-1080p.mkv')
        second = self.fixture_file('Star Train', 'B', 'Star.Train.S01E10-1080p.mkv')
        subtitle = self.fixture_file('Star Train', 'A', 'Star.Train.S01E1-1080p.zh-Hans.srt')
        parent = first.parent.parent
        before = sorted(self.root.rglob('*'))
        events = self.stream()
        [draft] = [event for event in events if event['event'] == 'draft']
        self.assertEqual(draft['season'], 1)
        self.assertEqual(draft['issues'], [])
        self.assertEqual({(row['source'], row['target']) for row in draft['rows']},
                         {(str(file), str(parent / file.name)) for file in (first, second, subtitle)})
        records = {Path(event['path']): event for event in events if event['event'] == 'inventory'}
        self.assertEqual(records[first.parent]['episodes'][0]['subtitles'], [subtitle.name])
        self.assertEqual(records[second.parent]['episodes'][0]['subtitles'], [])
        self.assertEqual(sorted(self.root.rglob('*')), before)

    def test_generic_parent_has_separate_season_drafts_and_review_issues(self):
        first = self.fixture_file('Downloads', 'A', '星轨 第01季第01集.mkv')
        self.fixture_file('Downloads', 'B', '星轨 第01季第02集.mkv')
        self.fixture_file('Downloads', 'B', '星轨 第01季第01集.mp4')
        self.fixture_file('Downloads', 'A', '星轨 第02季第01集.mkv')
        self.fixture_file('Downloads', 'B', '星轨 第02季第02集.mkv')
        self.fixture_file('Downloads', 'B', '星轨 第01季第10集.en.srt')
        for name in ('星轨 第02季第10集-12', '星轨 第02季第10集至第12集', '星轨.S02E10 & E12'):
            self.fixture_file('Downloads', 'A', name+'.mkv')
        dest = first.parent.parent / '星轨'
        events = self.stream()
        drafts = {event['season']: event for event in events if event['event'] == 'draft'}
        self.assertEqual(set(drafts), {1, 2})
        self.assertEqual(set(drafts[1]['issues']), {'duplicateEpisode', 'unpairedSubtitles', 'versions', 'mixedBatch'})
        self.assertEqual(len(drafts[2]['rows']), 2)
        self.assertTrue(all(Path(row['target']).parent == dest for draft in drafts.values() for row in draft['rows']))
        self.assertFalse(dest.exists())

    def test_drafts_flag_existing_targets_and_omit_protected_paths(self):
        first = self.fixture_file('Sky', 'A', 'Sky.S01E01.mkv')
        second = self.fixture_file('Sky', 'B', 'Sky.S01E02.mkv')
        secret_file = self.fixture_file('Sky', 'B', 'PRIVATE_DRAFT_TEST.S01E07.mkv')
        secret_target = second.parent.parent / second.name
        self.fixture_file('Sky', first.name)
        self.fixture_file('Sky', 'Runtime', 'main.py')
        (self.root / 'Sky/Runtime/models').mkdir()
        (self.root / 'Sky/Runtime/custom_nodes').mkdir()
        self.fixture_file('Sky', 'Runtime', 'Internal', 'Sky.S01E05.mkv')
        self.markers(self.root, (secret_file, 'SECRET'), (secret_target, 'SECRET'))
        events = self.stream()
        self.assertNotIn(secret_file.name, json.dumps(events))
        self.assertNotIn('Sky.S01E05.mkv', json.dumps(events))
        [draft] = [event for event in events if event['event'] == 'draft']
        self.assertEqual(set(draft['issues']), {'duplicateEpisode', 'nameConflict', 'blockedTarget', 'incompleteGroup'})
        self.assertIsNone(draft['rows'][1]['target'])

    def test_capped_inventories_only_offer_drafts_for_review(self):
        self.fixture_file('Sky', 'A', 'Sky.S01E01.mkv')
        self.fixture_file('Sky', 'A', 'Sky.S01E03.mkv')
        for ext in ('srt', 'ass'):
            self.fixture_file('Sky', 'A', f'Sky.S01E01.{ext}')
        self.fixture_file('Sky', 'B', 'Sky.S01E02.mkv')
        with patch.object(scan_tree, 'ENTRY_LIMIT', 3):
            events = self.stream()
        self.assertTrue(events[-1]['limited'])
        [draft] = [event for event in events if event['event'] == 'draft']
        self.assertIn('incompleteGroup', draft['issues'])
        self.assertFalse(any(event['event'] == 'draft' for event in self.stream(depth=1)))

    def test_ai_questions_are_bounded_and_bound_to_observed_names(self):
        for name, start in [('BATCH_ONE', 1), ('BATCH_TWO', 11)]:
            for episode in range(start, start + 10):
                self.fixture_file('Star Train', name, f'Star.Train.S01E{episode:02}.mkv')
            self.fixture_file('Morning', name, f'Morning.S01E{start:02}.mkv')
        self.fixture_file('Star Train', 'BATCH_TWO', 'Star.Train.S01E01.2160p.mp4')
        hidden = self.fixture_file('Star Train', 'BATCH_TWO', 'PRIVATE_EPISODE_SAMPLE.S01E30.mkv')
        self.markers(self.root, (hidden, 'SECRET'))

        def question():
            [item] = [event['reviewQuestion'] for event in self.stream() if 'reviewQuestion' in event]
            return item

        item = question()
        data = json.dumps(item)
        for secret in (json.dumps(str(self.root))[1:-1], 'BATCH_ONE', 'BATCH_TWO', hidden.name):
            self.assertNotIn(secret, data)
        evidence = item['evidence']
        self.assertEqual(evidence['videos'], 21)
        self.assertEqual(evidence['episodes']['duplicateExamples'], [1])
        self.assertEqual(evidence['sourceRanges'][0], {'folder': 1, 'videos': 10, 'min': 1, 'max': 10})
        self.assertLessEqual(len(evidence['sampleFiles']), 8)
        self.fixture_file('AAA-other-directory', 'hello.txt')
        self.assertEqual(question(), item)
        unnamed = self.root / 'Star Train/BATCH_ONE/Star.Train.S01E06.mkv'
        unnamed.rename(unnamed.with_name('Star_Train.S01E06.mkv'))
        changed = question()
        self.assertEqual(changed['evidence'], evidence)
        self.assertNotEqual(changed['id'], item['id'])

    def test_review_ranges_cover_sampled_folders_beyond_the_first_eight(self):
        for index in range(1, 11):
            self.fixture_file('Sky', f'Batch {index:02}', f'Sky.S01E{index:02}.mkv')
        self.fixture_file('Sky', 'Batch 10', 'Sky.S01E01.mp4')
        [evidence] = [event['reviewQuestion']['evidence'] for event in self.stream() if 'reviewQuestion' in event]
        samples = {item['folder'] for item in evidence['sampleFiles']}
        ranges = {item['folder'] for item in evidence['sourceRanges']}
        self.assertIn(10, samples)
        self.assertLessEqual(samples, ranges)
        self.assertLessEqual(len(ranges), 8)

    def test_shallow_classification_and_scoped_protection(self):
        series = self.root / 'Episodes'
        series.mkdir()
        for num in range(1, 4):
            (series / f'Sample.S01E0{num}.mkv').touch()
        runtime = self.root / 'Portable'
        runtime.mkdir()
        (runtime / 'ComfyUI').mkdir()
        (runtime / 'python_embeded').mkdir()
        (runtime / 'run_cpu.bat').touch()
        foreign = self.root / 'Foreign'
        foreign.mkdir()
        (foreign / 'FOREIGN_CHILD_TEST').mkdir()
        secret = self.root / 'SECRET_NAME_TEST'
        secret.mkdir()
        (secret / 'secret-document.txt').touch()
        self.markers(self.workspace, (secret, 'SECRET'), (foreign, 'FOREIGN'))

        events = self.stream()
        data = json.dumps(events)
        self.assertNotIn(secret.name, data)
        self.assertNotIn('FOREIGN_CHILD_TEST', data)
        nodes = self.by_name(events)
        self.assertEqual(nodes['Episodes']['kind'], 'series')
        self.assertEqual(nodes['Episodes']['sequence']['range'], '1-3')
        self.assertEqual(nodes['Portable']['kind'], 'runtime')
        self.assertEqual(nodes['Foreign']['status'], 'skipped')
        with self.assertRaises(SafetyError):
            scan_tree.scan_protection(secret)

    def test_limits_report_an_incomplete_sample(self):
        for number in (1, 2):
            (self.root / f'{number}.txt').touch()
        with patch.object(scan_tree, 'ENTRY_LIMIT', 1):
            events = self.stream(depth=0)
        root = self.by_name(events)[self.root.name]
        self.assertEqual(root['status'], 'review')
        self.assertEqual(root['kind'], 'documents')
        self.assertTrue(events[-1]['limited'])
        (self.root / 'Child').mkdir()
        depth_limited = self.stream(depth=0)
        self.assertNotIn('Child', self.by_name(depth_limited))
        self.assertTrue(depth_limited[-1]['limited'])

    def test_unrecovered_ancestor_marker_mirror_blocks_scanning(self):
        from executor import Journal
        with patch.dict('os.environ', {'LOCALAPPDATA': str(self.workspace / 'state')}):
            current = Journal(self.workspace).mirror_dir
            mirror = self.workspace / 'state' / 'packtidy-mirror' / current.name
            mirror.mkdir(parents=True)
            (mirror / 'markers.tsv').write_bytes(encode_tsv([], MARKER_FIELDS))
            with self.assertRaises(SafetyError):
                scan_tree.scan_protection(self.root)
            self.assertFalse(current.exists())

    def test_local_server_streams_updates_and_reloads_protection(self):
        child = self.root / 'AfterServerStarts'
        child.mkdir()
        server, url = scan_tree.make_server(self.root, 1, 'en')
        with server:
            worker = Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                with urlopen(url, timeout=3) as page:
                    self.assertIn(b'Directory assessment tree', page.read())
                self.markers(self.root, (child, 'SECRET'))
                with urlopen(url + 'events', timeout=3) as stream:
                    self.assertEqual(stream.headers.get_content_type(), 'text/event-stream')
                    events = [json.loads(line[6:]) for line in stream if line.startswith(b'data: ')]
                self.assertNotIn(child.name, self.by_name(events))
                self.assertEqual(events[0]['status'], 'queued')
                self.assertEqual(events[1]['status'], 'scanning')
                self.assertEqual(events[-1]['event'], 'done')
                connection = HTTPConnection('127.0.0.1', server.server_port, timeout=3)
                try:
                    connection.request('GET', '/', headers={'Host': 'invalid.local'})
                    self.assertEqual(connection.getresponse().status, 400)
                finally:
                    connection.close()
            finally:
                server.shutdown()
                worker.join()

    def test_system_drive_read_only_scan_protects_system_entries(self):
        import os
        if os.name != 'nt':
            self.skipTest('Windows-specific system drive scan')
        import ctypes
        sys_drive = os.environ.get('SystemDrive', 'C:').rstrip('\\/') + '\\'
        root, protection = scan_tree.scan_protection(sys_drive)
        entries, children, hidden, _ = scan_tree.inspect_level(root, root, protection)
        child_map = {child.name.casefold(): marker for child, marker in children}
        entry_names = {entry['name'].casefold() for entry in entries}
        for must_skip in ('windows', 'program files', 'programdata'):
            if must_skip in child_map:
                self.assertEqual(child_map[must_skip], 'protected', f'{must_skip} must be skipped')
        if 'users' in child_map:
            self.assertIsNone(child_map['users'], 'Users must remain browsable as a container')
        for sys_file in ('pagefile.sys', 'hiberfil.sys', 'swapfile.sys'):
            self.assertNotIn(sys_file, entry_names)
        self.assertGreater(hidden, 0)

        # Also verify FILE_ATTRIBUTE_SYSTEM files in a normal root are excluded from scan entries
        flagged = self.root / 'hidden_sys.dat'
        flagged.write_text('sys', encoding='utf-8')
        set_attr = ctypes.windll.kernel32.SetFileAttributesW
        try:
            self.assertNotEqual(set_attr(str(flagged), 0x4), 0)
            r2, p2 = scan_tree.scan_protection(self.root)
            e2, _, h2, _ = scan_tree.inspect_level(r2, r2, p2)
            self.assertNotIn('hidden_sys.dat', {item['name'] for item in e2})
            self.assertGreaterEqual(h2, 1)
        finally:
            set_attr(str(flagged), 0x80)

    def test_progressive_drill_down_expand_endpoint_and_traversal_guard(self):
        deep = self.root / 'Level1' / 'Level2' / 'Level3' / 'Level4'
        deep.mkdir(parents=True)
        (deep / 'doc.txt').write_text('ok', encoding='utf-8')
        secret = self.root / 'Level1' / 'TOP_SECRET_SUB'
        secret.mkdir()
        self.markers(self.root, (secret, 'SECRET'))

        # Depth 1 stops at Level1 and flags hasMore=True
        events = self.stream(depth=1)
        lvl1 = self.by_name(events)['Level1']
        self.assertTrue(lvl1['hasMore'])
        self.assertEqual(lvl1['unloaded'], 1)  # SECRET excluded!

        server, url = scan_tree.make_server(self.root, 1, 'zh-CN')
        with server:
            worker = Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                with urlopen(url + 'expand?path=Level1', timeout=3) as res:
                    data = json.loads(res.read().decode('utf-8'))
                names = [c['name'] for c in data['children']]
                self.assertEqual(names, ['Level2'])
                self.assertTrue(data['children'][0]['hasMore'])
                self.assertNotIn('TOP_SECRET_SUB', json.dumps(data))

                with urlopen(url + 'expand?path=Level1/Level2/Level3', timeout=3) as res:
                    data3 = json.loads(res.read().decode('utf-8'))
                self.assertEqual([c['name'] for c in data3['children']], ['Level4'])
                self.assertFalse(data3['children'][0]['hasMore'])

                # Traversal escape is rejected
                with urlopen(url + 'expand?path=../../outside', timeout=3) as res:
                    bad = json.loads(res.read().decode('utf-8'))
                self.assertIn('error', bad)
            finally:
                server.shutdown()
                worker.join()
