"""Independent bounded C6 reader/importer. Never imports the exporter or bot.

Integrity hashes prove byte consistency, not authenticity or distribution rights.
This research format is intentionally not a v7 signed/live catalog format.
"""
from collections import Counter
import gzip
import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile

from .c6_schema import (FORMAT,ROW_SCHEMA,FILES,MAX_COMPRESSED,MAX_RAW,MAX_LINE,MAX_ROWS,
                        canonical,decode,sha,validate,validate_row,schema_document)
from .errors import IntegrityError,PolicyError
from .disk import DiskGuard


def bounded_file(path,limit):
    path=Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size>limit:
        raise IntegrityError('catalog file is missing, linked, or exceeds bounds')
    raw=path.read_bytes()
    if len(raw)>limit:raise IntegrityError('catalog file grew beyond its bound')
    return raw


def manifest_value(raw):
    if not isinstance(raw,(str,bytes)) or len(raw)>65536:raise IntegrityError('manifest exceeds bounds')
    value=decode(raw)
    expected={'format','row_schema','ruleset_id','native_only','mods','research_only','production_ready',
              'distribution_allowed','row_count','rows_sha256','uncompressed_bytes','map_ids_sha256','accounting','files','inputs'}
    if not isinstance(value,dict) or set(value)!=expected:raise IntegrityError('unsupported manifest shape')
    for name,want in [('format',FORMAT),('row_schema',ROW_SCHEMA),('ruleset_id',2),('native_only',True),
                      ('mods',[]),('research_only',True),('production_ready',False),('distribution_allowed',False)]:
        if type(value[name])!=type(want) or value[name]!=want:raise IntegrityError('unsupported/non-research catalog manifest')
    if (type(value['row_count']) is not int or not 0<value['row_count']<=MAX_ROWS
            or type(value['uncompressed_bytes']) is not int or not 0<value['uncompressed_bytes']<=MAX_RAW):
        raise IntegrityError('manifest row/byte bounds refused')
    for key in ('rows_sha256','map_ids_sha256'):validate(value[key],'sha256')
    if not isinstance(value['files'],dict) or set(value['files'])!=set(FILES):raise IntegrityError('unexpected manifest files')
    for entry in value['files'].values():validate(entry,{'bytes':'positive_int','sha256':'sha256'})
    if not isinstance(value['accounting'],dict) or any(type(n)is not int or n<0 for n in value['accounting'].values()):
        raise IntegrityError('invalid row accounting')
    expected_inputs={'native_inputs','features','selection_reference','country_representation','country_acceptance'}
    if not isinstance(value['inputs'],dict) or set(value['inputs'])!=expected_inputs:raise IntegrityError('missing accepted input identities')
    for entry in value['inputs'].values():validate(entry,{'bytes':'positive_int','sha256':'sha256'})
    return value


def verify_rights(value):
    names={'ppy-bulk','osu-public-api','osu-map-files','c4-official-osu','c51-country-api'}
    if (not isinstance(value,dict) or set(value)!={'format','research_only','production_ready','distribution_allowed',
            'sources','field_groups','unresolved_sources','restrictions','calculator'}
            or value['format']!=FORMAT or value['research_only'] is not True
            or value['production_ready'] is not False or value['distribution_allowed'] is not False
            or not isinstance(value['sources'],dict) or set(value['sources'])!=names):raise IntegrityError('research-only source-rights gate refused')
    for name,source in value['sources'].items():
        validate(source,{'id':'text','url':'text','permission':['enum','research_only','unreviewed','production_permitted'],
                         'licence_url':['nullable','text'],'permission_evidence':['nullable','text']})
        if source['id']!=name:raise IntegrityError('source identity mismatch')
    expected={name for name,s in value['sources'].items() if s['permission']!='production_permitted' or not s['permission_evidence']}
    if (not isinstance(value['unresolved_sources'],list) or any(not isinstance(x,str) for x in value['unresolved_sources'])
            or set(value['unresolved_sources'])!=expected or len(value['unresolved_sources'])!=len(expected)):
        raise IntegrityError('unresolved source permission hidden')
    groups=set(schema_document()['row'])-{'schema'}
    if (not isinstance(value['field_groups'],dict) or set(value['field_groups'])!=groups
            or any(not isinstance(s,list) or not s or any(not isinstance(x,str) for x in s) or not set(s)<=names
                   for s in value['field_groups'].values())):
        raise IntegrityError('missing field-level source lineage')
    if not isinstance(value['restrictions'],list) or not value['restrictions'] or any(not isinstance(x,str)or not x for x in value['restrictions']):
        raise IntegrityError('source restrictions absent')
    c=value['calculator']
    validate(c,{'schema':['enum','catch-features-1'],'official_osu_commit':'commit',
        'assemblies':{name:{'informational':'text','version':'text'}for name in ('osu.Framework','osu.Game','osu.Game.Rulesets.Catch')},
        'runtime_snapshot_sha256':'sha256','dotnet_runtime':'text','difficulty_version':'positive_int',
        'feature_code':{name:'sha256'for name in ('c4_calculator.py','c4_features.py')},
        'mods':['list','text',0],'clock_rate':['enum',1.0],'native':['enum',True],
        'original_ruleset':['enum','fruits'],'calculator_id':'sha256'})
    if sha(canonical({k:v for k,v in c.items() if k!='calculator_id'}))!=c['calculator_id']:
        raise IntegrityError('calculator configuration identity differs')


class Catalog:
    def __init__(self,folder,*,purpose):
        if purpose!='research':raise PolicyError('C6 catalog may only be opened for private research')
        self.folder=Path(folder).absolute()
        if self.folder.is_symlink() or not self.folder.is_dir():raise IntegrityError('catalog root must be an ordinary directory')
        if {p.name for p in self.folder.iterdir()}!={'manifest.json',*FILES}:
            raise IntegrityError('unexpected catalog files; no private extras allowed')
        self.manifest=manifest_value(bounded_file(self.folder/'manifest.json',65536))
        self._identities={}
        for name in FILES:
            limit=MAX_COMPRESSED if name=='catalog.jsonl.gz' else 256*1024
            raw=bounded_file(self.folder/name,limit);entry=self.manifest['files'][name]
            if len(raw)!=entry['bytes'] or sha(raw)!=entry['sha256']:raise IntegrityError('catalog artifact hash/size mismatch')
            st=(self.folder/name).stat();self._identities[name]=(st.st_ino,st.st_size,st.st_mtime_ns)
            if name=='schema.json' and decode(raw)!=schema_document():raise IntegrityError('unsupported catalog schema contract')
            if name=='source-rights.json':self.rights=decode(raw);verify_rights(self.rights)

    def rows(self):
        name='catalog.jsonl.gz';path=self.folder/name;st=path.stat()
        if path.is_symlink() or (st.st_ino,st.st_size,st.st_mtime_ns)!=self._identities[name]:raise IntegrityError('catalog changed after verification')
        count=0;size=0;last=0;variants=set();h=hashlib.sha256();ids=hashlib.sha256();accounting=Counter()
        try:
            with gzip.open(path,'rb') as stream:
                while True:
                    line=stream.readline(MAX_LINE+1)
                    if not line:break
                    size+=len(line);count+=1
                    if (len(line)>MAX_LINE or not line.endswith(b'\n') or size>MAX_RAW or count>MAX_ROWS
                            or count>self.manifest['row_count']):raise IntegrityError('catalog expansion/row bounds exceeded')
                    row=decode(line);validate_row(row)
                    if canonical(row)+b'\n'!=line:raise IntegrityError('catalog row is not canonical')
                    s=row['selection'];mid=s['map_id']
                    if mid<=last or s['variant_id'] in variants:raise IntegrityError('duplicate or unordered map/variant')
                    if (s['calculator_id']!=self.rights['calculator']['calculator_id']
                            or row['identity']['official_commit']!=self.rights['calculator']['official_osu_commit']
                            or row['identity']['official_difficulty_version']!=self.rights['calculator']['difficulty_version']):
                        raise IntegrityError('row calculator provenance mismatch')
                    last=mid;variants.add(s['variant_id']);h.update(line);ids.update(str(mid).encode()+b'\n')
                    accounting[s['status']]+=1
                    for key in ('ranked_approved_reason','include_loved_reason'):accounting[key+':'+str(row['eligibility'][key])]+=1
                    yield row
        except (OSError,EOFError,ValueError) as exc:raise IntegrityError('corrupt/truncated catalog stream') from exc
        if (count!=self.manifest['row_count'] or size!=self.manifest['uncompressed_bytes']
                or h.hexdigest()!=self.manifest['rows_sha256'] or ids.hexdigest()!=self.manifest['map_ids_sha256']
                or dict(accounting)!=self.manifest['accounting']):raise IntegrityError('catalog row accounting/digest mismatch')

    def selection_maps(self):return [row['selection'] for row in self.rows()]

    def import_sqlite(self,destination):
        """Validate every row before exposing a new local research DB."""
        destination=Path(destination).absolute()
        if destination.exists() or destination.is_symlink():raise PolicyError('import destination exists; retain existing data')
        DiskGuard(destination.parent).check(self.manifest['uncompressed_bytes']*2+16*1024**2)
        destination.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        fd,temporary=tempfile.mkstemp(prefix='.c6-import-',dir=destination.parent);os.close(fd);os.chmod(temporary,0o600)
        try:
            db=sqlite3.connect(temporary)
            try:
                db.execute('PRAGMA trusted_schema=OFF');db.execute('PRAGMA synchronous=FULL')
                db.executescript('CREATE TABLE metadata (id INTEGER PRIMARY KEY CHECK(id=1),manifest TEXT NOT NULL);'
                    'CREATE TABLE maps (map_id INTEGER PRIMARY KEY,variant_id TEXT NOT NULL UNIQUE,payload TEXT NOT NULL);')
                for row in self.rows():
                    db.execute('INSERT INTO maps VALUES(?,?,?)',(row['selection']['map_id'],row['selection']['variant_id'],canonical(row).decode()))
                db.execute('INSERT INTO metadata VALUES(1,?)',(canonical(self.manifest).decode(),));db.commit()
                if db.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise IntegrityError('imported SQLite integrity failed')
            finally:db.close()
            # Link instead of replace: another creator must never be overwritten.
            os.link(temporary,destination)
            fd=os.open(destination.parent,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(fd)
            finally:os.close(fd)
        finally:Path(temporary).unlink(missing_ok=True)
        return {'rows':self.manifest['row_count'],'rows_sha256':self.manifest['rows_sha256'],'research_only':True}


def imported_rows(path):
    """Read imported payloads without executing supplied SQL or adopting a DB."""
    path=Path(path).absolute()
    if path.is_symlink() or not path.is_file() or path.stat().st_size>MAX_RAW*2:
        raise IntegrityError('invalid research SQLite file')
    db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
    try:
        db.execute('PRAGMA query_only=ON');db.execute('PRAGMA trusted_schema=OFF')
        manifest=manifest_value(db.execute('SELECT manifest FROM metadata WHERE id=1').fetchone()[0])
        count=0;h=hashlib.sha256();ids=hashlib.sha256()
        for mid,variant,payload in db.execute('SELECT map_id,variant_id,payload FROM maps ORDER BY map_id'):
            if not isinstance(payload,str) or len(payload.encode())>MAX_LINE:raise IntegrityError('imported payload exceeds row bound')
            row=decode(payload);validate_row(row)
            if row['selection']['map_id']!=mid or row['selection']['variant_id']!=variant:raise IntegrityError('imported index/payload mismatch')
            count+=1;h.update(canonical(row)+b'\n');ids.update(str(mid).encode()+b'\n')
            yield row
        if count!=manifest['row_count'] or h.hexdigest()!=manifest['rows_sha256'] or ids.hexdigest()!=manifest['map_ids_sha256']:
            raise IntegrityError('imported content mismatch')
    except (sqlite3.Error,TypeError) as exc:raise IntegrityError('invalid imported research database') from exc
    finally:db.close()
