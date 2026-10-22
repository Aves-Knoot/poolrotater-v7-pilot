"""Explicit hub-local booking slot administration on bounded private stdin."""
import sys
from pathlib import Path
from ..network.contracts import NetworkError,canonical,decode,fields,require,identifier
from ..network.hub_layout import check
from ..network.store import Store
from .hub import Repository
from .settings import validate_player_patch


def execute(root,value):
    fields(value,('format','action','slots'))
    require(type(value['format'])is int and value['format']==1 and value['action']in ('configure','list'),
            'booking_admin_input_refused')
    require(type(value['slots'])is list and len(value['slots'])<=16,'booking_admin_input_refused')
    require(value['action']=='configure'or not value['slots'],'booking_admin_input_refused')
    seen=set()
    for slot in value['slots']:
        fields(slot,('slot_id','host_id','alias','label','presets','base_settings','enabled'))
        require(type(slot['slot_id'])is str and slot['slot_id']not in seen,'booking_admin_input_refused')
        seen.add(slot['slot_id']);identifier(slot['host_id'])
        require(type(slot['presets'])is list and bool(slot['presets']),'booking_admin_input_refused')
        for preset in slot['presets']:validate_player_patch(slot['base_settings'],preset=preset)
    ledger=Repository(Store(root)).ledger
    for slot in value['slots']:ledger.configure_slot(**slot)
    return {'ok':True,'configured_slots':len(value['slots']),'slots':ledger.public_slots(),
            'rooms_started':False,'bookings_created':False,'services_changed':False}


def main(argv=None):
    import argparse
    parser=argparse.ArgumentParser(description='Explicit local booking slots, private JSON stdin only')
    parser.add_argument('--root',required=True,type=Path);args=parser.parse_args(argv)
    try:
        root,_=check(args.root)
        result=execute(root,decode(sys.stdin.buffer.read(65537)))
    except (NetworkError,OSError,ValueError,TypeError,KeyError)as exc:
        result={'ok':False,'error':exc.code if isinstance(exc,NetworkError)else'booking_admin_refused'}
    sys.stdout.buffer.write(canonical(result)+b'\n');sys.stdout.buffer.flush()
    return 0 if result['ok']else 1


if __name__=='__main__':raise SystemExit(main())
