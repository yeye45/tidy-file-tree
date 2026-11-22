"""Read-only live progress dashboard for TidyFileTree stages S0-S6."""
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import time
import webbrowser

from coverage import SKIP_NAMES, grouping_coverage, unit_count, valid_action
from executor import (Journal, active_events, artifact_has_signature, completed, meta, write_artifact,
                      read_journal)
import rules
from safety import (SafetyError, USER_SHELL_FOLDERS, assert_no_links, block_kind,
                    env_dependency_reason, is_reparse, is_system_attr, is_system_root,
                    is_user_profile_root, key, nested_policy_verdict, nested_verdict, normal, organize_dir,
                    toolchain_marker_reason, within)

TEMPLATE = Path(__file__).with_name('progress_ui.html')
QUARANTINE_NAMES = {'_待删除', '_pending_delete'}
RECENT_LIMIT = 30
NODE_SCAN_LIMIT = 1500


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


def _quarantine_info(root, secrets_markers):
    """Inspect holding/quarantine folder counts without deleting or revealing secrets."""
    total = 0
    found_dir = None
    for name in QUARANTINE_NAMES:
        candidate = Path(root) / name
        try:
            if not candidate.is_dir() or is_reparse(candidate):
                continue
            if block_kind(candidate, secrets_markers, descendants=True):
                continue
            found_dir = name
            with os.scandir(candidate) as it:
                for idx, entry in enumerate(it):
                    if idx >= 500:
                        break
                    if block_kind(entry.path, secrets_markers, descendants=True):
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
    plan_path = state_dir / 'plan.tsv'
    plan_count = _count_tsv_rows(plan_path) if plan_path.is_file() else 0
    ui_path = state_dir / 'ui.html'
    ui_ready = ui_path.is_file()
    if decisions_path.is_file():
        add_event('S4', 'decisions', 'decisions.json', f"{dec_counts['total']}", _iso_mtime(decisions_path))
    if plan_path.is_file():
        add_event('S4', 'plan', 'plan.tsv', f'{plan_count}', _iso_mtime(plan_path))
    if ui_ready:
        add_event('S4', 'ui', 'ui.html', 'ready', _iso_mtime(ui_path))

    # ── S5: journal.tsv, transactions, history.html ──
    journal_rows = []
    if journal.path.is_file():
        try:
            journal_rows = read_journal(journal.path)
        except (OSError, ValueError):
            journal_rows = []
    done_rows = completed(journal_rows)
    active_seqs = active_events(journal_rows)
    exec_moves = exec_renames = exec_mkdirs = exec_undos = 0
    exec_runs = set()
    for row in done_rows:
        if any(row[f] not in ('', '-') and block_kind(root_path / row[f], secret_markers, descendants=True)
               for f in ('before', 'after')):
            continue
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
    for row in reversed(done_rows[-12:]):
        if any(row[f] not in ('', '-') and block_kind(root_path / row[f], secret_markers, descendants=True)
               for f in ('before', 'after')):
            continue
        target_label = _rel_label(row['after'] if row['after'] != '-' else row['before'], root_path)
        add_event('S5', row['op'], target_label, row['op'], row['ts'])

    unresolved_count = 0
    try:
        if journal.tx_dir.is_dir() or (journal.mirror_dir / 'transactions').is_dir():
            unresolved_count = len(journal.unresolved())
    except (OSError, ValueError):
        unresolved_count = 0

    history_path = state_dir / 'history.html'
    history_ready = history_path.is_file()
    if history_ready:
        add_event('S5', 'history', 'history.html', 'ready', _iso_mtime(history_path))

    quarantine = _quarantine_info(root_path, secret_markers)

    # Sort newest activity first
    recent.sort(key=lambda item: item['time'] or '', reverse=True)
    recent = recent[:RECENT_LIMIT]

    # Determine current stage without falsely jumping to S4 on an empty decisions.json
    s4_has_work = bool(
        dec_counts['total'] > 0
        or plan_count > 0
        or (ui_ready and s1_done > 0)
    )
    if done_rows:
        current_stage = 'S6' if (missing_nodes == 0 and unverified_nodes == 0 and probe_errors == 0
                                 and unresolved_count == 0 and not node_scan_truncated) else 'S5'
    elif s4_has_work:
        current_stage = 'S4'
    elif s3_done > 0:
        current_stage = 'S3'
    elif s2_done > 0:
        current_stage = 'S2'
    elif s1_done > 0 or (s0_done and s1_expected > 0):
        current_stage = 'S1'
    elif s0_done:
        current_stage = 'S0'
    else:
        current_stage = 'idle'

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
                'done': len(done_rows),
                'active': len(active_seqs),
                'batches': len(exec_runs),
                'moves': exec_moves,
                'renames': exec_renames,
                'mkdirs': exec_mkdirs,
                'undos': exec_undos,
                'unresolved': unresolved_count,
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
    token = secrets.token_urlsafe(24)
    page_path = '/' + token + '/'
    initial = collect_snapshot(root, probe_override=probe)
    page = render_page({'lang': lang, 'live': True, 'intervalMs': int(interval * 1000)}, initial)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def _send_security_headers(self, content_type, length=None):
            self.send_response(200)
            self.send_header('Content-Type', content_type + '; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header(
                'Content-Security-Policy',
                "default-src 'none'; connect-src 'self'; script-src 'unsafe-inline'; "
                "style-src 'unsafe-inline'; frame-ancestors 'none'",
            )
            if length is not None:
                self.send_header('Content-Length', str(length))
            self.end_headers()

        def do_GET(self):
            if self.headers.get('Host') != f'127.0.0.1:{self.server.server_port}':
                self.send_error(400)
                return
            if self.path == page_path:
                self._send_security_headers('text/html', len(page))
                self.wfile.write(page)
                return
            if self.path == page_path + 'snapshot':
                try:
                    data = collect_snapshot(root, probe_override=probe)
                except (OSError, ValueError) as exc:
                    data = {'error': str(exc) if isinstance(exc, SafetyError) else '无法读取进度状态'}
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
                        try:
                            snap = collect_snapshot(root, probe_override=probe)
                            comparable = {k: v for k, v in snap.items() if k != 'captured'}
                            digest = json.dumps(comparable, sort_keys=True, ensure_ascii=True)
                        except (OSError, ValueError):
                            snap = {'event': 'stopped'}
                            digest = 'stopped'
                        now_mono = time.monotonic()
                        if digest != last_digest:
                            line = 'data: ' + json.dumps(snap, ensure_ascii=False) + '\n\n'
                            self.wfile.write(line.encode('utf-8'))
                            self.wfile.flush()
                            last_digest = digest
                            last_heartbeat = now_mono
                            if digest == 'stopped':
                                return
                        elif now_mono - last_heartbeat >= 10.0:
                            self.wfile.write(b': keepalive\n\n')
                            self.wfile.flush()
                            last_heartbeat = now_mono
                        time.sleep(max(0.4, interval))
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
