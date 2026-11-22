#!/usr/bin/env python3
"""内容形态检测与新类别发现的测试。"""
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
from safety import MARKER_FIELDS, encode_tsv  # noqa: E402


def make_dir(root, exts, count=1):
    """在 root 下造 count 个带指定扩展名的文件。"""
    for i, e in enumerate(exts):
        p = Path(root) / f"f{i}{e}"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")


class TestProtectedDecisions(unittest.TestCase):
    def test_cli_filters_old_probes_and_prunes_protected_samples(self):
        with tempfile.TemporaryDirectory(prefix=".organizer-test-", dir=HERE.parent) as tmp:
            base = Path(tmp)
            root = base / "root"
            state = root / "_organize"
            probe = state / "probe"
            node = root / "Downloads"
            bundle = node / "Bundle"
            safe = bundle / "Shareable"
            private = bundle / "ProtectedNested"
            excluded = root / "ProtectedTop"
            for folder in (probe, safe, private, excluded):
                folder.mkdir(parents=True)
            for path in (safe / "public.exe", bundle / "protected-file.txt",
                         private / "protected-nested.exe", excluded / "protected-top.exe"):
                path.touch()

            def write_record(file, data):
                file.write_text(json.dumps(data), encoding="utf-8")

            for parent, suffix in ((node, "main"), (excluded, "excluded")):
                write_record(probe / f"probe_{suffix}.json", {"path": str(parent), "action": "split"})
            write_record(probe / "group_excluded.json", {
                "path": str(excluded), "groups": [{"name": "Private record", "units": [str(excluded)]}],
            })
            write_record(probe / "group_main.json", {"path": str(node), "groups": [
                {"name": "Bundle", "units": [str(safe)]},
                {"name": "Mixed private group", "units": [str(safe), "Bundle/ProtectedNested"]},
                {"name": "Bundle/ProtectedNested", "units": 1},
            ]})

            command = [sys.executable, "-B", str(HERE.parent / "tidy_file_tree.py"), "decisions", str(root)]
            environment = dict(os.environ, LOCALAPPDATA=str(base / "local"), PYTHONIOENCODING="utf-8")
            protected = [excluded, private, bundle / "protected-file.txt"]
            for source in ("policy", "marker"):
                with self.subTest(source=source):
                    policy = {"no_go_zones": [p.relative_to(root).as_posix() for p in protected]} if source == "policy" else {}
                    write_record(state / "policy.json", policy)
                    if source == "marker":
                        rows = [dict(path=str(p), kind="NO_TOUCH", by="inferred", note="-", ts="-")
                                for p in protected]
                        (state / "markers.tsv").write_bytes(encode_tsv(rows, MARKER_FIELDS))
                    questions = subprocess.run(command + ["--need-ai"], env=environment,
                                               capture_output=True, encoding="utf-8")
                    self.assertEqual(questions.returncode, 1, questions.stdout + questions.stderr)
                    generated = json.loads(questions.stdout)["questions"]
                    self.assertEqual([q["group"] for q in generated], ["Bundle"])
                    self.assertEqual(generated[0]["sampleFiles"], ["public.exe"])
                    snapshot = subprocess.run(command, env=environment, capture_output=True, encoding="utf-8")
                    self.assertEqual(snapshot.returncode, 0, snapshot.stdout + snapshot.stderr)
                    payload = (state / "decisions.json").read_text(encoding="utf-8")
                    for hidden in ("ProtectedTop", "ProtectedNested", "Mixed private group", "protected-file.txt"):
                        self.assertNotIn(hidden, payload)


class TestAnswerObservations(unittest.TestCase):
    def test_cli_accepts_current_answer_and_rejects_old_or_incomplete_identity(self):
        with tempfile.TemporaryDirectory(prefix=".organizer-test-", dir=HERE.parent) as tmp:
            base = Path(tmp)
            root = base / "root"
            state = root / "_organize"
            probe = state / "probe"
            node = root / "Incoming"
            payload = node / "PackageFixture"
            payload.mkdir(parents=True)
            probe.mkdir(parents=True)
            (probe / "group_incoming.json").write_text(json.dumps({
                "path": str(node), "groups": [{"name": "x7F3 batch", "units": [str(payload)]}],
            }), encoding="utf-8")
            source = payload / "original.exe"
            source.write_text("installer fixture", encoding="utf-8")
            command = [sys.executable, "-B", str(HERE.parent / "tidy_file_tree.py"), "decisions", str(root)]
            env = dict(os.environ, LOCALAPPDATA=str(base / "local"), PYTHONIOENCODING="utf-8")

            def run(*args):
                return subprocess.run(command + list(args), env=env, capture_output=True, encoding="utf-8")

            question = json.loads(run("--need-ai").stdout)["questions"][0]
            self.assertIn("observationId", question)
            self.assertEqual(json.loads(run("--need-ai").stdout)["questions"][0]["observationId"],
                             question["observationId"])
            answer = {field: question[field] for field in ("nodeId", "groupId", "observationId")}
            answer.update(category="software_installer", label="Installers", hypothesis="Installer files",
                          verification={"method": "Checked sampled extension", "findings": "original.exe"},
                          confirmation={"holds": True, "confidence": "high"})
            answers = base / "answers.json"

            def apply(response):
                answers.write_text(json.dumps({"answers": [response]}), encoding="utf-8")
                result = run("--answers", str(answers))
                snapshot = json.loads((state / "decisions.json").read_text(encoding="utf-8"))
                return result, snapshot["nodes"][0]["groups"][0]["suggest"], snapshot["aiQuestions"][0]

            result, suggestion, updated_question = apply(answer)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(suggestion["aiJudgement"]["label"], "Installers")
            self.assertEqual(updated_question["status"], "confirmed")
            snapshot = json.loads((state / "decisions.json").read_text(encoding="utf-8"))
            saved_group = snapshot["nodes"][0]["groups"][0]
            self.assertTrue(D.observation_is_current(saved_group, root, permit=lambda _: True))
            self.assertFalse(D.observation_is_current(saved_group, root, permit=lambda _: False))
            incomplete = {key: value for key, value in answer.items() if key != "observationId"}
            result, suggestion, updated_question = apply(incomplete)
            self.assertNotEqual(result.returncode, 0)
            self.assertIsNone(suggestion["aiJudgement"])
            self.assertEqual(updated_question["status"], "pending")

            legacy = {key: value for key, value in answer.items() if key not in D.ANSWER_ID_FIELDS}
            legacy.update(parent=question["parent"], group=question["group"])
            result, suggestion, updated_question = apply(legacy)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(suggestion["aiJudgement"]["label"], "Installers")
            self.assertEqual(suggestion["mode"], "keep")
            self.assertEqual(updated_question["status"], "needs_review")

            # A replacement can keep its name and extension while invalidating sampled evidence.
            source.write_text("different fixture with the same filename", encoding="utf-8")
            self.assertFalse(D.observation_is_current(saved_group, root, permit=lambda _: True))
            result, suggestion, updated_question = apply(answer)
            self.assertNotEqual(result.returncode, 0)
            self.assertIsNone(suggestion["aiJudgement"])
            self.assertEqual(updated_question["sampleFiles"], question["sampleFiles"])
            self.assertNotEqual(updated_question["observationId"], question["observationId"])

            source.rename(payload / "replacement.mp4")
            result, suggestion, updated_question = apply(answer)
            self.assertNotEqual(result.returncode, 0)
            self.assertIsNone(suggestion["aiJudgement"])
            self.assertEqual(updated_question["groupId"], question["groupId"])
            self.assertNotEqual(updated_question["observationId"], question["observationId"])
            self.assertEqual(updated_question["status"], "pending")

            previous_snapshot = (state / "decisions.json").read_bytes()
            answers.write_text(json.dumps(answer), encoding="utf-8")
            result = run("--answers", str(answers))
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((state / "decisions.json").read_bytes(), previous_snapshot)


class TestContentShape(unittest.TestCase):
    def test_installer(self):
        with tempfile.TemporaryDirectory() as d:
            make_dir(d, [".exe"] * 8 + [".txt"])
            r = D.content_shape(d)
            self.assertEqual(r["kind"], "installer")
            self.assertGreaterEqual(r["shares"]["installer"], 0.5)

    def test_media(self):
        with tempfile.TemporaryDirectory() as d:
            make_dir(d, [".mp4"] * 8 + [".jpg"])
            r = D.content_shape(d)
            self.assertEqual(r["kind"], "media")

    def test_audio_counts_as_media(self):
        with tempfile.TemporaryDirectory() as d:
            make_dir(d, [".mp3"] * 7 + [".flac"] * 3)
            r = D.content_shape(d)
            self.assertEqual(r["kind"], "media")

    def test_image(self):
        with tempfile.TemporaryDirectory() as d:
            make_dir(d, [".jpg"] * 10)
            r = D.content_shape(d)
            self.assertEqual(r["kind"], "image")

    def test_code(self):
        with tempfile.TemporaryDirectory() as d:
            make_dir(d, [".py"] * 6 + [".md"])
            r = D.content_shape(d)
            self.assertEqual(r["kind"], "code")

    def test_document(self):
        with tempfile.TemporaryDirectory() as d:
            make_dir(d, [".pdf"] * 6 + [".docx"] * 4)
            r = D.content_shape(d)
            self.assertEqual(r["kind"], "document")

    def test_archive(self):
        with tempfile.TemporaryDirectory() as d:
            make_dir(d, [".zip"] * 6 + [".rar"] * 4)
            r = D.content_shape(d)
            self.assertEqual(r["kind"], "archive")

    def test_mixed(self):
        with tempfile.TemporaryDirectory() as d:
            make_dir(d, [".exe", ".jpg", ".txt", ".mp4", ".py"])
            r = D.content_shape(d)
            self.assertEqual(r["kind"], "mixed")

    def test_empty(self):
        with tempfile.TemporaryDirectory() as d:
            r = D.content_shape(d)
            self.assertEqual(r["kind"], "empty")
            self.assertEqual(r["total"], 0)

    def test_truncated_empty_tree_is_not_reported_as_genuinely_empty(self):
        with tempfile.TemporaryDirectory() as d:
            for index in range(D.SAMPLE_DIR_LIMIT + 1):
                (Path(d) / f"empty-{index:03}").mkdir()
            r = D.content_shape(d, max_files=1)
            self.assertEqual(r["total"], 0)
            self.assertEqual(r["kind"], "unknown")
            self.assertTrue(r["truncated"])

    def test_missing_dir(self):
        r = D.content_shape("/nonexistent/path/xyz")
        # 不抛异常，返回 total=0 或 None
        self.assertTrue(r is None or r["total"] == 0)


class TestBoundedSamples(unittest.TestCase):
    def test_representative_samples_cover_numeric_sequence(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d) / "series"
            folder.mkdir()
            for index in range(1, 31):
                (folder / f"episode{index:03}.mkv").touch()
            evidence = D.observe_sample_files(str(folder), d, limit=8)
            names = [Path(item[0]).name for item in evidence]
            self.assertEqual(len(names), 3)
            self.assertIn("episode001.mkv", names)
            self.assertIn("episode016.mkv", names)
            self.assertIn("episode030.mkv", names)

    def test_case_insensitive(self):
        with tempfile.TemporaryDirectory() as d:
            make_dir(d, [".EXE"] * 6 + [".Exe"] * 4)
            r = D.content_shape(d)
            self.assertEqual(r["kind"], "installer")

    def test_nested_dirs_counted(self):
        with tempfile.TemporaryDirectory() as d:
            sub = Path(d) / "sub" / "deep"
            sub.mkdir(parents=True)
            for i in range(8):
                (sub / f"x{i}.exe").write_text("x", encoding="utf-8")
            r = D.content_shape(d)
            self.assertEqual(r["kind"], "installer")
            self.assertEqual(r["total"], 8)


class TestNameStem(unittest.TestCase):
    def test_strips_brackets(self):
        self.assertNotIn("（", D.name_stem("某物（说明）"))

    def test_strips_generic_suffix(self):
        self.assertEqual(D.name_stem("编程课程合集"), "编程课程")
        self.assertEqual(D.name_stem("声库打包"), "声库")

    def test_keeps_space_words(self):
        # "4K 壁纸" 不该被空格切断
        self.assertEqual(D.name_stem("4K 壁纸打包"), "4K 壁纸")

    def test_splits_on_conjunction(self):
        stem = D.name_stem("VOCALOID 编辑器与声库")
        self.assertTrue(stem.startswith("VOCALOID"))

    def test_strips_trailing_number(self):
        self.assertEqual(D.name_stem("某物2023"), "某物")

    def test_empty_input(self):
        self.assertEqual(D.name_stem(""), "")

    def test_only_punctuation(self):
        self.assertEqual(D.name_stem("---"), "")


class TestPropose(unittest.TestCase):
    def test_same_stem_proposes(self):
        u = [("VOCALOID 编辑器与声库", "installer"),
             ("VOCALOID 声库合集", "installer")]
        r = D.propose_new_categories(u)
        self.assertTrue(any("VOCALOID" in k for k in r), r)

    def test_same_shape_fallback(self):
        u = [("声库A合集", "installer"),
             ("声库B合集", "installer"),
             ("声库C合集", "installer")]
        r = D.propose_new_categories(u)
        self.assertTrue(r, "形态一致应能聚类")

    def test_single_group_not_proposed(self):
        u = [("孤零零的一个", "installer")]
        r = D.propose_new_categories(u)
        self.assertEqual(r, {}, "孤件不建类")

    def test_two_groups_can_propose(self):
        u = [("声库A", "installer"), ("声库B", "installer")]
        r = D.propose_new_categories(u)
        self.assertTrue(r)

    def test_custom_threshold(self):
        u = [("声库A", "installer"), ("声库B", "installer")]
        r = D.propose_new_categories(u, min_groups=3)
        self.assertEqual(r, {})

    def test_no_kind_still_works_by_stem(self):
        u = [("VOCALOID 编辑器", None), ("VOCALOID 声库", None)]
        r = D.propose_new_categories(u)
        self.assertTrue(any("VOCALOID" in k for k in r))

    def test_members_recorded(self):
        u = [("声库A合集", "installer"), ("声库B合集", "installer")]
        r = D.propose_new_categories(u)
        for info in r.values():
            self.assertTrue(info["members"])
            self.assertIn("count", info)
            self.assertIn("basis", info)


class TestCommonPrefix(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(D._common_prefix(["声库A", "声库B"]), "声库")

    def test_no_common(self):
        self.assertEqual(D._common_prefix(["abc", "xyz"]), "")

    def test_single(self):
        self.assertEqual(D._common_prefix(["abc"]), "abc")

    def test_strips_punctuation(self):
        self.assertEqual(D._common_prefix(["A-B", "A_C"]), "A")

    def test_empty(self):
        self.assertEqual(D._common_prefix([]), "")


class TestSuggestConflicts(unittest.TestCase):
    """内容形态与名字冲突时必须标 needsCheck。"""

    @classmethod
    def setUpClass(cls):
        """加载默认规则包——suggest_group 依赖 CROSS_RULES，
        测试环境默认是空的，不加载就测不到名字匹配路径。"""
        import json
        from pathlib import Path as _P
        pack = _P(__file__).resolve().parent.parent / "rules" / "default.json"
        if pack.is_file():
            import rules
            data = json.loads(pack.read_text(encoding="utf-8-sig"))
            rules.apply_to(data, decisions_mod=D)

    def test_installer_to_synth_tool_ok(self):
        """安装包归到工具类目标（歌声合成）——不冲突，是对的。"""
        with tempfile.TemporaryDirectory() as d:
            make_dir(d, [".exe"] * 8)
            zones = [{"path": "/x/歌声合成", "label": "歌声合成",
                      "depth": 1, "files": 10}]
            r = D.suggest_group("音源合集", "下载目录", zones,
                                node_path="/x", group_path=d)
            self.assertFalse(r["needsCheck"], r)
            self.assertEqual(r["mode"], "move")

    def test_installer_to_music_product_conflicts(self):
        """安装包归到「音乐成品」——冲突，必须标待确认。"""
        with tempfile.TemporaryDirectory() as d:
            make_dir(d, [".exe"] * 8)
            zones = [{"path": "/x/音乐", "label": "音乐",
                      "depth": 1, "files": 10}]
            r = D.suggest_group("歌曲合集", "下载目录", zones,
                                node_path="/x", group_path=d)
            self.assertTrue(r["needsCheck"], r)
            self.assertTrue(any("冲突" in e or "成品" in e
                                for e in r["evidence"]), r)

    def test_media_named_tool(self):
        """名字命中"软件/代码"，但内容是成品媒体 → 必须标待确认。"""
        with tempfile.TemporaryDirectory() as d:
            make_dir(d, [".mp4"] * 8)
            zones = [{"path": "/x/软件安装包", "label": "软件安装包",
                      "depth": 1, "files": 10}]
            r = D.suggest_group("软件工具代码", "下载目录", zones,
                                node_path="/x", group_path=d)
            self.assertTrue(r["needsCheck"], r)

    def test_evidence_mentions_shape(self):
        with tempfile.TemporaryDirectory() as d:
            make_dir(d, [".exe"] * 8)
            r = D.suggest_group("随便什么", "下载目录", [], group_path=d)
            self.assertTrue(r.get("evidence"))
            self.assertTrue(any("installer" in e for e in r["evidence"]), r)




class TestIntegration(unittest.TestCase):
    """端到端：造目录 → 算建议 → 发现候选类别。"""

    @classmethod
    def setUpClass(cls):
        import json
        from pathlib import Path as _P
        pack = _P(__file__).resolve().parent.parent / "rules" / "default.json"
        if pack.is_file():
            import rules
            data = json.loads(pack.read_text(encoding="utf-8-sig"))
            rules.apply_to(data, decisions_mod=D)

    def test_three_installer_dirs_become_candidate(self):
        with tempfile.TemporaryDirectory() as base:
            names = ["工具A合集", "工具B合集", "工具C合集"]
            for n in names:
                p = os.path.join(base, n)
                os.makedirs(p)
                for i in range(6):
                    open(os.path.join(p, f"x{i}.exe"), "w").close()

            unmatched = []
            for n in names:
                p = os.path.join(base, n)
                r = D.suggest_group(n, "下载目录", [], node_path="/x",
                                    group_path=p)
                self.assertEqual(r["mode"], "keep")
                self.assertTrue(r.get("evidence"))
                unmatched.append((n, "installer"))

            props = D.propose_new_categories(unmatched)
            self.assertTrue(props, "三个同形态目录应产生候选")
            # 至少一个候选项包含全部 3 组
            counts = [v["count"] for v in props.values()]
            self.assertIn(3, counts)

    def test_single_installer_dir_no_candidate(self):
        with tempfile.TemporaryDirectory() as base:
            p = os.path.join(base, "孤件")
            os.makedirs(p)
            for i in range(6):
                open(os.path.join(p, f"x{i}.exe"), "w").close()
            r = D.suggest_group("孤件", "下载目录", [], node_path="/x",
                                group_path=p)
            props = D.propose_new_categories([("孤件", "installer")])
            self.assertEqual(props, {}, "单个孤件不该产生候选")




class TestNeedsAI(unittest.TestCase):
    """什么情况该交给 AI。"""

    def test_keep_with_shape_needs_ai(self):
        sug = {"mode": "keep", "reason": "无匹配",
               "evidence": ["内容以 installer 为主（.exe 占 89%）"]}
        self.assertEqual(D.needs_ai(sug, {"kind": "installer"}),
                         "no_rule_match")

    def test_keep_without_shape_no_ai(self):
        sug = {"mode": "keep", "reason": "无特征", "evidence": []}
        self.assertIsNone(D.needs_ai(sug, None))

    def test_conflict_needs_ai(self):
        sug = {"mode": "move", "needsCheck": True,
               "evidence": ["内容以安装包为主，但目标类别是成品类"]}
        self.assertEqual(D.needs_ai(sug, {"kind": "installer"}),
                         "shape_conflict")

    def test_new_with_check_needs_naming(self):
        sug = {"mode": "new", "newName": "某类", "needsCheck": True,
               "evidence": []}
        self.assertEqual(D.needs_ai(sug, None), "need_naming")

    def test_clean_move_no_ai(self):
        sug = {"mode": "move", "target": "/x/影视", "needsCheck": False,
               "evidence": []}
        self.assertIsNone(D.needs_ai(sug, {"kind": "media"}))

    def test_specialized_placement_waits_for_user_even_if_ai_offers_a_target(self):
        group = "ComfyUI 便携运行环境"
        zones = [{"path": "/ai", "label": "AI工具", "depth": 1, "files": 3}]
        suggestion = D.suggest_group(group, "下载", zones)
        self.assertEqual(suggestion["mode"], "keep")
        self.assertTrue(suggestion["needsCheck"])
        self.assertIsNone(D.needs_ai(suggestion, {"kind": "installer"}))
        with tempfile.TemporaryDirectory() as folder:
            reply = Path(folder) / "reply.json"
            reply.write_text(json.dumps([{
                "parent": "下载", "group": group, "target": "/ai",
            }]), encoding="utf-8")
            nodes = [{"name": "下载", "groups": [{"name": group, "suggest": suggestion}]}]
            self.assertEqual(D.apply_answers(nodes, reply, zones), (0, 1))
            self.assertEqual(suggestion["mode"], "keep")
            self.assertNotIn("target", suggestion)

    def test_model_extension_suggests_checking_the_app_location(self):
        suggestion = D.placement_review("用途待核实", member_paths=["weights.gguf"])
        self.assertEqual(suggestion["category"], "ai_model")
        self.assertEqual(suggestion["mode"], "keep")
        self.assertTrue(suggestion["userDecision"])


class TestBuildQuestion(unittest.TestCase):
    def test_question_has_facts(self):
        sug = {"mode": "keep", "reason": "无匹配",
               "evidence": ["内容以 installer 为主"]}
        q = D.build_ai_question("某组", "父目录", "/x", sug,
                                {"kind": "installer"},
                                ["a.exe", "b.exe"], trigger="no_rule_match")
        self.assertEqual(q["group"], "某组")
        self.assertEqual(q["parent"], "父目录")
        self.assertEqual(q["shape"]["kind"], "installer")
        self.assertEqual(q["sampleFiles"], ["a.exe", "b.exe"])
        self.assertIn("installer", q["question"])

    def test_need_naming_question_mentions_target(self):
        sug = {"mode": "new", "newName": "电子书", "needsCheck": True}
        q = D.build_ai_question("x", "y", "/p", sug, None, [],
                                trigger="need_naming")
        self.assertIn("电子书", q["question"])


class TestApplyAnswers(unittest.TestCase):
    """AI 自己走 猜想→验证→确认，代码只记录。"""

    def _nodes(self, n=5):
        return [{"name": "P", "groups": [
            {"name": f"G{i}", "suggest": {"mode": "keep", "needsCheck": True}}
            for i in range(n)]}]

    def _write(self, answers):
        import json, tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8") as f:
            json.dump({"answers": answers}, f, ensure_ascii=False)
            return f.name

    def test_full_flow_passes(self):
        nodes = self._nodes()
        path = self._write([{
            "parent": "P", "group": "G0", "category": "voice_synth",
            "label": "歌声合成", "hypothesis": "怀疑是成品音乐",
            "verification": {"method": "查 DLsite", "findings": "是声库",
                             "disproved": ["按名字归音乐是错的"]},
            "confirmation": {"holds": True, "confidence": "high",
                             "why": "验证推翻了初猜", "residualRisk": "无"}}])
        D.apply_answers(nodes, path)
        g = nodes[0]["groups"][0]["suggest"]
        self.assertFalse(g["needsCheck"])
        j = g["aiJudgement"]
        self.assertEqual(j["hypothesis"], "怀疑是成品音乐")
        self.assertIn("DLsite", j["verification"]["method"])
        self.assertTrue(j["confirmation"]["holds"])
        os.unlink(path)

    def test_missing_verification_pending(self):
        nodes = self._nodes()
        path = self._write([{
            "parent": "P", "group": "G1", "category": "x", "label": "X",
            "confirmation": {"holds": True, "confidence": "high",
                             "why": "我觉得"}}])
        D.apply_answers(nodes, path)
        g = nodes[0]["groups"][1]["suggest"]
        self.assertTrue(g["needsCheck"])
        self.assertTrue(any("验证过程" in e for e in g.get("evidence", [])))
        os.unlink(path)

    def test_missing_confirmation_pending(self):
        nodes = self._nodes()
        path = self._write([{
            "parent": "P", "group": "G2", "category": "x", "label": "X",
            "verification": {"method": "查了", "findings": "是"}}])
        D.apply_answers(nodes, path)
        g = nodes[0]["groups"][2]["suggest"]
        self.assertTrue(g["needsCheck"])
        self.assertTrue(any("确认结论" in e for e in g.get("evidence", [])))
        os.unlink(path)

    def test_ai_says_not_holds(self):
        """AI 自己确认'站不住'——代码尊重它的结论。"""
        nodes = self._nodes()
        path = self._write([{
            "parent": "P", "group": "G3", "category": "x", "label": "X",
            "hypothesis": "猜是A", "verification": {"method": "查了",
                                                    "findings": "是B不是A",
                                                    "disproved": ["A"]},
            "confirmation": {"holds": False, "confidence": "low",
                             "why": "验证推翻了", "residualRisk": "还需人看"}}])
        D.apply_answers(nodes, path)
        g = nodes[0]["groups"][3]["suggest"]
        self.assertTrue(g["needsCheck"], "AI 说站不住 → 保持待确认")
        self.assertFalse(g["aiJudgement"]["confirmation"]["holds"])
        os.unlink(path)

    def test_code_does_not_downgrade_confidence(self):
        """代码不替 AI 降级——记录原值。"""
        nodes = self._nodes()
        path = self._write([{
            "parent": "P", "group": "G4", "category": "x", "label": "X",
            "verification": {"method": "查了", "findings": "是"},
            "confirmation": {"holds": True, "confidence": "medium",
                             "why": "大概吧", "residualRisk": "细节不确定"}}])
        D.apply_answers(nodes, path)
        g = nodes[0]["groups"][4]["suggest"]
        # 代码如实记录 medium，不因为缺东西改成 low
        self.assertEqual(g["aiJudgement"]["confirmation"]["confidence"],
                         "medium")
        os.unlink(path)

    def test_existing_exact_label_target_becomes_move(self):
        nodes = self._nodes(1)
        zones = [{"path": "/catalog/语音合成", "label": "语音合成",
                  "depth": 2, "files": 12}]
        path = self._write([{
            "parent": "P", "group": "G0", "label": "语音合成",
            "hypothesis": "猜是工具链",
            "verification": {"method": "看元数据", "findings": "语音工具"},
            "confirmation": {"holds": True, "confidence": "high",
                             "why": "用途确认", "residualRisk": ""},
        }])
        D.apply_answers(nodes, path, zones=zones)
        sug = nodes[0]["groups"][0]["suggest"]
        self.assertEqual(sug["mode"], "move")
        self.assertEqual(sug["target"], "/catalog/语音合成")
        self.assertFalse(sug["needsCheck"])
        os.unlink(path)

    def test_target_path_must_match_zone_exactly(self):
        nodes = self._nodes(1)
        zones = [{"path": "/catalog/语音合成", "label": "语音合成",
                  "depth": 2, "files": 12}]
        path = self._write([{
            "parent": "P", "group": "G0", "target": "/catalog",
            "hypothesis": "猜是工具链",
            "verification": {"method": "看元数据", "findings": "语音工具"},
            "confirmation": {"holds": True, "confidence": "high",
                             "why": "用途确认", "residualRisk": ""},
        }])
        D.apply_answers(nodes, path, zones=zones)
        sug = nodes[0]["groups"][0]["suggest"]
        self.assertEqual(sug["mode"], "keep")
        self.assertTrue(sug["needsCheck"])
        self.assertTrue(any("未与 zones" in x for x in sug["evidence"]))
        os.unlink(path)

    def test_unmatched_reported(self):
        nodes = self._nodes()
        path = self._write([{"parent": "不存在", "group": "也没有",
                             "category": "x", "label": "X"}])
        applied, missed = D.apply_answers(nodes, path)
        self.assertEqual(applied, 0)
        self.assertEqual(missed, 1)
        os.unlink(path)

    def test_legacy_same_name_is_ambiguous_across_nodes(self):
        nodes = self._nodes(1) + self._nodes(1)
        nodes[0]["id"], nodes[1]["id"] = "first", "second"
        path = self._write([{"parent": "P", "group": "G0", "category": "x", "label": "X"}])
        self.addCleanup(os.unlink, path)
        self.assertEqual(D.apply_answers(nodes, path), (0, 1))
        self.assertTrue(all("aiJudgement" not in node["groups"][0]["suggest"] for node in nodes))


class TestQuestionPackage(unittest.TestCase):
    def test_requires_three_steps(self):
        sug = {"mode": "keep", "reason": "x", "evidence": []}
        q = D.build_ai_question("g", "p", "/x", sug, None, [],
                                trigger="no_rule_match")
        ro = q["requiredOutput"]
        self.assertIn("hypothesis", ro["fields"])
        self.assertIn("verification", ro["fields"])
        self.assertIn("confirmation", ro["fields"])
        self.assertIn("holds", ro["fields"]["confirmation"])




class TestVerificationTier(unittest.TestCase):
    """分级：不是所有判断都要写完整三步。"""

    def test_structural_signal_is_direct(self):
        sug = {"mode": "keep", "evidence": ["unity-layout: true"]}
        self.assertEqual(D.verification_tier("x", sug), "direct")

    def test_vm_layout_is_direct(self):
        sug = {"mode": "keep", "evidence": ["vm-layout: true"]}
        self.assertEqual(D.verification_tier("x", sug), "direct")

    def test_split_requires_full(self):
        sug = {"mode": "split", "evidence": []}
        self.assertEqual(D.verification_tier("x", sug), "full")

    def test_merge_requires_full(self):
        sug = {"mode": "merge", "evidence": []}
        self.assertEqual(D.verification_tier("x", sug), "full")

    def test_conflict_requires_full(self):
        sug = {"mode": "keep", "needsCheck": True, "evidence": ["冲突"]}
        self.assertEqual(D.verification_tier("x", sug), "full")

    def test_new_requires_full(self):
        sug = {"mode": "new", "evidence": ["unity-layout: true"]}
        self.assertEqual(D.verification_tier("x", sug), "full")

    def test_shape_evidence_requires_verify(self):
        sug = {"mode": "keep", "evidence": ["内容以 installer 为主"]}
        self.assertEqual(D.verification_tier("x", sug), "verify")

    def test_empty_evidence_defaults_verify(self):
        sug = {"mode": "keep", "evidence": []}
        self.assertEqual(D.verification_tier("x", sug), "verify")

    def test_question_carries_tier(self):
        sug = {"mode": "split", "evidence": []}
        q = D.build_ai_question("g", "p", "/x", sug, None, [],
                                trigger="no_rule_match")
        self.assertEqual(q["requiredOutput"]["tier"], "full")
        self.assertIn("tierGuide", q["requiredOutput"])


class TestTierEnforcement(unittest.TestCase):
    """按级别检查完整性。"""

    def _nodes(self):
        return [{"name": "P", "groups": [
            {"name": "D", "suggest": {"mode": "keep",
                                      "evidence": ["unity-layout: true"]}},
            {"name": "V", "suggest": {"mode": "keep",
                                      "evidence": ["内容以 installer 为主"]}},
            {"name": "F", "suggest": {"mode": "split", "evidence": []}},
        ]}]

    def _write(self, answers):
        import json, tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8") as f:
            json.dump({"answers": answers}, f, ensure_ascii=False)
            return f.name

    def test_direct_needs_only_evidence(self):
        """直接定的只要 evidence，不要求三步。"""
        nodes = self._nodes()
        path = self._write([{
            "parent": "P", "group": "D", "category": "x", "label": "X",
            "evidence": "steamapps/ 下有 appmanifest_*.acf",
            "confirmation": {"holds": True, "confidence": "high"}}])
        D.apply_answers(nodes, path)
        g = nodes[0]["groups"][0]["suggest"]
        self.assertEqual(g["aiJudgement"]["verificationTier"], "direct")
        self.assertFalse(g["needsCheck"], "直接定不该因为缺 verification 被拦")
        os.unlink(path)

    def test_direct_without_evidence_blocked(self):
        nodes = self._nodes()
        path = self._write([{
            "parent": "P", "group": "D", "category": "x", "label": "X",
            "confirmation": {"holds": True}}])
        D.apply_answers(nodes, path)
        g = nodes[0]["groups"][0]["suggest"]
        self.assertTrue(g["needsCheck"])

    def test_full_requires_three_steps(self):
        nodes = self._nodes()
        path = self._write([{
            "parent": "P", "group": "F", "category": "x", "label": "X",
            "evidence": "有内容"}])
        D.apply_answers(nodes, path)
        g = nodes[0]["groups"][2]["suggest"]
        self.assertEqual(g["aiJudgement"]["verificationTier"], "full")
        self.assertTrue(g["needsCheck"], "必须三步的缺步骤应仍待确认")
        os.unlink(path)

    def test_answer_cannot_lower_required_tier(self):
        nodes = self._nodes()
        path = self._write([{
            "parent": "P", "group": "F", "category": "x", "label": "X",
            "tier": "direct", "evidence": "有内容",
            "confirmation": {"holds": True}}])
        D.apply_answers(nodes, path)
        g = nodes[0]["groups"][2]["suggest"]
        self.assertEqual(g["aiJudgement"]["verificationTier"], "full")
        self.assertTrue(g["needsCheck"])
        os.unlink(path)

    def test_direct_recommendation_cannot_be_upgraded_to_bypass_full(self):
        nodes = self._nodes()
        path = self._write([{
            "parent": "P", "group": "D", "category": "x", "label": "X",
            "tier": "direct", "evidence": "结构明确",
            "confirmation": {"holds": "false"}}])
        D.apply_answers(nodes, path)
        g = nodes[0]["groups"][0]["suggest"]
        self.assertEqual(g["aiJudgement"]["verificationTier"], "direct")
        self.assertTrue(g["needsCheck"], "字符串 holds 不可作为确认结论")
        self.assertIsNone(g["aiJudgement"]["confirmation"]["holds"])
        os.unlink(path)

    def test_full_requires_all_three_step_fields_and_boolean_holds(self):
        nodes = self._nodes()
        path = self._write([{
            "parent": "P", "group": "F", "category": "x", "label": "X",
            "hypothesis": "初猜", "verification": {"method": "查证", "findings": "找到证据"},
            "confirmation": {"holds": "true", "confidence": "high", "why": "确认"}}])
        D.apply_answers(nodes, path)
        g = nodes[0]["groups"][2]["suggest"]
        self.assertTrue(g["needsCheck"])
        self.assertIsNone(g["aiJudgement"]["confirmation"]["holds"])
        os.unlink(path)




class TestPolicyMapping(unittest.TestCase):
    """policy.json 的 mapping 要真的生效——S0 前置确认的产物。"""

    def _setup_policy(self, tmp, mapping):
        import json
        from pathlib import Path as _P
        org = _P(tmp) / "_organize"
        org.mkdir(parents=True, exist_ok=True)
        (org / "policy.json").write_text(
            json.dumps({"mapping": mapping}, ensure_ascii=False),
            encoding="utf-8")
        return tmp

    def test_load_returns_mapping(self):
        with tempfile.TemporaryDirectory() as d:
            self._setup_policy(d, {"video_movie": "我的影视"})
            mp, st = D.load_policy_mapping(d)
            self.assertEqual(mp.get("video_movie"), "我的影视")

    def test_load_skips_comment_keys(self):
        with tempfile.TemporaryDirectory() as d:
            self._setup_policy(d, {"_comment": "说明", "game": "游戏"})
            mp, _ = D.load_policy_mapping(d)
            self.assertNotIn("_comment", mp)
            self.assertIn("game", mp)

    def test_load_missing_file(self):
        with tempfile.TemporaryDirectory() as d:
            mp, st = D.load_policy_mapping(d)
            self.assertEqual(mp, {})

    def test_apply_changes_cross_rules(self):
        """mapping 应该覆盖 CROSS_RULES 的目标。"""
        saved = [dict(r) for r in D.CROSS_RULES]
        saved_nodes = set(D.CATEGORY_NODE_NAMES)
        try:
            D.CROSS_RULES = [
                {"keys": ["电影", "影片"], "to": ["影视"]},
                {"keys": ["游戏"], "to": ["游戏"]},
            ]
            D.CATEGORY_NODE_NAMES = set()
            n = D.apply_policy_mapping({"video_movie": "我的影视区"})
            self.assertGreater(n, 0)
            rule = [r for r in D.CROSS_RULES if "电影" in r["keys"]][0]
            self.assertEqual(rule["to"], ["我的影视区"])
        finally:
            D.CROSS_RULES = saved
            D.CATEGORY_NODE_NAMES = saved_nodes

    def test_apply_adds_to_category_nodes(self):
        saved = [dict(r) for r in D.CROSS_RULES]
        saved_nodes = set(D.CATEGORY_NODE_NAMES)
        try:
            D.CROSS_RULES = []
            D.CATEGORY_NODE_NAMES = set()
            D.apply_policy_mapping({"game": "我的游戏区"})
            self.assertIn("我的游戏区", D.CATEGORY_NODE_NAMES)
        finally:
            D.CROSS_RULES = saved
            D.CATEGORY_NODE_NAMES = saved_nodes

    def test_apply_empty_is_noop(self):
        n = D.apply_policy_mapping({})
        self.assertEqual(n, 0)

    def test_category_keys_covers_default_pack(self):
        """CATEGORY_KEYS 要覆盖规则包里所有类别。"""
        import json
        from pathlib import Path as _P
        pack = _P(__file__).resolve().parent.parent / "rules" / "default.json"
        if not pack.is_file():
            self.skipTest("没有规则包")
        data = json.loads(pack.read_text(encoding="utf-8-sig"))
        cats = {k for k in data.get("categories", {})
                if not k.startswith("_")}
        missing = cats - set(D.CATEGORY_KEYS)
        self.assertFalse(missing, f"CATEGORY_KEYS 缺: {missing}")

    def test_boot_rules_uses_root_policy_rules_path(self):
        import json
        import rules
        from pathlib import Path as _P
        with tempfile.TemporaryDirectory() as d:
            root = _P(d)
            org = root / "_organize"
            org.mkdir()
            custom = root / "custom.json"
            data = json.loads((_P(__file__).resolve().parent.parent /
                               "rules" / "default.json").read_text(encoding="utf-8-sig"))
            data["categories"] = {"custom": {"keys": ["unique_custom_key"],
                                              "suggest": ["custom_target"]}}
            custom.write_text(json.dumps(data), encoding="utf-8")
            (org / "policy.json").write_text(
                json.dumps({"rules": "../custom.json"}), encoding="utf-8")
            old_rules = [dict(x) for x in D.CROSS_RULES]
            try:
                loaded = D._boot_rules(str(root))
                self.assertEqual(loaded["categories"].keys(), {"custom"})
                self.assertTrue(any("unique_custom_key" in r["keys"]
                                    for r in D.CROSS_RULES))
            finally:
                D.CROSS_RULES = old_rules

    def test_boot_rules_does_not_change_safety_globals(self):
        import safety
        before = (set(safety.PROTECTED_NAMES), set(safety.NESTED_ALLOW),
                  set(safety.NESTED_DENY))
        with tempfile.TemporaryDirectory() as d:
            D._boot_rules(d)
        after = (set(safety.PROTECTED_NAMES), set(safety.NESTED_ALLOW),
                 set(safety.NESTED_DENY))
        self.assertEqual(after, before)

    def test_invalid_root_rules_path_raises(self):
        import json
        from pathlib import Path as _P
        import rules
        with tempfile.TemporaryDirectory() as d:
            org = _P(d) / "_organize"
            org.mkdir()
            (org / "policy.json").write_text(
                json.dumps({"rules": "missing.json"}), encoding="utf-8")
            with self.assertRaises(rules.RulesError):
                D._boot_rules(d)

    def test_cli_rejects_invalid_root_rules_path(self):
        import json
        import sys
        from contextlib import redirect_stderr
        from io import StringIO
        from pathlib import Path as _P
        with tempfile.TemporaryDirectory() as d:
            org = _P(d) / "_organize"
            org.mkdir()
            (org / "policy.json").write_text(
                json.dumps({"rules": "missing.json"}), encoding="utf-8")
            old_argv = sys.argv
            try:
                sys.argv = ["decisions.py", "--root", d]
                with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as e:
                    D.main()
                self.assertEqual(e.exception.code, 2)
            finally:
                sys.argv = old_argv




class TestUiLoadsRules(unittest.TestCase):
    """ui.py 必须加载规则包，否则建议全线失效。"""

    def test_ui_applies_policy_mapping(self):
        from unittest.mock import patch
        import ui
        from executor import Journal

        with tempfile.TemporaryDirectory(prefix=".test-ui-rules-", dir=CORE.parent) as temp:
            base = Path(temp)
            root = base / "root"
            state = root / "_organize"
            state.mkdir(parents=True)
            mapping = {"video_course": "Fixture Courses"}
            (state / "policy.json").write_text(json.dumps({"mapping": mapping}), encoding="utf-8")
            environment = {"LOCALAPPDATA": str(base / "local"), "XDG_STATE_HOME": str(base / "local")}
            with patch.dict(os.environ, environment), patch.object(ui, "build_board") as build:
                ui.board_for_root(Journal(root))
                rule_state = build.call_args.kwargs["decision_rules"]
        self.assertIn("Fixture Courses", rule_state.CATEGORY_NODE_NAMES)
        self.assertTrue(any(rule["to"] == ["Fixture Courses"] for rule in rule_state.CROSS_RULES))
        self.assertTrue(rule_state.NEW_CATEGORIES)


class TestSuggestNotAllKeep(unittest.TestCase):
    """有规则包时，suggest 不能全部返回 keep。"""

    def test_mixed_modes_with_rules(self):
        saved = [dict(r) for r in D.CROSS_RULES]
        saved_nodes = set(D.CATEGORY_NODE_NAMES)
        try:
            D.CROSS_RULES = [
                {"keys": ["课程", "教程"], "to": ["学习资料"]},
                {"keys": ["游戏"], "to": ["游戏"]},
            ]
            D.CATEGORY_NODE_NAMES = set()
            zones = [
                {"path": "/x/学习资料", "label": "学习资料", "depth": 1, "files": 10},
                {"path": "/x/游戏", "label": "游戏", "depth": 1, "files": 10},
            ]
            modes = set()
            for name in ["编程课程", "某个游戏", "随便什么"]:
                r = D.suggest_group(name, "下载", zones)
                modes.add(r["mode"])
            self.assertGreater(len(modes), 1,
                               f"应返回多种 mode，实际只有 {modes}")
        finally:
            D.CROSS_RULES = saved
            D.CATEGORY_NODE_NAMES = saved_nodes


if __name__ == "__main__":
    unittest.main(verbosity=2)
