#!/usr/bin/env python3
"""
TidyFileTree — 根据目录结构整理文件

统一入口。子命令对应六个阶段：

    tidy-file-tree scan     <root>            观察一个目录的信号（S1/S2 的原料）
    tidy-file-tree coverage <root>            覆盖检查：有没有节点被跳过（S6）
    tidy-file-tree plan     <root>            从探针结果生成待批清单（S4）
    tidy-file-tree apply    <root> [--actions] 执行
    tidy-file-tree undo     <root>            逆序撤销
    tidy-file-tree recover  <root>            中断恢复
    tidy-file-tree ui       <root>            生成决策界面
    tidy-file-tree history  <root>            查看执行历史

也可以直接用 core/ 下的模块：
    python core/signals.py <dir> --json
    python core/coverage.py <root>
    python core/ui.py --root <root>

设计边界：不删除、不猜、一切经 journal 可回滚。
"""
import argparse
import importlib.util
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CORE = HERE / "core"

# core/ 下的模块互相用扁平导入（import decisions / from safety import ...），
# 所以 core/ 必须在 sys.path 上。
# 源码目录下能跑是因为当前工作目录恰好包含它——那是隐式依赖，
# wheel 安装到别处就断了。显式加进来。
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))
# rules/default.json 按 HERE/"rules" 查找，安装后仍然是同目录，不用改。
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))


def _load(name, files):
    """按文件名加载 core 下的模块。

    不要求 core/ 是包——用 spec_from_file_location 直接加载。
    """
    path = CORE / files
    if not path.is_file():
        sys.exit(f"缺少 {files}（在 {CORE} 下找不到）")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def cmd_scan(args):
    if args.tree:
        from safety import SafetyError
        if args.json or len(args.paths) > 1:
            raise SafetyError('--tree 每次只查看一个目录，不能与 --json 同用')
        scanner = _load('scan_tree', 'scan_tree.py')
        return scanner.main(args.paths[0] if args.paths else os.getcwd(), args.depth,
                            args.lang, open_browser=not args.no_open)
    sig = _load("signals", "signals.py")
    targets = args.paths or [os.getcwd()]
    results = [sig.analyze(p) for p in targets if os.path.isdir(p)]
    if args.json:
        import json
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for r in results:
            print(sig.fmt(r))
    return 0


def cmd_coverage(args):
    cov = _load("coverage", "coverage.py")
    from safety import organize_dir
    probe = Path(args.probe) if args.probe else organize_dir(args.root) / "probe"
    rows = cov.check(args.root, probe)
    bad = [r for r in rows if r.get("status") not in ("ok", "stale")]
    if args.json:
        import json
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        for r in rows:
            st = r.get("status", "ok")
            label = {"ok": "ok", "stale": "过时", "MISSING": "遗漏"}.get(st, st)
            detail = r.get("missing") or [r.get("reason") or ""]
            detail = " / ".join(str(x) for x in detail if x)
            print(f"{r.get('node', '-')}: {label} {detail}".rstrip())
        print()
        print(f"共 {len(rows)} 个节点，遗漏 {len(bad)} 个")
    return 0 if not bad else 1


def cmd_plan(args):
    ex = _load("executor", "executor.py")
    argv = ["plan", "--root", args.root]
    if args.probe:
        argv += ["--probe", args.probe]
    if args.policy:
        argv += ["--policy", args.policy]
    if args.out:
        argv += ["--out", args.out]
    if args.dry_run:
        argv.append("--dry-run")
    if args.allow_system_drive:
        argv.append("--allow-system-drive")
    return ex.main(argv)


def cmd_apply(args):
    ex = _load("executor", "executor.py")
    argv = ["apply", "--root", args.root]
    if args.actions:
        argv += ["--actions", args.actions]
    elif args.plan:
        argv += ["--plan", args.plan]
    elif args.rename:
        argv += ["--rename"] + list(args.rename)
    else:
        sys.exit("apply 需要 --actions、--plan 或 --rename 之一")
    if args.reason != "-":
        argv += ["--reason", args.reason]
    if args.dry_run:
        argv.append("--dry-run")
    if args.receipt:
        argv += ["--receipt", args.receipt]
    if args.preview_html:
        argv += ["--preview-html", args.preview_html, "--lang", args.lang]
    if args.batch_size:
        argv += ["--batch-size", str(args.batch_size)]
    if args.allow_cross_drive:
        argv.append("--allow-cross-drive")
    if args.allow_system_drive:
        argv.append("--allow-system-drive")
    for destination_root in args.destination_root:
        argv += ["--destination-root", destination_root]
    return ex.main(argv)


def cmd_undo(args):
    ex = _load("executor", "executor.py")
    argv = ["undo", "--journal", args.journal]
    if args.run_id:
        argv += ["--run-id", args.run_id]
    elif args.last:
        argv.append("--last")
    elif args.seq is not None:
        argv += ["--seq", str(args.seq)]
    elif args.list:
        argv.append("--list")
    else:
        sys.exit("undo 需要 --run-id、--last、--seq 或 --list 之一")
    if args.dry_run:
        argv.append("--dry-run")
    if args.allow_system_drive:
        argv.append("--allow-system-drive")
    return ex.main(argv)


def cmd_recover(args):
    ex = _load("executor", "executor.py")
    argv = ["recover", "--root", args.root]
    if args.allow_system_drive:
        argv.append("--allow-system-drive")
    return ex.main(argv)


def cmd_history(args):
    history = _load("history", "history.py")
    return history.main(args)


def cmd_ui(args):
    ui = _load("ui", "ui.py")
    argv = ["--root", args.root, "--lang", args.lang]
    if args.probe:
        argv += ["--probe", args.probe]
    if args.decisions:
        argv += ["--decisions", args.decisions]
    if args.out:
        argv += ["--out", args.out]
    if args.open:
        argv.append("--open")
    if args.allow_system_drive:
        argv.append("--allow-system-drive")
    old = sys.argv
    sys.argv = ["ui"] + argv
    try:
        return ui.main()
    finally:
        sys.argv = old


def cmd_decisions(args):
    dec = _load("decisions", "decisions.py")
    argv = ["--root", args.root]
    if args.need_ai:
        argv.append("--need-ai")
    if args.answers:
        argv += ["--answers", args.answers]
    if args.out:
        argv += ["--out", args.out]
    if args.probe:
        argv += ["--probe", args.probe]
    if args.allow_system_drive:
        argv.append("--allow-system-drive")
    old = sys.argv
    sys.argv = ["decisions"] + argv
    try:
        return dec.main()
    finally:
        sys.argv = old


def cmd_verify(args):
    ver = _load("verify", "verify.py")
    if args.action == "run":
        argv = ["run", args.path or "cases/"]
        if args.verbose:
            argv.append("--verbose")
    elif args.action == "record":
        if not args.root or not args.out:
            sys.exit("record 需要 --root 和 --out")
        argv = ["record", args.root, args.out]
    else:  # coverage
        if not args.root:
            sys.exit("coverage 需要 --root")
        argv = ["coverage", args.root]
    old = sys.argv
    sys.argv = ["verify"] + argv
    try:
        return ver.main(argv)
    finally:
        sys.argv = old


def cmd_rules(args):
    r = _load("rules", "rules.py")
    if args.action == "show":
        argv = ["show"]
        if args.file:
            argv += ["--file", args.file]
    elif args.action == "check":
        if not args.file:
            sys.exit("check 需要 --file")
        argv = ["check", args.file]
    else:  # sync
        argv = ["sync"]
        if args.url:
            argv += ["--url", args.url]
    old = sys.argv
    sys.argv = ["rules"] + argv
    try:
        return r.main(argv)
    finally:
        sys.argv = old


def cmd_setup(args):
    su = _load("setup", "setup.py")
    argv = [args.action]
    if args.action == "apply":
        ans = getattr(args, "answers_pos", None) or getattr(args, "answers", None)
        if not ans:
            sys.exit("用法: tidy-file-tree setup apply <root> <answers.json>")
        argv += [args.root, ans]
        if getattr(args, "allow_system_drive", False):
            argv.append("--allow-system-drive")
    else:
        argv.append(args.root)
    old = sys.argv
    sys.argv = ["setup"] + argv
    try:
        return su.main(argv)
    finally:
        sys.argv = old


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    # Choose the help language before argparse formats any help text.
    languages = ("zh-CN", "en")
    language_parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    language_parser.add_argument("--lang", default="zh-CN")
    requested, _ = language_parser.parse_known_args(argv)

    def text(zh, en):
        return en if requested.lang == "en" else zh

    language_help = text("帮助与界面语言", "Language for CLI help and the interface")
    p = argparse.ArgumentParser(
        prog="tidy-file-tree",
        description=text(__doc__, "TidyFileTree groups files by directory structure. "
                         "Review and preview a plan before moving files."),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--lang", choices=languages, default="zh-CN", help=language_help)
    sub = p.add_subparsers(dest="command", required=True)

    def command(name, zh, en):
        description = text(zh, en)
        return sub.add_parser(name, help=description, description=description)

    sys_drive_help = text(
        "显式允许整理系统盘根目录（如 C:\\），状态写入用户目录而非 C:\\_organize",
        "Explicitly allow organizing the system drive root (state is stored in user app data)",
    )

    s = command("scan", "观察目录的信号（只读）", "Inspect directory structure (read-only)")
    s.add_argument("paths", nargs="*")
    s.add_argument("--json", action="store_true")
    s.add_argument("--tree", action="store_true", help=text("在浏览器查看实时目录判断树", "Open a live directory assessment tree"))
    s.add_argument("--depth", type=int, default=3, help=text("目录树最大深度，默认 3", "Maximum tree depth; defaults to 3"))
    s.add_argument("--no-open", action="store_true", help=text("树模式只输出本地地址", "Print the local tree URL without opening a browser"))
    s.add_argument("--lang", choices=languages, default=argparse.SUPPRESS, help=language_help)
    s.set_defaults(func=cmd_scan)

    s = command("coverage", "覆盖检查", "Check for directories missing review records")
    s.add_argument("root")
    s.add_argument("--probe")
    s.add_argument("--json", action="store_true")
    s.add_argument("--allow-system-drive", action="store_true", help=sys_drive_help)
    s.set_defaults(func=cmd_coverage)

    s = command("plan", "生成待批清单", "Generate an action plan")
    s.add_argument("root")
    s.add_argument("--probe")
    s.add_argument("--policy", help=text("计划的审批策略，可指定额外的禁区",
                                        "Approval policy and optional additional protected paths"))
    s.add_argument("--out")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--allow-system-drive", action="store_true", help=sys_drive_help)
    s.set_defaults(func=cmd_plan)

    s = command("apply", "执行", "Preview or apply file actions")
    s.add_argument("root")
    s.add_argument("--actions")
    s.add_argument("--plan")
    s.add_argument("--rename", nargs=2)
    s.add_argument("--reason", default="-")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--receipt", help=text("预演时写入新的 JSON 清单，执行时核对同一清单",
                                         "Write a JSON receipt during preview and verify it when applying"))
    s.add_argument("--preview-html", help=text("将 --dry-run 结果保存为图形预演页面",
                                              "Save --dry-run results as an offline preview page"))
    s.add_argument("--lang", choices=languages, default=argparse.SUPPRESS, help=language_help)
    s.add_argument("--batch-size", type=int, default=50)
    s.add_argument("--allow-cross-drive", action="store_true")
    s.add_argument("--destination-root", action="append", default=[],
                   help=text("显式授权的外部目标根目录，可重复指定",
                             "Authorized external destination root; may be repeated"))
    s.add_argument("--allow-system-drive", action="store_true", help=sys_drive_help)
    s.set_defaults(func=cmd_apply)

    s = command("undo", "逆序撤销", "Undo recorded actions in reverse order")
    s.add_argument("--journal", required=True)
    s.add_argument("--run-id")
    s.add_argument("--last", action="store_true")
    s.add_argument("--seq", type=int)
    s.add_argument("--list", action="store_true")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--allow-system-drive", action="store_true", help=sys_drive_help)
    s.set_defaults(func=cmd_undo)

    s = command("recover", "中断恢复", "Recover interrupted actions")
    s.add_argument("root")
    s.add_argument("--allow-system-drive", action="store_true", help=sys_drive_help)
    s.set_defaults(func=cmd_recover)

    s = command("history", "生成只读执行历史", "Generate read-only execution history")
    s.add_argument("root")
    s.add_argument("--out", help=text("输出 HTML，默认写入 _organize/history.html",
                                      "Output HTML; defaults to _organize/history.html"))
    s.add_argument("--open", action="store_true")
    s.add_argument("--lang", choices=languages, default=argparse.SUPPRESS, help=language_help)
    s.add_argument("--allow-system-drive", action="store_true", help=sys_drive_help)
    s.set_defaults(func=cmd_history)

    s = command("setup", "整理前的一次性确认", "Configure sorting preferences before organizing")
    s.add_argument("action", choices=("ask", "apply", "show"))
    s.add_argument("root")
    s.add_argument("answers_pos", nargs="?", help=text("apply 时的答案文件", "Answer file for setup apply"))
    s.add_argument("--allow-system-drive", action="store_true", help=sys_drive_help)
    s.set_defaults(func=cmd_setup)

    s = command("decisions", "算建议 / 生成 AI 问题 / 回填答案",
                "Suggest destinations, export AI questions, and import AI answers")
    s.add_argument("root")
    s.add_argument("--probe")
    s.add_argument("--out")
    s.add_argument("--need-ai", action="store_true",
                   help=text("只输出需要 AI 判断的问题清单", "Output only questions that need AI review"))
    s.add_argument("--answers", help=text("AI 判断结果 JSON，回填", "Import AI answers from JSON"))
    s.add_argument("--allow-system-drive", action="store_true", help=sys_drive_help)
    s.set_defaults(func=cmd_decisions)

    s = command("verify", "回归验证", "Run classification checks")
    s.add_argument("action", choices=("run", "record", "coverage"))
    s.add_argument("path", nargs="?", help=text("run 时的用例路径", "Test case file for verify run"))
    s.add_argument("--root", help=text("record/coverage 的盘根", "Root directory for record or coverage"))
    s.add_argument("--out", help=text("record 的输出文件", "Output file for verify record"))
    s.add_argument("--verbose", action="store_true")
    s.set_defaults(func=cmd_verify)

    s = command("rules", "规则包", "Show, validate, or sync the rule pack")
    s.add_argument("action", choices=("show", "check", "sync"))
    s.add_argument("--file")
    s.add_argument("--url")
    s.set_defaults(func=cmd_rules)

    s = command("ui", "生成决策界面", "Generate the standalone review board")
    s.add_argument("root")
    s.add_argument("--probe")
    s.add_argument("--decisions", help=text("S4 decisions.json，默认读取根目录的阶段产物",
                                            "S4 decisions file; defaults to the root's _organize/decisions.json"))
    s.add_argument("--out")
    s.add_argument("--open", action="store_true")
    s.add_argument("--lang", choices=languages, default=argparse.SUPPRESS, help=language_help)
    s.add_argument("--allow-system-drive", action="store_true", help=sys_drive_help)
    s.set_defaults(func=cmd_ui)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except (OSError, ValueError) as exc:
        from safety import SafetyError  # noqa: F401  延迟导入，允许模块缺失

        if isinstance(exc, SafetyError):
            print(f"停止: {exc}", file=sys.stderr)
        else:
            print(f"停止: {type(exc).__name__}；请检查日志并运行 recover",
                  file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.path.insert(0, str(CORE))
    sys.exit(main())
