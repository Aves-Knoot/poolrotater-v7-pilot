"""Staff-channel fleet controls; no provider credentials, shell or public replies.

Discord roles are checked by the existing guild client and checked again here.
Gateway message identities give durable exact retries across bridge restarts.
"""
import json
import re
import sqlite3
import uuid
from ..config import ConfigError
from ..diagnostic_logs.common import transaction
from ..network.contracts import NetworkError,identifier,integer,require,decode
from ..managed_host.contracts import alias as check_alias,digest
from ..managed_host.hub import Repository

HELP=('Fleet controls: .fleet hosts; .fleet status HOST [roomN]; '
      '.fleet settings HOST roomN [FIELD]; .fleet greeting HOST roomN USER; '
      '.fleet open|close|restart HOST roomN; .fleet configure HOST roomN {"setting":value}; '
      '.fleet cancel HOST roomN OPERATION_ID. HOST is owner, an exact unique label, or host UUID. '
      'Close/restart wait for empty rooms; --immediate explicitly interrupts the target. '
      'Optional --revision N and --generation HEX|none reject stale selections. '
      'Only the host operator can change its room ceiling.')
SAFE_ERRORS=frozenset(('host_not_found','managed_host_ambiguous','managed_fresh_capabilities_required',
    'configuration_revision_changed','room_not_found','managed_operation_pending','managed_operation_conflict',
    'managed_role_refused','managed_channel_refused','managed_message_identity_required','invalid_managed_action',
    'invalid_managed_patch','invalid_managed_generation','invalid_managed_cancel','invalid_request',
    'invalid_managed_alias','invalid_managed_options','unsupported_managed_policy'))
SAFE_ERRORS|=frozenset(('request_too_large',))


def operation_identity(channel,message):
    integer(channel,1,2**63-1);integer(message,1,2**63-1)
    return str(uuid.uuid5(uuid.NAMESPACE_URL,f'poolrotater-discord-fleet-v1:{channel}:{message}'))


class Fleet:
    def __init__(self,store,*,channel,owner_host):
        integer(channel,1,2**63-1);identifier(owner_host)
        self.store=store;self.channel=channel;self.owner_host=owner_host;self.repo=Repository(store)
        with transaction(self.repo.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS discord_fleet_notices(id TEXT PRIMARY KEY,channel TEXT NOT NULL,last_receipt TEXT)')
    def hosts(self):
        with self.store.transaction() as db:
            return [dict(r) for r in db.execute('SELECT id,label,account_id FROM hosts WHERE revoked=0 ORDER BY label,id')]
    def resolve(self,value):
        if value=='owner':value=self.owner_host
        rows=[row for row in self.hosts() if row['id']==value or row['label'].casefold()==value.casefold()]
        require(bool(rows),'host_not_found',404)
        require(len(rows)==1,'managed_host_ambiguous',409)
        return rows[0]['id']
    @staticmethod
    def options(tail):
        choices={};tail=tail.strip()
        while tail.startswith('--'):
            flag,_,tail=tail.partition(' ');tail=tail.lstrip()
            if flag=='--immediate':
                require('timing' not in choices,'invalid_managed_options');choices['timing']='immediate';continue
            require(flag in ('--revision','--generation'),'invalid_managed_options')
            item,_,tail=tail.partition(' ');tail=tail.lstrip()
            key='expected_revision' if flag=='--revision' else 'expected_room_generation'
            require(key not in choices and bool(item),'invalid_managed_options')
            if key=='expected_revision':
                require(item.isascii() and item.isdigit() and len(item)<=16,'invalid_managed_options')
                choices[key]=int(item);integer(choices[key],0,2**53-1)
            else:
                require(item=='none' or re.fullmatch('[a-f0-9]{32}',item) is not None,'invalid_managed_generation')
                choices[key]=None if item=='none' else item
        return choices,tail
    @staticmethod
    def summary(report,alias=None,*,now=0):
        caps=report['capabilities']
        if caps is None:return ['This host has not advertised managed-room support; its existing rooms were not changed.']
        text=[f"Controller: {caps['controller_phase']}; host ceiling {caps['max_concurrent_rooms']}; "
              f"provider allowance {caps['provider_room_limit']}; reserved/owned {caps['capacity_used']}."]
        selected=[r for r in caps['rooms'] if alias is None or r['alias']==alias]
        require(bool(selected),'room_not_found',404)
        for row in selected:
            room=('https://osu.ppy.sh/multiplayer/rooms/'+str(row['room_id'])) if row['room_id'] else 'no observed room'
            text.append(f"{row['alias']}: desired {row['desired']}, observed {row['phase']}, {row['preset']}, "
                f"{row['visibility']}; revision {row['revision']} (applied {row['applied_revision']}); "
                f"incarnation {row['room_generation'] or 'none'}; {room}; "
                f"pending {row['pending_operation'] or 'none'}; failure {row['failure_code'] or 'none'}.")
        for op in report['operations'][:5]:
            if alias is not None and op['alias']!=alias:continue
            receipt=op['receipt']
            phase=receipt['phase'] if receipt else ('delivery-expired; outcome unconfirmed' if op['expires']<=now else 'queued; awaiting host receipt')
            text.append(f"Operation {op['id']} ({op['alias']}): {phase}"+
                        (f"; {receipt['failure_code']}" if receipt and receipt['failure_code'] else ''))
        return text
    @staticmethod
    def chunks(lines):
        # Discord's fixed outbound limit must not truncate a large settings
        # inspection. Split staff output without making it a public attachment.
        result=[];pending=''
        for line in lines:
            for start in range(0,max(1,len(line)),1700):
                part=line[start:start+1700]
                if pending and len(pending)+len(part)+1>1800:result.append(pending);pending=''
                pending+=('\n' if pending else '')+part
        if pending:result.append(pending)
        return result
    def settings(self,host,alias,field=None,*,greeting=False):
        from ..managed_host.details import Repository as Details
        report=self.repo.inspect(host);cached=Details(self.store).inspect(host,report['capabilities'])
        if cached['document'] is None:return ['Room settings are synchronizing; no current settings snapshot is available.']
        rooms=cached['document']['rooms'];require(alias is None or alias in rooms,'room_not_found',404)
        lines=['Settings snapshot: '+cached['phase']+'. Desired defaults and last confirmed effective defaults are separate.']
        if cached['phase']!='current':lines.append('These cached settings are historical; the latest room revisions have not arrived yet.')
        for name,row in rooms.items():
            if alias is not None and name!=alias:continue
            lines.append(f"{name}: desired revision {row['revision']}; applied revision {row['applied_revision']}.")
            keys=sorted(set(row['desired'])|set(row['effective']))
            if greeting:
                require(type(field) is str and 0<len(field)<=80,'invalid_managed_options')
                for kind in ('desired','effective'):
                    values=row[kind].get('custom_join_greetings')
                    if values is None:
                        from bot.custom_greetings import DEFAULT_JOIN_GREETINGS
                        values=DEFAULT_JOIN_GREETINGS
                    found=next((v for k,v in values.items() if k.casefold()==field.casefold()),None)
                    lines.append(kind+' greeting: '+json.dumps(found,ensure_ascii=False))
                continue
            if field is not None:
                require(field in keys,'invalid_managed_options');keys=[field]
            for key in keys:
                def display(value):
                    if key=='custom_join_greetings' and type(value) is dict:
                        return f"{len(value)} greetings; use .fleet greeting HOST roomN USER to inspect one; configure custom_join_greeting_changes to edit entries"
                    return json.dumps(value,ensure_ascii=False)
                desired=display(row['desired'].get(key));effective=display(row['effective'].get(key))
                lines.append(key+': '+desired+(' (effective same)' if desired==effective else '; effective '+effective))
        return self.chunks(lines)
    def handle(self,command):
        """Return staff-only output; never echo JSON, errors or secret input."""
        operation=None
        try:
            require(command.channel_id==self.channel,'managed_channel_refused',403)
            require(command.role=='ADMIN','managed_role_refused',403)
            content=command.content
            require(type(content) is str and len(content)<=1000 and '\n' not in content and '\r' not in content,'invalid_request')
            parts=content.split(None,4)
            require(parts and parts[0].casefold()=='.fleet','invalid_managed_action')
            if len(parts)==1 or parts[1].casefold()=='help':return [HELP]
            action=parts[1].casefold()
            if action=='hosts':
                require(len(parts)==2,'invalid_managed_options')
                rows=self.hosts()
                return [f"{row['label']}: {row['id']} (hosting account {row['account_id']})" for row in rows] or ['No enrolled hosts.']
            require(len(parts)>=3,'invalid_managed_options');host=self.resolve(parts[2])
            alias=check_alias(parts[3]) if len(parts)>=4 else None
            if action=='status':
                require(len(parts)<=4,'invalid_managed_options')
                report=self.repo.inspect(host)
                return self.summary(report,alias,now=self.store.clock())
            if action in ('settings','greeting'):
                require(alias is not None,'invalid_managed_options')
                return self.settings(host,alias,parts[4] if len(parts)==5 else None,greeting=action=='greeting')
            require(action in ('open','close','restart','configure','cancel') and alias is not None,'invalid_managed_action')
            choices,tail=self.options(parts[4] if len(parts)==5 else '')
            patch=None;cancel=None
            if action=='configure':
                patch=decode(tail.encode(),maximum=65536)
                require(type(patch) is dict and bool(patch),'invalid_managed_patch')
            elif action=='cancel':cancel=tail;identifier(cancel)
            else:require(not tail,'invalid_managed_options')
            message=getattr(command,'message_id',0)
            require(type(message) is int and message>0,'managed_message_identity_required')
            actor='discord:'+str(command.discord_user_id);integer(command.discord_user_id,1,2**63-1)
            operation=operation_identity(command.channel_id,message)
            # Track the message before queue commit: a crash after the durable
            # request is accepted must not lose its eventual completion notice.
            with transaction(self.repo.path) as db:
                db.execute('INSERT OR IGNORE INTO discord_fleet_notices(id,channel) VALUES(?,?)',(operation,str(self.channel)))
            result=self.repo.queue(host,alias,action,patch=patch,actor=actor,cancel_operation_id=cancel,
                operation_id=operation,**choices)
            value=result['request'];receipt=result.get('receipt')
            phase=receipt['phase'] if receipt else 'queued; awaiting the host'
            mode=' Explicit immediate action may interrupt this target room.' if value['timing']=='immediate' else ''
            return [f"{alias} {action}: {phase}. Operation {value['id']}; revision {value['expected_revision']}; "
                    f"incarnation {value['expected_room_generation'] or 'none'}.{mode}"]
        except NetworkError as error:
            if operation is not None and error.code not in SAFE_ERRORS:
                return ['Fleet request outcome requires review. Operation '+operation+'; inspect fleet status before another request.']
            code=error.code if error.code in SAFE_ERRORS else 'managed_request_refused'
            return ['Fleet request refused: '+code+'. No replacement operation was sent.']
        except (ConfigError,ValueError,TypeError,KeyError,AttributeError,OSError,sqlite3.Error):
            if operation is not None:
                return ['Fleet request outcome requires review. Operation '+operation+'; inspect fleet status before another request.']
            return ['Fleet request refused: invalid settings or command. No replacement operation was sent.']
    def notices(self):
        with transaction(self.repo.path) as db:
            values=[]
            for row in db.execute('SELECT n.*,r.alias,r.receipt FROM discord_fleet_notices n JOIN requests r ON r.id=n.id WHERE n.channel=?',(str(self.channel),)):
                if row['receipt'] is None:continue
                receipt=json.loads(row['receipt']);pin=digest(receipt)
                if pin==row['last_receipt']:continue
                text=f"Operation {row['id']} ({row['alias']}): {receipt['phase']}"
                if receipt['failure_code']:text+='; '+receipt['failure_code']
                if receipt['result'] and receipt['result'].get('room_id'):
                    text+='; https://osu.ppy.sh/multiplayer/rooms/'+str(receipt['result']['room_id'])
                values.append({'id':row['id'],'pin':pin,'channel':self.channel,'content':text})
            return values
    def acknowledge_notice(self,value):
        require(value['channel']==self.channel,'managed_channel_refused')
        with transaction(self.repo.path) as db:
            db.execute('UPDATE discord_fleet_notices SET last_receipt=? WHERE id=? AND channel=?',
                       (value['pin'],value['id'],str(self.channel)))
