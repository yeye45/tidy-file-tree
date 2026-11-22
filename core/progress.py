"""Live progress snapshots and local workbench drafts for stages S0-S6."""
from argparse import Namespace
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
from threading import Lock
import threading
import time
import webbrowser

from coverage import SKIP_NAMES, grouping_coverage, unit_count, valid_action
from executor import (ACTION_FIELDS, PLAN_REVIEW_FORMAT, TRACKED_PLANS, WORKBENCH_PLAN, Journal, apply,
                      actions_digest, active_events, artifact_has_signature,
                      check_artifact_path, completed, cross_scopes, digest, directory_identity, guard, meta,
                      plan_review_path, read_journal, receipt_path, rename_target, write_artifact)
import rules
from safety import (SafetyError, USER_SHELL_FOLDERS, assert_no_links, block_kind,
                    encode_tsv, env_dependency_reason, is_reparse, is_system_attr, is_system_root,
                    is_user_profile_root, key, nested_policy_verdict, nested_verdict, normal, organize_dir,
                    read_tsv, toolchain_marker_reason, within)

TEMPLATE = Path(__file__).with_name('progress_ui.html')
QUARANTINE_NAMES = {'_待删除', '_pending_delete'}
RECENT_LIMIT = 30
NODE_SCAN_LIMIT = 1500
WORKBENCH_LIMIT = 4 * 1024 * 1024


def _iso_mtime(path):
    try:
        ts = os.stat(path, follow_symlinks=False).st_mtime
        return datetime.fromtimestamp(ts, timezone.utc).isoformat()
    except OSError:
        return None


def _safe_read_json(path):
    """Best-effort JSON read resilient to concurrent writes by subagents."""
    try:
        assert_no_links(path)
        data = json.loads(Path(path).read_text(encoding='utf-8-sig'))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _rel_label(path, root):
    try:
        rel = os.path.relpath(normal(path), normal(root))
        return '.' if rel == '.' else rel.replace('\\', '/')
    except ValueError:
        return str(path)


def _count_tsv_rows(path):
    try:
        assert_no_links(path)
        with open(path, encoding='utf-8-sig') as f:
            lines = [line for line in f if line.strip()]
        return max(0, len(lines) - 1)
    except (OSError, ValueError):
        return 0


def _review_artifact(record, field):
    try:
        value = record[field]
        path = receipt_path(value) if field == 'receipt' else value
        path = check_artifact_path(path)
        if path.is_file() and digest(path.read_bytes()) == record.get(field + '_sha256'):
            return str(path)
    except (KeyError, TypeError, OSError, ValueError):
        pass
    return None


def _plan_review(journal, plan, done_rows):
    record_path = plan_review_path(journal, plan)
    record = _safe_read_json(record_path) if record_path.is_file() else None
    if not record or record.get('format') != PLAN_REVIEW_FORMAT:
        return None
    try:
        actions = read_tsv(plan, ACTION_FIELDS)
        if (record.get('root_id') != directory_identity(journal.root)
                or record.get('actions_sha256') != actions_digest(actions)):
            return None
        count = record.get('count')
        phase = record.get('phase')
        if type(count) is not int or not 0 <= count <= len(actions):
            return None
        if phase == 'blocked':
            return {'status': phase, 'count': count, 'preview': _review_artifact(record, 'preview')}
        if phase == 'draft':
            return {'status': phase, 'count': count}
        if phase == 'no_changes' and count == 0:
            return {'status': phase, 'count': 0}
        if phase == 'previewed':
            receipt = _review_artifact(record, 'receipt')
            if receipt:
                return {'status': phase, 'count': count, 'receipt': receipt,
                        'preview': _review_artifact(record, 'preview')}
            return {'status': 'draft', 'count': len(actions)}
        elif phase in ('executing', 'executed', 'partial') and isinstance(record.get('run'), str):
            did_run = any(row['run_id'] == record['run'] for row in done_rows)
            done = record.get('done')
            if phase == 'executed' and did_run and type(done) is int and done == count and count > 0:
                return {'status': phase, 'count': count, 'done': done}
            if phase == 'partial' and type(done) is int and 0 <= done < count and (did_run or done == 0):
                return {'status': phase, 'count': count, 'done': done}
            return {'status': 'executing', 'count': count}
    except (KeyError, TypeError, OSError, ValueError):
        pass
    return None


def _submit_workbench(journal, rows, probe=None):
    if not isinstance(rows, list) or not rows:
        raise SafetyError('动作表为空')
    root = journal.root
    with journal.locked(prepare=False):
        protection = journal.protection(include_skips=True, probe=Path(probe) if probe else None)
        guard(root, root, protection, allow_root=True)
        if journal.unresolved():
            raise SafetyError('有未完成事务')
        actions = []
        for row in rows:
            if (not isinstance(row, list) or len(row) != len(ACTION_FIELDS)
                    or any(not isinstance(value, str) or '\x00' in value for value in row)):
                raise SafetyError('动作格式无效')
            op, source, target, _ = row
            if (op not in ('move', 'rename') or not os.path.isabs(source)
                    or (op == 'move' and not os.path.isabs(target))):
                raise SafetyError('实时工作台只接收整理根内的移动和改名')
            guard(source, root, protection)
            if block_kind(source, protection.markers, descendants=True):
                raise SafetyError('来源含有受保护路径')
            if op == 'move':
                guard(target, root, protection, allow_container=True)
            else:
                rename_target(source, target, root, protection)
            actions.append(dict(zip(ACTION_FIELDS, row)))
        plan = check_artifact_path(journal.state_dir / WORKBENCH_PLAN)
        data = encode_tsv(actions, ACTION_FIELDS)
        existing = plan.read_bytes() if plan.is_file() else None
        legacy_mtime = _iso_mtime(journal.state_dir / 'plan.tsv')
        if data != existing or (legacy_mtime or '') > (_iso_mtime(plan) or ''):
            def owns_plan(path):
                return bool(read_tsv(path, ACTION_FIELDS))

            write_artifact(plan, data, owns_existing=owns_plan)
        return len(actions)


def _workbench_preview(journal, lang):
    """Use the executor's dry run and receipt checks for browser previews."""
    draft = journal.state_dir / WORKBENCH_PLAN
    folder = check_artifact_path(journal.mirror_dir / 'workbench-previews' / secrets.token_hex(12))
    folder.mkdir(parents=True, exist_ok=False)
    report = folder / 'preview.html'
    try:
        apply(Namespace(root=str(journal.root), actions=str(draft), rename=None, plan=None,
                        dry_run=True, allow_cross_drive=False, batch_size=50, lang=lang,
                        preview_html=str(report), receipt=str(folder / 'receipt.json')))
    except SafetyError:
        review = _plan_review(journal, draft, ())
        if not review or review['status'] != 'blocked' or review.get('preview') != str(report):
            raise
    return 'preview/' + folder.name


def _quarantine_info(root, markers):
    """Inspect holding/quarantine folder counts without deleting or revealing secrets."""
    total = 0
    found_dir = None
    for name in QUARANTINE_NAMES:
        candidate = Path(root) / name
        try:
            if block_kind(candidate, markers, descendants=True):
                continue
            if not candidate.is_dir() or is_reparse(candidate):
                continue
            found_dir = name
            with os.scandir(candidate) as it:
                for idx, entry in enumerate(it):
                    if idx >= 500:
                        break
                    if block_kind(entry.path, markers, descendants=True):
                        continue
                    total += 1
        except (OSError, ValueError):
            continue
    return {'dir': found_dir, 'count': total}


def collect_snapshot(root, probe_override=None):
    """Collect a read-only, secret-safe snapshot of S0-S6 artifacts."""
    journal = Journal(root, allow_system_drive=True)
    root_path = journal.root
    assert_no_links(root_path)
    state_dir = journal.state_dir
    probe_dir = Path(normal(probe_override)) if probe_override else state_dir / 'probe'

    # Load protection markers if readable; never fail just because state doesn't exist yet.
    markers = []
    marker_error = False
    try:
        protection = journal.protection(include_skips=True, probe=probe_dir)
        markers = protection.markers
    except (OSError, ValueError, SafetyError) as exc:
        marker_error = True
        raise SafetyError(f'无法读取保护标记，进度扫描已停止：{exc}') from exc

    secret_markers = [m for m in markers if m['kind'] == 'SECRET']
    if block_kind(root_path, markers):
        raise SafetyError('整理根目录命中凭据保护标记')
    secret_count = len(secret_markers)

    recent = []

    def add_event(stage, kind, label, detail='', ts=None):
        if not ts:
            return
        recent.append({
            'stage': stage,
            'kind': kind,
            'label': label,
            'detail': detail,
            'time': ts,
        })

    # ── S0: policy.json ──
    policy_path = state_dir / 'policy.json'
    policy = {}
    if policy_path.is_file():
        try:
            policy = rules.load_root_policy(root_path)
        except (OSError, ValueError):
            policy = {}
    setup_data = policy.get('setup') if isinstance(policy.get('setup'), dict) else {}
    mapping_data = policy.get('mapping') if isinstance(policy.get('mapping'), dict) else {}
    no_go = policy.get('no_go_zones') if isinstance(policy.get('no_go_zones'), list) else []
    allow_cache_cleanup = bool(policy.get('allow_cache_cleanup', False))
    s0_done = bool(setup_data) or policy_path.is_file()
    if policy_path.is_file():
        add_event('S0', 'setup', 'policy.json', f'{len(setup_data)} 项确认', _iso_mtime(policy_path))

    # ── Discover top-level directories (and expand current user's shell folders on system root) ──
    expected = {}
    protected_skipped = 0
    node_scan_truncated = False
    is_sys = is_system_root(root_path)

    def _register_dir(dir_path):
        nonlocal protected_skipped
        try:
            if is_reparse(dir_path):
                protected_skipped += 1
                return
            if block_kind(dir_path, secret_markers):
                return
            if (not nested_policy_verdict(dir_path, protection.nested_rules, allow_container=True)[0]
                    or is_sys and (is_system_attr(dir_path) or not nested_verdict(dir_path,
                        allow_cache_cleanup=allow_cache_cleanup, allow_container=bool(protection.nested_rules),
                        nested_rules=protection.nested_rules)[0])):
                protected_skipped += 1
                return
            if env_dependency_reason(dir_path, root=root_path, allow_ancestor=True) or toolchain_marker_reason(dir_path):
                protected_skipped += 1
                norm = normal(dir_path)
                expected[key(norm)] = {
                    'path': norm, 'name': _rel_label(norm, root_path),
                    'protected': 'ENV_DEP', 'derived': False, 'enter_child': False,
                }
                return
            norm = normal(dir_path)
            mk = block_kind(norm, markers)
        except (OSError, SafetyError):
            protected_skipped += 1
            return
        if mk:
            protected_skipped += 1
            expected[key(norm)] = {
                'path': norm, 'name': _rel_label(norm, root_path),
                'protected': mk, 'derived': False, 'enter_child': False,
            }
            return
        expected[key(norm)] = {
            'path': norm, 'name': _rel_label(norm, root_path),
            'protected': None, 'derived': False, 'enter_child': False,
        }

    try:
        with os.scandir(root_path) as it:
            for idx, entry in enumerate(it):
                if idx >= NODE_SCAN_LIMIT:
                    node_scan_truncated = True
                    break
                lname = entry.name.casefold()
                if lname in SKIP_NAMES:
                    continue
                if is_reparse(entry.path):
                    protected_skipped += 1
                    continue
                if not entry.is_dir(follow_symlinks=False):
                    continue
                # On system drive root (e.g. C:\), traverse through C:\Users into the active
                # user's KnownFolders (Desktop/Documents/Downloads/Pictures/Music/Videos)
                # instead of hiding Users behind a nested SECRET marker or stopping at depth 1.
                if is_sys and is_system_root(entry.path):
                    try:
                        home = Path(normal(Path.home()))
                    except (OSError, ValueError):
                        home = None
                    if home and home.is_dir() and within(home, entry.path, inclusive=False):
                        try:
                            with os.scandir(home) as hit:
                                for sub in hit:
                                    if (sub.name.casefold() in USER_SHELL_FOLDERS
                                            and sub.is_dir(follow_symlinks=False)):
                                        _register_dir(sub.path)
                            if allow_cache_cleanup:
                                for rel_cache in (
                                    Path('AppData') / 'Local' / 'Temp',
                                    Path('AppData') / 'Local' / 'CrashDumps',
                                    Path('.cache'),
                                    Path('.gradle') / 'caches',
                                ):
                                    cdir = home / rel_cache
                                    if cdir.is_dir() and not is_reparse(cdir):
                                        _register_dir(str(cdir))
                        except OSError:
                            pass
                    continue
                if is_sys and is_user_profile_root(entry.path):
                    try:
                        home = Path(normal(Path.home()))
                    except (OSError, ValueError):
                        home = None
                    if home and key(entry.path) == key(home):
                        try:
                            with os.scandir(entry.path) as hit:
                                for sub in hit:
                                    if (sub.name.casefold() in USER_SHELL_FOLDERS
                                            and sub.is_dir(follow_symlinks=False)):
                                        _register_dir(sub.path)
                        except OSError:
                            pass
                    continue
                _register_dir(entry.path)
    except OSError:
        pass

    # ── S1 / S2 / S3: probe/*.json ──
    probes, units, groups = {}, {}, {}
    probe_errors = 0
    if probe_dir.is_dir():
        try:
            assert_no_links(probe_dir)
            for file in sorted(probe_dir.glob('*.json')):
                if file.name.startswith('dup_'):
                    continue
                obj = _safe_read_json(file)
                if not obj or not isinstance(obj.get('path'), str):
                    probe_errors += 1
                    continue
                raw_path = obj['path']
                try:
                    norm = normal(raw_path if os.path.isabs(raw_path) else os.path.join(root_path, raw_path))
                except (OSError, ValueError):
                    probe_errors += 1
                    continue
                if not within(norm, root_path, inclusive=False):
                    continue
                if (block_kind(norm, secret_markers, descendants=True)
                        or not nested_policy_verdict(norm, protection.nested_rules, allow_container=True)[0]):
                    continue
                k = key(norm)
                mtime = _iso_mtime(file)
                if k not in expected:
                    expected[k] = {
                        'path': norm, 'name': _rel_label(norm, root_path),
                        'protected': block_kind(norm, markers),
                        'derived': True, 'enter_child': False,
                    }
                if file.name.startswith('units_'):
                    try:
                        total_u = unit_count(obj.get('total_units'))
                    except SafetyError:
                        probe_errors += 1
                        continue
                    units[k] = {'total_units': total_u, 'mtime': mtime, 'record': obj}
                    add_event('S2', 'units', expected[k]['name'], f'{total_u}', mtime)
                    children = obj.get('sub_containers') or []
                    if not isinstance(children, list):
                        probe_errors += 1
                        continue
                    for child in children:
                        cval = child.get('path') if isinstance(child, dict) else child
                        if isinstance(cval, str) and cval:
                            try:
                                cnorm = normal(cval if os.path.isabs(cval) else os.path.join(norm, cval))
                                if within(cnorm, root_path, inclusive=False) and not block_kind(cnorm, secret_markers, descendants=True):
                                    ck = key(cnorm)
                                    expected.setdefault(ck, {
                                        'path': cnorm, 'name': _rel_label(cnorm, root_path),
                                        'protected': block_kind(cnorm, markers),
                                        'derived': True, 'enter_child': False,
                                    })
                            except (OSError, ValueError):
                                pass
                elif file.name.startswith('group_'):
                    glist = obj.get('groups')
                    if not isinstance(glist, list):
                        probe_errors += 1
                        continue
                    groups[k] = {'count': len(glist), 'mtime': mtime, 'record': obj}
                    add_event('S3', 'group', expected[k]['name'], f'{len(glist)}', mtime)
                else:
                    action = obj.get('action')
                    if not valid_action(action):
                        probe_errors += 1
                        continue
                    probes[k] = {
                        'action': action,
                        'confidence': str(obj.get('confidence') or ''),
                        'mtime': mtime,
                    }
                    add_event('S1', 'probe', expected[k]['name'], action, mtime)
                    if action == 'enter':
                        children = obj.get('enter_children') or []
                        if not isinstance(children, list):
                            probe_errors += 1
                            continue
                        for child in children:
                            cval = child.get('path') if isinstance(child, dict) else child
                            if isinstance(cval, str) and cval:
                                try:
                                    cnorm = normal(cval if os.path.isabs(cval) else os.path.join(norm, cval))
                                    if within(cnorm, root_path, inclusive=False) and not block_kind(cnorm, secret_markers, descendants=True):
                                        ck = key(cnorm)
                                        item = expected.setdefault(ck, {
                                            'path': cnorm, 'name': _rel_label(cnorm, root_path),
                                            'protected': block_kind(cnorm, markers),
                                            'derived': True, 'enter_child': True,
                                        })
                                        item['enter_child'] = True
                                except (OSError, ValueError):
                                    pass
        except (OSError, ValueError):
            pass

    # Assemble per-node status & S1/S2/S3/S6 counters
    node_rows = []
    s1_expected = s1_done = 0
    s2_expected = s2_done = 0
    s3_expected = s3_done = 0
    total_units = 0
    total_groups = 0
    missing_nodes = 0
    unverified_nodes = 0

    for k, info in sorted(expected.items(), key=lambda pair: pair[1]['name'].casefold()):
        if info['protected']:
            node_rows.append({
                'name': info['name'], 'stage': 'protected', 'action': info['protected'],
                'units': None, 'groups': None, 'updatedAt': None,
            })
            continue
        exists_now = os.path.lexists(info['path'])
        is_directory = exists_now and os.path.isdir(info['path'])
        p_info = probes.get(k)
        u_info = units.get(k) if is_directory else None
        g_info = groups.get(k) if is_directory else None
        action = p_info['action'] if p_info else None
        u_count = u_info['total_units'] if u_info else None
        g_count = g_info['count'] if g_info else None
        if u_count is not None:
            total_units += u_count
        if g_count is not None:
            total_groups += g_count
        updated = max((x for x in (p_info and p_info['mtime'], u_info and u_info['mtime'], g_info and g_info['mtime']) if x), default=None)

        if not exists_now:
            node_rows.append({
                'name': info['name'], 'stage': 'stale', 'action': action,
                'units': u_count, 'groups': g_count, 'updatedAt': updated,
            })
            continue

        needs_s1 = not is_directory or not info['derived'] or info['enter_child']
        if needs_s1:
            s1_expected += 1
            if p_info:
                s1_done += 1
        needs_s2 = is_directory and (action == 'split' or u_info is not None)
        if needs_s2:
            s2_expected += 1
            if u_info:
                s2_done += 1
        needs_s3 = u_info is not None
        if needs_s3:
            s3_expected += 1
            if g_info:
                s3_done += 1

        node_missing = []
        if needs_s1 and not p_info:
            node_missing.append('S1')
        if needs_s2 and not u_info:
            node_missing.append('S2')
        if needs_s3 and not g_info:
            node_missing.append('S3')
        if u_info and g_info:
            try:
                gaps, unverified = grouping_coverage(info['path'], u_info['record'], g_info['record'], markers,
                                                    root=root_path, nested_rules=protection.nested_rules)
                if gaps:
                    node_missing.append('S3')
                elif unverified:
                    unverified_nodes += 1
            except (OSError, ValueError):
                node_missing.append('S3')
        if node_missing:
            missing_nodes += 1

        if g_info:
            stage = 's3_done'
        elif u_info:
            stage = 's2_done'
        elif p_info:
            stage = 's1_split' if needs_s2 else 's1_done'
        else:
            stage = 'pending'

        node_rows.append({
            'name': info['name'],
            'stage': stage,
            'action': action,
            'units': u_count,
            'groups': g_count,
            'missing': node_missing,
            'updatedAt': updated,
        })

    # ── S4: decisions.json, plan.tsv, ui.html ──
    decisions_path = state_dir / 'decisions.json'
    decisions_obj = _safe_read_json(decisions_path) if decisions_path.is_file() else None
    raw_summary = (decisions_obj or {}).get('summary')
    dec_summary = raw_summary if isinstance(raw_summary, dict) else {}
    invalid_artifacts = []

    def _summary_count(field):
        value = dec_summary.get(field, 0)
        integral = type(value) is int or (isinstance(value, float) and value.is_integer())
        if not integral or not 0 <= value < 2**53:
            invalid_artifacts.append('decisions.summary.' + field)
            return 0
        return int(value)

    dec_counts = {field: _summary_count(field) for field in ('total', 'needCheck', 'move', 'keep', 'new')}
    if decisions_path.is_file() and not isinstance(raw_summary, dict):
        invalid_artifacts.append('decisions.summary')
    ai_questions = (decisions_obj or {}).get('aiQuestions', [])
    if not isinstance(ai_questions, list):
        invalid_artifacts.append('decisions.aiQuestions')
        ai_questions = []
    ai_answered = sum(1 for q in ai_questions if isinstance(q, dict) and q.get('aiJudgement'))
    plan_path = max((state_dir / name for name in TRACKED_PLANS),
                    key=lambda path: _iso_mtime(path) or '')
    plan_count = _count_tsv_rows(plan_path) if plan_path.is_file() else 0
    ui_path = state_dir / 'ui.html'
    ui_ready = ui_path.is_file()
    if decisions_path.is_file():
        add_event('S4', 'decisions', 'decisions.json', f"{dec_counts['total']}", _iso_mtime(decisions_path))
    if plan_path.is_file():
        add_event('S4', 'plan', plan_path.name, f'{plan_count}', _iso_mtime(plan_path))
    if ui_ready:
        add_event('S4', 'ui', 'ui.html', 'ready', _iso_mtime(ui_path))

    # ── S5: journal.tsv, transactions, history.html ──
    journal_rows = []
    journal_error = False
    if journal.path.is_file():
        try:
            journal_rows = read_journal(journal.path)
        except (OSError, ValueError):
            journal_error = True
    done_rows = completed(journal_rows)
    plan_review = _plan_review(journal, plan_path, done_rows) if plan_count else None
    active_seqs = active_events(journal_rows)
    external_markers = {}

    def visible_event(row):
        paths = [root_path / row[name] for name in ('before', 'after') if row[name] not in ('', '-')]
        try:
            if any(block_kind(path, markers, descendants=True) for path in paths):
                return False
            if all(within(path, root_path) for path in paths):
                return True
            data = meta(row)
            if data.get('effect') != 'cross-move':
                return False
            scopes = cross_scopes(root_path, data.get('cross'), row['before'], row['after'])
            other = next(scope for scope in scopes if scope != root_path)
            if other not in external_markers:
                external_markers[other] = Journal(other).protection(include_skips=True).markers
            return not any(block_kind(path, external_markers[other], descendants=True) for path in paths)
        except (OSError, ValueError):
            return False

    visible_rows = [row for row in done_rows if visible_event(row)]
    exec_moves = exec_renames = exec_mkdirs = exec_undos = 0
    exec_runs = set()
    for row in visible_rows:
        exec_runs.add(row['run_id'])
        op = row['op']
        if op.startswith('undo-'):
            exec_undos += 1
        elif op == 'move':
            exec_moves += 1
        elif op == 'rename':
            exec_renames += 1
        elif op == 'mkdir':
            exec_mkdirs += 1
    for row in reversed(visible_rows[-12:]):
        target_label = _rel_label(row['after'] if row['after'] != '-' else row['before'], root_path)
        add_event('S5', row['op'], target_label, row['op'], row['ts'])

    unresolved_count = 0
    unresolved_error = journal_error
    try:
        if journal.path.is_file() or journal.tx_dir.is_dir() or (journal.mirror_dir / 'transactions').is_dir():
            unresolved_count = len(journal.unresolved())
    except (OSError, ValueError):
        # An unreadable recovery state is not evidence that recovery is unnecessary.
        unresolved_error = True

    history_path = state_dir / 'history.html'
    history_ready = history_path.is_file()
    if history_ready:
        add_event('S5', 'history', 'history.html', 'ready', _iso_mtime(history_path))

    quarantine = _quarantine_info(root_path, markers)

    # Sort newest activity first
    recent.sort(key=lambda item: item['time'] or '', reverse=True)
    recent = recent[:RECENT_LIMIT]

    # Determine current stage without falsely jumping to S4 on an empty decisions.json
    s4_has_work = bool(
        dec_counts['total'] > 0
        or plan_count > 0
        or (ui_ready and s1_done > 0)
    )
    if unresolved_count or unresolved_error:
        current_stage = 'S5'
    elif done_rows:
        current_stage = 'S6' if (missing_nodes == 0 and unverified_nodes == 0 and probe_errors == 0
                                 and unresolved_count == 0 and not node_scan_truncated) else 'S5'
    elif s4_has_work:
        current_stage = 'S4'
    elif s3_expected > s3_done or s3_done > 0:
        current_stage = 'S3'
    elif s2_expected > s2_done or s2_done > 0:
        current_stage = 'S2'
    elif s1_done > 0 or (s0_done and s1_expected > 0):
        current_stage = 'S1'
    elif s0_done:
        current_stage = 'S0'
    else:
        current_stage = 'idle'

    # A next step is derived from artifacts and counters, never from UI hints alone.
    # `actor` tells the client whether this is automatic work, a human decision, or
    # an informational terminal state. In particular, a generated plan is only a
    # preview/review prompt; it is not proof that the plan was approved.
    last_execution = max((row['ts'] for row in done_rows), default='')
    action_mtime = _iso_mtime(plan_path) if plan_count else None
    has_plan = bool(plan_count and (plan_path.name == WORKBENCH_PLAN or not last_execution
                                    or (action_mtime or '') > last_execution))
    review_status = plan_review['status'] if plan_review else None
    if review_status == 'executed':
        has_plan = False
    no_actions = bool(dec_counts['total'] and dec_counts['keep'] == dec_counts['total']
                      and not dec_counts['move'] and not dec_counts['new'] and not dec_counts['needCheck'])
    latest_review = max((_iso_mtime(file) or '' for file in (decisions_path, ui_path)), default='')
    review_current = s4_has_work and (not last_execution or latest_review > last_execution)
    if unresolved_error:
        next_step = {'id': 'recovery_state_unreadable', 'actor': 'user',
                     'title': '恢复状态无法读取', 'reason': '事务恢复清单读取失败；执行前需要检查状态。'}
    elif unresolved_count:
        next_step = {'id': 'recover_transactions', 'actor': 'auto', 'count': unresolved_count,
                     'title': '核对未完成事务', 'reason': '确认是否仍在执行，已中断的授权操作才进入恢复。'}
    elif review_status == 'executing':
        next_step = {'id': 'check_execution', 'actor': 'auto', 'count': plan_review['count']}
        current_stage = 'S5'
    elif review_status == 'partial':
        next_step = {'id': 'review_partial', 'actor': 'user',
                     'count': plan_review['count'], 'done': plan_review['done']}
        current_stage = 'S5'
    elif review_status == 'no_changes' or (review_status == 'previewed' and not plan_review['count']):
        next_step = {'id': 'no_changes', 'actor': 'none', 'count': 0}
        current_stage = 'S4'
    elif review_status == 'previewed':
        next_step = {'id': 'confirm_preview', 'actor': 'user', 'count': plan_review['count']}
        current_stage = 'S4'
    elif review_status == 'blocked':
        next_step = {'id': 'review_blocked', 'actor': 'user', 'count': plan_review['count']}
        current_stage = 'S4'
    elif has_plan or review_status == 'draft':
        next_step = {'id': 'preview_plan', 'actor': 'auto', 'count': plan_count}
        current_stage = 'S4'
    elif done_rows and not review_current and current_stage == 'S6' and allow_cache_cleanup and quarantine['count']:
        next_step = {'id': 'review_quarantine', 'actor': 'auto', 'count': quarantine['count']}
    elif done_rows and not review_current and current_stage == 'S6':
        next_step = {'id': 'completed', 'actor': 'none', 'count': len(done_rows),
                     'title': '整理已完成', 'reason': '执行记录存在且当前覆盖检查没有发现缺口。'}
    elif done_rows and not review_current:
        next_step = {'id': 'complete_coverage', 'actor': 'auto', 'count': missing_nodes + unverified_nodes,
                     'title': '补齐覆盖检查', 'reason': f'覆盖检查仍有 {missing_nodes} 个缺项、{unverified_nodes} 个未验证项。'}
    elif not s0_done and not s4_has_work and s1_expected:
        next_step = {'id': 'start_analysis', 'actor': 'auto', 'count': s1_expected,
                     'title': '开始扫描与识别', 'reason': f'发现 {s1_expected} 个待检查节点；可以开始自动分析。'}
    elif s1_done < s1_expected and not s4_has_work:
        next_step = {'id': 'continue_classification', 'actor': 'auto', 'count': s1_expected - s1_done,
                     'title': '继续识别目录', 'reason': f'已识别 {s1_done}/{s1_expected} 个待处理节点。'}
    elif s2_done < s2_expected and not s4_has_work:
        next_step = {'id': 'continue_counting', 'actor': 'auto', 'count': s2_expected - s2_done,
                     'title': '继续清点文件单元', 'reason': f'已清点 {s2_done}/{s2_expected} 个需要清点的目录。'}
    elif s3_done < s3_expected and not s4_has_work:
        next_step = {'id': 'continue_grouping', 'actor': 'auto', 'count': s3_expected - s3_done,
                     'title': '继续完成归组', 'reason': f'归组 {s3_done}/{s3_expected}。'}
    elif no_actions:
        next_step = {'id': 'no_changes', 'actor': 'none', 'count': 0,
                     'title': '没有待处理变更', 'reason': '已有决策将所有项目保留，没有拟议的文件操作。'}
    elif s4_has_work and ui_ready:
        next_step = {'id': 'review_plan', 'actor': 'user', 'count': dec_counts['needCheck']}
    elif s4_has_work:
        next_step = {'id': 'prepare_review', 'actor': 'auto', 'count': dec_counts['total']}
    elif s0_done and not s1_expected and not node_scan_truncated:
        next_step = {'id': 'no_changes', 'actor': 'none', 'count': 0,
                     'title': '没有待处理变更', 'reason': '没有拟议操作，也没有执行记录。'}
    elif not s1_expected and not node_scan_truncated:
        next_step = {'id': 'no_data', 'actor': 'none', 'count': 0,
                     'title': '尚无可处理数据', 'reason': '当前扫描范围没有发现可处理节点。'}
    else:
        next_step = {'id': 'continue_analysis', 'actor': 'auto', 'count': s1_expected,
                     'title': '继续分析', 'reason': '当前阶段尚未形成可供审核的方案。'}
    current_stage = {
        'continue_classification': 'S1', 'continue_counting': 'S2', 'continue_grouping': 'S3',
        'review_plan': 'S4', 'prepare_review': 'S4',
    }.get(next_step['id'], current_stage)

    latest_ts = recent[0]['time'] if recent else None
    recently_active = False
    if latest_ts:
        try:
            delta = (datetime.now(timezone.utc) - datetime.fromisoformat(latest_ts.replace('Z', '+00:00'))).total_seconds()
            recently_active = 0 <= delta <= 20
        except ValueError:
            recently_active = False

    return {
        'root': str(root_path),
        'stateDir': str(state_dir),
        'isSystemDrive': is_sys,
        'captured': datetime.now(timezone.utc).isoformat(),
        'currentStage': current_stage,
        'nextStep': next_step,
        'planReview': plan_review,
        'recentlyActive': recently_active,
        'latestEventAt': latest_ts,
        'markerError': marker_error,
        'probeErrors': probe_errors,
        'protectedSkipped': protected_skipped,
        'secretCount': secret_count,
        'quarantine': quarantine,
        'stages': {
            's0': {
                'done': s0_done,
                'answered': len(setup_data),
                'mappings': len(mapping_data),
                'noGoZones': len(no_go),
            },
            's1': {'done': s1_done, 'total': s1_expected},
            's2': {'done': s2_done, 'total': s2_expected, 'units': total_units},
            's3': {'done': s3_done, 'total': s3_expected, 'groups': total_groups},
            's4': {
                'ready': s4_has_work,
                'totalGroups': dec_counts['total'],
                'needCheck': dec_counts['needCheck'],
                'move': dec_counts['move'],
                'keep': dec_counts['keep'],
                'newDirs': dec_counts['new'],
                'invalidArtifactCount': len(set(invalid_artifacts)),
                'aiTotal': len(ai_questions),
                'aiAnswered': ai_answered,
                'planCount': plan_count,
                'uiReady': ui_ready,
            },
            's5': {
                'done': len(visible_rows),
                'active': sum(row['seq'] in active_seqs for row in visible_rows),
                'batches': len(exec_runs),
                'moves': exec_moves,
                'renames': exec_renames,
                'mkdirs': exec_mkdirs,
                'undos': exec_undos,
                'unresolved': unresolved_count,
                'recoveryError': unresolved_error,
                'historyReady': history_ready,
            },
            's6': {
                'covered': max(0, s1_expected - missing_nodes - unverified_nodes),
                'total': s1_expected,
                'missing': missing_nodes,
                'unverified': unverified_nodes,
                'truncated': node_scan_truncated,
                'ok': s1_expected > 0 and missing_nodes == 0 and unverified_nodes == 0
                      and probe_errors == 0 and not node_scan_truncated,
            },
        },
        'scan': {'truncated': node_scan_truncated, 'limit': NODE_SCAN_LIMIT},
        'artifacts': {
            'policy': str(policy_path) if policy_path.is_file() else None,
            'decisions': str(decisions_path) if decisions_path.is_file() else None,
            'ui': str(ui_path) if ui_ready else None,
            'plan': str(plan_path) if plan_path.is_file() else None,
            'journal': str(journal.path) if journal.path.is_file() else None,
            'history': str(history_path) if history_ready else None,
        },
        'nodes': node_rows,
        'recent': recent,
    }


def render_page(config, initial_snapshot=None):
    template = TEMPLATE.read_text(encoding='utf-8')
    cfg_json = json.dumps(config, ensure_ascii=True).replace('<', r'\u003c')
    snap_json = json.dumps(initial_snapshot, ensure_ascii=False).replace('<', r'\u003c') if initial_snapshot is not None else 'null'
    page = template.replace('__PROGRESS_CONFIG__', cfg_json).replace('__PROGRESS_SNAPSHOT__', snap_json)
    return page.encode('utf-8')


def make_server(root, lang='zh-CN', *, probe=None, interval=1.2):
    from ui import board_for_root, render_html

    journal = Journal(root, allow_system_drive=True)
    root = journal.root
    assert_no_links(root)
    token = secrets.token_urlsafe(24)
    page_path = '/' + token + '/'
    interval = max(0.4, interval)
    page = render_page({'lang': lang, 'live': True, 'intervalMs': int(interval * 1000)})
    scan_lock = Lock()
    preview_lock = Lock()
    latest = None
    refresh_at = 0.0

    def preview_url(path):
        if path:
            folder = Path(path).parent
            if folder.parent == journal.mirror_dir / 'workbench-previews':
                return 'preview/' + folder.name
        return None

    def read_snapshot():
        nonlocal latest, refresh_at
        with scan_lock:
            if latest is None or time.monotonic() >= refresh_at:
                try:
                    latest = collect_snapshot(root, probe_override=probe)
                    review = latest.get('planReview')
                    latest['previewUrl'] = preview_url(review.get('preview')) if review else None
                except (OSError, ValueError):
                    latest = {'nextStep': {'id': 'snapshot_unavailable', 'actor': 'user'}}
                refresh_at = time.monotonic() + interval
            return latest

    def active_preview():
        draft = journal.state_dir / WORKBENCH_PLAN
        review = _plan_review(journal, draft, ())
        path = Path(review['preview']) if review and review.get('preview') else None
        if preview_url(path):
            return path
        return None

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def _send_security_headers(self, content_type, length=None):
            self.send_response(200)
            self.send_header('Content-Type', content_type + '; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'same-origin')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header(
                'Content-Security-Policy',
                "default-src 'none'; connect-src 'self'; script-src 'unsafe-inline'; "
                "style-src 'unsafe-inline'; frame-ancestors 'none'",
            )
            if length is not None:
                self.send_header('Content-Length', str(length))
            self.end_headers()

        def do_POST(self):
            nonlocal refresh_at
            host = f'127.0.0.1:{self.server.server_port}'
            if self.headers.get('Host') != host or self.headers.get('Origin') != f'http://{host}':
                self.send_error(403)
                return
            if self.path not in (page_path + 'workbench-actions', page_path + 'workbench-actions?lang=en',
                                 page_path + 'workbench-actions?lang=zh-CN'):
                self.send_error(404)
                return
            if self.headers.get_content_type() != 'application/json':
                self.send_error(415)
                return
            try:
                length = int(self.headers['Content-Length'])
                if not 0 < length <= WORKBENCH_LIMIT:
                    self.send_error(413)
                    return
                self.connection.settimeout(5)
                rows = json.loads(self.rfile.read(length))
                with preview_lock:
                    count = _submit_workbench(journal, rows, probe=probe)
                    preview = None
                    if not is_system_root(root):
                        locale = self.path.split('?lang=')[-1] if '?' in self.path else lang
                        try:
                            preview = _workbench_preview(journal, locale)
                        except (OSError, ValueError):
                            preview = None
            except (OSError, ValueError, TypeError, RecursionError):
                self.send_error(400, 'Could not save this draft')
                return
            with scan_lock:
                refresh_at = 0.0
            body = json.dumps({'count': count, 'preview': preview}).encode('utf-8')
            self._send_security_headers('application/json', len(body))
            self.wfile.write(body)

        def do_GET(self):
            if self.headers.get('Host') != f'127.0.0.1:{self.server.server_port}':
                self.send_error(400)
                return
            if self.path == page_path:
                self._send_security_headers('text/html', len(page))
                self.wfile.write(page)
                return
            if self.path in (page_path + 'workbench', page_path + 'workbench?lang=en', page_path + 'workbench?lang=zh-CN'):
                try:
                    board = board_for_root(journal, probe=probe)
                    locale = self.path.split('?lang=')[-1] if '?' in self.path else lang
                    body = render_html(root, board, lang=locale, live=True).encode('utf-8')
                except (OSError, ValueError):
                    self.send_error(400, 'Could not build the review board')
                    return
                self._send_security_headers('text/html', len(body))
                self.wfile.write(body)
                return
            if self.path.startswith(page_path + 'preview/'):
                try:
                    path = active_preview()
                    if path and self.path == page_path + 'preview/' + path.parent.name:
                        record = _safe_read_json(plan_review_path(journal, journal.state_dir / WORKBENCH_PLAN))
                        body = check_artifact_path(path).read_bytes()
                        if record and record.get('preview') == str(path) and digest(body) == record.get('preview_sha256'):
                            self._send_security_headers('text/html', len(body))
                            self.wfile.write(body)
                            return
                except (OSError, ValueError):
                    pass
                self.send_error(404, 'This preview is no longer current')
                return
            if self.path == page_path + 'snapshot':
                data = read_snapshot()
                body = json.dumps(data, ensure_ascii=False).encode('utf-8')
                self._send_security_headers('application/json', len(body))
                self.wfile.write(body)
                return
            if self.path == page_path + 'events':
                self._send_security_headers('text/event-stream')
                last_digest = None
                last_heartbeat = 0.0
                try:
                    while True:
                        snap = read_snapshot()
                        comparable = {k: v for k, v in snap.items() if k != 'captured'}
                        snapshot_digest = json.dumps(comparable, sort_keys=True, ensure_ascii=True)
                        now_mono = time.monotonic()
                        if snapshot_digest != last_digest:
                            line = 'data: ' + json.dumps(snap, ensure_ascii=False) + '\n\n'
                            self.wfile.write(line.encode('utf-8'))
                            self.wfile.flush()
                            last_digest = snapshot_digest
                            last_heartbeat = now_mono
                        elif now_mono - last_heartbeat >= 10.0:
                            self.wfile.write(b': keepalive\n\n')
                            self.wfile.flush()
                            last_heartbeat = now_mono
                        time.sleep(interval)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    return
            self.send_error(404)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    return server, f'http://127.0.0.1:{server.server_port}{page_path}'


def main(root, *, lang='zh-CN', probe=None, out=None, open_browser=True, interval=1.2):
    if out:
        out_path = Path(out)
        if out_path.suffix.lower() != '.html':
            raise SafetyError('进度页输出文件须使用 .html 扩展名')
        snap = collect_snapshot(root, probe_override=probe)
        html = render_page({'lang': lang, 'live': False}, snap)
        signature = '<title>整理实时进度</title>'.encode('utf-8')
        write_artifact(out_path, html, owns_existing=lambda path: artifact_has_signature(path, signature))
        print(f'Created: {out_path}' if lang == 'en' else f'已生成进度快照：{out_path}')
        if open_browser:
            webbrowser.open(out_path.resolve().as_uri())
        return 0
    server, url = make_server(root, lang, probe=probe, interval=interval)
    with server:
        hint = 'Live progress dashboard ready. Press Ctrl+C to stop.' if lang == 'en' else '实时进度页已就绪。按 Ctrl+C 结束。'
        print(url, flush=True)
        print(hint, flush=True)
        if open_browser:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0
