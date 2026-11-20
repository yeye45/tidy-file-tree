"""Read-only episode inventories and sibling-folder grouping proposals."""
from collections import Counter, defaultdict
import os
from pathlib import Path
import re

from executor import guard
from safety import SafetyError
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
        if folder == parent:
            numbers.update(episode['episode'] for episode in levels[parent_id]['episodes']
                           if episode['episode'] in numbers and episode['title'].casefold() == title_key
                           and episode['season'] == season)
        if max(numbers.values()) > 1:
            issues.add('duplicateEpisode')

        rows = []
        names = Counter(name.casefold() for _, episode in members
                        for name in [episode['name'], *episode['subtitles']])
        for node, episode in sorted(members, key=lambda pair: (pair[1]['episode'], pair[0])):
            for name in [episode['name'], *episode['subtitles']]:
                source = levels[node]['path'] / name
                target = None if folder is None else folder / name
                if target == source:
                    continue
                if target is not None:
                    try:
                        guard(target, root, protection)
                        if names[name.casefold()] > 1 or os.path.lexists(target):
                            issues.add('nameConflict')
                    except (OSError, SafetyError):
                        issues.add('blockedTarget')
                        target = None
                rows.append({'source': str(source), 'target': str(target) if target else None,
                             'episode': episode['episode'], 'subtitle': name != episode['name']})
        yield {'event': 'draft', 'id': parent_id, 'title': title, 'season': season,
               'folders': len(sources), 'issues': sorted(issues), 'rows': rows}
