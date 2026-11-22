#!/usr/bin/env python3
"""Check S1–S3 for enrolled nodes. Exit 1 on a confirmed gap or invalid artifact."""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import sys

from executor import Journal
from safety import (SafetyError, assert_no_links, block_kind, is_reparse, nested_policy_verdict,
                    is_system_attr, is_system_root, key, nested_verdict,
                    normal, organize_dir, within)

SKIP_NAMES = {
    '$recycle.bin', 'system volume information', '_organize',
    '_待删除', '_重复待确认', '_未归类',
}
ACTIONS = {'keep','enter','split','rename','merge','ask','skip','unclear','uncertain'}


def _units_ok(value):
    """total_units accepts int, and float whose value equals an integer."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value >= 0
    if isinstance(value, float):
        return value >= 0 and value.is_integer()
    return False


def valid_action(action):
    return isinstance(action, str) and action in ACTIONS


def unit_count(value):
    if not _units_ok(value):
        raise SafetyError('单元数必须是非负整数')
    return int(value)


def grouping_coverage(node, inventory, grouping, markers, *, root=None, nested_rules=()):
    """Check membership without rescanning the inventory represented by the artifacts."""
    resolved = {}

    def member_key(value):
        if isinstance(value, dict):
            value = value.get('path') or value.get('unit') or value.get('name')
        if not isinstance(value, str) or not value.strip() or '\0' in value:
            raise SafetyError('单元缺少有效路径')
        if value not in resolved:
            path = os.path.join(node, value)
            if root is not None and not within(path, root, inclusive=False):
                raise SafetyError('单元路径超出整理根')
            locked = (block_kind(path, markers, descendants=True)
                      or not nested_policy_verdict(path, nested_rules, descendants=True)[0])
            resolved[value] = None if locked else key(path)
        return resolved[value]

    total = unit_count(inventory['total_units'])
    expected, assigned = {}, Counter()
    missing = []
    listed = protected = 0
    duplicate_inventory = set()
    anonymous = 0
    unknown_group_size = False

    for field in ('confirmed_units', 'loose_files_as_units'):
        members = inventory.get(field, [])
        if not isinstance(members, list):
            raise SafetyError(f'{field} 必须是数组')
        for value in members:
            count = unit_count(value.get('unit_count', 1)) if field == 'confirmed_units' and isinstance(value, dict) else 1
            listed += count
            path = member_key(value)
            if path is None:
                protected += count
            elif count:
                if path in expected:
                    duplicate_inventory.add(path)
                expected[path] = count

    groups = grouping['groups']
    if not isinstance(groups, list):
        raise SafetyError('groups 必须是数组')
    buckets = [grouping.get('solo_units', []), grouping.get('uncertain', [])]
    for group in groups:
        if not isinstance(group, dict):
            raise SafetyError('groups 的每个条目必须是对象')
        if 'units' not in group and 'unit_count' not in group:
            unknown_group_size = True
            continue
        members = group.get('units', group.get('unit_count'))
        if isinstance(members, list):
            buckets.append(members)
        else:
            anonymous += unit_count(members)
    for members in buckets:
        if not isinstance(members, list):
            raise SafetyError('solo_units 和 uncertain 必须是数组')
        for value in members:
            path = member_key(value)
            if path is not None:
                assigned[path] += 1

    exact_inventory = listed == total
    s3_names_complete = not anonymous and not unknown_group_size
    if listed > total:
        missing.append(f'S2 列出的单元数超过总数（{listed}/{total}）')
    if duplicate_inventory:
        missing.append(f'S2 重复登记 {len(duplicate_inventory)} 个单元')
    duplicates = sum(count > 1 for count in assigned.values())
    if duplicates:
        missing.append(f'有 {duplicates} 个单元重复归入 S3')
    extra = assigned.keys() - expected.keys()
    if extra and exact_inventory:
        missing.append(f'S3 有 {len(extra)} 个单元未在 S2 登记')
    unassigned = expected.keys() - assigned.keys()
    if unassigned and s3_names_complete:
        missing.append(f'S2 有 {len(unassigned)} 个单元未归入 S3')

    # An anonymous legacy count can include protected members. Compare totals only
    # when the verified scope makes the comparison meaningful.
    if (not missing and not unknown_group_size and (exact_inventory and s3_names_complete
            or (not block_kind(node, markers, descendants=True)
                and nested_policy_verdict(node, nested_rules, descendants=True)[0]))):
        assigned_count = anonymous + sum(expected.get(path, 1) * count for path, count in assigned.items())
        if assigned_count != total - protected:
            missing.append(f'S3 单元数与 S2 不符（{assigned_count}/{total - protected}）')

    reason = None
    if not missing and (not exact_inventory or not s3_names_complete or any(count > 1 for count in expected.values())):
        reason = '部分单元只有计数，无法逐项核对归组'
    return missing, reason


def cross_departures(root):
    from executor import active_events, completed, cross_scopes, meta, read_journal

    rows = read_journal(organize_dir(root)/'journal.tsv')
    active = active_events(rows)
    result = []
    for row in completed(rows):
        data = meta(row)
        if row['seq'] not in active or data.get('effect') != 'cross-move':
            continue
        source, _ = cross_scopes(root, data.get('cross'), row['before'], row['after'])
        if key(source) == key(root):
            result.append((row['before'], row['after']))
    return result


def check(root, probe):
    root, probe = normal(root), Path(probe)
    assert_no_links(root)
    assert_no_links(probe)
    protection = Journal(root, allow_system_drive=True).protection(include_skips=True, probe=probe)
    markers = protection.markers
    if block_kind(root, markers) or not nested_policy_verdict(root, protection.nested_rules, allow_container=True)[0]:
        return []
    expected, probes, units, groups, errors, stale = {}, {}, {}, {}, [], []

    derived = set()
    enter_children = set()
    departures = None
    reported_departures = set()

    def add(path, base=root, is_derived=False):
        nonlocal departures
        value = normal(path if os.path.isabs(path) else os.path.join(base,path))
        if not within(value,root,inclusive=False):
            if departures is None:
                departures = cross_departures(root)
            for before, after in departures:
                if within(value, after):
                    if before not in reported_departures:
                        stale.append({'node': os.path.relpath(before, root), 'reason': '已跨卷移出'})
                        reported_departures.add(before)
                    return None
            raise SafetyError('阶段产物引用了根目录之外或根目录自身')
        if block_kind(value,markers) or not nested_policy_verdict(value, protection.nested_rules, allow_container=True)[0]:
            return None
        assert_no_links(value)
        expected[key(value)] = value
        if is_derived:
            derived.add(key(value))
        return key(value)

    for entry in os.scandir(root):
        if entry.name.casefold() in SKIP_NAMES or is_reparse(entry.path):
            continue
        if is_system_root(root) and (is_system_attr(entry.path) or not nested_verdict(
                entry.path, allow_container=bool(protection.nested_rules), nested_rules=protection.nested_rules)[0]):
            continue
        if entry.is_dir(follow_symlinks=False):
            add(entry.path)
    if not block_kind(probe, markers) and probe.is_dir():
        for file in sorted(probe.glob('*.json')):
            if file.name.startswith('dup_') or block_kind(file, markers):
                continue
            try:
                assert_no_links(file)
                with file.open(encoding='utf-8-sig') as f:
                    obj = json.load(f)
                if not isinstance(obj,dict) or not isinstance(obj.get('path'),str):
                    raise SafetyError('JSON 需要对象及 path 字段')
                node = add(obj['path'])
                if node is None:
                    continue
                if file.name.startswith('units_'):
                    destination = units
                    if not _units_ok(obj.get('total_units')):
                        raise SafetyError('total_units 必须是非负整数')
                    children = obj.get('sub_containers',[])
                elif file.name.startswith('group_'):
                    destination = groups
                    if not isinstance(obj.get('groups'),list):
                        raise SafetyError('groups 必须是数组')
                    children = []
                else:
                    destination = probes
                    if not valid_action(obj.get('action')):
                        valid = ', '.join(sorted(ACTIONS))
                        raise SafetyError(f"action 无效或缺失（当前={obj.get('action')!r}，合法值：{valid}）")
                    children = obj.get('enter_children',[])
                if node in destination:
                    raise SafetyError('同一节点存在重复阶段产物')
                children = children or []
                if not isinstance(children,list):
                    raise SafetyError('子节点清单必须是数组')
                for child in children:
                    value = child.get('path') if isinstance(child,dict) else child
                    if not isinstance(value,str) or not value:
                        raise SafetyError('子节点缺少有效 path')
                    child_key = add(value,expected[node],is_derived=True)
                    if child_key is not None and destination is probes and obj.get('action') == 'enter':
                        enter_children.add(child_key)
                destination[node] = obj
            except (OSError,ValueError) as exc:
                errors.append({'node':'<阶段产物读取错误>', 'action':None,'units':None,
                               'groups':None,'status':'ERROR',
                               'missing':[str(exc) if isinstance(exc,SafetyError)
                                          else 'JSON 无法解析或文件无法读取']})
    rows = []
    for node,path in sorted(expected.items()):
        if block_kind(path,markers):
            continue
        action = probes.get(node,{}).get('action')
        missing = []
        if not os.path.lexists(path):
            # 源路径已不存在（通常是已迁移或已删除），单独诊断，不算遗漏
            stale.append({'node': os.path.relpath(path, root), 'reason': '源路径已不存在'})
            continue
        if not os.path.isdir(path):
            # 文件型节点（如散落的大归档）：文件本身就是唯一单元，没有 S2/S3 可言，
            # 只要求有 S1 探针。过去这里误报成「源路径已不存在」。
            if node not in probes:
                missing.append('S1探针')
            rows.append({'node': os.path.relpath(path, root), 'action': action,
                         'units': 1,
                         'groups': None,
                         'status': 'MISSING' if missing else 'ok', 'missing': missing})
            continue
        if node not in probes and (node not in derived or node in enter_children):
            missing.append('S1探针')
        if action == 'split' and node not in units:
            missing.append('S2清点')
        if node in units and node not in groups:
            missing.append('S3归组')
        row = {'node': os.path.relpath(path, root), 'action': action,
               'units': units.get(node, {}).get('total_units'),
               'groups': len(groups[node]['groups']) if node in groups else None,
               'status': 'MISSING' if missing else 'ok', 'missing': missing}
        if node in units and node in groups:
            try:
                gaps, reason = grouping_coverage(path, units[node], groups[node], markers, root=root,
                                                  nested_rules=protection.nested_rules)
                missing.extend(gaps)
                if missing:
                    row['status'] = 'MISSING'
                elif reason:
                    row.update(status='unverified', reason=reason)
            except (OSError, ValueError) as exc:
                row['status'] = 'ERROR'
                missing.append(str(exc) if isinstance(exc, SafetyError) else '无法核对 S2/S3 单元路径')
        rows.append(row)
    return rows+errors+[{'status':'stale', **x} for x in stale]


def main(argv=None):
    if hasattr(sys.stdout,'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8',errors='replace')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root',nargs='?',default=os.getcwd())
    parser.add_argument('--probe')
    parser.add_argument('--json',action='store_true')
    args = parser.parse_args(argv)
    try:
        rows = check(args.root, args.probe or organize_dir(args.root)/'probe')
    except (OSError,ValueError):
        rows = [{'node':'<根目录或标记读取错误>','status':'ERROR',
                 'missing':['无法安全读取覆盖范围']}]
    failed = any(row.get('status') in ('MISSING', 'ERROR') for row in rows)
    if args.json:
        print(json.dumps(rows,ensure_ascii=False,indent=2))
    else:
        for row in rows:
            status = row.get('status', 'ok')
            detail = row.get('missing') or [row.get('reason') or '']
            label = {'ok': 'ok', 'stale': '过时', 'MISSING': '遗漏', 'unverified': '未核对'}.get(status, status)
            print(f"{row.get('node','-')}: {label} {' / '.join(str(x) for x in detail if x)}")
        if failed:
            print(f'存在遗漏或读取错误，共 {len(rows)} 条检查结果')
        elif any(row['status'] == 'unverified' for row in rows):
            print(f'检查完成，部分单元未能逐项核对，共 {len(rows)} 条检查结果')
        elif rows:
            print(f'本次检查未发现覆盖遗漏，共 {len(rows)} 条检查结果')
        else:
            print('本次没有可检查的节点')
    return int(failed)


if __name__ == '__main__':
    sys.exit(main())
