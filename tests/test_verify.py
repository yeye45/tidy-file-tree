#!/usr/bin/env python3
"""验证机制与查询能力的测试。不依赖网络。"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
CORE = HERE.parent / "core"
sys.path.insert(0, str(CORE))

import decisions as D  # noqa: E402


class TestVerifyCases(unittest.TestCase):
    """verify.py 的用例执行逻辑。"""

    def setUp(self):
        import verify
        import rules  # Import before stdout capture; the CLI module configures encoding.
        self.V = verify

    def test_stem_case(self):
        case = {"kind": "stem", "name": "编程课程合集", "expect": "编程课程"}
        ok, msg = self.V.run_case(case, D)
        self.assertTrue(ok, msg)

    def test_shape_case(self):
        with tempfile.TemporaryDirectory() as d:
            for i in range(8):
                (Path(d) / f"x{i}.exe").write_text("x", encoding="utf-8")
            case = {"kind": "shape", "path": d, "expect": "installer"}
            ok, msg = self.V.run_case(case, D)
            self.assertTrue(ok, msg)

    def test_shape_case_mismatch(self):
        with tempfile.TemporaryDirectory() as d:
            for i in range(8):
                (Path(d) / f"x{i}.mp4").write_text("x", encoding="utf-8")
            case = {"kind": "shape", "path": d, "expect": "installer"}
            ok, msg = self.V.run_case(case, D)
            self.assertFalse(ok)

    def test_missing_path_skipped(self):
        case = {"kind": "shape", "path": "/nonexistent/xyz", "expect": "x"}
        ok, msg = self.V.run_case(case, D)
        self.assertIsNone(ok)

    def test_suggest_case_mode(self):
        case = {"kind": "suggest", "group": "随便", "parent": "父",
                "expectMode": "keep"}
        ok, msg = self.V.run_case(case, D)
        self.assertTrue(ok, msg)

    def test_suggest_target_assertion_catches_wrong_destination(self):
        case = {"kind":"suggest", "group":"ComfyUI 便携运行环境", "parent":"下载",
                "zones":[{"path":"/ai", "label":"AI工具", "depth":1, "files":1}],
                "expectMode":"move", "expectTarget":"/wrong"}
        self.assertFalse(self.V.run_case(case, D)[0])
        case["expectTarget"] = "/ai"
        self.assertTrue(self.V.run_case(case, D)[0])

    def test_suggest_without_assertions_fails(self):
        self.assertFalse(self.V.run_case({"kind":"suggest", "group":"随便"}, D)[0])

    def test_unknown_kind_skipped(self):
        case = {"kind": "nonsense"}
        ok, msg = self.V.run_case(case, D)
        self.assertIsNone(ok)

    def test_load_cases_from_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            p.write_text(json.dumps({"cases": [
                {"kind": "stem", "name": "a合集", "expect": "a"}]}),
                encoding="utf-8")
            cases = self.V.load_cases(p)
            self.assertEqual(len(cases), 1)
            self.assertIn("_source", cases[0])

    def test_load_cases_skips_bad_json(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "good.json").write_text(
                json.dumps({"cases": [{"kind": "stem", "name": "x"}]}),
                encoding="utf-8")
            (Path(d) / "bad.json").write_text("{bad", encoding="utf-8")
            cases = self.V.load_cases(d)
            self.assertEqual(len(cases), 1)

    def test_recorded_policy_mapping_is_applied_during_replay(self):
        original = [dict(rule) for rule in D.CROSS_RULES]
        try:
            D.CROSS_RULES[:] = [{'keys': ['VOCALOID', '声库', '音源', '歌声合成'], 'to': ['歌声合成']}]
            with tempfile.TemporaryDirectory() as d:
                path = Path(d) / 'cases.json'
                path.write_text(json.dumps({
                    'policyMapping': {'voice_synth': 'MyVault'},
                    'zones': [{'path': '/vault', 'label': 'MyVault', 'depth': 1, 'files': 2}],
                    'cases': [{'kind': 'suggest', 'group': 'VOCALOID 声库', 'parent': '下载',
                               'zones': [{'path': '/vault', 'label': 'MyVault', 'depth': 1, 'files': 2}],
                               'expectMode': 'move', 'expectTarget': '/vault'}],
                }), encoding='utf-8')
                import argparse
                args = argparse.Namespace(path=str(path), verbose=True)
                from contextlib import redirect_stdout
                from io import StringIO
                output = StringIO()
                with redirect_stdout(output):
                    self.assertEqual(self.V.cmd_run(args), 0)
                self.assertIn('target 期望 /vault / 实际 /vault', output.getvalue())
                self.assertEqual(D.CROSS_RULES, [{'keys': ['VOCALOID', '声库', '音源', '歌声合成'], 'to': ['歌声合成']}])
        finally:
            D.CROSS_RULES[:] = original

    def test_directory_replay_keeps_context_per_source_file(self):
        original = [dict(rule) for rule in D.CROSS_RULES]
        try:
            D.CROSS_RULES[:] = [{'keys': ['VOCALOID', '声库', '音源', '歌声合成'], 'to': ['歌声合成']}]
            with tempfile.TemporaryDirectory() as d:
                base = Path(d)
                for filename, target in (('a.json', 'Vault A'), ('b.json', 'Vault B')):
                    path = base / filename
                    path.write_text(json.dumps({
                        'policyMapping': {'voice_synth': target},
                        'cases': [{'kind': 'suggest', 'group': 'VOCALOID 声库', 'parent': '下载',
                                   'zones': [{'path': f'/{target}', 'label': target, 'depth': 1, 'files': 2}],
                                   'expectMode': 'move', 'expectTarget': f'/{target}'}],
                    }), encoding='utf-8')
                import argparse
                from contextlib import redirect_stdout
                from io import StringIO
                output = StringIO()
                with redirect_stdout(output):
                    self.assertEqual(self.V.cmd_run(argparse.Namespace(path=d, verbose=False)), 0)
                self.assertIn('通过 2 / 2', output.getvalue())
                self.assertEqual(D.CROSS_RULES, [{'keys': ['VOCALOID', '声库', '音源', '歌声合成'], 'to': ['歌声合成']}])
        finally:
            D.CROSS_RULES[:] = original

    def test_valid_decision_example_is_manual_review_not_automated_pass(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'semantic.json'
            path.write_text(json.dumps({'cases': [{
                'kind': 'decision', 'stage': 'S2', 'caseType': 'semantic',
                'evidence': ['installer files'], 'expectedCategory': 'software_installer',
            }]}), encoding='utf-8')
            import argparse
            from contextlib import redirect_stdout
            from io import StringIO
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(self.V.cmd_run(argparse.Namespace(path=str(path), verbose=False)), 0)
            self.assertIn('通过 0 / 0', output.getvalue())
            self.assertIn('待人工判断 1', output.getvalue())

    def test_old_case_file_without_policy_context_remains_supported(self):
        case = {'kind': 'suggest', 'group': 'unknown', 'parent': 'downloads', 'expectMode': 'keep'}
        self.assertTrue(self.V.run_case(case, D)[0])

    def test_record_and_rule_coverage_use_protected_s4_evaluation(self):
        with tempfile.TemporaryDirectory(prefix='.organizer-test-', dir=HERE.parent) as tmp:
            base = Path(tmp)
            root = base / 'root'
            probe = root / '_organize' / 'probe'
            probe.mkdir(parents=True)
            (probe.parent / 'policy.json').write_text(
                json.dumps({'no_go_zones': ['Excluded'], 'mapping': {'voice_synth': 'MyVault'}}),
                encoding='utf-8')
            for node, label in (('Incoming', 'x7F3 batch'), ('Excluded', 'Protected group')):
                source = root / node / 'Package'
                source.mkdir(parents=True)
                (source / 'fixture.exe').touch()
                record = {'path': str(source.parent), 'groups': [{'name': label, 'units': ['Package']}]}
                (probe / f'group_{node}.json').write_text(json.dumps(record), encoding='utf-8')

            command = [sys.executable, '-B', str(HERE.parent / 'tidy_file_tree.py'), 'verify']
            env = dict(os.environ, LOCALAPPDATA=str(base / 'local'), PYTHONIOENCODING='utf-8')
            coverage = subprocess.run([*command, 'coverage', '--root', str(root)],
                                      env=env, capture_output=True, encoding='utf-8')
            self.assertEqual(coverage.returncode, 0, coverage.stderr)
            self.assertRegex(coverage.stdout, r'总组数\s+1\b')
            self.assertRegex(coverage.stdout, r'未匹配\s+1\b')
            self.assertRegex(coverage.stdout, r'需人工/AI\s+1\b')
            self.assertNotIn('Protected group', coverage.stdout)

            cases_path = base / 'recorded.json'
            record = subprocess.run([*command, 'record', '--root', str(root), '--out', str(cases_path)],
                                    env=env, capture_output=True, encoding='utf-8')
            self.assertEqual(record.returncode, 0, record.stderr)
            text = cases_path.read_text(encoding='utf-8')
            self.assertNotIn('Excluded', text)
            recorded_payload = json.loads(text)
            self.assertEqual(recorded_payload['policyMapping'], {'voice_synth': 'MyVault'})
            self.assertIn('rulePack', recorded_payload)
            cases = self.V.load_cases(cases_path)
            self.assertEqual(len(cases), 1)
            self.assertEqual(cases[0]['shape']['kind'], 'installer')
            replay = subprocess.run([*command, 'run', str(cases_path)],
                                    env=env, capture_output=True, encoding='utf-8')
            self.assertEqual(replay.returncode, 0, replay.stdout + replay.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
