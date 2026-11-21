#!/usr/bin/env python3
"""Build the standalone TidyFileTree wiring board from local stage artifacts."""
import argparse
from datetime import datetime
from functools import lru_cache
import json
import os
from pathlib import Path
import re
import sys

import decisions
from decisions import group_key
from executor import Journal, atomic_write, guard
import rules
from safety import assert_no_links, block_kind, key, within

TEMPLATE = Path(__file__).with_name('wire_ui.html')
ENGLISH_MESSAGES = Path(__file__).with_name('wire_ui.en.json')
TIERS = ('direct', 'verify', 'full')


def read_json(path):
    assert_no_links(path)
    data = path.read_text(encoding='utf-8-sig')
    return json.loads(data)


def allowed_path(path, root, protection, descendants=False, allow_container=False):
    try:
        result = guard(path, root, protection, allow_container=allow_container)
        if descendants and block_kind(result, protection.markers, descendants=True):
            return None
        return result
    except (OSError, ValueError):
        return None


def load_probes(probe_dir, root, protection):
    assert_no_links(probe_dir)
    probes, inventory, groups = {}, {}, {}
    protected_nodes = []
    seen_protected = set()
    secret_markers = [m for m in protection.markers if m['kind'] == 'SECRET']
    if not probe_dir.is_dir():
        return probes, inventory, groups, protected_nodes
    for file in sorted(probe_dir.glob('*.json')):
        if file.name.startswith('dup_'):
            continue
        try:
            data = read_json(file)
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or not isinstance(data.get('path'), str):
            continue
        raw_path = data['path']
        # Container nodes (e.g. C:\Users\<user>\Downloads or Documents with a NO_TOUCH subfolder)
        # remain browsable in the UI; individual movable units are still checked strictly in unit_paths().
        try:
            path = guard(raw_path, root, protection, allow_container=True)
        except (OSError, ValueError) as exc:
            # Never leak SECRET paths; expose ordinary protected/toolchain skips so UI stays consistent with S6.
            if not (file.name.startswith(('units_', 'group_')) or
                    (os.path.isabs(raw_path) and block_kind(raw_path, secret_markers, descendants=True))):
                norm_key = raw_path.casefold()
                if norm_key not in seen_protected:
                    seen_protected.add(norm_key)
                    protected_nodes.append({
                        'name': Path(raw_path).name or raw_path,
                        'path': raw_path,
                        'action': str(data.get('action') or 'skip'),
                        'reason': str(exc) if isinstance(exc, ValueError) else '命中保护规则',
                    })
            continue
        data['path'] = path
        dest = inventory if file.name.startswith('units_') else groups if file.name.startswith('group_') else probes
        if dest is groups:
            items = data.get('groups')
            if not isinstance(items, list):
                continue
            data['groups'] = [g for g in items if isinstance(g, dict) and isinstance(g.get('name'), str)]
        dest[path] = data
    return probes, inventory, groups, protected_nodes


def decision_index(snapshot, root):
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get('root'), str):
        return {}
    if key(snapshot['root']) != key(root):
        raise ValueError('decisions.json 属于其他整理根目录')
    index = {}
    for node in snapshot.get('nodes') or []:
        if not isinstance(node, dict) or not isinstance(node.get('path'), str):
            continue
        for group in node.get('groups') or []:
            if isinstance(group, dict) and isinstance(group.get('id'), str):
                index[key(node['path']), group['id']] = group
    return index


def unit_paths(node, inventory, root, protection):
    """Only S2-confirmed atomic units can become filesystem actions."""
    checked = {}
    confirmed = [u.get('path') for u in (inventory.get('confirmed_units') or [])
                 if isinstance(u, dict) and u.get('unit_count', 1) == 1]
    for value in confirmed + (inventory.get('loose_files_as_units') or []):
        if not isinstance(value, str) or not value:
            continue
        try:
            candidate = str(Path(node)/value)
            path = (allowed_path(candidate, root, protection, descendants=True)
                    if within(candidate, node, inclusive=False) else None)
        except (OSError, ValueError):
            path = None
        if path and os.path.exists(path):
            checked[key(path)] = path
    return checked


def view_units(group, node, checked, root, protection):
    units = []
    seen = {}
    for value in group.get('units') or []:
        raw = (value.get('path') or value.get('name')) if isinstance(value, dict) else value
        if not isinstance(raw, str) or not raw:
            continue
        try:
            candidate = str(Path(node)/raw)
            if within(candidate, root) and block_kind(candidate, protection.markers, descendants=True):
                continue
            source = checked.get(key(candidate))
        except (OSError, ValueError):
            source = None
        leaf = Path(raw).name or raw
        unit_id = source or raw
        seen[unit_id] = seen.get(unit_id, 0) + 1
        units.append({'id': [unit_id, seen[unit_id]], 'label': leaf, 'source': source})
    return units


def proposal_targets(name, parent, root, protection):
    if not isinstance(name, str) or not name.strip() or name in ('.', '..'):
        return None
    if re.search(r'[\\/:*?"<>|\x00-\x1f]', name) or name.rstrip(' .') != name:
        return None
    if isinstance(parent, str) and parent:
        target = Path(root)/parent/name
    else:
        target = Path(root)/name
    path = allowed_path(str(target), root, protection)
    if not path or (os.path.lexists(path) and not os.path.isdir(path)):
        return None
    return path


def build_board(root, probe_dir, snapshot, protection):
    probes, inventory, groups, protected_nodes = load_probes(probe_dir, root, protection)
    snapshots = decision_index(snapshot, root)

    @lru_cache(maxsize=8192)
    def permitted(path):
        return bool(allowed_path(path, root, protection, allow_container=True))

    zones = decisions.collect_zones(root, permit=permitted, count_limit=1)
    destinations = {key(z['path']): z for z in zones}
    nodes = []
    unmatched = []
    matched_answers = 0
    for node in decisions.build_nodes(root, probes, inventory, groups):
        path = node['path']
        solo = groups.get(path, {}).get('solo_units')
        solo_groups = []
        if isinstance(solo, list):
            for member in solo:
                if not isinstance(member, str) or not member:
                    continue
                name = Path(member).name or member
                fingerprint = group_key({'name': name, 'units': [member]})
                solo_groups.append({'id': 'solo:'+fingerprint, 'fingerprint': fingerprint, 'name': name,
                                    'n': 1, 'units': [member], 'why': 'S3 标为独立单元'})
        # 节点级待拍板项（rename/ask/keep 且无组）也必须进工作台，
        # 否则「改名」「多版本并存」「空壳去留」这类决策在工作台里完全不可见。
        # 合成为单单元组，复用既有连线/导出机制。
        node_level = node.get('action') in ('rename', 'ask', 'keep', 'unclear')
        if not node['groups'] and not solo_groups and node_level and os.path.isdir(path):
            fingerprint = group_key({'name': node['name'], 'units': [path]})
            solo_groups.append({
                'id': 'node:' + fingerprint, 'fingerprint': fingerprint,
                'name': node['name'], 'n': 1, 'units': [path],
                'why': node.get('unit_why') or ('节点级判定：' + str(node.get('action'))),
            })
        if not node['groups'] and not solo_groups:
            continue
        known_units = unit_paths(path, inventory.get(path, {}), root, protection)
        items = []
        for group in node['groups'] + solo_groups:
            record = snapshots.get((key(path), group['id'])) or {}
            units = view_units(group, path, known_units, root, protection)
            if record.get('fingerprint') == group['fingerprint'] and isinstance(record.get('suggest'), dict):
                suggestion = dict(record['suggest'])
            else:
                sample = next((u['source'] for u in units if u['source'] and os.path.isdir(u['source'])), None)
                suggestion = decisions.suggest_group(group['name'], node['name'], zones, path, group_path=sample)
            ai = suggestion.get('aiJudgement')
            evidence = suggestion.get('evidence')
            if not isinstance(evidence, list):
                evidence = []
            if isinstance(ai, dict):
                matched_answers += 1
            tier = suggestion.get('tier') or suggestion.get('verificationTier') or record.get('tier')
            if tier not in TIERS:
                tier = ai.get('verificationTier') if isinstance(ai, dict) else None
            if tier not in TIERS:
                tier = decisions.verification_tier(group['name'], suggestion)
            destination = None
            if suggestion.get('mode') == 'keep':
                destination = '@keep'
            elif suggestion.get('mode') == 'move' and isinstance(suggestion.get('target'), str):
                zone = destinations.get(key(suggestion['target']))
                destination = zone['path'] if zone else None
            elif suggestion.get('mode') == 'new':
                path_new = proposal_targets(suggestion.get('newName'), suggestion.get('newParent'), root, protection)
                if path_new:
                    relative = Path(path_new).relative_to(root)
                    zone = destinations.setdefault(key(path_new), {
                        'path': path_new, 'label': relative.as_posix(),
                        'depth': len(relative.parts), 'files': 0, 'new': True})
                    destination = zone['path']
            if suggestion.get('mode') == 'keep':
                kind = next((k for k in ('installer', 'media', 'image', 'code', 'document', 'archive')
                             if any(k in str(e) for e in evidence)), None)
                if kind:
                    unmatched.append((group['name'], kind))
            items.append({
                'id': group['id'], 'name': group['name'], 'count': group['n'], 'units': units,
                'why': group.get('why'), 'tier': tier, 'ai': ai if isinstance(ai, dict) else None,
                'suggest': {'target': destination, 'label': suggestion.get('targetLabel') or suggestion.get('newName'),
                            'needsCheck': bool(suggestion.get('needsCheck')), 'reason': suggestion.get('reason'),
                            'evidence': evidence}
            })
        nodes.append({'id': node['id'], 'path': path, 'name': node['name'], 'groups': items,
                      'action': node.get('action'), 'label': node.get('label') or '',
                      'renameTo': node.get('rename_to'),
                      'confidence': node.get('confidence'),
                      'why': node.get('unit_why') or '',
                      'units': node.get('total_units')})

    # Include node-level S1 decisions (rename / merge / ask / uncertain / unclear) that have no S3 groups,
    # so the review board is never empty when only top-level/node-level decisions remain.
    grouped_keys = {key(n['path']) for n in nodes}
    node_level_items = []
    for p_path, p_data in sorted(probes.items(), key=lambda pair: pair[0].casefold()):
        if key(p_path) in grouped_keys:
            continue
        act = str(p_data.get('action') or '')
        if act not in ('rename', 'merge', 'ask', 'uncertain', 'unclear'):
            continue
        if not os.path.lexists(p_path):
            continue
        leaf = Path(p_path).name or p_path
        movable = allowed_path(p_path, root, protection, descendants=True)
        why_parts = []
        if act == 'rename' and p_data.get('rename_to'):
            why_parts.append(f"S1 建议改名为：{p_data['rename_to']}")
        elif act == 'merge' and p_data.get('merge_target'):
            why_parts.append(f"S1 建议合并到：{p_data['merge_target']}")
        else:
            why_parts.append(f"S1 节点级待拍板（action={act}）")
        if p_data.get('unit_why'):
            why_parts.append(str(p_data['unit_why']))
        ev_list = p_data.get('evidence') if isinstance(p_data.get('evidence'), list) else []
        ev_strs = [str(e) for e in ev_list if e]
        dest_target = '@keep'
        dest_label = None
        if act == 'merge' and isinstance(p_data.get('merge_target'), str):
            zone = destinations.get(key(p_data['merge_target']))
            if zone:
                dest_target = zone['path']
                dest_label = zone['label']
        fp = group_key({'name': leaf, 'units': [p_path]})
        node_level_items.append({
            'id': 'node:' + fp,
            'name': leaf,
            'count': 1,
            'units': [{'id': [movable or p_path, 1], 'label': leaf, 'source': movable}],
            'why': ' · '.join(why_parts),
            'tier': 'verify',
            'ai': None,
            'suggest': {
                'target': dest_target,
                'label': dest_label,
                'needsCheck': True,
                'reason': why_parts[0],
                'evidence': ev_strs or why_parts[1:],
            },
        })
    if node_level_items:
        nodes.append({
            'id': 'node-decisions',
            'path': str(root),
            'name': '节点级待拍板（S1）',
            'groups': node_level_items,
            'action': 'ask',
            'label': 'S1 节点级待拍板',
            'renameTo': None,
            'confidence': 'medium',
            'why': '',
            'units': len(node_level_items),
        })

    proposals = decisions.propose_new_categories(unmatched)
    stored = snapshot.get('proposedCategories', {}) if isinstance(snapshot, dict) else {}
    visible_names = {g['name'] for node in nodes for g in node['groups']}
    if isinstance(stored, dict):
        for title, record in stored.items():
            if not isinstance(record, dict):
                continue
            members = record.get('members')
            if isinstance(members, list) and members and all(isinstance(m, str) and m in visible_names for m in members):
                proposals[title] = record
    return {
        'root': str(root), 'generated': datetime.now().isoformat(timespec='minutes'),
        'caseInsensitive': os.name == 'nt', 'nodes': nodes, 'zones': list(destinations.values()),
        'proposedCategories': proposals, 'matchedAnswers': matched_answers,
        'protectedNodes': protected_nodes,
    }


def render_html(root, data, *, lang='zh-CN'):
    # JSON values cannot end the script element or introduce an HTML tag.
    from html import escape
    if lang not in ('zh-CN', 'en'):
        raise ValueError('Unsupported board language')
    payload = json.dumps(data, ensure_ascii=False).replace('<', r'\u003c')
    template = TEMPLATE.read_text(encoding='utf-8')
    before, marker, after = template.partition('__TIDY_FILE_TREE_DATA__')
    script_start, locale_marker, script_end = after.partition('__BOARD_MESSAGES__')
    if not marker or not locale_marker:
        raise ValueError('Missing data or language catalog placeholder in board template')
    messages = json.loads(ENGLISH_MESSAGES.read_text(encoding='utf-8'))
    catalog = json.dumps(messages, ensure_ascii=False).replace('<', r'\u003c')
    title = escape(str(root))
    return (before.replace('__BOARD_LANG__', lang).replace('__ROOT__', title) + payload
            + script_start.replace('__ROOT__', title) + catalog + script_end.replace('__ROOT__', title))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--root', required=True)
    ap.add_argument('--probe')
    ap.add_argument('--decisions', help='S4 decisions.json；默认读取根目录的 _organize/decisions.json')
    ap.add_argument('--out')
    ap.add_argument('--open', action='store_true')
    ap.add_argument('--no-open', action='store_true', help='仅生成 HTML，不自动打开浏览器')
    ap.add_argument('--lang', choices=('zh-CN', 'en'), default='zh-CN')
    ap.add_argument('--allow-system-drive', action='store_true')
    args = ap.parse_args()

    try:
        journal = Journal(args.root, allow_system_drive=args.allow_system_drive)
        root = journal.root
        assert_no_links(root)
        policy = rules.load_root_policy(root)
        protection = journal.protection(policy)
        rules.apply_to(rules.load_for_root(root, policy=policy), decisions_mod=decisions)
        mapping, _ = decisions.load_policy_mapping(root)
        if mapping:
            decisions.apply_policy_mapping(mapping)
        snapshot_path = Path(args.decisions) if args.decisions else journal.state_dir/'decisions.json'
        snapshot = read_json(snapshot_path) if args.decisions or os.path.lexists(snapshot_path) else None
        board = build_board(root, Path(args.probe) if args.probe else journal.state_dir/'probe', snapshot, protection)
        out = Path(args.out) if args.out else journal.state_dir/'ui.html'
        atomic_write(out, render_html(root, board, lang=args.lang).encode('utf-8'))
    except (OSError, ValueError) as exc:
        prefix = 'Could not generate the review board' if args.lang == 'en' else '无法生成连线工作台'
        ap.error(f'{prefix}: {exc}')

    count = sum(len(node['groups']) for node in board['nodes'])
    if args.lang == 'en':
        print(f'Created: {out} ({count} groups, {board["matchedAnswers"]} recorded decisions, '
              f'{len(board["proposedCategories"])} suggested categories)')
    else:
        print(f'已生成: {out}（{count} 组，{board["matchedAnswers"]} 份已回填判断，{len(board["proposedCategories"])} 个候选类别）')
    if args.open and not args.no_open:
        if os.name == 'nt':
            os.startfile(str(out))
        else:
            import subprocess
            subprocess.run(['open' if sys.platform == 'darwin' else 'xdg-open', str(out)], check=False)
    return 0


if __name__ == '__main__':
    sys.exit(main())
