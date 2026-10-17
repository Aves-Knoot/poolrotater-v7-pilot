"""Repackage exact published v7.1.6 with the corrected stopped repair helper."""
import ast
import hashlib
import json
from pathlib import Path
import shutil
import zipfile
from scripts.prepare_pilot_release import scan, zip_tree
from scripts.test_isolation import require_test_isolation

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'validation/v7.1.6-repair1/publication'
BASE=ROOT/'validation/v7.1.6-feedback/publication/poolrotater-7.1.6-live-pilot.zip'
BASE_SHA='7082a309f56c105d9471a48a3406f7c92fc573a795a7aa690f31dbe46a037239'
RUNTIME_SHA='554ac796491d912ee6c94965f09e65897bd3eb2559785540b5304e9a4a15d9ee'
TAG='v7.1.6-repair1'

def sha(raw):return hashlib.sha256(raw).hexdigest()

def main():
    require_test_isolation('ordinary')
    assert sha(BASE.read_bytes())==BASE_SHA
    assert not OUT.exists(),'retain partial output before retry'
    OUT.mkdir(parents=True);kit=OUT/'kit';kit.mkdir()
    with zipfile.ZipFile(BASE)as z:
        old=json.loads(z.read('release.json'))
        assert old['runtime_sha256']==RUNTIME_SHA
        for info in z.infolist():
            name=info.filename
            assert not name.startswith('/') and '..' not in Path(name).parts
            if name in ('release.json','SHA256SUMS.txt','V7_1_5_REPAIR_AND_START.md'):continue
            p=kit/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(z.read(name))
    assert sha((kit/'runtime.zip').read_bytes())==RUNTIME_SHA
    shutil.copyfile(ROOT/'pilot/repair_and_start.py',kit/'repair_and_start.py')
    shutil.copyfile(ROOT/'docs/V7_1_6_REPAIR1.md',kit/'V7_1_6_REPAIR1.md')
    (kit/'README.md').write_text('# v7.1.6-repair1 stopped-host repair\n\n'
        'Read V7_1_6_REPAIR1.md and use the new pinned repair launcher for the existing stopped installation. '
        'It fixes missing MainPID on systemd timers and installs the exact published v7.1.6 runtime. '
        'Preserve all existing state; do not reinstall, reset OAuth or repeat an uncertain start. '
        'The old v7.1.5 repair command is superseded.\n\n'
        'For ordinary v7.1.6 installation/operation see V7_1_6_OPERATOR_GUIDE.md and LIVE_AUTHORIZATION.md.\n')
    release={**old,'kind':'qualified-systemd-timer-stopped-repair','repair_revision':1,
        'files':{p.relative_to(kit).as_posix():{'sha256':sha(p.read_bytes()),'bytes':p.stat().st_size}
                 for p in sorted(kit.rglob('*'))if p.is_file()}}
    (kit/'release.json').write_text(json.dumps(release,indent=2)+'\n')
    (kit/'SHA256SUMS.txt').write_text(''.join(sha(p.read_bytes())+'  '+p.relative_to(kit).as_posix()+'\n'
        for p in sorted(kit.rglob('*'))if p.is_file()and p.name!='SHA256SUMS.txt'))
    archive=OUT/'poolrotater-7.1.6-repair1-live-pilot.zip';zip_tree(kit,archive)
    launcher=(ROOT/'pilot/repair_launcher.py.template').read_text().replace('__ARCHIVE_SHA256__',sha(archive.read_bytes()))
    ast.parse(launcher);launcher_name='poolrotater-repair-v7.1.6-repair1.py'
    (OUT/launcher_name).write_text(launcher)
    (OUT/'RELEASE_SHA256SUMS.txt').write_text(sha(archive.read_bytes())+'  '+archive.name+'\n'
        +RUNTIME_SHA+'  runtime.zip\n'+sha(launcher.encode())+'  '+launcher_name+'\n')
    delta=OUT/'repository-delta';delta.mkdir()
    for name in ('pilot/repair_and_start.py','pilot/repair_launcher.py.template','tests/test_repair_and_start.py',
                 'tests/prompt_feedback_installed_fixture.py','scripts/prepare_repair1_publication.py','docs/V7_1_6_REPAIR1.md'):
        p=delta/name;p.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(ROOT/name,p)
    (delta/'pilot/repair_launcher.py').write_text(launcher)
    (delta/'README.md').write_text('# Stopped-host repair: v7.1.6-repair1\n\n'
        'Use the [corrected pinned repair release](https://github.com/Aves-Knoot/poolrotater-v7-pilot/releases/tag/v7.1.6-repair1) '
        'and [repair guide](docs/V7_1_6_REPAIR1.md) for an existing stopped host. '
        'This corrects timer MainPID handling and failure diagnostics in the installer helper; '
        'the bot runtime remains the exact published v7.1.6. The v7.1.5 repair launcher is superseded; its assets stay unchanged.\n\n'
        +(OUT.parent/'readme-before.md').read_text())
    review=OUT/'payload-review';review.mkdir()
    # Re-scan changed code only; the embedded runtime is byte-identical to the
    # already audited/published v7.1.6, not rebuilt from the development checkout.
    for name in ('pilot/repair_and_start.py','pilot/repair_launcher.py','pilot/repair_launcher.py.template',
                 'tests/test_repair_and_start.py','scripts/prepare_repair1_publication.py'):
        if name.endswith('.template'):continue
        p=review/name;p.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(delta/name,p)
    for folder in(kit,delta):
        for p in folder.rglob('*'):
            if p.is_file()and p.name!='runtime.zip':
                scan(p.read_bytes())
                if p.suffix=='.py':ast.parse(p.read_bytes())
    q={'ok':True,'runtime_sha256':RUNTIME_SHA,'runtime_unchanged':True,'archive':archive.name,
       'archive_sha256':sha(archive.read_bytes()),'archive_bytes':archive.stat().st_size,
       'launcher':launcher_name,'launcher_sha256':sha(launcher.encode()),
       'github_parent':json.loads((OUT.parent/'github-before.json').read_text())['head'],
       'changed_public_files':{p.relative_to(delta).as_posix():{'sha256':sha(p.read_bytes()),'bytes':p.stat().st_size}
            for p in sorted(delta.rglob('*'))if p.is_file()},'gitleaks_pending':True,'unresolved_findings':None}
    (OUT/'preparation.json').write_text(json.dumps(q,indent=2)+'\n')
    print(json.dumps({k:q[k]for k in('ok','runtime_unchanged','archive_sha256','launcher_sha256')}))

if __name__=='__main__':main()
