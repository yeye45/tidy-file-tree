import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys
PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "core"))
import coverage as coverage_module


class TestEnterCoverage(unittest.TestCase):
    def setUp(self):
        local_state = tempfile.TemporaryDirectory()
        self.addCleanup(local_state.cleanup)
        environment = patch.dict(os.environ, {"LOCALAPPDATA": local_state.name})
        environment.start()
        self.addCleanup(environment.stop)

    def test_policy_no_go_zones_skip_nodes_like_no_touch_markers(self):
        with tempfile.TemporaryDirectory(prefix=".organizer-test-", dir=PROJECT) as tmp:
            root = Path(tmp) / "workspace"
            protected_root_node = root / "private"
            branch = root / "branch"
            protected_child = branch / "locked"
            sibling = branch / "safe"
            probe = Path(tmp) / "probe"
            for directory in (root, protected_root_node, branch, protected_child, sibling, probe):
                directory.mkdir(parents=True, exist_ok=True)

            organize = root / "_organize"
            organize.mkdir()
            (organize / "policy.json").write_text(json.dumps({
                "no_go_zones": ["private", "branch/locked"],
            }), encoding="utf-8")

            # A direct reference must not reintroduce a protected nested node.
            (probe / "probe_locked.json").write_text(json.dumps({
                "path": str(protected_child), "action": "keep",
            }), encoding="utf-8")
            (probe / "probe_branch.json").write_text(json.dumps({
                "path": str(branch), "action": "enter", "enter_children": ["locked", "safe"],
            }), encoding="utf-8")

            rows = coverage_module.check(root, probe)
            reported_nodes = {row.get("node", "").replace("\\", "/") for row in rows}
            self.assertNotIn("private", reported_nodes)
            self.assertNotIn("branch/locked", reported_nodes)
            sibling_row = next(row for row in rows
                               if row.get("node", "").replace("\\", "/") == "branch/safe")
            self.assertEqual(sibling_row["status"], "MISSING")
            self.assertIn("S1探针", sibling_row["missing"])
            self.assertEqual(coverage_module.check(protected_child, probe), [])

    def test_enter_child_requires_probe(self):
        with tempfile.TemporaryDirectory(prefix=".organizer-test-", dir=PROJECT) as tmp:
            root = Path(tmp) / "root"
            parent = root / "parent"
            child = parent / "nested"
            probe = Path(tmp) / "probe"
            root.mkdir()
            parent.mkdir()
            child.mkdir()
            probe.mkdir()
            (probe / "probe_parent.json").write_text(json.dumps({
                "path": str(parent), "action": "enter",
                "enter_children": [{"path": str(child)}],
            }), encoding="utf-8")
            rows = coverage_module.check(root, probe)
            parent_row = next(row for row in rows if row.get("node") == "parent")
            self.assertEqual(parent_row["status"], "ok")
            child_row = next(row for row in rows if row.get("node", "").endswith("nested"))
            self.assertEqual(child_row["status"], "MISSING")
            self.assertIn("S1探针", child_row["missing"])
            (probe / "units_parent.json").write_text(json.dumps({
                "path": str(parent), "total_units": 0,
            }), encoding="utf-8")
            parent_row = coverage_module.check(root, probe)[0]
            self.assertEqual(parent_row["missing"], ["S3归组"])

    def test_sub_container_inventory_child_remains_compatible(self):
        with tempfile.TemporaryDirectory(prefix=".organizer-test-", dir=PROJECT) as tmp:
            root = Path(tmp) / "root"
            parent = root / "parent"
            child = parent / "nested"
            probe = Path(tmp) / "probe"
            root.mkdir()
            parent.mkdir()
            child.mkdir()
            probe.mkdir()
            (probe / "units_parent.json").write_text(json.dumps({
                "path": str(parent), "total_units": 0,
                "sub_containers": [{"path": str(child)}],
            }), encoding="utf-8")
            rows = coverage_module.check(root, probe)
            child_row = next(row for row in rows if row.get("node", "").endswith("nested"))
            self.assertEqual(child_row["status"], "ok")

    def test_uncertain_is_valid_action_and_bad_action_lists_valid_values(self):
        with tempfile.TemporaryDirectory(prefix=".organizer-test-", dir=PROJECT) as tmp:
            root = Path(tmp) / "root"
            good_dir = root / "uncertain_dir"
            bad_dir = root / "bad_dir"
            probe = Path(tmp) / "probe"
            for d in (root, good_dir, bad_dir, probe):
                d.mkdir(parents=True, exist_ok=True)
            (probe / "probe_good.json").write_text(json.dumps({
                "path": str(good_dir), "action": "uncertain",
            }), encoding="utf-8")
            (probe / "probe_bad.json").write_text(json.dumps({
                "path": str(bad_dir), "action": ["bogus_op"],
            }), encoding="utf-8")
            rows = coverage_module.check(root, probe)
            good_row = next(r for r in rows if r.get("node") == "uncertain_dir")
            self.assertEqual(good_row["status"], "ok")
            err_row = next(r for r in rows if r.get("status") == "ERROR")
            self.assertIn("uncertain", err_row["missing"][0])
            self.assertIn("bogus_op", err_row["missing"][0])

    def test_s3_memberships_include_solo_and_uncertain_exactly_once(self):
        with tempfile.TemporaryDirectory(prefix=".organizer-test-", dir=PROJECT) as tmp:
            root = Path(tmp)
            node = root / "inbox"
            probe = root / "_organize" / "probe"
            for directory in (node, probe):
                directory.mkdir(parents=True)
            (probe / "probe_node.json").write_text(json.dumps({
                "path": str(node), "action": "split",
            }), encoding="utf-8")
            (probe / "units_node.json").write_text(json.dumps({
                "path": str(node), "total_units": 3,
                "confirmed_units": [{"path": str(node / "package")}],
                "loose_files_as_units": ["standalone.txt", "ambiguous.bin"],
            }), encoding="utf-8")
            group_file = probe / "group_node.json"
            for description, grouping, status, findings in (
                ("missing", {"groups": [{"name": "Known", "units": ["package"]}]},
                 "MISSING", ["未归入 S3"]),
                ("duplicate and extra", {"groups": [{"name": "Known", "units": ["package", "extra.txt"]}],
                  "solo_units": ["package", "standalone.txt"], "uncertain": ["ambiguous.bin"]},
                 "MISSING", ["重复归入", "未在 S2 登记"]),
                ("verified", {"groups": [{"name": "Known", "units": ["./package"]}],
                  "solo_units": [str(node / "standalone.txt")], "uncertain": [{"unit": "ambiguous.bin"}]},
                 "ok", []),
                ("legacy", {"groups": [{"name": "Known", "units": 3}]}, "unverified", []),
            ):
                with self.subTest(description=description):
                    group_file.write_text(json.dumps({"path": str(node), **grouping}), encoding="utf-8")
                    row, = coverage_module.check(root, probe)
                    self.assertEqual(row["status"], status)
                    for detail in findings:
                        self.assertTrue(any(detail in reason for reason in row["missing"]), row)


if __name__ == "__main__":
    unittest.main()
