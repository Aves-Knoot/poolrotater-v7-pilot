#!/usr/bin/env python3
"""Stage private narrative review packets from verified manifest changes."""
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

STATE = Path('/var/lib/poolrotator-wiki/documentation')
FILES = Path('/srv/poolrotator-wiki/documentation')
PAGES = Path('/var/lib/poolrotator-wiki/data/pages')
LIMIT = 1024 * 1024
VERSION = re.compile(r'v\d+\.\d+\.\d+\Z')
FIELDS = ('title', 'canonical', 'aliases', 'usage', 'usage_rulesets', 'rulesets',
          'availability', 'permissions', 'gates', 'timing', 'purpose', 'summary',
          'search_terms', 'style_arguments', 'commands')


def read_json(path):
    if path.stat().st_size > LIMIT:
        raise ValueError('oversized metadata: ' + str(path))
    return json.loads(path.read_text())


def atomic(path, value):
    payload = json.dumps(value, ensure_ascii=False, indent=2) + '\n'
    previous_gid = os.getegid()
    try:
        if os.geteuid() == 0:
            # Create with the wiki group using the reader's existing SETGID
            # authority; the hardened service deliberately has no CHOWN.
            import pwd
            os.setegid(pwd.getpwnam('poolrotator-wiki').pw_gid)
        fd, name = tempfile.mkstemp(prefix=path.name+'.', dir=path.parent)
    finally:
        os.setegid(previous_gid)
    tmp = Path(name)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), 0o640)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def changed(old, new):
    """Return structured field changes, keeping articles and style arguments."""
    rows = []
    for section in ('commands', 'features'):
        before = {item['id']: item for item in old.get(section, [])}
        after = {item['id']: item for item in new.get(section, [])}
        for key in sorted(before.keys() | after.keys()):
            left, right = before.get(key), after.get(key)
            if left == right:
                continue
            article = (right or left)['article']
            deltas = []
            for field in FIELDS:
                a = left.get(field) if left else None
                b = right.get(field) if right else None
                if a != b:
                    deltas.append({'field': field, 'before': a, 'after': b})
            rows.append({'section': section, 'id': key, 'article': article,
                         'kind': 'added' if left is None else 'removed' if right is None else 'changed',
                         'fields': deltas})
    if old.get('style_arguments') != new.get('style_arguments'):
        a, b = old.get('style_arguments', {}), new.get('style_arguments', {})
        for key in sorted(a.keys() | b.keys()):
            if a.get(key) != b.get(key):
                rows.append({'section': 'styles', 'id': key, 'article': 'wiki:mania_styles',
                             'kind': 'changed', 'fields': [{'field': 'style_arguments',
                             'before': a.get(key), 'after': b.get(key)}]})
    return rows


def literal(value):
    # Manifest validator disallows %% in prose. JSON keeps arrays/maps legible.
    value = json.dumps(value, ensure_ascii=False, sort_keys=True) if not isinstance(value, str) else value
    value = value.replace('%%', '% %')
    return '%%' + value + '%%'


def render(old, new, rows, page_hashes, source_changes):
    version = new['version']
    lines = [f'====== Release article review · {version} ======', '',
             'This is an automatically prepared review packet. It is private to wiki editors and maintainers. Check the linked source and actual deployed behavior before copying any text into a public article.', '',
             f'^ Previous verified inventory | {old["version"]} |',
             f'^ Current verified inventory | {version} · reviewed {new["reviewed_on"]} |',
             '^ Release evidence | Verified release-owned manifest; compare the release summary if available |',
             '', '===== Changed source fingerprints =====']
    if source_changes:
        lines += [f'  * {literal(path)}' for path in source_changes]
    else:
        lines.append('  * No bot source fingerprint changed; inspect the manifest correction and deployment evidence.')
    lines += ['', '===== Article proposals =====']
    if not rows:
        lines.append('No command, feature, or style-argument facts changed. Review release notes for narrative-only changes before closing this packet.')
    for row in rows:
        lines += ['', f'==== {row["section"].title()}: {row["id"]} ====',
                  f'Article: [[{row["article"]}]] · {row["kind"]}.',
                  f'Current page SHA-256 at drafting: {literal(page_hashes.get(row["article"]))}. Recheck the page before applying an edit.']
        for field in row['fields']:
            lines += [f'  * {field["field"]}: {literal(field["before"])} → {literal(field["after"])}']
        lines += ['Suggested edit: update the article wording and examples to match the new verified fields above; keep claims about eligibility, timing, and permission within their stated scope. Add a crosslink to this release summary where the change needs context.',
                  'Review checks: test copyable syntax; confirm availability and gates; check related links and examples; update the Behavior reference only after reviewing the whole article.']
    lines += ['', '===== Maintainer decision =====',
              'Mark each affected article reviewed in your editorial workflow. Publish prose through normal DokuWiki revisions. This packet never approves or publishes narrative changes automatically.', '']
    return '\n'.join(lines)


def main():
    import fcntl
    STATE.mkdir(exist_ok=True)
    with (STATE/'drafts.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        publication = read_json(STATE/'published.json')
        release = read_json(FILES/'release.json')
        status = read_json(STATE/'status.json')
        version = publication.get('version', '')
        if not VERSION.fullmatch(version) or not (release.get('confirmed') and release.get('ready') and (release.get('reason') == 'verified' or release.get('source_kind') == 'owner_v7_registered' and release.get('reason') == 'registered_code_and_current_workers_verified') and release.get('active_version') == version and status.get('ok') and status.get('version') == version):
            print('No currently verified publication; draft state retained.')
            return
        current = publication['manifest']
        baseline_path = STATE/'draft_baseline.json'
        baseline = read_json(baseline_path) if baseline_path.exists() else None
        if baseline is None:
            atomic(baseline_path, {'schema': 1, 'manifest_digest': publication['manifest_digest'], 'manifest': current})
            print('Seeded verified draft baseline '+version+'.')
            return
        if baseline['manifest_digest'] == publication['manifest_digest']:
            print('Draft baseline unchanged.')
            return
        previous = baseline['manifest']
        rows = changed(previous, current)
        page_hashes = {}
        for row in rows:
            article = row['article']
            path = PAGES/(article.replace(':', '/')+'.txt')
            page_hashes[article] = digest(path.read_bytes()) if path.is_file() else None
        old_hashes, new_hashes = previous.get('source_hashes', {}), current.get('source_hashes', {})
        source_changes = sorted(key for key in old_hashes.keys() | new_hashes.keys() if old_hashes.get(key) != new_hashes.get(key))
        page_id = 'drafts:release_'+version.replace('.', '_')
        output = render(previous, current, rows, page_hashes, source_changes)
        plan = {'schema': 1, 'version': version, 'page_id': page_id,
                'manifest_digest': publication['manifest_digest'], 'text': output}
        if release.get('source_kind') == 'rolling_worker':
            plan.update(source_kind='rolling_worker', supervisor_version=release['supervisor_version'])
        if release.get('source_kind') == 'owner_v7_registered':
            plan.update(source_kind='owner_v7_registered',
                        runtime_sha256=release['runtime_sha256'],
                        source_files_sha256=release['source_files_sha256'])
        plan_path = STATE/'draft_plan.json'
        atomic(plan_path, plan)
        # The PHP helper refuses an existing draft, so contributor edits are never overwritten.
        command = ['/usr/bin/php', str(FILES/'draft_commit.php'), str(plan_path)]
        if plan.get('source_kind') == 'owner_v7_registered':
            # The privileged reader attests source; native revisions retain the wiki UID.
            import pwd
            if os.geteuid() != 0 or FILES != Path('/srv/poolrotator-wiki/documentation') or STATE != Path('/var/lib/poolrotator-wiki/documentation'):
                raise ValueError('owner draft requires its fixed privileged reader')
            if pwd.getpwnam('poolrotator-wiki').pw_uid <= 0:
                raise ValueError('native wiki writer identity unavailable')
            command = ['/usr/sbin/runuser', '-u', 'poolrotator-wiki', '--', *command]
        subprocess.run(command, check=True, timeout=30)
        written = PAGES/(page_id.replace(':', '/')+'.txt')
        if not written.is_file() or digest(written.read_bytes()) != digest(output.encode()):
            raise ValueError('Private draft was not saved; baseline and queue retained')
        queue = {'schema': 1, 'version': version, 'previous_version': previous['version'],
                 'draft_page': page_id, 'manifest_digest': publication['manifest_digest'],
                 'prepared_at': int(time.time()), 'changed_articles': sorted(page_hashes),
                 'changed_source_files': source_changes, 'status': 'awaiting_maintainer_review'}
        atomic(STATE/'draft_queue.json', queue)
        atomic(baseline_path, {'schema': 1, 'manifest_digest': publication['manifest_digest'], 'manifest': current})
        plan_path.unlink(missing_ok=True)
        print('Prepared private release review packet '+page_id+'.')


if __name__ == '__main__':
    main()
