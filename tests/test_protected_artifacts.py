import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parents[1]


class ProtectedArtifactsTests(unittest.TestCase):
    def setUp(self):
        workspace = tempfile.TemporaryDirectory(prefix='.organizer-test-', dir=PROJECT)
        self.addCleanup(workspace.cleanup)
        self.base = Path(workspace.name)
        self.root = self.base / 'root'
        self.probe = self.root / '_organize' / 'probe'
        self.probe.mkdir(parents=True)
        self.env = dict(os.environ, LOCALAPPDATA=str(self.base / 'local'), PYTHONIOENCODING='utf-8')

    def run_cli(self, *args):
        return subprocess.run([sys.executable, '-B', str(PROJECT / 'tidy_file_tree.py'), *map(str, args)],
                              env=self.env, capture_output=True, encoding='utf-8')

    def write_json(self, path, value):
        path.write_text(json.dumps(value), encoding='utf-8')

    def test_report_outputs_protect_existing_files_and_allow_refresh(self):
        protected = self.root / 'Private'
        permitted = self.root / 'Public'
        protected.mkdir()
        permitted.mkdir()
        self.write_json(self.probe.parent / 'policy.json', {'no_go_zones': ['Private']})
        stable = 'existing user content'
        for command, extension in (('decisions', 'json'), ('ui', 'html'), ('progress', 'html')):
            with self.subTest(command=command):
                destination = protected / (command + '.' + extension)
                destination.write_text(stable, encoding='utf-8')
                args = ['--no-open'] if command in ('ui', 'progress') else []
                # The protected output is on a sibling of the selected organizing scope.
                result = self.run_cli(command, permitted, '--out', destination, *args)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('保护', result.stderr)
                self.assertEqual(destination.read_text(encoding='utf-8'), stable)

                unknown = self.base / ('user-' + destination.name)
                unknown.write_text(stable, encoding='utf-8')
                refused = self.run_cli(command, permitted, '--out', unknown, *args)
                self.assertNotEqual(refused.returncode, 0)
                self.assertEqual(unknown.read_text(encoding='utf-8'), stable)

                generated = self.base / ('generated-' + destination.name)
                for _ in range(2):
                    refreshed = self.run_cli(command, permitted, '--out', generated, *args)
                    self.assertEqual(refreshed.returncode, 0, refreshed.stderr)

        answers = self.base / 'answers.json'
        self.write_json(answers, {'video_split': '全放一起'})
        result = self.run_cli('setup', 'apply', protected, answers)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((protected / '_organize').exists())

    def test_s1_skip_hides_stale_work_and_blocks_execution_in_narrower_root(self):
        for node, action, sample in [('Ignored', 'skip', 'never-export.exe'), ('Visible', 'split', 'ordinary.exe')]:
            folder = self.root / node
            unit = folder / 'Payload'
            unit.mkdir(parents=True)
            (unit / sample).touch()
            self.write_json(self.probe / f'probe_{node}.json', {'path': str(folder), 'action': action})
            self.write_json(self.probe / f'units_{node}.json', {
                'path': str(folder), 'total_units': 1,
                'confirmed_units': [{'path': str(unit), 'unit_count': 1}],
            })
            self.write_json(self.probe / f'group_{node}.json', {
                'path': str(folder), 'groups': [{'name': 'x7f3 batch', 'units': ['Payload']}],
            })

        questions = self.run_cli('decisions', self.root, '--need-ai')
        self.assertEqual(questions.returncode, 1, questions.stderr)
        self.assertEqual([q['parent'] for q in json.loads(questions.stdout)['questions']], ['Visible'])
        self.assertNotIn('never-export.exe', questions.stdout)

        board = self.base / 'board.html'
        result = self.run_cli('ui', self.root, '--out', board, '--no-open')
        self.assertEqual(result.returncode, 0, result.stderr)
        embedded = board.read_text(encoding='utf-8').split('const DATA = ', 1)[1].split(';\n', 1)[0]
        self.assertEqual([node['name'] for node in json.loads(embedded)['nodes']], ['Visible'])

        ignored = self.root / 'Ignored'
        source = ignored / 'Payload' / 'never-export.exe'
        denied = self.run_cli('apply', ignored, '--rename', source, 'renamed.exe')
        self.assertNotEqual(denied.returncode, 0)
        self.assertTrue(source.is_file())
        self.assertFalse((ignored / '_organize').exists())

        public = self.root / 'Visible' / 'Payload' / 'ordinary.exe'
        allowed = self.run_cli('apply', self.root, '--rename', public, 'renamed.exe', '--dry-run')
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
