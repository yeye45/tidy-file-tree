import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'core'))
from executor import Journal, guard
from safety import SafetyError
from scan_tree import inspect_level
from signals import subtree_size


class NestedProtectionTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix='.organizer-test-nested-', dir=PROJECT)
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)
        self.root = self.base / 'root'
        self.state = self.root / '_organize'
        self.state.mkdir(parents=True)
        environment = patch.dict(os.environ, {'LOCALAPPDATA': str(self.base / 'state'), 'PYTHONIOENCODING': 'utf-8'})
        environment.start()
        self.addCleanup(environment.stop)

    def put(self, relative, text='fixture'):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def policy(self, parent, exceptions, **extra):
        self.put('_organize/policy.json', json.dumps({
            'nested_protection': [{'no_touch': parent, 'exceptions': exceptions}], **extra,
        }))

    def cli(self, *args):
        return subprocess.run([sys.executable, '-B', str(PROJECT / 'tidy_file_tree.py'), *map(str, args)],
                              capture_output=True, encoding='utf-8')

    def test_export_can_move_and_undo_but_runtime_and_saves_stay_locked(self):
        shot = self.put('Games/Example/Screenshots/picture.jpg')
        live = self.put('Games/Example/Saves/slot.dat')
        sibling = self.put('Games/Example/Downloads/other.zip')
        self.put('SteamLibrary/steamapps/common/Demo/Saves/slot.dat')
        backup = self.put('Copied Backups/Example/Saves/snapshot.dat')
        self.policy('Games', ['Games/*/Screenshots', 'Games/*/Saves'])
        protection = Journal(self.root).protection()
        self.assertEqual(Path(guard(shot.parent, self.root, protection, descendants=True)), shot.parent)
        self.assertEqual(Path(guard(backup, self.root, protection)), backup)
        for path in (self.root / 'Games', shot.parent.parent, live, sibling):
            with self.subTest(path=path), self.assertRaises(SafetyError):
                guard(path, self.root, protection, descendants=True)
        self.assertEqual(Path(guard(shot.parent.parent, self.root, protection, allow_container=True)), shot.parent.parent)
        entries, children, _, _ = inspect_level(shot.parent.parent, self.root, protection)
        self.assertEqual([entry['name'] for entry in entries], ['Screenshots'])
        self.assertIsNotNone(dict(children)[live.parent])

        receipt = self.base / 'review.json'
        target = self.root / 'Media'
        actions = self.put('_organize/actions.tsv', f'op\tsrc\tdst\treason\nmove\t{shot.parent}\t{target}\tfixture\n')
        command = ('apply', self.root, '--actions', actions, '--receipt', receipt)
        for params in (('--dry-run',), ()):
            result = self.cli(*command, *params)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((target / 'Screenshots' / shot.name).is_file())
        self.assertTrue(live.is_file())
        result = self.cli('undo', '--journal', self.state / 'journal.tsv', '--last')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(shot.is_file())

    def test_declared_rule_inherits_and_absolute_protection_takes_priority(self):
        source = self.put('Package/Demo/Export/image.jpg')
        neighbor = self.put('Package/Demo/Private/content.txt')
        self.policy('Package', ['Package/*/Export'])
        narrow = source.parent.parent
        protection = Journal(narrow).protection()
        self.assertEqual(subtree_size(narrow, protection=protection)[1], 1)
        with self.assertRaises(SafetyError):
            guard(neighbor, narrow, protection)
        self.policy('Package', ['Package/*/Export'], no_go_zones=['Package/Demo/Export'])
        with self.assertRaises(SafetyError):
            guard(source, narrow, Journal(narrow).protection())

    def test_wildcard_cannot_escape_or_reopen_steamapps(self):
        source = self.put('SteamLibrary/steamapps/common/Demo/Exports/image.jpg')
        self.policy('SteamLibrary/steamapps', ['SteamLibrary/steamapps/common/*/Exports'])
        with self.assertRaises(SafetyError):
            guard(source, self.root, Journal(self.root).protection())
        for rule in ('../Exports', 'Package/**/Exports', 'Other/*/Exports', 'Package'):
            with self.subTest(rule=rule):
                self.policy('Package', [rule])
                with self.assertRaises(SafetyError):
                    Journal(self.root).protection()
