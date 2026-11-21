"""Generate an offline, read-only view of the execution journal."""
from datetime import datetime, timezone
import json
from pathlib import Path
import re

from executor import Journal, atomic_write, cross_scopes, meta, read_journal
from safety import SafetyError, assert_no_links, block_kind, is_system_root, within


TEMPLATE = Path(__file__).with_name('history_ui.html')
SIGNATURE = b'<!doctype html>\n<!-- TidyFileTree execution history -->\n'


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


def collect_history(journal, protection):
    primary_secrets = [row for row in protection.markers if row['kind'] == 'SECRET']
    if block_kind(journal.root, primary_secrets):
        raise SafetyError('整理根目录命中凭据保护标记')
    rows = read_journal(journal.path)
    try:
        mirror_differs = read_journal(journal.mirror) != rows
    except (OSError, ValueError):
        mirror_differs = True
    external_secrets = {}
    operations = {}
    hidden = set()
    for row in rows:
        metadata = meta(row)
        tx = metadata.get('tx')
        op_id = (row['run_id'], tx) if isinstance(tx, str) and tx else row['seq']
        paths = tuple(row[field] for field in ('before', 'after'))
        markers = primary_secrets
        if metadata.get('effect') == 'cross-move':
            scope = metadata.get('cross')
            try:
                source_root, target_root = cross_scopes(journal.root, scope, *paths)
            except (OSError, ValueError):
                hidden.add(op_id)
                continue
            external = target_root if source_root == journal.root else source_root
            if external not in external_secrets:
                try:
                    assert_no_links(external)
                    external_secrets[external] = [row for row in Journal(external).protection().markers
                                                  if row['kind'] == 'SECRET']
                except (OSError, ValueError):
                    external_secrets[external] = None
            if external_secrets[external] is None:
                hidden.add(op_id)
                continue
            markers = markers + external_secrets[external]
        if any(path not in ('', '-') and block_kind(journal.root / path, markers, descendants=True) for path in paths):
            hidden.add(op_id)
            continue

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
    return {
        'root': str(journal.root), 'journalPath': str(journal.path),
        'systemDrive': bool(is_system_root(journal.root)),
        'captured': datetime.now(timezone.utc).isoformat(),
        'mirrorDiffers': mirror_differs,
        'hidden': len(hidden), 'operations': visible,
    }


def write_report(out, data, lang, protection):
    if out.suffix.lower() != '.html':
        raise SafetyError('执行历史的输出文件须使用 .html 扩展名')
    assert_no_links(out)
    if block_kind(out, protection.markers):
        raise SafetyError('执行历史输出命中保护标记')
    refreshing = out.exists()
    if refreshing:
        with out.open('rb') as previous:
            if previous.read(len(SIGNATURE)) != SIGNATURE:
                raise SafetyError('输出位置已有其他文件，请为执行历史选择新的文件名')
    payload = json.dumps(data, ensure_ascii=False).replace('<', r'\u003c')
    before, marker, after = TEMPLATE.read_text(encoding='utf-8').partition('__HISTORY_DATA__')
    if not marker:
        raise ValueError('Missing history data placeholder')
    document = before.replace('__HISTORY_LANG__', lang) + payload + after
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


def main(args):
    journal = Journal(args.root, allow_system_drive=getattr(args, 'allow_system_drive', False))
    assert_no_links(journal.root)
    protection = journal.protection()
    out = Path(args.out) if args.out else journal.state_dir / 'history.html'
    write_report(out, collect_history(journal, protection), args.lang, protection)
    print(f'Created: {out}' if args.lang == 'en' else f'已生成执行历史：{out}')
    if args.open:
        import webbrowser
        webbrowser.open(out.resolve().as_uri())
    return 0
