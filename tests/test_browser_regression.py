"""End to end browser acceptance for the generated TidyFileTree review board.

Run with: python -m pip install playwright && playwright install chromium
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:  # Keep the ordinary unit suite dependency-free.
    sync_playwright = None


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "core"))
from ui import group_key


@unittest.skipIf(sync_playwright is None, "install the browser extra: pip install playwright")
class BrowserRegression(unittest.TestCase):
    def test_offline_preview_and_progress_and_project_site(self):
        with tempfile.TemporaryDirectory(prefix=".organizer-test-visual-reports-", dir=PROJECT) as temp:
            base = Path(temp)
            root = base / "root"
            root.mkdir()
            source = root / "sample.txt"
            source.touch()
            actions = base / "actions.tsv"
            actions.write_text(f"op\tsrc\tdst\treason\nrename\t{source}\tfiled.txt\tfixture\n", encoding="utf-8")
            offline = base / "progress.html"
            preview = base / "preview.html"
            env = dict(os.environ, LOCALAPPDATA=str(base / "app-state"),
                       XDG_STATE_HOME=str(base / "state"), PYTHONIOENCODING="utf-8")
            if env.get("TIDY_FILE_TREE_BROWSER_USE_INSTALLED") != "1":
                env["PYTHONPATH"] = str(PROJECT) + os.pathsep + env.get("PYTHONPATH", "")

            def run(*args):
                proc = subprocess.run([sys.executable, "-m", "tidy_file_tree", *map(str, args)],
                                      cwd=base, env=env, capture_output=True, encoding="utf-8")
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

            run("progress", root, "--out", offline, "--no-open", "--lang", "en")
            run("apply", root, "--actions", actions, "--dry-run", "--preview-html", preview,
                "--receipt", base / "receipt.json", "--lang", "en")
            self.assertTrue(source.is_file())
            with sync_playwright() as playwright:
                chrome = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
                edge = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
                executable = next((str(path) for path in (chrome, edge) if path.is_file()), None)
                browser = playwright.chromium.launch(executable_path=executable)
                page = browser.new_page(viewport={"width": 390, "height": 844})
                page.goto(offline.as_uri())
                expect(page.locator("#liveText")).to_have_text("Offline progress snapshot")
                expect(page.locator("#refreshBtn")).to_be_hidden()
                expect(page.locator("#openWorkbench")).to_be_hidden()
                page.locator("#langSelect").select_option("zh-CN")
                expect(page.locator("#liveText")).to_have_text("静态进度快照")

                page.goto(preview.as_uri())
                expect(page.locator("#title")).to_contain_text("Execution preview")
                expect(page.locator("#snapshot")).to_contain_text("no files changed")
                expect(page.get_by_text("filed.txt", exact=True).first).to_be_visible()

                page.goto((PROJECT / "site" / "index.html").as_uri())
                expect(page.get_by_role("heading", name="从样本开始")).to_be_visible()
                self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
                page.get_by_role("link", name="English", exact=True).click()
                expect(page.get_by_role("link", name="简体中文", exact=True)).to_be_visible()
                self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
                page.get_by_role("link", name="Try the sample").click()
                self.assertTrue(page.url.endswith("#demo"))
                browser.close()

    def test_progress_opens_before_setup_and_updates_its_next_step(self):
        with tempfile.TemporaryDirectory(prefix=".tidy-file-tree-progress-browser-", dir=PROJECT) as temp:
            base = Path(temp)
            root = base / "root"
            inbox = root / "Inbox"
            inbox.mkdir(parents=True)
            env = dict(os.environ, LOCALAPPDATA=str(base / "app-state"),
                       XDG_STATE_HOME=str(base / "state"), PYTHONIOENCODING="utf-8")
            if env.get("TIDY_FILE_TREE_BROWSER_USE_INSTALLED") != "1":
                env["PYTHONPATH"] = str(PROJECT) + os.pathsep + env.get("PYTHONPATH", "")
            command = [sys.executable, "-m", "tidy_file_tree", "progress", str(root), "--no-open", "--lang", "en"]
            with subprocess.Popen(command, cwd=temp, env=env, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True, encoding="utf-8") as server:
                try:
                    url = server.stdout.readline().strip()
                    self.assertTrue(url.startswith("http://127.0.0.1:"), url)
                    with sync_playwright() as playwright:
                        windows_browsers = [Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
                                            Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")]
                        executable = next((str(path) for path in windows_browsers if path.is_file()), None)
                        browser = playwright.chromium.launch(executable_path=executable)
                        page = browser.new_page()
                        page.goto(url)
                        step = page.locator("#nextStepBanner")
                        expect(step).to_have_attribute("data-step", "start_analysis")
                        expect(step).to_contain_text("AI next step")
                        expect(page.locator("#stepper .step")).to_have_count(7)
                        expect(page.locator("#stepper")).to_contain_text("S6")
                        org = root / "_organize"
                        self.assertFalse(org.exists())
                        probe = org / "probe"
                        probe.mkdir(parents=True)
                        (org / "policy.json").write_text(json.dumps({"setup": {"goal": "organize"}}),
                                                         encoding="utf-8")
                        expect(step).to_have_attribute("data-step", "continue_classification")
                        (probe / "probe_inbox.json").write_text(json.dumps({"path": str(inbox), "action": "split"}),
                                                               encoding="utf-8")
                        expect(step).to_have_attribute("data-step", "continue_counting")

                        def run(*args):
                            result = subprocess.run([sys.executable, "-m", "tidy_file_tree", *map(str, args)],
                                                    cwd=base, env=env, capture_output=True, encoding="utf-8")
                            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

                        (probe / "probe_inbox.json").write_text(json.dumps({
                            "path": str(inbox), "action": "rename", "rename_to": "Filed",
                        }), encoding="utf-8")
                        run("ui", root, "--no-open")
                        expect(step).to_have_attribute("data-step", "review_plan")
                        with page.context.expect_page() as popup:
                            page.locator("#openWorkbench").click()
                        board = popup.value
                        board.get_by_role("button", name="Preview changes").click()
                        expect(board.locator("#reviewStatus")).to_contain_text("Select an item on the left")
                        board.locator('#review form button').click()
                        board.get_by_role("button", name="Confirm rename").click()
                        with board.expect_response(lambda response: "/workbench-actions?" in response.url) as reply:
                            board.get_by_role("button", name="Preview changes (1)").click()
                        self.assertEqual(reply.value.status, 200, f"Origin={reply.value.request.header_value('origin')}")
                        expect(board.locator("#previewNext")).to_contain_text("Check each source")
                        expect(board.locator("#previewBoard")).to_have_text("Back to the review board")
                        board.locator("#language").select_option("zh-CN")
                        expect(board.locator("#previewNext")).to_contain_text("确认执行这份预演")
                        expect(step).to_have_attribute("data-step", "confirm_preview")
                        expect(step.get_by_role("link", name="Open preview")).to_be_visible()
                        actions = org / "workbench-actions.tsv"
                        snapshot = page.request.get(url + "snapshot").json()
                        receipt = Path(snapshot["planReview"]["receipt"])
                        self.assertTrue(actions.is_file())
                        self.assertTrue(receipt.is_file())
                        self.assertFalse((org / "journal.tsv").exists())
                        self.assertTrue(inbox.is_dir())
                        run("apply", root, "--actions", actions, "--receipt", receipt)
                        expect(page.locator("#artSub")).to_contain_text("1 action executed")
                        self.assertEqual(page.request.get(url + snapshot["previewUrl"]).status, 404)
                        self.assertTrue((root / "Filed").is_dir())
                        run("undo", "--journal", org / "journal.tsv", "--last")
                        self.assertTrue(inbox.is_dir())
                        browser.close()
                finally:
                    if server.poll() is None:
                        server.terminate()

    def test_explicit_rename_exports_previews_executes_and_undoes(self):
        with tempfile.TemporaryDirectory(prefix='.tidy-file-tree-browser-rename-', dir=PROJECT) as tmp:
            base = Path(tmp)
            root = base / 'root'
            source = root / 'New folder'
            source.mkdir(parents=True)
            (source / 'sample.txt').touch()
            state = root / '_organize'
            probe = state / 'probe'
            probe.mkdir(parents=True)
            (probe / 'probe_rename.json').write_text(json.dumps({
                'path': str(source), 'action': 'rename', 'rename_to': 'Meeting Notes',
            }), encoding='utf-8')
            env = dict(os.environ, LOCALAPPDATA=str(base / 'local'), PYTHONIOENCODING='utf-8')
            if not os.environ.get('TIDY_FILE_TREE_BROWSER_USE_INSTALLED'):
                env['PYTHONPATH'] = str(PROJECT) + os.pathsep + env.get('PYTHONPATH', '')

            def run(*args):
                result = subprocess.run([sys.executable, '-m', 'tidy_file_tree', *map(str, args)],
                                        cwd=base, env=env, capture_output=True, encoding='utf-8')
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            board = base / 'board.html'
            run('ui', root, '--out', board, '--lang', 'en')
            actions = base / 'actions.tsv'
            with sync_playwright() as p:
                chrome = Path(r'C:\Program Files\Google\Chrome\Application\chrome.exe')
                edge = Path(r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe')
                executable = chrome if chrome.is_file() else edge if edge.is_file() else None
                browser = p.chromium.launch(executable_path=str(executable) if executable else None)
                page = browser.new_page(accept_downloads=True)
                page.set_default_timeout(5000)
                page.goto(board.as_uri())
                self.assertTrue(page.get_by_text('Suggested rename: New folder → Meeting Notes').is_visible())
                self.assertEqual(page.evaluate('window.foState().linked'), 0)
                page.get_by_role('button', name='Confirm rename').click()
                page.locator('#undoButton').click()
                self.assertEqual(page.evaluate('window.foState().linked'), 0)
                page.locator('#redoButton').click()
                page.reload()
                self.assertEqual(page.evaluate('window.foState().linked'), 1)
                page.locator('#reviewButton').click()
                self.assertIn('Scheduled to rename (1)', page.locator('#reviewText').inner_text())
                with page.expect_download() as download:
                    page.locator('#downloadActions').click()
                download.value.save_as(actions)
                browser.close()

            [header, action] = actions.read_text(encoding='utf-8').splitlines()
            self.assertEqual(header, 'op\tsrc\tdst\treason')
            self.assertEqual(action.split('\t')[:3], ['rename', str(source), 'Meeting Notes'])
            receipt = base / 'receipt.json'
            run('apply', root, '--actions', actions, '--receipt', receipt, '--dry-run')
            self.assertTrue(source.is_dir())
            run('apply', root, '--actions', actions, '--receipt', receipt)
            self.assertTrue((root / 'Meeting Notes' / 'sample.txt').is_file())
            run('undo', '--journal', state / 'journal.tsv', '--last')
            self.assertTrue((source / 'sample.txt').is_file())

    def test_scan_drafts_export_ai_questions_and_import_only_current_answers(self):
        with tempfile.TemporaryDirectory(prefix=".tidy-file-tree-browser-ai-", dir=PROJECT) as temp:
            root = Path(temp) / "library"
            for title, season_episodes in (("Sky", (1, 1)), ("Morning", (1, 2))):
                for index, episode in enumerate(season_episodes):
                    batch = root / title / f"fixture-batch-{index}"
                    batch.mkdir(parents=True)
                    (batch / f"{title}.S01E{episode:02}.mkv").touch()
            before = sorted(root.rglob("*"))
            env = os.environ.copy()
            if env.get("TIDY_FILE_TREE_BROWSER_USE_INSTALLED") != "1":
                env["PYTHONPATH"] = str(PROJECT) + os.pathsep + env.get("PYTHONPATH", "")
            env["LOCALAPPDATA"] = str(Path(temp) / "appdata")
            env["XDG_STATE_HOME"] = str(Path(temp) / "state")
            command = [sys.executable, "-m", "tidy_file_tree", "scan", str(root), "--tree", "--no-open", "--lang", "en"]
            with subprocess.Popen(command, cwd=temp, env=env, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace") as server:
                try:
                    url = server.stdout.readline().strip()
                    self.assertTrue(url.startswith("http://127.0.0.1:"), url)
                    with sync_playwright() as p:
                        chrome = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
                        edge = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
                        executable = chrome if chrome.is_file() else edge if edge.is_file() else None
                        browser = p.chromium.launch(executable_path=str(executable) if executable else None)
                        page = browser.new_page(accept_downloads=True)
                        page.set_default_timeout(5000)
                        page.goto(url)
                        page.locator("#ai-export").wait_for()
                        with page.expect_download() as draft_file:
                            page.locator("#draft-download").click()
                        draft = json.loads(draft_file.value.path().read_text(encoding="utf-8"))
                        self.assertEqual(draft["root"], ".")
                        self.assertNotIn(str(root).replace("\\", "/"),
                                         json.dumps(draft).replace("\\\\", "/"))
                        rows = [row for group in draft["groups"] for row in group["rows"]]
                        self.assertTrue(rows)
                        self.assertTrue(all(not Path(row["source"]).is_absolute() for row in rows))
                        self.assertTrue(all(not Path(row["target"]).is_absolute() for row in rows if row["target"]))
                        with page.expect_download() as download:
                            page.locator("#ai-export").click()
                        questions = json.loads(download.value.path().read_text(encoding="utf-8"))
                        [question] = questions["questions"]
                        self.assertEqual(question["title"], "Sky")
                        self.assertNotIn("fixture-batch-", json.dumps(questions))
                        answer = {
                            "id": question["id"], "hypothesis": "These may be different editions.",
                            "verification": {"method": "Compare names", "findings": "Two candidates for episode 1"},
                            "confirmation": {"recommendation": "needs_more_evidence", "confidence": "medium",
                                             "why": "The episode numbers overlap.", "residualRisk": "Edition is unknown."},
                        }
                        staged_answers = Path(temp) / "answers.json"
                        duplicate = {**answer, "id": "duplicate-id"}
                        staged_answers.write_text(json.dumps({
                            "format": questions["answerFormat"]["format"],
                            "answers": [answer, {**answer, "id": "unmatched-id"}, duplicate, duplicate],
                        }), encoding="utf-8")
                        page.locator("#draft-node").select_option(label="Morning")
                        page.locator("#ai-file").set_input_files(str(staged_answers))
                        notice = page.locator("#ai-message")
                        expect(notice).to_have_text("Imported 1 · invalid or duplicate 2 · unmatched to this scan 1")
                        self.assertEqual(page.locator("#draft-node option:checked").inner_text(), "Sky")
                        self.assertIn("Needs review", page.locator("#drafts details summary").inner_text())
                        self.assertIn(answer["verification"]["findings"], page.locator(".ai-review").inner_text())
                        page.locator("#rescan").click()
                        expect(page.locator("#ai-count")).to_have_text("AI review · 1 of 1 answered")

                        source = root / "Sky/fixture-batch-0/Sky.S01E01.mkv"
                        changed = source.with_name("Sky.S01E01.alt.mkv")
                        source.rename(changed)
                        try:
                            page.locator("#rescan").click()
                            expect(page.locator("#ai-count")).to_have_text("AI review · 0 of 1 answered")
                            page.locator("#ai-file").set_input_files(str(staged_answers))
                            expect(notice).to_have_text("Imported 0 · invalid or duplicate 2 · unmatched to this scan 2")
                        finally:
                            changed.rename(source)
                        browser.close()
                finally:
                    if server.poll() is None:
                        server.terminate()
            self.assertEqual(sorted(root.rglob("*")), before)

    def test_history_filters_late_arrivals_and_browses_large_live_folders(self):
        with tempfile.TemporaryDirectory(prefix=".tidy-file-tree-history-browser-", dir=PROJECT) as temp:
            base = Path(temp)
            root = base / "root"
            old = root / "Intake" / "Toolkit"
            old.mkdir(parents=True)
            (old / "original.txt").touch()
            incoming = root / "incoming.txt"
            incoming.touch()
            archive = root / "Archive"
            archive.mkdir()
            crowded = root / "Crowded"
            crowded.mkdir()
            for i in range(520):
                (crowded / f"{i:03}.txt").touch()
            env = dict(os.environ, LOCALAPPDATA=str(base / "app-state"),
                       XDG_STATE_HOME=str(base / "state"), PYTHONIOENCODING="utf-8")
            if env.get("TIDY_FILE_TREE_BROWSER_USE_INSTALLED") != "1":
                env["PYTHONPATH"] = str(PROJECT) + os.pathsep + env.get("PYTHONPATH", "")
            cli = [sys.executable, "-m", "tidy_file_tree"]
            actions = base / "actions.tsv"
            for operation, source, target in (
                ("mkdir", "-", old / "NewFolder"),
                ("move", old, archive),
                ("move", incoming, archive / old.name),
            ):
                actions.write_text(f"op\tsrc\tdst\treason\n{operation}\t{source}\t{target}\tfixture\n",
                                   encoding="utf-8")
                result = subprocess.run(cli + ["apply", str(root), "--actions", str(actions)],
                                        cwd=base, env=env, capture_output=True, encoding="utf-8")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            report = base / "history.html"
            result = subprocess.run(cli + ["history", str(root), "--out", str(report), "--lang", "en"],
                                    cwd=base, env=env, capture_output=True, encoding="utf-8")
            self.assertEqual(result.returncode, 0, result.stderr)
            with sync_playwright() as playwright:
                chrome = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
                edge = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
                executable = next((str(path) for path in (chrome, edge) if path.is_file()), None)
                browser = playwright.chromium.launch(executable_path=executable)
                page = browser.new_page()
                page.add_init_script("""
                    Object.defineProperty(navigator, 'clipboard', {value: {
                      writeText: async (text) => { window.copiedUndo = text; }
                    }});
                """)
                page.set_default_timeout(7000)
                page.goto(report.as_uri())
                page.locator('#expTree button[data-exp-dir="Intake"]').click()
                page.locator("#expListPane tr").filter(has_text="Toolkit").dblclick()
                expect(page.locator("#expListPane .exp-item-name")).to_have_text(["original.txt"])
                page.get_by_role("button", name="Preview undo for the parent folder operation").click()
                expect(page.locator("[data-exp-undo-seq]")).to_have_text("Copied")
                self.assertIn(" --exact-seq ", page.evaluate("window.copiedUndo"))
                self.assertTrue(page.evaluate("window.copiedUndo").endswith(" --dry-run"))
                page.locator("#expHome").click()
                page.locator("#expListPane tr").filter(has_text="Crowded").dblclick()
                expect(page.locator("#expBanner")).to_contain_text("contains only part of this folder")

                command = cli + ["history", str(root), "--serve", "--no-open", "--lang", "en"]
                with subprocess.Popen(command, cwd=base, env=env, stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT, text=True, encoding="utf-8") as server:
                    try:
                        url = server.stdout.readline().strip()
                        self.assertTrue(url.startswith("http://127.0.0.1:"), url)
                        checks = []

                        def deny_status(route):
                            checks.append(route.request.url)
                            route.fulfill(status=503, content_type="application/json", body='{"ok":false}')

                        page.route("**/stat?*", deny_status)
                        page.goto(url)
                        retry = page.get_by_role("button", name="Could not check current status · Retry")
                        expect(retry).to_be_visible()
                        page.locator("#expOnlyChanged").check()
                        page.locator("#expOnlyChanged").uncheck()
                        expect(retry).to_be_visible()
                        initial_path = checks[0]
                        self.assertEqual(checks.count(initial_path), 1)
                        retry.click()
                        expect(retry).to_be_visible()
                        self.assertEqual(checks.count(initial_path), 2)
                        page.locator('#expTree button[data-exp-dir="Intake"]').click()
                        page.locator("#expListPane tr").filter(has_text="Toolkit").dblclick()
                        expect(page.locator("#expListPane .exp-item-name")).to_have_text(["original.txt"])
                        page.locator("#expHome").click()
                        page.locator("#expListPane tr").filter(has_text="Crowded").dblclick()
                        more = page.get_by_role("button", name="Load more entries")
                        expect(more).to_be_visible()
                        for _ in range(10):
                            if page.locator("#expListPane tbody tr").count() == 520:
                                break
                            more.click()
                            expect(page.get_by_text("Loading folder contents…")).to_have_count(0)
                        expect(page.locator("#expListPane tbody tr")).to_have_count(520)
                    finally:
                        if server.poll() is None:
                            server.terminate()
                browser.close()

    def test_review_board_to_receipted_apply_and_undo(self):
        with tempfile.TemporaryDirectory(prefix=".tidy-file-tree-browser-", dir=PROJECT) as temp:
            root = Path(temp) / "library $tick`'s"
            source = root / "incoming"
            source.mkdir(parents=True)
            a, b = source / "part-a.bin", source / "part-b.bin"
            a.write_bytes(b"part A fixture\0")
            b.write_bytes(b"part B fixture\0")
            (root / "archive").mkdir()
            (root / "archive" / "existing.txt").write_text("existing destination fixture", encoding="utf-8")
            probe = root / "_organize" / "probe"
            probe.mkdir(parents=True)
            (probe / "group_fixture.json").write_text(json.dumps({
                "path": str(source), "groups": [{"name": "Sample Set", "units": ["part-a.bin", "part-b.bin"]}]
            }), encoding="utf-8")
            (probe / "units_fixture.json").write_text(json.dumps({
                "path": str(source), "loose_files_as_units": [str(a), str(b)]
            }), encoding="utf-8")
            fingerprint = group_key({"name": "Sample Set", "units": ["part-a.bin", "part-b.bin"]})
            decisions = root / "decisions.json"
            decisions.write_text(json.dumps({
                "root": str(root), "nodes": [{"path": str(source), "groups": [{
                    "id": f"{fingerprint}:0", "fingerprint": fingerprint,
                    "suggest": {"mode": "move", "target": str(root / "archive"),
                                "targetLabel": "archive", "tier": "direct"}
                }]}]
            }), encoding="utf-8")
            board = Path(temp) / "review.html"
            env = os.environ.copy()
            if env.get("TIDY_FILE_TREE_BROWSER_USE_INSTALLED") != "1":
                env["PYTHONPATH"] = str(PROJECT) + os.pathsep + env.get("PYTHONPATH", "")
            env["LOCALAPPDATA"] = str(Path(temp) / "appdata")
            env["XDG_STATE_HOME"] = str(Path(temp) / "state")
            generated = subprocess.run([sys.executable, "-m", "tidy_file_tree", "ui", str(root),
                                        "--decisions", str(decisions), "--out", str(board), "--lang", "en"],
                                       cwd=temp, env=env, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(generated.returncode, 0, generated.stderr)
            self.assertTrue(board.is_file())
            originals = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (a, b)}

            with sync_playwright() as p:
                chrome = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
                edge = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
                executable = chrome if chrome.is_file() else edge if edge.is_file() else None
                browser = p.chromium.launch(executable_path=str(executable) if executable else None)
                page = browser.new_page(accept_downloads=True)
                page.set_default_timeout(5000)
                page.goto(board.as_uri())
                card = page.locator("article.card").filter(has=page.get_by_role("heading", name="Sample Set"))
                card.locator("summary").click()
                first_key = card.locator("button.port[data-source][data-unit]").first.get_attribute("data-unit")
                page.evaluate("""([path,key,destination]) => {
                    localStorage.setItem('packtidy-wires-v1:' + path,JSON.stringify([[key,destination]]));
                    localStorage.setItem('packtidy-theme','dark');
                }""", [str(root), first_key, str(root / "archive")])
                page.reload()
                self.assertEqual(page.evaluate("window.foState().linked"), 0)
                self.assertIn("1 invalidated saved assignment", page.locator("#staleMetric").inner_text())
                self.assertEqual(page.locator("html").get_attribute("data-theme"), "dark")
                self.assertEqual(page.evaluate("localStorage.getItem('tidy-file-tree-theme')"), "dark")
                page.evaluate("""path => {
                    localStorage.setItem('tidy-file-tree-wires-v2:' + path,'[]');
                    localStorage.setItem('tidy-file-tree-theme','light');
                }""", str(root))
                page.reload()
                self.assertEqual(page.evaluate("window.foState().linked"), 0)
                self.assertEqual(page.locator("html").get_attribute("data-theme"), "light")
                self.assertIn("TidyFileTree", page.title())
                self.assertEqual(page.locator("html").get_attribute("lang"), "en")
                self.assertEqual(page.locator("#groupSearch").get_attribute("placeholder"), "Group or source")
                archive_target = page.locator("[data-target]").filter(has=page.locator(".target-label", has_text="archive"))
                page.locator("#groupSearch").fill("Sample Set")
                card.locator("summary").click()
                card.locator("button.port[data-source][data-unit]").first.click()
                self.assertEqual(page.locator('[data-target="@keep"] .target-label').inner_text(), "Keep in place")
                page.locator('[data-target="@keep"] .target-port').click()
                self.assertIn('"linked":1', page.evaluate("JSON.stringify(window.foState())"))

                page.locator("#reviewButton").click()
                self.assertIn("Unassigned", page.locator("#reviewText").inner_text())
                page.locator("#review form button").click()
                page.locator("#language").select_option("zh-CN")
                self.assertEqual(page.locator("#groupSearch").get_attribute("placeholder"), "分组或来源")
                self.assertEqual(page.evaluate("window.foState().linked"), 1)
                self.assertEqual(card.locator("h3").inner_text(), "Sample Set")

                card.locator("button.port.group-port[data-source]").click()
                page.locator("#groupSearch").fill("no matching group")
                self.assertIn("已取消选择", page.locator("#hint").inner_text())
                archive_target.first.locator(".target-port").click()
                self.assertEqual(page.evaluate("window.foState().linked"), 1)
                page.locator("#groupSearch").fill("Sample Set")

                page.locator("#reviewButton").click()
                self.assertIn("待连", page.locator("#reviewText").inner_text())
                page.locator("#review form button").click()
                # The ordinary group action fills the unassigned unit while
                # retaining the first unit's explicit Keep assignment.
                card.locator("button.port.group-port[data-source]").click()
                page.locator("#targetSearch").fill("archive")
                archive_target.first.locator(".target-port").click()
                state = page.evaluate("window.foState()")
                self.assertEqual(state["linked"], 2)
                self.assertCountEqual(list(state["placements"].values()), ["@keep", str(root / "archive")])

                # Exercise the visible action history controls without relying on internals.
                undo = page.locator("#undoButton")
                redo = page.locator("#redoButton")
                undo.click()
                self.assertEqual(page.evaluate("window.foState().linked"), 1)
                redo.click()
                self.assertEqual(page.evaluate("window.foState().linked"), 2)

                card.locator('button[data-action="override-group"]').click()
                page.once("dialog", lambda dialog: dialog.accept())
                archive_target.first.locator(".target-port").click()
                self.assertEqual(list(page.evaluate("Object.values(window.foState().placements)")),
                                 [str(root / "archive")] * 2)
                undo.click()
                self.assertCountEqual(page.evaluate("Object.values(window.foState().placements)"),
                                      ["@keep", str(root / "archive")])

                page.locator("#reviewButton").click()
                root_arg = str(root).replace("'", "''") if os.name == "nt" else str(root).replace("'", "'\"'\"'")
                self.assertIn(f"tidy-file-tree apply '{root_arg}' --actions", page.locator("#previewCmdText").inner_text())
                correction = page.locator(".correction-row").filter(has=page.locator('[data-correction="what"]'))
                self.assertEqual(correction.count(), 1)
                correction.locator('[data-correction="what"]').fill("user-confirmed retained sample")
                with page.expect_download() as correction_download:
                    page.locator("#downloadCorrections").click()
                corrections_file = Path(temp) / "corrections.tsv"
                correction_download.value.save_as(corrections_file)
                correction_text = corrections_file.read_text(encoding="utf-8")
                self.assertIn("user-confirmed retained sample", correction_text)
                self.assertIn("Sample Set", correction_text)
                self.assertIn("incoming", correction_text)
                self.assertNotIn(str(a), correction_text)
                with page.expect_download() as download_info:
                    page.locator("#downloadActions").click()
                action_file = Path(temp) / "actions.tsv"
                self.assertEqual(download_info.value.suggested_filename, "tidy-file-tree-actions.tsv")
                download_info.value.save_as(action_file)
                self.assertIn("part-b.bin", action_file.read_text(encoding="utf-8"))
                self.assertNotIn("part-a.bin", action_file.read_text(encoding="utf-8"))
                receipt = Path(temp) / "receipt.json"
                cli = lambda *args: subprocess.run([sys.executable, "-m", "tidy_file_tree", *map(str, args)],
                                                    cwd=temp, env=env, check=True, capture_output=True,
                                                    text=True, encoding="utf-8")
                cli("apply", root, "--actions", action_file, "--dry-run", "--receipt", receipt)
                cli("apply", root, "--actions", action_file, "--receipt", receipt)
                self.assertTrue(a.exists())
                self.assertFalse(b.exists())
                self.assertEqual(hashlib.sha256(a.read_bytes()).hexdigest(), originals[a])
                self.assertEqual(hashlib.sha256((root / "archive" / b.name).read_bytes()).hexdigest(), originals[b])
                cli("undo", "--journal", root / "_organize" / "journal.tsv", "--last")
                self.assertEqual(hashlib.sha256(a.read_bytes()).hexdigest(), originals[a])
                self.assertEqual(hashlib.sha256(b.read_bytes()).hexdigest(), originals[b])
                browser.close()

    def test_saved_wires_expire_when_a_source_is_replaced(self):
        with tempfile.TemporaryDirectory(prefix=".tidy-file-tree-wire-", dir=PROJECT) as temp:
            workspace = Path(temp)
            root = workspace / "library"
            source = root / "incoming"
            archive = root / "archive"
            probe = root / "_organize" / "probe"
            for folder in (probe, source, archive):
                folder.mkdir(parents=True)
            (archive / "existing.txt").touch()
            replaced = source / "replace.bin"
            stable = source / "stable.bin"
            for item in (replaced, stable):
                item.write_bytes(b"reviewed")
            (probe / "units_wire.json").write_text(json.dumps({
                "path": str(source), "total_units": 2,
                "loose_files_as_units": [replaced.name, stable.name],
            }), encoding="utf-8")
            (probe / "group_wire.json").write_text(json.dumps({
                "path": str(source), "groups": [{"name": "Wire Set", "units": [replaced.name, stable.name]}],
            }), encoding="utf-8")
            board = workspace / "review.html"
            snapshot = workspace / "decisions.json"
            fingerprint = group_key({"name": "Wire Set", "units": [replaced.name, stable.name]})
            snapshot.write_text(json.dumps({
                "root": str(root), "nodes": [{"path": str(source), "groups": [{
                    "id": f"{fingerprint}:0", "fingerprint": fingerprint,
                    "suggest": {"mode": "new", "newName": "Transient Suggestion", "needsCheck": True},
                }]}],
            }), encoding="utf-8")
            env = os.environ.copy()
            if env.get("TIDY_FILE_TREE_BROWSER_USE_INSTALLED") != "1":
                env["PYTHONPATH"] = str(PROJECT) + os.pathsep + env.get("PYTHONPATH", "")
            env["LOCALAPPDATA"] = str(workspace / "appdata")
            env["XDG_STATE_HOME"] = str(workspace / "state")

            def regenerate():
                command = [sys.executable, "-m", "tidy_file_tree", "ui", str(root),
                           "--out", str(board), "--lang", "en"]
                if snapshot.exists():
                    command.extend(["--decisions", str(snapshot)])
                result = subprocess.run(command, cwd=temp, env=env, capture_output=True, text=True, encoding="utf-8")
                self.assertEqual(result.returncode, 0, result.stderr)

            regenerate()
            with sync_playwright() as playwright:
                chrome = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
                edge = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
                executable = chrome if chrome.is_file() else edge if edge.is_file() else None
                browser = playwright.chromium.launch(executable_path=str(executable) if executable else None)
                page = browser.new_page(accept_downloads=True)
                page.set_default_timeout(5000)
                page.goto(board.as_uri())
                automatic_target = page.get_by_role("button", name="Assign to Transient Suggestion", exact=True)
                expect(automatic_target).to_be_visible()
                page.locator("article.card button.group-port[data-source]").click()
                custom_label = "personal $tick`'s"
                page.locator("#newTargetInput").fill(custom_label)
                page.locator("#newTargetForm button[type=submit]").click()
                custom_target = page.locator("[data-target]").filter(has=page.locator(".target-label", has_text=custom_label))
                custom_target.first.locator(".target-port").click()
                self.assertEqual(page.evaluate("window.foState().linked"), 2)
                snapshot.unlink()
                regenerate()
                page.reload()
                self.assertEqual(page.evaluate("window.foState().linked"), 2)
                self.assertEqual(automatic_target.count(), 0)
                custom_target = page.locator("[data-target]").filter(has=page.locator(".target-label", has_text=custom_label))
                self.assertTrue(custom_target.is_visible())

                replacement = workspace / "new.bin"
                replacement.write_bytes(b"a different file at the same path")
                replacement.replace(replaced)
                regenerate()
                page.reload()
                self.assertEqual(page.evaluate("window.foState().linked"), 1)
                self.assertIn("1 invalidated saved assignment", page.locator("#staleMetric").inner_text())
                page.locator("#reviewButton").click()
                with page.expect_download() as download_info:
                    page.locator("#downloadActions").click()
                actions = download_info.value.path().read_text(encoding="utf-8")
                self.assertIn(str(stable), actions)
                self.assertNotIn(str(replaced), actions)
                page.locator("#review form button").click()
                page.locator("article.card button.group-port[data-source]").click()
                custom_target.first.locator(".target-port").click()
                self.assertEqual(page.evaluate("window.foState().linked"), 2)
                self.assertTrue(page.locator("#staleMetric").is_hidden())
                page.reload()
                self.assertEqual(page.evaluate("window.foState().linked"), 2)
                browser.close()


if __name__ == "__main__":
    unittest.main()
