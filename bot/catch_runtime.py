"""Small Catch-specific boundary helpers; the ordinary room controller is reused."""
import math
from .languages import translated, bilingual


def catch_message(bot, key):
    # say() applies the active command's player language in the normal engine.
    english = {
        'catch_nm_only': 'Catch supports native NoMod only, at clock rate 1.0.',
        'catch_fixed_difficulty': 'Catch uses a fixed star range; automatic difficulty is not enabled.',
        'catch_style_unavailable': 'Catch style and Farm filters are not qualified; variety selection remains active.',
        'catch_research_only': 'Catch catalogs are offline research only; public distribution and live activation are not authorized.',
    }[key]
    context = getattr(bot, '_language_context', None)
    language = bot.cfg.get('default_response_language', 'en')
    if context:
        language = bot._language_record(context[0]).get('language') or language
    return bilingual(english, translated(language, key))


def handle_command(bot, cmd, parts):
    p = bot.prefix
    if cmd in {'farm', 'stream', 'jump', 'aim', 'style', 'jack', 'chordjack', 'ln',
               'technical', 'rice', 'noodle', 'noodles', 'freedomdive'}:
        bot.say(catch_message(bot, 'catch_style_unavailable'))
        return True
    if cmd in {'auto', 'bias', 'halfwidth', 'maxage', 'minpasses'}:
        bot.say(catch_message(bot, 'catch_fixed_difficulty'))
        return True
    if cmd in {'dt', 'nm', 'mods', 'freestyle'}:
        bot.say(catch_message(bot, 'catch_nm_only'))
        return True
    if cmd == 'help' and len(parts) > 1 and parts[1].lstrip(p).lower() == 'pp':
        bot.say(f'{p}pp — Show the qualified native NM judgement scenarios; arbitrary accuracy/mod projections are unavailable.')
        return True
    if cmd == 'help' and len(parts) > 1 and parts[1].lstrip(p).lower() in {
            'farm','stream','jump','aim','style','jack','chordjack','ln','technical',
            'rice','noodle','noodles','dt','nm','auto','bias','halfwidth','freedomdive'}:
        return handle_command(bot, parts[1].lstrip(p).lower(), [parts[1]])
    if cmd == 'help2':
        bot.say(catch_message(bot, 'catch_nm_only'))
        bot.say(f'{p}queue {p}pool {p}map {p}start {p}wait {p}skip {p}harder/{p}easier '
                f'{p}good/{p}bad {p}pp {p}time {p}redo {p}stats {p}hidden/{p}show '
                f'{p}lang {p}discord | https://poolrotater.com/commands')
        # Existing privilege-specific help remains available per command.
        return True
    if cmd == 'variety':
        bot.say(catch_message(bot, 'catch_style_unavailable') if len(parts) > 1
                else bot.pool.source_mode_label())
        return True
    if cmd == 'map':
        m = bot._pp_command_map()
        bot.say(m.label + ' | native Catch NM | ' + m.url if m else 'No current map yet.')
        return True
    if cmd == 'pp':
        m = bot._pp_command_map()
        if len(parts) > 1:
            bot.say(catch_message(bot, 'catch_nm_only'))
            return True
        if m is None:
            bot.say('No current map yet.')
        else:
            values = ' | '.join(f"{s['name']}: {s['pp']:.1f}pp" for s in m.pp_scenarios
                                if s['pp'] is not None)
            bot.say('Catch NM theoretical judgement scenarios: ' + (values or 'unavailable')
                    + ' | not a player score prediction')
        return True
    return False


def playlist_valid(bot, item):
    if (type(item.get('ruleset_id')) is not int or item['ruleset_id'] != 2
            or item.get('required_mods') != [] or item.get('allowed_mods') != []
            or item.get('freestyle') is not False
            or type(item.get('clock_rate', 1.)) not in (int, float)
            or item.get('clock_rate', 1.) != 1.
            or item.get('convert', False) is not False):
        return False
    try:
        m = bot.pool.get_map(item.get('beatmap_id'))
        bot.pool.verify_variant(m)
        beatmap = item.get('beatmap') or {}
        if 'mode_int' in beatmap and (type(beatmap['mode_int']) is not int or beatmap['mode_int'] != 2):
            return False
        if beatmap.get('convert', False) is not False:
            return False
        checksum = item.get('checksum', beatmap.get('checksum'))
        if checksum is not None:
            if checksum != m.file_md5 or beatmap.get('mode_int', 2) != 2:
                return False
        elif not bot._check_catch_revision(m):
            return False  # ID alone never proves a current map-file variant.
        stars = item.get('star_rating')
        if stars is not None and (type(stars) not in (int,float)
                                  or not math.isfinite(stars) or abs(stars-m.sr) > .05):
            return False
    except (TypeError,ValueError,AttributeError):
        return False
    return True


def display_metadata(m):
    """Map an accepted Catch record into the existing local display lookup.

    This helper grants no publication rights and writes no public index.
    Provider total is only an approximate progress length, never audio length.
    """
    if getattr(m, 'ruleset_id', None) != 2 or not getattr(m, 'native', False):
        raise ValueError('native Catch display identity required')
    return {'beatmapset_id':m.beatmapset_id,'title':m.title,'artist':m.artist,
            'version':m.version,'sr':m.sr,'total_length':m.provider_total_seconds,
            'hit_length':m.provider_hit_seconds}
