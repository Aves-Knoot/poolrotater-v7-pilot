#!/usr/bin/env python3
"""Bounded, read-only DokuWiki documentation audit. Publishes only page IDs and issue codes."""
from datetime import datetime, timezone
from pathlib import Path
import json
import os
import re
import time

PAGES = Path('/var/lib/poolrotator-wiki/data/pages')
MEDIA = Path('/var/lib/poolrotator-wiki/data/media')
RELEASE = Path('/srv/poolrotator-wiki/documentation/release.json')
PUBLISHED = Path('/var/lib/poolrotator-wiki/documentation/published.json')
STATUS = Path('/var/lib/poolrotator-wiki/documentation/status.json')
OUTPUT = Path('/var/lib/poolrotator-wiki/documentation/health.json')
LINK = re.compile(r'\[\[([^\]|]+)')
IMAGE = re.compile(r'\{\{([^}|]+)')
REFERENCE = re.compile(r'^\^ Behavior reference \|[^\r\n]*?\b(v\d+\.\d+\.\d+)\b[^\r\n]*?\breviewed\s+(\d{4}-\d{2}-\d{2})[^\r\n]*\|', re.M)
USAGE_BLOCK = re.compile(r'^===== Usage and examples =====(.*?)(?=^===== |\Z)', re.M | re.S)
MODE_NOTE = re.compile(r'\s+\((?:Standard|Mania|4K|7K)\)$')
VERSION = re.compile(r'v(\d+)\.(\d+)\.(\d+)\b')
EXTERNAL = re.compile(r'^(?:[a-z][a-z0-9+.-]*://|mailto:|tel:|//)', re.I)
MAX_PAGES = 500
MAX_PAGE_BYTES = 262144
MAX_ISSUES = 1000


def read_json(path):
    try:
        if path.stat().st_size > 1048576:
            return None
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def version_tuple(value):
    match = VERSION.fullmatch(str(value))
    return tuple(map(int, match.groups())) if match else None


def page_id(path, root):
    return ':'.join(path.relative_to(root).with_suffix('').parts)


def target_id(raw, source):
    """Resolve the documented DokuWiki page-link forms, without guessing externals."""
    target = raw.strip().split('#', 1)[0].strip()
    if not target or EXTERNAL.match(target) or '>' in target:
        return None
    target = target.replace('/', ':')
    if target.startswith(':') or (':' in target and not target.startswith('.')):
        parts = []
    else:
        parts = source.split(':')[:-1]
    for item in target.strip(':').split(':'):
        if item in ('', '.'):
            continue
        if item == '..':
            if parts:
                parts.pop()
        else:
            parts.append(item.lower().replace(' ', '_'))
    return ':'.join(parts) or 'start'


def released_documentation(pages):
    """Only a root-bound article/manifest review can explain a newer review label."""
    import hashlib,stat
    path=Path('/srv/poolrotator-wiki/documentation/reviewed-release.json')
    try:
        st=path.lstat()
        if not stat.S_ISREG(st.st_mode) or st.st_uid!=0 or st.st_mode&0o022:return None
        review=read_json(path)
        if not isinstance(review,dict) or not re.fullmatch(r'v7\.\d+\.\d+',review.get('version','')):return None
        manifest=path.parent/'manifests'/(review['version']+'.json');info=manifest.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_mode&0o022 or hashlib.sha256(manifest.read_bytes()).hexdigest()!=review.get('manifest_sha256'):return None
        rows=review.get('pages')
        if not isinstance(rows,dict) or not 1<=len(rows)<=100:return None
        for name,pin in rows.items():
            if not re.fullmatch(r'[a-z0-9_]+(?::[a-z0-9_]+)*',name) or not isinstance(pin,str) or not re.fullmatch('[a-f0-9]{64}',pin):return None
            article=pages/(name.replace(':','/')+'.txt')
            if article.is_symlink() or article.stat().st_size>262144 or hashlib.sha256(article.read_bytes()).hexdigest()!=pin:return None
        return review
    except (OSError,ValueError,TypeError,AttributeError):return None


def scan(pages=PAGES, media=MEDIA, release_path=RELEASE, published_path=PUBLISHED,
         status_path=STATUS, now=None):
    now = int(time.time() if now is None else now)
    release, published, status = (read_json(p) or {} for p in (release_path, published_path, status_path))
    manifest = published.get('manifest') if isinstance(published.get('manifest'), dict) else {}
    current = release.get('active_version')
    verified = published.get('version')
    issues = []

    def issue(code, severity, page=None, target=None):
        if len(issues) < MAX_ISSUES:
            row = {'code': code, 'severity': severity}
            if page:
                row['page'] = page
            if target:
                row['target'] = target
            issues.append(row)

    current_ok = (release.get('schema') == 1 and release.get('confirmed') is True and
                  release.get('ready') is True and (release.get('reason') == 'verified' or release.get('source_kind') == 'owner_v7_registered' and release.get('reason') == 'registered_code_and_current_workers_verified') and
                  current == verified and status.get('ok') is True and
                  status.get('version') == verified and
                  isinstance(release.get('observed_at'), int) and
                  0 <= now - release['observed_at'] <= 900 and
                  isinstance(status.get('checked_at'), int) and
                  0 <= now - status['checked_at'] <= 900)
    if not current_ok:
        issue('verification_pending', 'action')

    paths = []
    if pages.is_dir():
        for path in sorted(pages.rglob('*.txt')):
            if path.is_symlink() or not path.is_file() or 'drafts' in path.relative_to(pages).parts:
                continue
            paths.append(path)
    if len(paths) > MAX_PAGES:
        issue('page_limit', 'action')
        paths = paths[:MAX_PAGES]
    ids = {page_id(path, pages) for path in paths}
    texts = {}
    for path in paths:
        key = page_id(path, pages)
        if key == 'wiki:syntax':
            continue  # bundled DokuWiki reference with example markup
        if path.stat().st_size > MAX_PAGE_BYTES:
            issue('page_too_large', 'action', key)
            continue
        try:
            texts[key] = path.read_text(encoding='utf-8')
        except (OSError, UnicodeError):
            issue('page_unreadable', 'action', key)

    documentation_review=released_documentation(pages)
    references = 0
    media_references = 0
    for source, text in texts.items():
        # Literal blocks are examples, not navigable page or media links.
        searchable = re.sub(r'<(?:code|file|nowiki)\b[^>]*>.*?</(?:code|file|nowiki)>', '', text, flags=re.I | re.S)
        searchable = re.sub(r'%%.*?%%', '', searchable, flags=re.S)
        for raw in LINK.findall(searchable):
            target = target_id(raw, source)
            if target is None:
                continue
            references += 1
            if target not in ids:
                issue('broken_link', 'action', source, target)
        for raw in IMAGE.findall(searchable):
            name = raw.strip().split('?', 1)[0].split('#', 1)[0]
            if not name or EXTERNAL.match(name):
                continue
            media_references += 1
            normalized = target_id(name, source)
            if not normalized:
                continue
            file = media.joinpath(*normalized.split(':'))
            if not file.is_file() or file.is_symlink():
                issue('missing_media', 'action', source, normalized)

    expected = set()
    commands = manifest.get('commands', []) if isinstance(manifest.get('commands'), list) else []
    features = manifest.get('features', []) if isinstance(manifest.get('features'), list) else []
    for item in commands + features:
        if not isinstance(item, dict) or not isinstance(item.get('article'), str):
            issue('inventory_invalid', 'action')
            continue
        expected.add(item['article'])
    for key in sorted(expected):
        if key not in texts:
            issue('missing_inventory_article', 'action', key)
            continue
        match = REFERENCE.search(texts[key])
        if not match:
            issue('missing_review_reference', 'action', key)
            continue
        reviewed_version = version_tuple(match[1])
        published_version = version_tuple(verified)
        review=documentation_review
        if review and key in review['pages']:
            published_version=max(published_version or (),version_tuple(review['version']) or ())
        try:
            datetime.strptime(match[2], '%Y-%m-%d')
        except ValueError:
            issue('invalid_review_date', 'action', key)
        if reviewed_version and published_version:
            if reviewed_version > published_version:
                issue('review_ahead_of_release', 'action', key)
            elif reviewed_version < published_version:
                # Older reviews are candidates, not proof of inaccurate prose.
                if verified in texts[key]:
                    issue('current_claim_older_review', 'action', key)
                else:
                    issue('older_review_version', 'review', key)

    for command in commands:
        if not isinstance(command, dict) or not isinstance(command.get('article'), str):
            continue
        key = command['article']
        if key not in texts:
            continue
        usage = command.get('usage', [])
        if not isinstance(usage, list):
            issue('inventory_invalid', 'action', key)
            continue
        normalized = set()
        for example in usage:
            if not isinstance(example, str):
                issue('inventory_invalid', 'action', key)
                continue
            if MODE_NOTE.search(example):
                issue('usage_annotation', 'action', key, example)
            normalized.add(MODE_NOTE.sub('', example))
        block = USAGE_BLOCK.search(texts[key])
        if block:
            for raw in re.findall(r'^\s*(\.[^\n<]+)$', block[1], re.M):
                example = MODE_NOTE.sub('', raw.strip())
                if example not in normalized:
                    issue('example_not_in_inventory', 'action', key, example)

    claims = {}
    for command in commands:
        if not isinstance(command, dict):
            continue
        aliases = command.get('aliases', [])
        if not isinstance(aliases, list):
            issue('inventory_invalid', 'action', command.get('article'))
            aliases = []
        for alias in [{'command': command.get('canonical'), 'rulesets': command.get('rulesets', [])}] + aliases:
            if not isinstance(alias, dict):
                continue
            modes = alias.get('rulesets', [])
            for mode in modes if isinstance(modes, list) else []:
                claim = (alias.get('command'), mode)
                if claim in claims:
                    issue('duplicate_alias', 'action', command.get('article'), str(claim[0]))
                claims[claim] = command.get('article')

    order = {'action': 0, 'review': 1}
    issues.sort(key=lambda row: (order.get(row['severity'], 9), row['code'], row.get('page', ''), row.get('target', '')))
    action = sum(row['severity'] == 'action' for row in issues)
    review = sum(row['severity'] == 'review' for row in issues)
    return {'schema': 1, 'scanned_at': now, 'active_version': current,
            'inventory_version': verified, 'verification_current': current_ok,
            'pages_scanned': len(texts), 'links_checked': references,
            'media_checked': media_references, 'action_count': action,
            'review_count': review, 'issues': issues,
            'notes': 'Older review versions are candidates for editorial review, not proof of incorrect behavior. Historical prose is never rewritten by this audit.'}


def publish(report, output=OUTPUT):
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + '.new')
    temporary.write_text(json.dumps(report, ensure_ascii=True, separators=(',', ':')) + '\n')
    temporary.chmod(0o644)
    os.replace(temporary, output)


if __name__ == '__main__':
    result = scan()
    publish(result)
    print(json.dumps({key: result[key] for key in ('pages_scanned', 'links_checked', 'media_checked', 'action_count', 'review_count')}))
