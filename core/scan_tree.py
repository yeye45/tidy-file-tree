"""Stream bounded, read-only directory observations to a local browser."""
from collections import Counter, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
from urllib.parse import parse_qs, urlsplit
import webbrowser

from executor import Journal, Protection, guard
from safety import (SafetyError, assert_no_links, block_kind, is_reparse,
                    is_system_attr, is_system_protected_file, nested_verdict,
                    normal, within)
from scan_drafts import INTACT_KINDS, inventory, proposals
from signals import VID_EXT, bucket, sig_bin_plus_data, sig_sequence, sig_unity, sig_vm


TEMPLATE = Path(__file__).with_name('scan_tree_ui.html')
ENTRY_LIMIT = 1200
DIRECTORY_LIMIT = 240
KINDS = {'img': 'images', 'vid': 'videos', 'aud': 'audio', 'arc': 'archives', 'doc': 'documents'}


def scan_protection(root):
    journal = Journal(root, allow_system_drive=True)
    root = journal.root
    assert_no_links(root)
    scopes = [journal]
    for parent in root.parents:
        try:
            scope = Journal(parent, allow_system_drive=True)
        except SafetyError:
            continue
        state = scope.state_dir
        if any(os.path.lexists(path) for path in (
                state / 'markers.tsv', state / 'policy.json',
                state / 'marker-update.json', scope.mirror_dir / 'markers.tsv',
                scope.mirror_dir / 'marker-update.json')):
            scopes.append(scope)
    markers, names = [], set()
    for scope in scopes:
        protection = scope.protection()
        markers.extend(protection.markers)
        names.update(protection.extra_names)
    protection = Protection(markers, names)
    guard(root, root, protection, allow_root=True)
    return root, protection


def inspect_level(path, root, protection):
    """Check names and shallow structure, omitting protected entries before classification."""
    entries, children = [], []
    hidden = 0
    limited = False
    with os.scandir(path) as contents:
        for index, item in enumerate(contents):
            if index == ENTRY_LIMIT:
                limited = True
                break
            if item.name.casefold() == '_organize':
                continue
            try:
                marker = block_kind(item.path, protection.markers) if protection.markers else None
            except (OSError, SafetyError):
                hidden += 1
                continue
            if marker == 'SECRET':
                hidden += 1
                continue
            is_dir = item.is_dir(follow_symlinks=False)
            if is_dir:
                try:
                    if marker or is_reparse(item.path) or is_system_attr(item.path):
                        raise SafetyError('protected')
                    guard(item.path, root, protection, allow_container=True)
                except SafetyError:
                    children.append((Path(item.path), marker or 'protected'))
                    hidden += 1
                    continue
                children.append((Path(item.path), None))
            elif (marker or not item.is_file(follow_symlinks=False)
                  or is_system_attr(item.path)
                  or is_system_protected_file(item.path)
                  or not nested_verdict(item.path)[0]):
                hidden += 1
                continue
            entries.append({'name': item.name, 'dir': is_dir})
    return entries, sorted(children, key=lambda child: child[0].name.casefold()), hidden, limited


def classify(entries, hidden, limited):
    directories = {entry['name'].casefold() for entry in entries if entry['dir']}
    files = [entry for entry in entries if not entry['dir']]
    names = {entry['name'].casefold() for entry in files}
    kind, basis = None, 'mixed'
    sequence = None
    if sig_unity(entries)['unity-layout']:
        kind, basis = 'game', 'unity'
    elif sig_vm(entries)['vm-layout']:
        kind, basis = 'vm', 'vm'
    elif sig_bin_plus_data(entries)['bin-plus-data'] or (
            {'models', 'custom_nodes'} <= directories and 'main.py' in names) or (
            'comfyui' in directories and directories & {'python_embeded', 'python_embedded'}
            and any(name.startswith('run_') and name.endswith('.bat') for name in names)):
        kind, basis = 'runtime', 'runtime'
    elif not files and directories:
        kind, basis = 'container', 'container'
    elif not entries and not (hidden or limited):
        kind, basis = 'empty', 'empty'
    elif files:
        counts = Counter(bucket(Path(entry['name']).suffix) for entry in files)
        dominant, number = counts.most_common(1)[0]
        if dominant == 'vid':
            sequence = sig_sequence(files).get('sequence')
        if sequence and sequence['ext'] in VID_EXT and sequence['unique-count'] >= .75 * len(files):
            tagged = sum(bool(re.search(r'(?i)(?:s\d{1,2}e\d+|第\s*\d+\s*[集话話])',
                                       entry['name'])) for entry in files)
            kind, basis = ('series' if tagged >= .75 * len(files) else 'videos'), 'sequence'
        elif dominant in KINDS and number >= .8 * len(files):
            kind, basis = KINDS[dominant], 'dominant'
    # An interrupted sample or a mixed directory with nested contents can suggest
    # a type, but cannot establish the purpose of the entire folder.
    review = kind is None or limited or (hidden and basis in ('dominant', 'sequence')) or (
        basis == 'dominant' and (len(files) < 2 or bool(directories)))
    result = {'status': 'review' if review else 'judged', 'kind': kind, 'basis': basis,
              'files': len(files), 'folders': sum(entry['dir'] for entry in entries),
              'excluded': hidden, 'limited': limited}
    if basis == 'sequence':
        result['sequence'] = {key: sequence[key] for key in ('range', 'unique-count', 'missing')}
    return result


def observations(root, protection, depth):
    """Yield flat node updates in breadth-first order without walking size totals."""
    pending = deque([(root, 0, None, 0, True)])
    levels, incomplete = {}, set()
    next_id = 1
    yield {'event': 'node', 'id': 0, 'parent': None, 'path': str(root),
           'name': root.name or str(root), 'status': 'queued'}
    capped = False
    while pending:
        path, node, parent, level, draftable = pending.popleft()
        yield {'event': 'node', 'id': node, 'status': 'scanning'}
        try:
            guard(path, root, protection, allow_root=True)
            entries, children, excluded, limited = inspect_level(path, root, protection)
        except SafetyError:
            incomplete.add(parent)
            yield {'event': 'node', 'id': node, 'status': 'skipped', 'reason': 'protected'}
            continue
        except OSError:
            incomplete.add(parent)
            yield {'event': 'node', 'id': node, 'status': 'unreadable'}
            continue
        capped |= limited
        result = classify(entries, excluded, limited)
        at_depth_cap = level >= depth and bool(children)
        yield {'event': 'node', 'id': node, **result,
               'hasMore': at_depth_cap, 'unloaded': len(children) if at_depth_cap else 0}
        draftable = draftable and result['kind'] not in INTACT_KINDS
        if draftable:
            episodes, unpaired, hidden = inventory(path, entries, root, protection)
            known = sum(1 + len(episode['subtitles']) for episode in episodes) + len(unpaired)
            levels[node] = {'path': path, 'parent': parent, 'episodes': episodes, 'unpaired': unpaired,
                            'mixed': bool(children) or known < result['files'], 'limited': limited,
                            'excluded': bool(hidden or excluded)}
        if level >= depth:
            capped |= bool(children)
            continue
        added = 0
        for child, marker in children:
            if next_id >= DIRECTORY_LIMIT:
                capped = True
                incomplete.add(node)
                yield {'event': 'node', 'id': node, 'hasMore': True,
                       'unloaded': len(children) - added}
                break
            yield {'event': 'node', 'id': next_id, 'parent': node, 'name': child.name,
                   'status': 'skipped' if marker else 'queued', 'reason': marker}
            if not marker:
                pending.append((child, next_id, node, level + 1, draftable))
            next_id += 1
            added += 1
    for node, level in levels.items():
        if level['episodes'] or level['unpaired']:
            yield {'event': 'inventory', 'id': node, 'path': str(level['path']),
                   'episodes': [{key: value for key, value in episode.items() if key != 'variant'}
                                for episode in level['episodes']], 'unpaired': level['unpaired']}
    yield from proposals(levels, incomplete, root, protection)
    yield {'event': 'done', 'limited': capped}


def expand_node(root, protection, rel_path, *, child_limit=200):
    """Read-only one-level drill-down for an existing directory inside root."""
    rel = (rel_path or '').strip()
    if rel in ('', '.'):
        target = root
    else:
        if os.path.isabs(rel):
            raise SafetyError('只能按相对路径逐级展开扫描')
        target = Path(normal(root / rel))
    if not within(target, root, inclusive=True):
        raise SafetyError('展开路径超出扫描根目录')
    guard(target, root, protection, allow_root=True)
    _, children, _, limited = inspect_level(target, root, protection)
    items = []
    truncated = limited or len(children) > child_limit
    for child, marker in children[:child_limit]:
        if marker:
            items.append({
                'name': child.name, 'status': 'skipped', 'reason': marker,
                'hasMore': False, 'unloaded': 0,
            })
            continue
        try:
            guard(child, root, protection, allow_root=True)
            c_entries, c_children, c_excluded, c_limited = inspect_level(child, root, protection)
            res = classify(c_entries, c_excluded, c_limited)
            items.append({
                'name': child.name, **res,
                'hasMore': bool(c_children), 'unloaded': len(c_children),
            })
        except SafetyError:
            items.append({
                'name': child.name, 'status': 'skipped', 'reason': 'protected',
                'hasMore': False, 'unloaded': 0,
            })
        except OSError:
            items.append({
                'name': child.name, 'status': 'unreadable',
                'hasMore': False, 'unloaded': 0,
            })
    return {'children': items, 'limited': truncated}


def make_server(root, depth, lang):
    token = secrets.token_urlsafe(24)
    page_path = '/' + token + '/'
    template = TEMPLATE.read_text(encoding='utf-8')
    page = template.replace('__SCAN_CONFIG__', json.dumps({'lang': lang, 'depth': depth},
                                                          ensure_ascii=True).replace('<', r'\u003c')).encode('utf-8')

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            if self.headers.get('Host') != f'127.0.0.1:{self.server.server_port}':
                self.send_error(400)
                return
            parsed = urlsplit(self.path)
            if parsed.path not in (page_path, page_path + 'events', page_path + 'expand'):
                self.send_error(404)
                return
            stream = parsed.path.endswith('/events')
            expanding = parsed.path.endswith('/expand')
            self.send_response(200)
            ctype = 'text/event-stream' if stream else 'application/json' if expanding else 'text/html'
            self.send_header('Content-Type', ctype + '; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'none'; connect-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; frame-ancestors 'none'")
            if not stream and not expanding:
                self.send_header('Content-Length', str(len(page)))
            if expanding:
                params = parse_qs(parsed.query)
                rel = params.get('path', [''])[0]
                try:
                    current_root, current_protection = scan_protection(root)
                    payload = expand_node(current_root, current_protection, rel)
                except (OSError, ValueError) as exc:
                    payload = {'error': str(exc) if isinstance(exc, SafetyError) else '无法读取该子目录'}
                body = json.dumps(payload, ensure_ascii=True).encode('utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    return
                return
            self.end_headers()
            try:
                if stream:
                    # Re-read markers before every scan, including requests after a refresh.
                    try:
                        current_root, current_protection = scan_protection(root)
                        events = observations(current_root, current_protection, depth)
                    except (OSError, ValueError):
                        events = ({'event': 'stopped'},)
                    for event in events:
                        line = 'data: ' + json.dumps(event, ensure_ascii=True) + '\n\n'
                        self.wfile.write(line.encode('utf-8'))
                        self.wfile.flush()
                else:
                    self.wfile.write(page)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                return

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    return server, f'http://127.0.0.1:{server.server_port}{page_path}'


def main(path, depth, lang, *, open_browser=True):
    if not 0 <= depth <= 12:
        raise SafetyError('目录树深度须在 0 到 12 之间')
    root, _ = scan_protection(path)
    server, url = make_server(root, depth, lang)
    with server:
        hint = 'Open in a browser. Press Ctrl+C to stop.' if lang == 'en' else '在浏览器打开。按 Ctrl+C 结束。'
        print(url, flush=True)
        print(hint, flush=True)
        if open_browser:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0
