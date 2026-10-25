"""Taiko boundaries around the retained multiplayer engine.

The published catalog qualifies NM at rate 1.0. Other variants must be qualified
before enabling them; provider Standard ratings never prove a Taiko conversion.
"""
import math
import time


def handle_command(bot, cmd, parts):
    topic=parts[1].lstrip(bot.prefix).lower() if cmd=='help' and len(parts)>1 else cmd
    if topic in {'dt','nm','mods','freestyle'}:
        bot.say('Taiko currently supports qualified NoMod at clock rate 1.0.')
        return True
    if topic in {'farm','stream','jump','aim','style','jack','chordjack','ln','technical',
                 'rice','noodle','noodles','freedomdive'}:
        bot.say('Taiko uses variety selection; additional style filters are not available.')
        return True
    if cmd=='variety':
        bot.say(bot.pool.source_mode_label());return True
    if cmd=='pp':
        item=bot._pp_command_map()
        if len(parts)>1:
            bot.say('Taiko PP uses the catalog’s qualified NM reference scenarios.')
        elif item is None:bot.say('No current map yet.')
        else:
            scenarios=getattr(item,'pp_scenarios',())
            values=[]
            for row in scenarios:
                if isinstance(row,dict) and type(row.get('pp')) in (int,float):
                    label=row.get('name',row.get('accuracy','NM'))
                    values.append(f"{label}: {row['pp']:.1f}pp")
            if not values and type(getattr(item,'max_pp',None)) in (int,float):
                values=[f'SS: {item.max_pp:.1f}pp']
            bot.say('Taiko NM reference: '+(' | '.join(values) or 'unavailable'))
        return True
    if cmd=='map':
        item=bot._pp_command_map()
        bot.say(item.label+' | Taiko NM | '+item.url if item else 'No current map yet.')
        return True
    return False


def revision_valid(bot, item, *, force=False):
    cache=getattr(bot,'_taiko_revisions',{})
    bot._taiko_revisions=cache
    checksum=getattr(item,'file_md5',getattr(item,'source_md5',None))
    original=getattr(item,'original_mode',1 if getattr(item,'native',True) else 0)
    old=cache.get(item.beatmap_id)
    if not force and old and old[1]==checksum and time.monotonic()-old[0]<30:
        return True
    try:observed=bot.api.beatmap(item.beatmap_id)
    except Exception:return False
    if not isinstance(observed,dict):return False
    valid=(observed.get('checksum')==checksum and observed.get('mode_int')==original
           and observed.get('status')==item.status)
    if valid:
        cache[item.beatmap_id]=(time.monotonic(),checksum)
        return True
    cache.pop(item.beatmap_id,None)
    bot.pool.exclude_beatmap(item.beatmap_id)
    return False


def playlist_valid(bot, item):
    if (item.get('ruleset_id')!=1 or type(item.get('ruleset_id')) is not int
            or item.get('required_mods')!=[] or item.get('allowed_mods')!=[]
            or item.get('freestyle') is not False
            or type(item.get('clock_rate',1.)) not in (int,float)
            or item.get('clock_rate',1.)!=1.):
        return False
    try:
        chosen=bot.pool.get_map(item.get('beatmap_id'))
        if chosen is None:return False
        bot.pool.verify_variant(chosen)
        raw=item.get('beatmap') or {}
        original=getattr(chosen,'original_mode',1 if getattr(chosen,'native',True) else 0)
        if 'mode_int' in raw and raw['mode_int']!=original:return False
        checksum=item.get('checksum',raw.get('checksum'))
        expected=getattr(chosen,'file_md5',getattr(chosen,'source_md5',None))
        if checksum is not None:
            if checksum!=expected:return False
        elif not revision_valid(bot,chosen):return False
        stars=item.get('star_rating')
        if stars is not None and (type(stars) not in (int,float)
                or not math.isfinite(stars) or abs(stars-chosen.sr)>.05):return False
    except (TypeError,ValueError,AttributeError):return False
    return True


def display_metadata(item):
    if getattr(item,'ruleset_id',None)!=1:raise ValueError('Taiko display identity required')
    return {'beatmapset_id':item.beatmapset_id,'title':item.title,'artist':item.artist,
            'version':item.version,'sr':item.sr,
            'total_length':getattr(item,'provider_total_seconds',item.hit_length),
            'hit_length':getattr(item,'provider_hit_seconds',item.hit_length)}
