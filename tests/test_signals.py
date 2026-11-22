#!/usr/bin/env python3
"""signals.py 的单元测试。用合成布局，不依赖真实盘。"""
import os
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "core"))

import signals  # noqa: E402


def mk(base, name, content=b"x"):
    p = os.path.join(base, name)
    os.makedirs(os.path.dirname(p), exist_ok=True) if os.path.dirname(p) else None
    with open(p, "wb") as f:
        f.write(content)
    return p


@contextmanager
def isolated_analysis_env(base):
    # Avoid inheriting this machine's real protection markers during synthetic scans.
    local = os.path.join(base, ".local")
    os.makedirs(local, exist_ok=True)
    with patch.dict(os.environ, {"LOCALAPPDATA": local}):
        yield


def run_analyze(path, **kwargs):
    with isolated_analysis_env(path + ".local"):
        return signals.analyze(path, **kwargs)


class TestUnity(unittest.TestCase):
    def test_unity_layout(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as d:
            mk(d, "game.exe", b"MZ")
            os.makedirs(os.path.join(d, "game_Data"))
            r = run_analyze(d)
            self.assertTrue(r["signals"]["unity-layout"])
            self.assertTrue(len(r["signals"]["unity-evidence"]) > 0)

    def test_exe_without_data(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as d:
            mk(d, "setup.exe", b"MZ")
            r = run_analyze(d)
            self.assertFalse(r["signals"]["unity-layout"])


class TestVM(unittest.TestCase):
    def test_vm_disks(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as d:
            mk(d, "disk-s001.vmdk", b"KDMV")
            mk(d, "disk-s002.vmdk", b"KDMV")
            mk(d, "vm.vmx", b"config")
            mk(d, "vm.vmsd", b"snapshot")
            r = run_analyze(d)
            self.assertTrue(r["signals"]["vm-layout"])
            self.assertEqual(r["signals"]["vm-disk-count"], 2)


class TestSequence(unittest.TestCase):
    def test_continuous(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as d:
            for i in range(1, 13):
                mk(d, f"[Group] Show - {i:02d} [1080p].mkv")
            r = run_analyze(d)
            self.assertTrue(r["signals"]["sequence-detected"])
            seq = r["signals"]["sequence"]
            self.assertEqual(seq["count"], 12)
            self.assertEqual(seq["missing"], [])
            self.assertGreaterEqual(seq["coverage"], 0.7)
            self.assertEqual(seq["range"], '1-12')
            self.assertLessEqual(seq["coverage"], 1)

    def test_gap(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as d:
            for i in [1, 2, 3, 7, 8, 9, 10]:
                mk(d, f"ep{i:02d}.mp4")
            r = run_analyze(d)
            self.assertTrue(r["signals"]["sequence-detected"])
            self.assertIn(4, r["signals"]["sequence"]["missing"])

    def test_too_few(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as d:
            mk(d, "ep01.mp4")
            mk(d, "ep02.mp4")
            r = run_analyze(d)
            self.assertFalse(r["signals"]["sequence-detected"])


class TestVolumeParts(unittest.TestCase):
    def test_complete(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as d:
            for i in range(1, 4):
                mk(d, f"data.part{i}.rar", b"Rar!")
            r = run_analyze(d)
            self.assertTrue(r["signals"]["volume-parts"])
            self.assertEqual(r["signals"]["volume-groups"][0]["missing"], [])

    def test_missing_part(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as d:
            mk(d, "data.part1.rar")
            mk(d, "data.part3.rar")
            r = run_analyze(d)
            self.assertTrue(r["signals"]["volume-parts"])
            self.assertIn(2, r["signals"]["volume-groups"][0]["missing"])


class TestShell(unittest.TestCase):
    def test_nested_same_name(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as d:
            inner = os.path.join(d, "Game", "Game")
            os.makedirs(inner)
            mk(inner, "x.exe")
            r = run_analyze(os.path.join(d, "Game"))
            self.assertTrue(r["signals"]["is-shell"])


class TestBatchArrival(unittest.TestCase):
    def test_same_second(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as d:
            for i in range(5):
                mk(d, f"f{i}.txt")
            # 同秒创建在真实文件系统上通常成立
            r = run_analyze(d)
            self.assertIn("batch-ratio", r["signals"])

    def test_few_items(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as d:
            mk(d, "a.txt")
            mk(d, "b.txt")
            r = run_analyze(d)
            self.assertFalse(r["signals"]["batch-arrival"])


class TestHidden(unittest.TestCase):
    def test_hidden_visible_to_scandir(self):
        """Hidden 属性不该让项消失。"""
        with tempfile.TemporaryDirectory(dir=ROOT) as d:
            p = os.path.join(d, "hidden_dir")
            os.makedirs(p)
            try:
                os.system(f'attrib +h "{p}"')
            except Exception:
                pass
            r = run_analyze(d)
            self.assertEqual(r["signals"]["dir-count"], 1)


class TestComposition(unittest.TestCase):
    def test_single_file(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as d:
            mk(d, "a.txt")
            r = run_analyze(d)
            self.assertEqual(r["signals"]["file-count"], 1)

    def test_mixed(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as d:
            mk(d, "a.txt")
            mk(d, "b.jpg")
            mk(d, "c.mp4")
            mk(d, "d.zip")
            r = run_analyze(d)
            self.assertGreaterEqual(r["signals"]["kind-count"], 4)


class TestEmpty(unittest.TestCase):
    def test_empty(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as d:
            r = run_analyze(d)
            self.assertTrue(r["empty"])
            self.assertEqual(r["status"], "empty")

    def test_protected_only_is_not_reported_as_physically_empty(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as d:
            protected = os.path.join(d, "private")
            mk(protected, "hidden.txt", b"secret")
            os.makedirs(os.path.join(d, "_organize"), exist_ok=True)
            with open(os.path.join(d, "_organize", "markers.tsv"), "w", encoding="utf-8") as f:
                f.write("path\tkind\tby\tnote\tts\n")
                f.write(f"{protected}\tNO_TOUCH\ttest\tfixture\t2026-11-22\n")
            r = run_analyze(d)
            self.assertIsNone(r["empty"])
            self.assertEqual(r["status"], "protected-only")
            self.assertTrue(r["complete"])
            self.assertNotIn("private", json.dumps(r, ensure_ascii=False))


class TestScanSafetyAndSizing(unittest.TestCase):
    def test_default_is_shallow_and_does_not_size(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as d:
            mk(d, "top.txt", b"x")
            mk(os.path.join(d, "child"), "deep.txt", b"xx")
            with patch.object(signals, "subtree_size", side_effect=AssertionError("unexpected recursion")):
                r = run_analyze(d)
            self.assertNotIn("size-gb", r)
            self.assertNotIn("file-count-total", r)
            self.assertEqual(r["signals"]["file-count"], 1)

    def test_core_script_cli_keeps_size_opt_in(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as d:
            mk(d, "file.txt", b"abc")
            env = dict(os.environ, LOCALAPPDATA=d + ".local", PYTHONIOENCODING="utf-8")
            script = os.path.join(ROOT, "core", "signals.py")
            plain = subprocess.run([sys.executable, script, d, "--json"], capture_output=True,
                                   text=True, encoding="utf-8", env=env, check=True)
            sized = subprocess.run([sys.executable, script, d, "--json", "--size"], capture_output=True,
                                   text=True, encoding="utf-8", env=env, check=True)
            self.assertNotIn("size-gb", json.loads(plain.stdout)[0])
            self.assertIn("size-gb", json.loads(sized.stdout)[0])

    def test_unified_cli_accepts_opt_in_size(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as d:
            mk(d, "file.txt", b"abc")
            env = dict(os.environ, LOCALAPPDATA=d + ".local", PYTHONIOENCODING="utf-8")
            cli = os.path.join(ROOT, "tidy_file_tree.py")
            result = subprocess.run([sys.executable, cli, "scan", d, "--json", "--size"],
                                    capture_output=True, text=True, encoding="utf-8", env=env, check=True)
            self.assertIn("size-gb", json.loads(result.stdout)[0])

    def test_size_excludes_marked_file_and_never_opens_protected_directory(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as d:
            mk(d, "visible.txt", b"x")
            protected_dir = os.path.join(d, "private")
            mk(protected_dir, "hidden.txt", b"secret")
            mk(d, "secret.txt", b"secret")
            os.makedirs(os.path.join(d, "_organize"), exist_ok=True)
            with open(os.path.join(d, "_organize", "markers.tsv"), "w", encoding="utf-8") as f:
                f.write("path\tkind\tby\tnote\tts\n")
                f.write(f"{protected_dir}\tNO_TOUCH\ttest\tfixture\t2026-11-22\n")
                f.write(f"{os.path.join(d, 'secret.txt')}\tSECRET\ttest\tfixture\t2026-11-22\n")
            original = signals.os.scandir
            def guarded_scandir(path):
                if os.path.abspath(str(path)) == protected_dir:
                    raise AssertionError("opened protected directory")
                return original(path)
            with patch.object(signals.os, "scandir", side_effect=guarded_scandir), isolated_analysis_env(d + ".local"):
                r = signals.analyze(d, totals=True)
            self.assertTrue(r["complete"])
            self.assertEqual(r["file-count-total"], 1)
            self.assertEqual(r["signals"]["file-count"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
