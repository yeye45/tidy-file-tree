"""Generate an offline or live read-only view of the execution journal."""
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import islice
import json
import os
from pathlib import Path
import re
import secrets as py_secrets
import subprocess
import sys
import time
from urllib.parse import parse_qs, urlsplit
import webbrowser

import executor as _executor
from executor import Journal, atomic_write, cross_scopes, guard, meta, read_journal
from safety import (SafetyError, assert_no_links, block_kind, is_system_attr,
                    is_system_root, key, normal, within)


TEMPLATE = Path(__file__).with_name('history_ui.html')
SIGNATURE = b'<!doctype html>\n<!-- TidyFileTree execution history -->\n'

MAX_SCAN_DIRS = 120
MAX_ENTRIES_PER_DIR = 250
MAX_TOTAL_ENTRIES = 3500
SCAN_TIME_BUDGET = 1.5
LIVE_LS_LIMIT = 500
LIVE_LS_SCAN_LIMIT = 1000


def operation_kind(op, effect):
    base = re.sub(r'^(undo-)+', '', op)
    inverses = (len(op) - len(base)) // len('undo-')
    if effect == 'cross-move':
        base = 'cross-move'
    if base not in ('move', 'cross-move', 'rename', 'mkdir', 'note'):
        base = 'other'
    return base, ('undo' if inverses % 2 else 'redo') if inverses else ''


def inverse_seq(row, metadata):
    value = metadata.get('inverse_of')
    if not value and row['op'].startswith('undo-'):
        match = re.search(r'撤销 seq (\d+)', row['reason'])
        value = match[1] if match else None
    return int(value) if str(value).isascii() and str(value).isdigit() else None


def _resolve_op_path(root, raw_path):
    """Resolve an operation path (relative or absolute) into a Path object."""
    if not raw_path or raw_path == '-':
        return None
    p = Path(raw_path)
    return p if p.is_absolute() else (Path(root) / p)


def _rel_dir_key(root, directory):
    """Return a normalized POSIX relative directory key ('.' for root), or None if outside root."""
    try:
        if not within(directory, root):
            return None
        rel = os.path.relpath(normal(directory), normal(root))
        if rel == '.':
            return '.'
        return rel.replace('\\', '/')
    except (OSError, ValueError):
        return None


def _read_protection(journal):
    return journal.protection(inherit_cache_cleanup=True, include_skips=True)


def _guard_history_path(path, root, protection, *, descendants=False):
    if any(part.casefold() == '_organize' for part in path.relative_to(root).parts):
        raise SafetyError('整理状态目录不参与历史浏览')
    if descendants and block_kind(path, protection.markers, descendants=True):
        raise SafetyError('路径命中保护标记')
    guard(path, root, protection, allow_root=path == root, allow_container=True)
    if path != root and is_system_attr(path):
        raise SafetyError('命中 Windows 系统属性，禁止浏览')


def _list_dir_safe(target_dir, root, protection, *, limit, offset=0, deadline=None):
    """Return one bounded page, counting filtered entries in the directory offset."""
    _guard_history_path(target_dir, root, protection)
    entries = []
    next_offset = None
    with os.scandir(target_dir) as contents:
        for index, item in enumerate(islice(contents, offset, None), offset):
            if (len(entries) >= limit or index - offset >= LIVE_LS_SCAN_LIMIT or
                    (index > offset and deadline is not None and time.monotonic() >= deadline)):
                next_offset = index
                break
            try:
                _guard_history_path(Path(item.path), root, protection)
                is_dir = item.is_dir(follow_symlinks=False)
                size = 0 if is_dir else item.stat(follow_symlinks=False).st_size
            except (OSError, ValueError):
                continue
            entries.append([item.name, int(is_dir), size])
    entries.sort(key=lambda e: (0 if e[1] else 1, e[0].casefold()))
    return entries, next_offset


def collect_present_entries(journal, protection, visible):
    """Build a bounded, secret-safe snapshot of current directories for the Time-Machine Explorer."""
    root = Path(journal.root)
    queue = deque()
    queued = set()

    def enqueue(path_obj):
        rel = _rel_dir_key(root, path_obj)
        if rel is None:
            return
        k = rel.casefold()
        if k in queued:
            return
        queued.add(k)
        queue.append((rel, Path(normal(path_obj))))

    enqueue(root)
    # Prioritize every historical parent directory and every moved folder's destination inside root.
    for op in visible:
        for field in ('before', 'after'):
            resolved = _resolve_op_path(root, op.get(field))
            if resolved is None:
                continue
            try:
                if within(resolved, root):
                    cur = resolved.parent
                    while within(cur, root):
                        enqueue(cur)
                        if key(cur) == key(root):
                            break
                        cur = cur.parent
                    if field == 'after' and resolved.is_dir():
                        enqueue(resolved)
            except (OSError, ValueError):
                continue

    present = {}
    truncated = []
    scanned_dirs = 0
    total_entries = 0
    deadline = time.monotonic() + SCAN_TIME_BUDGET

    while queue and scanned_dirs < MAX_SCAN_DIRS and total_entries < MAX_TOTAL_ENTRIES:
        if time.monotonic() > deadline:
            break
        rel, dir_path = queue.popleft()
        scanned_dirs += 1
        remaining = min(MAX_ENTRIES_PER_DIR, MAX_TOTAL_ENTRIES - total_entries)
        try:
            entries, next_offset = _list_dir_safe(dir_path, root, protection, limit=remaining, deadline=deadline)
        except (OSError, ValueError):
            continue
        if next_offset is not None:
            truncated.append(rel)
        present[rel] = entries
        total_entries += len(entries)
        # Also enqueue immediate subdirectories of root (depth 1) so first-level browsing is smooth offline.
        if rel == '.':
            for name, is_dir, _ in entries:
                if is_dir and len(queue) < MAX_SCAN_DIRS:
                    enqueue(dir_path / name)

    return present, truncated


def collect_destination_meta(journal, protection, visible, external_protections):
    """Record current existence and file/folder type for visible operation paths (secret-safe)."""
    root = Path(journal.root)
    meta_map = {}
    for op in visible:
        for field in ('after', 'before'):
            raw = op.get(field)
            if not raw or raw in meta_map:
                continue
            resolved = _resolve_op_path(root, raw)
            if resolved is None:
                continue
            try:
                scope, scope_protection = next(
                    ((external, rules) for external, rules in external_protections.items()
                     if within(resolved, external)), (root, protection),
                )
                _guard_history_path(Path(normal(resolved)), scope, scope_protection)
                exists = os.path.lexists(resolved)
                is_dir = bool(exists and resolved.is_dir())
                size = 0
                if exists and not is_dir:
                    try:
                        size = int(os.stat(resolved, follow_symlinks=False).st_size)
                    except OSError:
                        size = 0
                meta_map[raw] = {'exists': bool(exists), 'isDir': is_dir, 'size': size}
            except (OSError, ValueError):
                continue
    return meta_map


def _norm_compare(path_str):
    return os.path.normpath(path_str).replace('/', '\\').rstrip('\\').casefold()


def _join_suffix(base_str, suffix):
    if not suffix:
        return base_str
    sep = '\\' if ('\\' in base_str or re.match(r'^[A-Za-z]:', base_str)) else '/'
    return base_str.rstrip('\\/') + sep + suffix.replace('/', sep).lstrip('\\/')


def trace_path(operations, root, query_path, *, as_of_seq=None):
    """Trace where a path located at `query_path` (at `as_of_seq`) ended up after subsequent done ops.

    Handles exact moves, renames, cross-volume moves, multi-hop chains, undos, and parent-directory
    move inheritance (when a parent folder was moved while querying a nested child path).
    """
    root_str = str(Path(root))
    resolved = _resolve_op_path(root_str, query_path)
    original = str(resolved) if resolved is not None else str(query_path)
    cur = original
    steps = []

    done_ops = []
    for op in operations:
        if op.get('kind') not in ('move', 'cross-move', 'rename'):
            continue
        if not op.get('before') or not op.get('after'):
            continue
        done_events = [e for e in (op.get('events') or []) if e.get('status') == 'done']
        if not done_events:
            continue
        done_seq = done_events[-1]['seq']
        if as_of_seq is not None and done_seq <= as_of_seq:
            continue
        done_ops.append((done_seq, done_events[-1].get('time', ''), op))

    done_ops.sort(key=lambda item: item[0])

    for done_seq, done_time, op in done_ops:
        before_abs = str(_resolve_op_path(root_str, op['before']))
        after_abs = str(_resolve_op_path(root_str, op['after']))
        cur_cmp = _norm_compare(cur)
        bef_cmp = _norm_compare(before_abs)
        if cur_cmp == bef_cmp:
            next_path = after_abs
            inherited = False
        elif cur_cmp.startswith(bef_cmp + '\\'):
            suffix = os.path.normpath(cur)[len(os.path.normpath(before_abs).rstrip('\\/')) + 1:]
            next_path = _join_suffix(after_abs, suffix)
            inherited = True
        else:
            continue
        steps.append({
            'seq': done_seq,
            'time': done_time,
            'run': op.get('run', ''),
            'kind': op.get('kind', 'move'),
            'direction': op.get('direction', ''),
            'from': cur,
            'to': next_path,
            'opBefore': before_abs,
            'opAfter': after_abs,
            'inherited': inherited,
        })
        cur = next_path

    if not steps:
        status = 'unchanged'
    elif _norm_compare(cur) == _norm_compare(original):
        status = 'undone'
    elif any(s['kind'] == 'cross-move' for s in steps) and not within(cur, root_str):
        status = 'cross-move'
    elif _norm_compare(os.path.dirname(os.path.normpath(cur))) == _norm_compare(os.path.dirname(os.path.normpath(original))):
        status = 'rename'
    else:
        status = 'move'

    return {
        'original': original,
        'current': cur,
        'status': status,
        'steps': steps,
    }


def collect_operations(journal, protection, rows):
    external_protections = {}
    operation_scopes = {}
    operations = {}
    hidden = set()
    for row in rows:
        metadata = meta(row)
        tx = metadata.get('tx')
        op_id = (row['run_id'], tx) if isinstance(tx, str) and tx else row['seq']
        paths = tuple(row[field] for field in ('before', 'after'))
        external_root = None
        external_paths = []
        if metadata.get('effect') == 'cross-move':
            scope = metadata.get('cross')
            try:
                source_root, target_root = cross_scopes(journal.root, scope, *paths)
            except (OSError, ValueError):
                hidden.add(op_id)
                continue
            external_root = target_root if source_root == journal.root else source_root
            if external_root not in external_protections:
                try:
                    assert_no_links(external_root)
                    external_protections[external_root] = _read_protection(Journal(external_root))
                except (OSError, ValueError):
                    external_protections[external_root] = None
            if external_protections[external_root] is None:
                hidden.add(op_id)
                continue
        try:
            for raw in paths:
                if raw in ('', '-'):
                    continue
                path = Path(normal(journal.root / raw))
                scope, rules = journal.root, protection
                if external_root is not None and within(path, external_root):
                    scope, rules = external_root, external_protections[external_root]
                    external_paths.append(path)
                _guard_history_path(path, scope, rules, descendants=True)
        except (OSError, ValueError):
            hidden.add(op_id)
            continue
        if external_paths:
            operation_scopes.setdefault(op_id, {}).update({path: external_root for path in external_paths})

        kind, direction = operation_kind(row['op'], metadata.get('effect'))
        operation = operations.setdefault(op_id, {
            'id': row['seq'], 'run': row['run_id'], 'kind': kind, 'direction': direction,
            'before': None if paths[0] == '-' else paths[0],
            'after': None if paths[1] == '-' else paths[1],
            'inverseOf': inverse_seq(row, metadata), 'events': [],
        })
        status = row['status']
        if status not in ('pending', 'done', 'aborted'):
            status = 'failed' if status.startswith('failed') else 'other'
        operation['events'].append({
            'seq': row['seq'], 'time': row['ts'], 'status': status,
            'recovered': metadata.get('recorded_by') == 'recover',
        })

    visible = [op for op_id, op in operations.items() if op_id not in hidden]
    visible_seqs = {event['seq'] for op in visible for event in op['events']}
    for operation in visible:
        if operation['inverseOf'] not in visible_seqs:
            operation['inverseOf'] = None
    scopes = {path: (root, external_protections[root])
              for op_id, paths in operation_scopes.items() if op_id not in hidden
              for path, root in paths.items()}
    return visible, len(hidden), scopes


def collect_history(journal, protection):
    _guard_history_path(journal.root, journal.root, protection)
    rows = read_journal(journal.path)
    try:
        mirror_differs = read_journal(journal.mirror) != rows
    except (OSError, ValueError):
        mirror_differs = True
    visible, hidden, scopes = collect_operations(journal, protection, rows)
    present, truncated = collect_present_entries(journal, protection, visible)
    external_protections = dict(scopes.values())
    dest_meta = collect_destination_meta(journal, protection, visible, external_protections)

    return {
        'root': str(journal.root), 'journalPath': str(journal.path),
        'systemDrive': bool(is_system_root(journal.root)),
        'captured': datetime.now(timezone.utc).isoformat(),
        'mirrorDiffers': mirror_differs,
        'hidden': hidden, 'operations': visible,
        'present': present, 'presentTruncated': truncated, 'destinationMeta': dest_meta,
    }


def render_document(data, lang, *, live=False):
    payload = json.dumps(data, ensure_ascii=False).replace('<', r'\u003c')
    before, marker, after = TEMPLATE.read_text(encoding='utf-8').partition('__HISTORY_DATA__')
    if not marker:
        raise ValueError('Missing history data placeholder')
    head = before.replace('__HISTORY_LANG__', lang)
    if live:
        head = head.replace(
            "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'",
            "default-src 'none'; connect-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'",
            1,
        )
    return head + payload + after


def write_report(out, data, lang, protection):
    if out.suffix.lower() != '.html':
        raise SafetyError('执行历史的输出文件须使用 .html 扩展名')
    assert_no_links(out)
    if block_kind(out, protection.markers):
        raise SafetyError('执行历史输出命中保护标记')
    document = render_document(data, lang, live=False)
    write_artifact = getattr(_executor, 'write_artifact', None)
    if write_artifact is not None:
        def owns_history(path):
            with path.open('rb') as previous:
                return previous.read(len(SIGNATURE)) == SIGNATURE

        write_artifact(out, document.encode('utf-8'), owns_existing=owns_history)
    else:
        refreshing = out.exists()
        if refreshing:
            with out.open('rb') as previous:
                if previous.read(len(SIGNATURE)) != SIGNATURE:
                    raise SafetyError('输出位置已有其他文件，请为执行历史选择新的文件名')
        atomic_write(out, document.encode('utf-8'), replace=refreshing)


def write_preview(journal, out, prepared, blocked, external_roots, receipt_requested, lang):
    if not out.parent.is_dir():
        raise SafetyError('图形预演须写入已有目录')
    protection = journal.protection()
    if block_kind(journal.root, [row for row in protection.markers if row['kind'] == 'SECRET']):
        raise SafetyError('整理根目录命中凭据保护标记')
    for external in external_roots:
        if within(out, external['root']) and block_kind(out, Journal(external['root']).protection().markers):
            raise SafetyError('图形预演输出命中跨卷保护标记')
    operations = []
    for action in prepared:
        details = action['preview']
        paths = [action['dst'], *details['parents']]
        if action['src'] != '-':
            paths.append(action['src'])
        if any(within(out, path) for path in paths):
            raise SafetyError('图形预演不能写入将移动或创建的区域')
        operations.append({
            'id': details['row'], 'run': '', 'kind': 'cross-move' if action.get('cross') else action['op'],
            'direction': '', 'before': None if action['src'] == '-' else action['src'],
            'after': action['dst'], 'parents': details['parents'], 'events': [],
        })
    write_report(out, {
        'mode': 'preview', 'root': str(journal.root), 'captured': datetime.now(timezone.utc).isoformat(),
        'operations': operations, 'blocked': blocked, 'receiptBlocked': bool(receipt_requested and blocked),
    }, lang, protection)


def _validate_live_path(raw_path, journal, *, descendants=False):
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise SafetyError('路径不能为空')
    target = _resolve_op_path(journal.root, raw_path)
    if target is None:
        raise SafetyError('路径无效')
    target = Path(normal(target))
    root, protection = journal.root, _read_protection(journal)
    _guard_history_path(root, root, protection)
    if not within(target, root):
        _, _, scopes = collect_operations(journal, protection, read_journal(journal.path))
        match = next((scope for endpoint, scope in scopes.items() if within(target, endpoint)), None)
        if match is None:
            raise SafetyError('路径超出当前历史允许范围')
        root, protection = match
    _guard_history_path(target, root, protection, descendants=descendants)
    return target, root, protection


def _directory_revision(path):
    info = path.stat()
    return f'{info.st_dev}:{info.st_ino}:{info.st_mtime_ns}:{info.st_ctime_ns}'


def _reveal_in_file_manager(target_path):
    """Reveal an existing file or folder in the OS file manager without invoking a shell."""
    norm = os.path.normpath(str(target_path))
    if not os.path.lexists(norm):
        raise SafetyError('目标路径当前已不存在，可能已被外部移动或删除')
    if sys.platform == 'win32':
        # Windows Explorer /select,<path> highlights the exact file or folder in its parent window.
        subprocess.Popen(['explorer.exe', f'/select,{norm}'])
    elif sys.platform == 'darwin':
        subprocess.Popen(['open', '-R', norm])
    else:
        folder = norm if os.path.isdir(norm) else os.path.dirname(norm)
        subprocess.Popen(['xdg-open', folder])


def make_server(journal, protection, lang='zh-CN'):
    """Create a local-only live server for the Time-Machine Historical File Explorer."""
    _guard_history_path(journal.root, journal.root, protection)
    token = py_secrets.token_urlsafe(24)
    page_path = f'/{token}/'

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def _send_headers(self, status, content_type, length):
            self.send_response(status)
            self.send_header('Content-Type', content_type + '; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header(
                'Content-Security-Policy',
                "default-src 'none'; connect-src 'self'; script-src 'unsafe-inline'; "
                "style-src 'unsafe-inline'; frame-ancestors 'none'",
            )
            self.send_header('Content-Length', str(length))
            self.end_headers()

        def _send_json(self, status, payload):
            body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            self._send_headers(status, 'application/json', len(body))
            self.wfile.write(body)

        def _check_host(self):
            if self.headers.get('Host') != f'127.0.0.1:{self.server.server_port}':
                self.send_error(400)
                return False
            return True

        def do_GET(self):
            if not self._check_host():
                return
            parsed = urlsplit(self.path)
            if parsed.path == page_path:
                try:
                    snap = collect_history(journal, _read_protection(journal))
                    snap['live'] = True
                    snap['apiBase'] = page_path
                    body = render_document(snap, lang, live=True).encode('utf-8')
                    self._send_headers(200, 'text/html', len(body))
                    self.wfile.write(body)
                except (OSError, ValueError) as exc:
                    msg = str(exc) if isinstance(exc, SafetyError) else '无法加载执行历史'
                    self._send_json(500, {'ok': False, 'error': msg})
                return

            if parsed.path == page_path + 'ls':
                params = parse_qs(parsed.query)
                raw = params.get('path', ['.'])[0]
                try:
                    offset = int(params.get('offset', ['0'])[0])
                    resume = params.get('revision', [''])[0]
                    if offset < 0 or (offset and not resume):
                        raise SafetyError('目录分页位置无效')
                    target, root, protection = _validate_live_path(raw, journal)
                    if not target.is_dir():
                        self._send_json(200, {'ok': True, 'exists': False, 'entries': [], 'truncated': False})
                        return
                    revision = _directory_revision(target)
                    if offset and resume != revision:
                        self._send_json(409, {'ok': False, 'changed': True})
                        return
                    entries, next_offset = _list_dir_safe(
                        target, root, protection, limit=LIVE_LS_LIMIT, offset=offset,
                        deadline=time.monotonic() + SCAN_TIME_BUDGET,
                    )
                    if _directory_revision(target) != revision:
                        self._send_json(409, {'ok': False, 'changed': True})
                        return
                    self._send_json(200, {'ok': True, 'exists': True, 'entries': entries,
                                          'truncated': next_offset is not None,
                                          'nextOffset': next_offset, 'revision': revision})
                except (OSError, ValueError) as exc:
                    msg = str(exc) if isinstance(exc, SafetyError) else '无法读取目录'
                    self._send_json(400, {'ok': False, 'error': msg})
                return

            if parsed.path == page_path + 'stat':
                params = parse_qs(parsed.query)
                raw = params.get('path', [''])[0]
                try:
                    target, _, _ = _validate_live_path(raw, journal, descendants=True)
                    exists = os.path.lexists(target)
                    is_dir = bool(exists and target.is_dir())
                    size = 0
                    if exists and not is_dir:
                        try:
                            size = int(os.stat(target, follow_symlinks=False).st_size)
                        except OSError:
                            size = 0
                    self._send_json(200, {'ok': True, 'exists': bool(exists), 'isDir': is_dir, 'size': size})
                except (OSError, ValueError) as exc:
                    msg = str(exc) if isinstance(exc, SafetyError) else '无法核验路径状态'
                    self._send_json(400, {'ok': False, 'error': msg})
                return

            self.send_error(404)

        def do_POST(self):
            if not self._check_host():
                return
            parsed = urlsplit(self.path)
            if parsed.path != page_path + 'reveal':
                self.send_error(404)
                return
            if self.headers.get('X-Tidy-Action') != 'reveal':
                self._send_json(403, {'ok': False, 'error': '缺少安全操作请求头'})
                return
            try:
                length = int(self.headers.get('Content-Length') or '0')
                if length <= 0 or length > 16384:
                    raise SafetyError('请求体大小无效')
                body = json.loads(self.rfile.read(length).decode('utf-8'))
                raw = body.get('path') if isinstance(body, dict) else None
                target, _, _ = _validate_live_path(raw, journal, descendants=True)
                _reveal_in_file_manager(target)
                self._send_json(200, {'ok': True, 'path': str(target)})
            except (OSError, ValueError) as exc:
                msg = str(exc) if isinstance(exc, SafetyError) else '无法打开文件管理器'
                self._send_json(400, {'ok': False, 'error': msg})

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    return server, f'http://127.0.0.1:{server.server_port}{page_path}'


def main(args):
    journal = Journal(args.root, allow_system_drive=getattr(args, 'allow_system_drive', False))
    assert_no_links(journal.root)
    protection = _read_protection(journal)
    if getattr(args, 'serve', False):
        server, url = make_server(journal, protection, lang=args.lang)
        with server:
            hint = 'Live history explorer ready. Press Ctrl+C to stop.' if args.lang == 'en' else '实时历史文件管理器已就绪。按 Ctrl+C 结束。'
            print(url, flush=True)
            print(hint, flush=True)
            if not getattr(args, 'no_open', False):
                webbrowser.open(url)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
        return 0
    out = Path(args.out) if args.out else journal.state_dir / 'history.html'
    write_report(out, collect_history(journal, protection), args.lang, protection)
    print(f'Created: {out}' if args.lang == 'en' else f'已生成执行历史：{out}')
    if args.open and not getattr(args, 'no_open', False):
        webbrowser.open(out.resolve().as_uri())
    return 0
