"""Static HTML view of already filtered hub records. No external scripts/assets."""
from html import escape

from .contracts import integer, require, text


def render(value):
    require(type(value) is dict and type(value.get('rooms')) is list and len(value['rooms']) <= 100,
            'invalid_directory')
    rows = []
    for room in value['rooms']:
        # Ignore supplied URLs: derive links exclusively from validated numeric IDs.
        integer(room['osu_room_id']);text(room['title']);text(room['operator'],80)
        label = escape(room['title']); operator = escape(room['operator'])
        freshness = room.get('freshness')
        require(freshness in ('fresh','stale'),'invalid_directory_freshness')
        if freshness == 'fresh' and room.get('state') != 'unavailable':
            label = '<a rel="noreferrer" href="https://osu.ppy.sh/multiplayer/rooms/'+str(room['osu_room_id'])+'">'+label+'</a>'
        rows.append('<li>'+label+' — '+operator+' — '+freshness+' (self-reported)</li>')
    next_offset = value.get('next_offset'); more = ''
    if next_offset is not None:
        integer(next_offset,0,1000000);more='<a href="/directory?offset='+str(next_offset)+'">Next page</a>'
    return ('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
            '<title>PoolRotater directory</title><body><h1>PoolRotater directory</h1>'
            '<p>Listed hosts share their status voluntarily. Stale status means unknown availability.</p><ul>'
            +''.join(rows)+'</ul>'+more+'</body></html>').encode('utf-8')
