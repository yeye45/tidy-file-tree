"""Read-only episode inventories and sibling-folder grouping proposals."""
from collections import Counter, defaultdict
from hashlib import sha256
from itertools import islice
import json
import os
from pathlib import Path
import re

from executor import guard
from safety import SafetyError, is_system_root
from signals import VID_EXT


EPISODE = re.compile(r'(?i)(?<![a-z0-9])s(\d{1,2})[ ._-]*e(\d{1,4})(?!\d)|第\s*(\d{1,2})\s*季\s*第\s*(\d{1,4})\s*[集话話]')
MULTI_EPISODE = re.compile(
    r'(?i)^(?:[ ._-]*e\d|\s*[-–—+&~～至]\s*(?:e|第\s*)?\d{1,4}(?![a-z0-9])|'
    r'\s*第\s*\d{1,4}\s*[集话話])'
)
SUBTITLE_EXT = {'.srt', '.ass', '.ssa', '.vtt'}
SUBTITLE_TAGS = re.compile(
    r'(?i)(?:[ ._-](?:zh(?:[-_](?:cn|tw|hk|hans|hant))?|chs|cht|chi|'
    r'en(?:[-_](?:us|gb))?|eng|ja|jpn|ko|kor|fr|fra|de|deu|es|spa|'
    r'forced|sdh|cc|简体|繁體|繁体|中英))+$'
)
INTACT_KINDS = {'game', 'runtime', 'vm'}
REVIEW_SAMPLE_LIMIT = 8


def episode_identity(stem):
    matches = list(EPISODE.finditer(stem))
    if len(matches) != 1:
        return None
    match = matches[0]
    tail = stem[match.end():]
    title = re.sub(r'[._\s]+', ' ', stem[:match.start()]).strip(' -')
    if not title or MULTI_EPISODE.match(tail):
        return None
    season, episode = match.groups()[:2] if match[1] is not None else match.groups()[2:]
    return title, int(season), int(episode), tail.casefold()


def inventory(path, entries, root, protection):
    episodes, subtitles = [], []
    excluded = False
    for entry in entries:
        if entry['dir']:
            continue
        name = entry['name']
        file = path / name
        ext = file.suffix.casefold()
        identity = episode_identity(file.stem) if ext in VID_EXT else None
        if identity is None and ext not in SUBTITLE_EXT:
            continue
        try:
            guard(file, root, protection)
        except (OSError, SafetyError):
            excluded = True
            continue
        if identity is None:
            subtitles.append(file)
        else:
            title, season, number, tail = identity
            episodes.append({'name': name, 'title': title, 'season': season, 'episode': number,
                             'variant': tail + ext, 'subtitles': []})

    by_stem = defaultdict(list)
    for episode in episodes:
        by_stem[Path(episode['name']).stem.casefold()].append(episode)
    unpaired = []
    for subtitle in subtitles:
        stem = subtitle.stem.casefold()
        # Prefer an exact media stem before considering an appended language tag.
        matches = by_stem.get(stem) or by_stem.get(SUBTITLE_TAGS.sub('', stem), [])
        if len(matches) == 1:
            matches[0]['subtitles'].append(subtitle.name)
        else:
            unpaired.append(subtitle.name)
    for episode in episodes:
        episode['subtitles'].sort(key=str.casefold)
    episodes.sort(key=lambda entry: (entry['title'].casefold(), entry['season'], entry['episode'],
                                     entry['name'].casefold()))
    return episodes, sorted(unpaired, key=str.casefold), excluded


def proposed_folder(parent, title):
    if re.sub(r'[._\s]+', ' ', parent.name).strip(' -').casefold() == title.casefold():
        return parent
    reserved = re.fullmatch(r'(?i)con|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³]', title)
    if reserved or any(char in '<>:"/\\|?*' or ord(char) < 32 for char in title):
        return None
    return parent / title


def review_question(draft, members, levels, numbers, folder, existing):
    """Summarize already scanned names without exporting their directory paths."""
    parent = levels[draft['id']]['path']
    sources = {node for node, _ in members}
    ordered_sources = sorted(sources, key=lambda node: str(levels[node]['path']))
    aliases = {node: index for index, node in enumerate(ordered_sources, 1)}
    by_source = defaultdict(list)
    patterns = Counter()
    duplicates = sorted(number for number, count in numbers.items() if count > 1)
    unpaired = [(node, name) for node in ordered_sources for name in levels[node]['unpaired']]
    order = sorted(members, key=lambda pair: (pair[1]['episode'], aliases[pair[0]], pair[1]['name']))
    for node, episode in order:
        by_source[node].append(episode)
        patterns[episode['variant']] += 1

    # Prioritize conflicts before filling the sample from the source folders.
    samples = [(None, episode['name'], episode['episode'], 'existingVideo') for episode in existing[:2]]
    samples.extend((node, name, None, 'unpairedSubtitle') for node, name in unpaired[:2])
    duplicate_samples = ((node, episode['name'], episode['episode'], 'video') for node, episode in order
                         if numbers[episode['episode']] > 1)
    samples.extend(islice(duplicate_samples, 4))
    for node in ordered_sources:
        first, last = by_source[node][0], by_source[node][-1]
        samples.extend((node, episode['name'], episode['episode'], 'video') for episode in (first, last))
    sampled = list(dict.fromkeys(samples))[:REVIEW_SAMPLE_LIMIT]
    examples = [{'folder': aliases.get(node), 'name': name, 'episode': number, 'kind': kind}
                for node, name, number, kind in sampled]
    sampled_sources = [node for node, *_ in sampled if node is not None]
    range_sources = list(dict.fromkeys(sampled_sources + ordered_sources))[:REVIEW_SAMPLE_LIMIT]
    ranges = [{'folder': aliases[node], 'videos': len(by_source[node]),
               'min': by_source[node][0]['episode'], 'max': by_source[node][-1]['episode']}
              for node in sorted(range_sources, key=aliases.__getitem__)]
    question = {
        'title': draft['title'], 'season': draft['season'], 'issues': draft['issues'],
        'evidence': {
            'folders': draft['folders'], 'videos': len(members),
            'pairedSubtitles': sum(len(episode['subtitles']) for _, episode in members),
            'unpairedInSourceFolders': len(unpaired),
            'episodes': {'min': min(numbers), 'max': max(numbers), 'distinct': len(numbers),
                         'duplicateCount': len(duplicates), 'duplicateExamples': duplicates[:8]},
            'sourceRanges': ranges,
            'namingPatterns': [{'suffix': suffix[:64], 'count': count}
                               for suffix, count in patterns.most_common(4)],
            'otherPatternCount': max(0, len(patterns) - 4),
            'proposedDestination': ('undetermined' if folder is None else
                                    'commonParent' if folder == parent else 'seriesFolder'),
            'unavailableTargets': sum(row['target'] is None for row in draft['rows']),
            'conflictingTargets': sum(row['nameConflict'] for row in draft['rows']),
            'sampleFiles': examples,
        },
    }
    # Bind answers to the full observed proposal, including changes outside the sample.
    binding = [str(parent), question, sorted(draft['rows'], key=lambda row: row['source']),
               existing, [(aliases[node], episode) for node, episode in order],
               sorted((aliases[node], name) for node, name in unpaired)]
    question['id'] = sha256(json.dumps(binding, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
    return question


def proposals(levels, incomplete, root, protection):
    cohorts = defaultdict(list)
    for node, level in levels.items():
        if level['parent'] in levels:
            for episode in level['episodes']:
                cohorts[(level['parent'], episode['title'].casefold(), episode['season'])].append((node, episode))

    for (parent_id, title_key, season), members in cohorts.items():
        sources = {node for node, _ in members}
        if len(sources) < 2:
            continue
        title = members[0][1]['title']
        parent = levels[parent_id]['path']
        if is_system_root(parent):
            continue
        folder = proposed_folder(parent, title)
        issues = set()
        if parent_id in incomplete or levels[parent_id]['limited'] or levels[parent_id]['excluded'] or any(
                levels[node]['limited'] or levels[node]['excluded'] for node in sources):
            issues.add('incompleteGroup')
        if any(levels[node]['unpaired'] for node in sources):
            issues.add('unpairedSubtitles')
        if any(levels[node]['mixed'] for node in sources):
            issues.add('mixedBatch')
        if len({episode['variant'] for _, episode in members}) > 1:
            issues.add('versions')
        numbers = Counter(episode['episode'] for _, episode in members)

        try:
            if folder is None:
                raise SafetyError('invalid proposed folder')
            guard(folder, root, protection, allow_root=True)
            if folder != parent and os.path.lexists(folder):
                issues.add('existingFolder')
                if not folder.is_dir():
                    folder = None
        except (OSError, SafetyError):
            issues.add('blockedTarget')
            folder = None

        # A matching episode in an existing destination subfolder is already in
        # the cohort. If lifting into the parent, check its episodes here too.
        existing = []
        if folder == parent:
            existing = [episode for episode in levels[parent_id]['episodes']
                        if episode['episode'] in numbers and episode['title'].casefold() == title_key
                        and episode['season'] == season]
            numbers.update(episode['episode'] for episode in existing)
        if max(numbers.values()) > 1:
            issues.add('duplicateEpisode')

        rows = []
        names = Counter(name.casefold() for _, episode in members
                        for name in [episode['name'], *episode['subtitles']])
        for node, episode in sorted(members, key=lambda pair: (pair[1]['episode'], pair[0])):
            for name in [episode['name'], *episode['subtitles']]:
                source = levels[node]['path'] / name
                target = None if folder is None else folder / name
                conflict = False
                if target == source:
                    continue
                if target is not None:
                    try:
                        guard(target, root, protection)
                        conflict = names[name.casefold()] > 1 or os.path.lexists(target)
                        if conflict:
                            issues.add('nameConflict')
                    except (OSError, SafetyError):
                        issues.add('blockedTarget')
                        target = None
                rows.append({'source': str(source), 'target': str(target) if target else None,
                             'episode': episode['episode'], 'subtitle': name != episode['name'],
                             'nameConflict': conflict})
        draft = {'event': 'draft', 'id': parent_id, 'title': title, 'season': season,
                 'folders': len(sources), 'issues': sorted(issues), 'rows': rows}
        if issues:
            draft['reviewQuestion'] = review_question(draft, members, levels, numbers, folder, existing)
        yield draft
