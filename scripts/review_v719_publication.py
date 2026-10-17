"""Bind the isolated public scan to the exact shared-history/statistics artifacts."""
import hashlib
import json
from pathlib import Path
import pprint
import zipfile
from scripts.audit_v71_release import scan, inventory
from scripts.prepare_v71_runtime import fingerprints, source_files
from scripts.prepare_v719_history_runtime import BASELINES

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'validation/v7.1.9-history-stats/publication'


def main():
    from scripts.test_isolation import require_test_isolation
    require_test_isolation('ordinary')
    candidate=OUT.parent/'release';source=candidate/'source'
    q=json.loads((OUT/'preparation.json').read_text())
    with zipfile.ZipFile(ROOT/'validation/v7.1.8-maintenance/release/runtime.zip') as archive:
        baseline={n:archive.read(n) for n in archive.namelist() if n.startswith('payload/')}
        original=baseline['payload/poolrotater_host/integrated_upgrade.py'].decode()
    with zipfile.ZipFile(ROOT/'validation/v7.1.8-maintenance/publication/poolrotater-7.1.8-live-pilot.zip') as archive:
        previouskit={n:archive.read(n) for n in archive.namelist()}
    release_source=(source/'rolling/release.py').read_text()
    after=fingerprints(source_files(source),release_source);pairs=[]
    for path,pin in BASELINES:
        assert hashlib.sha256(path.read_bytes()).hexdigest()==pin
        with zipfile.ZipFile(path) as archive:
            manifest=json.loads(archive.read('bundle.json'))
            pairs.append((fingerprints(manifest['files'],archive.read('payload/rolling/release.py').decode()),after))
    expected=original+'\n# Exact unchanged-storage v7.1.1-v7.1.8 -> v7.1.9 reviewed history and player controls transitions.\nREVIEWED_TRANSITIONS += '+pprint.pformat(tuple(pairs),width=110,sort_dicts=True)+'\n'
    assert (source/'poolrotater_host/integrated_upgrade.py').read_text()==expected
    dispositions=[];unresolved=[]
    def resolution(name,raw,line=None):
        if name=='poolrotater_host/integrated_upgrade.py' and raw.decode()==expected:
            return 'exact_reconstructed_public_source_inventory_grant'
        if baseline.get('payload/'+name)==raw or previouskit.get(name)==raw:
            return 'byte_identical_previously_audited_published_v718_file'
        prior=baseline.get('payload/'+name) or previouskit.get(name)
        if line and prior:
            lines=raw.splitlines(keepends=True)
            context=b''.join(lines[max(0,line-2):min(len(lines),line+1)])
            if context and context in prior:
                return 'exact_unchanged_three_line_region_in_audited_v718_source'

    for folder in ('payload-review','kit','repository-delta'):
        for p in (OUT/folder).rglob('*'):
            if not p.is_file() or p.name=='runtime.zip':continue
            name=p.relative_to(OUT/folder).as_posix();raw=p.read_bytes()
            for finding in scan(raw,folder+'/'+name):
                reason=resolution(name,raw,finding['line'])
                if reason:dispositions.append({**finding,'resolution':reason})
                else:unresolved.append(finding)
    flags=json.loads((OUT/'scan/gitleaks.json').read_text())
    for flag in flags:
        name=flag['File'].removeprefix('/payload/');raw=(OUT/'payload-review'/name).read_bytes()
        row={'category':'gitleaks','path':name,'line':flag['StartLine'],'rule':flag['RuleID']}
        reason=resolution(name,raw,flag['StartLine'])
        if reason:dispositions.append({**row,'resolution':reason})
        else:unresolved.append(row)
    (OUT/'scan/dispositions.json').write_text(json.dumps(dispositions,indent=2)+'\n')
    (OUT/'scan/unresolved.json').write_text(json.dumps(unresolved,indent=2)+'\n')
    assert not unresolved,'Retain redacted unresolved findings for review.'
    receipt=json.loads((OUT/'scan/scan-receipt.json').read_text())
    assert receipt['source_inventory_sha256']==hashlib.sha256(json.dumps(inventory(OUT/'payload-review'),sort_keys=True).encode()).hexdigest()
    def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
    result={'ok':True,'unresolved_findings':0,'gitleaks_flags':len(flags),
        'isolated_receipt_sha256':sha(OUT/'scan/scan-receipt.json'),
        'dispositions_sha256':sha(OUT/'scan/dispositions.json'),
        'runtime_sha256':q['runtime_sha256'],'archive_sha256':q['archive_sha256'],
}
    (OUT/'scan/receipt.json').write_text(json.dumps(result,indent=2)+'\n')
    q.update(gitleaks_pending=False,unresolved_findings=0)
    (OUT/'preparation.json').write_text(json.dumps(q,indent=2)+'\n')
    print(json.dumps(result))


if __name__=='__main__':main()
