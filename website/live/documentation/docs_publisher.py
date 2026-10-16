#!/usr/bin/env python3
"""Read verified release facts, then publish wiki-only changes. No bot imports/API."""
import argparse,ast,fcntl,hashlib,json,math,os,re,subprocess,time
from pathlib import Path
from datetime import datetime,timezone
import sys
sys.path.insert(0,'/usr/local/lib/poolrotator-ops')
import owner_source
OPERATION_SOURCE=owner_source.selected()
SOURCE=Path('/opt/poolrotator');RELEASES=Path('/opt/poolrotator-releases')
def source_version(release):return owner_source.release_version(OPERATION_SOURCE,release)
DEPLOY=Path(os.environ.get('POOLROTATOR_DOCS_DEPLOY','/var/lib/poolrotator-ops/deploy'))
ROLLING=Path(os.environ.get('POOLROTATOR_DOCS_ROLLING','/opt/poolrotator-runtime/state/rolling/public-status.json'))
FILES=Path('/srv/poolrotator-wiki/documentation');MANIFESTS=FILES/'manifests'
STATE=Path('/var/lib/poolrotator-wiki/documentation');PAGES=Path('/var/lib/poolrotator-wiki/data/pages')
DW=Path('/srv/poolrotator-wiki/dokuwiki');BASELINE=FILES/'baseline.json'
VERSION=re.compile(r'v\d+\.\d+\.\d+\Z');ID=re.compile(r'[a-z][a-z0-9_]*\Z');CMD=re.compile(r'\.[a-z][a-z0-9]*\Z');MODES={'standard','mania4k','mania7k'}
def digest(data):return hashlib.sha256(data).hexdigest()
def readjson(path):
 if path.stat().st_size>1024*1024:raise ValueError('JSON exceeds size limit')
 return json.loads(path.read_text())
def atomic(path,value):
 temp=path.with_name(path.name+'.new');temp.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n');temp.chmod(0o644);temp.replace(path)
def prose(value,maximum=1800):
 if not isinstance(value,str) or not value or len(value)>maximum or any(ord(c)<32 for c in value) or '%%' in value:raise ValueError('invalid plain text')
 return value
def modes(items):
 if not isinstance(items,list) or not items or len(set(items))!=len(items) or not set(items)<=MODES:raise ValueError('invalid rulesets')
def validate(m,release):
 if set(m)!={'schema','version','reviewed_on','scope','source_hashes','commands','features','style_arguments'}:raise ValueError('unknown manifest fields')
 if m.get('schema')!=1 or m.get('version')!=source_version(release) or not VERSION.fullmatch(m['version']):raise ValueError('manifest version mismatch')
 if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',m.get('reviewed_on','')):raise ValueError('missing review date')
 hashes=m.get('source_hashes',{})
 required={str(p.relative_to(release)) for p in (release/'bot').glob('*.py') if '_pre_' not in p.name}
 if not required or set(hashes)!=required:raise ValueError('source fingerprint coverage mismatch')
 for name,expected in hashes.items():
  if not re.fullmatch(r'bot/[A-Za-z_][A-Za-z_0-9]*\.py',name) or not re.fullmatch('[0-9a-f]{64}',expected):raise ValueError('invalid source fingerprint')
  path=release/name
  if path.is_symlink() or path.resolve().parent!=release/'bot' or path.stat().st_size>2*1024*1024 or digest(path.read_bytes())!=expected:raise ValueError('source changed since documentation review')
 commands=m.get('commands',[]);features=m.get('features',[])
 if not isinstance(commands,list) or not 1<=len(commands)<=100 or not isinstance(features,list) or not 1<=len(features)<=60:raise ValueError('inventory limits')
 ids=set();claimed={}
 for c in commands:
  if set(c)!={'id','title','canonical','aliases','usage','usage_rulesets','rulesets','availability','permissions','gates','timing','purpose','summary','search_terms','article'}:raise ValueError('unknown command fields')
  key=c.get('id','')
  if not ID.fullmatch(key) or key in ids or c.get('article')!='commands:'+key:raise ValueError('invalid command ID')
  if any(x in c['title'] for x in ['[',']','|','<','>']):raise ValueError('unsafe link title')
  ids.add(key);prose(c['title'],100);prose(c['summary']);prose(c['availability']);prose(c['permissions']);prose(c['timing']);modes(c['rulesets'])
  if not CMD.fullmatch(c['canonical']) or c['purpose'] not in {'help','stats','selection','feedback','session'}:raise ValueError('invalid command metadata')
  for field in ['gates','usage','search_terms']:
   if not isinstance(c[field],list) or len(c[field])>30:raise ValueError('command list limit')
   for value in c[field]:prose(value,1800)
  if set(c['usage_rulesets'])!=set(c['usage']):raise ValueError('missing usage applicability')
  for usage_modes in c['usage_rulesets'].values():
   modes(usage_modes)
   if not set(usage_modes)<=set(c['rulesets']):raise ValueError('usage ruleset escapes command')
  if not c['usage'] or any(not re.match(r'\.[a-z][a-z0-9]*(?: |$)',u) or re.search(r'\s+\((?:Standard|Mania|4K|7K)\)$',u) for u in c['usage']):raise ValueError('invalid usage')
  if not isinstance(c['aliases'],list) or len(c['aliases'])>30:raise ValueError('alias limit')
  for a in [{'command':c['canonical'],'rulesets':c['rulesets']}]+c['aliases']:
   if set(a)!={'command','rulesets'}:raise ValueError('unknown alias fields')
   if not CMD.fullmatch(a['command']):raise ValueError('invalid alias')
   modes(a['rulesets'])
   if not set(a['rulesets'])<=set(c['rulesets']):raise ValueError('alias ruleset escapes command')
   for mode in a['rulesets']:
    claim=(a['command'],mode)
    if claim in claimed:raise ValueError('duplicate alias')
    claimed[claim]=key
 feature_ids=set()
 for f in features:
  if set(f)!={'id','title','article','summary','availability','commands'}:raise ValueError('unknown feature fields')
  key=f.get('id','')
  if not ID.fullmatch(key) or key in feature_ids or f.get('article')!='wiki:'+key:raise ValueError('invalid feature ID')
  if any(x in f['title'] for x in ['[',']','|','<','>']):raise ValueError('unsafe feature title')
  feature_ids.add(key)
  for field in ['title','summary','availability']:prose(f[field])
  if not isinstance(f['commands'],list) or len(f['commands'])>100 or any(v not in {'commands:'+k for k in ids} for v in f['commands']):raise ValueError('invalid feature command reference')
 args=m.get('style_arguments',{})
 if not isinstance(args,dict) or len(args)>100 or any(not ID.fullmatch(k) or v not in ids for k,v in args.items()):raise ValueError('invalid style argument')
 tree=ast.parse((release/'bot/bot.py').read_text())
 style_node=next((n.value for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='_MANIA_STYLE_ALIASES' for t in n.targets)),None)
 if style_node is None:raise ValueError('style implementation contract changed')
 if ast.literal_eval(style_node)!=args:raise ValueError('style arguments differ from implementation')
 prose(m['scope'])
 return m

def active():
 if OPERATION_SOURCE.owner:return owner_source.fresh_code_proof(OPERATION_SOURCE)[1]
 release=SOURCE.resolve(strict=True)
 if release.parent!=RELEASES or not VERSION.fullmatch(release.name):raise ValueError('unexpected active release')
 return release

def linked_release():
 if OPERATION_SOURCE.owner:return owner_source.fresh_code_proof(OPERATION_SOURCE)[1]
 target=Path(os.readlink(SOURCE))
 if not target.is_absolute() or target.parent!=RELEASES or not VERSION.fullmatch(target.name):raise ValueError('unexpected active release link')
 return target

def version_key(version):
 if not isinstance(version,str) or not VERSION.fullmatch(version):raise ValueError('invalid worker version')
 return tuple(map(int,version[1:].split('.')))

def recent(value,now,maximum=60):
 return (type(value) in (int,float) and math.isfinite(value)
         and -30<=now-value<=maximum)

def worker_release(supervisor,now=None):
 """Only the sanitized live feed supplies deployment evidence; never targets."""
 now=time.time() if now is None else now
 feed=readjson(ROLLING)
 if not recent(feed.get('generated_at'),now) or feed.get('supervisor_version')!=supervisor.name:
  raise ValueError('rolling supervisor evidence stale or mismatched')
 rooms=feed.get('rooms')
 if not isinstance(rooms,dict) or not 1<=len(rooms)<=32:raise ValueError('invalid worker evidence')
 versions={};healthy=set()
 for name,room in rooms.items():
  if not re.fullmatch(r'room[1-9][0-9]*',name) or not isinstance(room,dict):raise ValueError('invalid worker identity')
  version=room.get('version');version_key(version)
  if not recent(room.get('observed_at'),now):raise ValueError('worker heartbeat stale')
  versions[name]=version
  if room.get('healthy') is True:healthy.add(version)
 latest=max(versions.values(),key=version_key)
 if latest not in healthy:raise ValueError('latest deployed worker has no healthy evidence')
 return RELEASES/latest,versions

def sealed(release):
 """Static immutable-release/test checks; no bot imports, SQLite or transport."""
 path=release/'release.json'
 if release.is_symlink() or path.is_symlink() or release.parent!=RELEASES:
  raise ValueError('sealed release path escapes release root')
 for owned in (release,release.parent,path):
  if owned.stat().st_uid!=0 or owned.stat().st_mode & 0o022:raise ValueError('release is mutable')
 m=readjson(path)
 tests=m.get('validation',{}).get('tests')
 passing=(type(tests) is int and tests>0) or (isinstance(tests,dict)
          and type(tests.get('count')) is int and tests['count']>0
          and tests.get('failures')==0 and tests.get('errors')==0)
 if (m.get('format')!=1 or m.get('version')!=source_version(release) or m.get('migrations')!=[]
     or not passing):
  raise ValueError('passing sealed validation is missing')
 files=m.get('files')
 if not isinstance(files,dict) or not 1<=len(files)<=2000:raise ValueError('invalid sealed file inventory')
 for name,expected in files.items():
  if (not isinstance(name,str) or Path(name).is_absolute() or '..' in Path(name).parts
      or not isinstance(expected,str) or not re.fullmatch('[0-9a-f]{64}',expected)):
   raise ValueError('invalid sealed file fingerprint')
  item=release/name
  if (item.is_symlink() or item.resolve()!=item.absolute()
      or not item.resolve().is_relative_to(release)
      or item.stat().st_uid!=0 or item.stat().st_mode & 0o022
      or item.parent.stat().st_uid!=0 or item.parent.stat().st_mode & 0o022):raise ValueError('sealed file mutable')
  actual=hashlib.sha256()
  with item.open('rb') as stream:
   for block in iter(lambda:stream.read(1024*1024),b''):actual.update(block)
  if actual.hexdigest()!=expected:raise ValueError('sealed release contents changed')
 return m

def compatible(supervisor,worker):
 baseline,candidate=sealed(supervisor),sealed(worker)
 for key in ('protocol','shared','storage','storage_epoch','requirements','python_hash','environment_hash'):
  if key not in baseline or key not in candidate or baseline[key]!=candidate[key]:
   raise ValueError('worker incompatible with confirmed shared release')

def publication_release(candidate):
 if OPERATION_SOURCE.owner:return owner_source.documentation_release(OPERATION_SOURCE,candidate)
 if candidate.get('source_kind')!='rolling_worker':return linked_release()
 path=FILES/'release.json'
 if path.is_symlink() or path.stat().st_uid!=0 or path.stat().st_mode & 0o022:
  raise ValueError('deployment attestation not root-owned')
 proof=readjson(path)
 if (not recent(proof.get('observed_at'),time.time()) or proof.get('confirmed') is not True
     or proof.get('ready') is not True or proof.get('source_kind')!='rolling_worker'
     or proof.get('active_version')!=candidate['version']
     or proof.get('manifest_digest')!=candidate['manifest_digest']
     or proof.get('supervisor_version')!=candidate.get('supervisor_version')
     or linked_release().name!=candidate.get('supervisor_version')):
  raise ValueError('worker deployment attestation changed or expired')
 return RELEASES/candidate['version']

def prepare():
 FILES.mkdir(parents=True,exist_ok=True)
 with (FILES/'prepare.lock').open('w') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  if OPERATION_SOURCE.owner:
   return owner_source.documentation_prepare(sys.modules[__name__],OPERATION_SOURCE)
  observed=dict(schema=1,observed_at=int(time.time()),active_version=None,confirmed=False,ready=False,reason='source_unavailable')
  try:
   supervisor=active();release=supervisor;observed['active_version']=release.name
   marker=readjson(DEPLOY/'last-attempt.json')
   deployed=datetime.fromisoformat(marker.get('at',''))
   if deployed.tzinfo is None:raise ValueError('deployment date missing timezone')
   rolling=ROLLING.exists() or bool(os.environ.get('POOLROTATOR_DOCS_ROLLING'))
   legacy_confirmed=marker.get('status')=='success' and marker.get('version')==supervisor.name
   if not legacy_confirmed and not rolling:observed['reason']='deployment_unconfirmed'
   else:
    observed['confirmed']=True;observed['deployed_at']=marker.get('at') if legacy_confirmed else None;observed['reason']='manifest_missing'
    if rolling:
     release,versions=worker_release(supervisor)
     observed.update(source_kind='rolling_worker',supervisor_version=supervisor.name,
                     active_version=release.name,room_versions=versions)
     observed['supervisor_deployed_at']=observed.pop('deployed_at')
     compatible(supervisor,release)
    path=release/'docs/wiki-manifest.json'
    if not path.exists():path=MANIFESTS/(release.name+'.json')
    if path.exists():
     if path.is_symlink() or (path.parent!=MANIFESTS and not path.resolve().is_relative_to(release)):raise ValueError('manifest path escapes release')
     if path.stat().st_uid!=0 or path.stat().st_mode & 0o022:raise ValueError('manifest is not release-owned')
     manifest=validate(readjson(path),release)
     # Recheck both marker and link after all source reads.
     if active()!=supervisor or readjson(DEPLOY/'last-attempt.json')!=marker:raise ValueError('deployment changed while reading')
     if rolling:
      current,versions=worker_release(supervisor)
      if current!=release:raise ValueError('worker deployment changed while reading')
      observed['room_versions']=versions
     observed.update(ready=True,reason='verified',reviewed_on=manifest['reviewed_on'])
     observed['manifest_digest']=digest(json.dumps(manifest,sort_keys=True).encode())
     candidate=dict(schema=1,version=release.name,event_id=digest((release.name+str(marker.get('at'))).encode()),manifest_digest=observed['manifest_digest'],manifest=manifest)
     if rolling:candidate.update(source_kind='rolling_worker',supervisor_version=supervisor.name)
     atomic(FILES/'candidate.json',candidate)
  except (OSError,ValueError,KeyError,TypeError,AttributeError,SyntaxError,json.JSONDecodeError):
   observed['ready']=False;observed['reason']='source_or_manifest_invalid'
  if not observed['ready']:(FILES/'candidate.json').unlink(missing_ok=True)
  atomic(FILES/'release.json',observed)
  print(json.dumps({k:observed[k] for k in ['active_version','confirmed','ready','reason']}))

# DokuWiki syntax escaped as unformatted inline text. Wiki targets are schema-validated.
def literal(s):return '%%'+s+'%%'
def command_table(m):
 lines=['^ Command ^ Syntax and aliases ^ Availability ^']
 for c in m['commands']:
  tokens=[c['canonical']]+[a['command']+(' (Mania)' if a['rulesets']!=c['rulesets'] else '') for a in c['aliases']]+[u+(' (Mania)' if c['usage_rulesets'][u]==['mania4k','mania7k'] and 'standard' in c['rulesets'] else ' (Standard)' if c['usage_rulesets'][u]==['standard'] and len(c['rulesets'])>1 else '') for u in c['usage'] if u!=c['canonical']]
  lines.append('| [['+c['article']+'|'+c['title']+']] | '+literal(' / '.join(dict.fromkeys(tokens)))+' | '+literal(c['availability'])+' |')
 return '\n'.join(lines)
def feature_page(m):
 lines=['====== Feature inventory ======','','This generated inventory follows verified release facts. Detailed articles remain editable; propose inventory changes through the release documentation contract.','','^ Feature ^ What it does ^ Availability ^ Commands ^']
 for f in m['features']:
  links=', '.join('[['+v+'|.'+next(c['canonical'][1:] for c in m['commands'] if c['article']==v)+']]' for v in f['commands']) or 'See article'
  lines.append('| [['+f['article']+'|'+f['title']+']] | '+literal(f['summary'])+' | '+literal(f['availability'])+' | '+links+' |')
 lines+=['','Facts reviewed '+m['reviewed_on']+' for '+m['version']+'. This verifies the inventory, not every paragraph in the linked articles.','','See [[commands:start|commands]], [[wiki:start|detailed features]], and [[wiki:documentation_updates|how automatic updates work]].','']
 return '\n'.join(lines)
def section(text,key):
 if key=='commands:start':
  match=re.search(r'^\^ Command \^.*?(?=\n\n|\Z)',text,re.S|re.M)
  if not match:raise ValueError('command table missing')
  return match.group(0)
 return text

def publish(dry=False):
 STATE.mkdir(parents=True,exist_ok=True)
 with (STATE/'publish.lock').open('w') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  observed=readjson(FILES/'release.json')
  if not observed.get('ready') or int(time.time())-observed['observed_at']>600:
   print('No verified candidate; existing documentation retained.');return
  candidate=readjson(FILES/'candidate.json');m=candidate['manifest']
  if source_version(publication_release(candidate))!=candidate['version']:raise ValueError('active version changed before publication')
  previous=readjson(STATE/'published.json') if (STATE/'published.json').exists() else None
  if previous and previous['manifest_digest']==candidate['manifest_digest']:
   for key,expected in previous['ownership'].items():
    path=PAGES/(key.replace(':','/')+'.txt')
    if not path.exists() or digest(section(path.read_text(),key).encode())!=expected:raise ValueError('generated content edited; preserve contributor changes: '+key)
   atomic(STATE/'status.json',dict(ok=True,checked_at=int(time.time()),version=candidate['version']))
   print('Inventory unchanged; no wiki revisions created.');return
  for item in m['commands']+m['features']:
   if not (PAGES/(item['article'].replace(':','/')+'.txt')).is_file():raise ValueError('linked article missing: '+item['article'])
  ownership=previous['ownership'] if previous else readjson(BASELINE)
  resume=readjson(STATE/'pending.json') if (STATE/'pending.json').exists() else None
  if resume and resume['manifest_digest']!=candidate['manifest_digest']:raise ValueError('unfinished previous change set needs review')
  changes=resume['changes'] if resume else []
  for key,wanted in ([] if resume else [('commands:start',command_table(m)),('wiki:feature_inventory',feature_page(m))]):
   path=PAGES/(key.replace(':','/')+'.txt');old=path.read_text() if path.exists() else '';owned=section(old,key) if old else ''
   expected=ownership.get(key)
   if expected!=(digest(owned.encode()) if old else None):raise ValueError('generated content edited; preserve contributor changes: '+key)
   new=old.replace(owned,wanted,1) if key=='commands:start' else wanted
   if new!=old:changes.append(dict(id=key,expected_sha256=digest(old.encode()) if path.exists() else None,text=new))
  pending=dict(schema=1,version=candidate['version'],manifest_digest=candidate['manifest_digest'],changes=changes)
  if candidate.get('source_kind')=='rolling_worker':
   pending.update(source_kind='rolling_worker',supervisor_version=candidate['supervisor_version'])
  elif candidate.get('source_kind')=='owner_v7_registered':
   pending.update(source_kind='owner_v7_registered',runtime_sha256=candidate['runtime_sha256'],
       source_files_sha256=candidate['source_files_sha256'])
  atomic(STATE/'pending.json',pending)
  if dry:print(json.dumps({'dry_run':True,'pages':[c['id'] for c in changes]}));return
  # PHP takes all DokuWiki locks and rechecks hashes before creating revisions.
  subprocess.run(owner_source.wiki_commit_argv(OPERATION_SOURCE,FILES/'commit.php',STATE/'pending.json'),check=True,timeout=60)
  for change in changes:
   if (PAGES/(change['id'].replace(':','/')+'.txt')).read_text()!=change['text']:raise ValueError('native revision did not match the verified plan')
  if source_version(publication_release(candidate))!=candidate['version']:raise ValueError('active version changed during publication; rerun verification')
  result=dict(schema=1,version=candidate['version'],manifest_digest=candidate['manifest_digest'],event_id=candidate['event_id'],published_at=int(time.time()),manifest=m,ownership={k:digest(v.encode()) for k,v in [('commands:start',command_table(m)),('wiki:feature_inventory',feature_page(m))]})
  atomic(STATE/'published.json',result)
  atomic(STATE/'status.json',dict(ok=True,checked_at=int(time.time()),version=candidate['version']))
  (STATE/'pending.json').unlink(missing_ok=True)
  print('Published verified inventory for '+candidate['version']+' ('+str(len(changes))+' wiki revisions).')

def main():
 parser=argparse.ArgumentParser();parser.add_argument('action',choices=['prepare','publish','validate']);parser.add_argument('--dry-run',action='store_true');parser.add_argument('--manifest');parser.add_argument('--release');args=parser.parse_args()
 if args.action=='prepare':prepare()
 elif args.action=='publish':
  try:publish(args.dry_run)
  except Exception:
   atomic(STATE/'status.json',dict(ok=False,checked_at=int(time.time()),reason='publication_needs_review'));raise
 else:
  release=Path(args.release).resolve() if args.release else active()
  if OPERATION_SOURCE.owner:
   if release!=active():raise ValueError('unexpected registered validation release')
  elif release.parent!=RELEASES or not VERSION.fullmatch(release.name):raise ValueError('unexpected validation release')
  validate(readjson(Path(args.manifest)),release);print('Manifest validated against active source.')
if __name__=='__main__':main()
