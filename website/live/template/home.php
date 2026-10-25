<?php
/** A player-first front page using existing sanitized exports and authored history. */
if (!defined('DOKU_INC')) die();

function prHomeStatsSummary($data) {
    $totals = $data['scopes']['all']['7d']['totals'] ?? [];
    $out = ['schema'=>1, 'generated_at'=>$data['generated_at'] ?? 0, 'expires_at'=>$data['expires_at'] ?? 0, 'available'=>!empty($data['available']) && prAnalyticsFresh($data), 'totals'=>[]];
    foreach (['players','maps','pp'] as $key) {
        $value = $totals[$key] ?? null;
        if (is_numeric($value) && is_finite((float)$value) && $value >= 0) $out['totals'][$key] = (float)$value;
    }
    return $out;
}

function prHomeReleases($text = null) {
    if ($text === null) $text = rawWiki('updates:start');
    if (!is_string($text) || strlen($text) > 262144) return [];
    preg_match_all('/^\|\s*\[\[(updates:v\d+(?:_\d+){2})\|([^\]\r\n]+)\]\]\s*\|\s*([^|\r\n]+)\|\s*([^|\r\n]+)\|\s*$/m', $text, $rows, PREG_SET_ORDER);
    $out = [];
    foreach ($rows as $row) {
        $id = $row[1];
        if (isset($out[$id])) continue;
        $out[$id] = ['article'=>$id, 'version'=>trim($row[2]), 'subject'=>trim($row[3]), 'status'=>trim($row[4])];
        if (count($out) === 3) break;
    }
    return array_values($out);
}

function prHomePage($status = null, $statistics = null, $releases = null) {
    $status = $status ?? prAnalyticsData('status');
    $summary = prHomeStatsSummary($statistics ?? prAnalyticsData('statistics'));
    $fresh = prAnalyticsFresh($status) && !empty($status['available']);
    $rooms = $fresh ? ($status['rooms'] ?? []) : [];
    require_once __DIR__.'/community.php';
    $community=prCommunityRead('/srv/poolrotator-wiki/public/community/rooms.json');
    $networkPeople=prCombinedOccupancy($status,$community);
    $counts = array_column($rooms, 'players');
    $countKnown = count($counts) === 4 && count(array_filter($counts, fn($n)=>is_int($n) && $n >= 0)) === 4;
    $people = $networkPeople===null ? '—' : prAnalyticsNumber($networkPeople);
    echo '<div class="pr-home pr-rhythm" data-home data-server-now="'.time().'"><section class="rh-stage" aria-labelledby="pr-home-title"><div class="rh-intro"><div class="rh-brand">PoolRotater <span>the player wiki</span></div><h1 id="pr-home-title">Just one<br><em>more map.</em></h1><p>Circles, fruit, or keys. Find your next round.<br>Pick a room. We’ll handle the rotation.</p><a class="rh-first" href="'.hsc(wl('guides:first_session')).'">First time here? <span>Start with the basics ↗</span></a><svg class="rh-playfield" viewBox="0 0 320 158" aria-hidden="true" focusable="false"><path d="M50 92Q107 26 162 68T267 65" fill="none" stroke="#ff8ebe" stroke-opacity=".14" stroke-width="27" stroke-linecap="round"/><path d="M50 92Q107 26 162 68T267 65" fill="none" stroke="#ffacd0" stroke-opacity=".38" stroke-width="2" stroke-dasharray="3 7"/><g class="rh-approach"><circle cx="50" cy="92" r="43" fill="none" stroke="#ff8ebe" stroke-width="2"/></g><g fill="#463046" stroke="#ffacd0" stroke-width="3"><circle cx="50" cy="92" r="28"/><circle cx="162" cy="68" r="28"/><circle cx="267" cy="65" r="28"/></g><g fill="white" text-anchor="middle" font-size="22" font-family="sans-serif" font-weight="600"><text x="50" y="100">1</text><text x="162" y="76">2</text><text x="267" y="73">3</text></g><g fill="#ff8ebe"><circle cx="88" cy="46" r="3"/><circle cx="219" cy="113" r="3"/><circle cx="288" cy="120" r="2"/></g></svg></div><div class="rh-selector" id="choose_your_room"><div class="rh-selector-head"><h2>Room select</h2><a href="'.hsc(wl('lobbies:start')).'">Compare rooms ↗</a></div><div class="rh-rooms">';
    $choices = [
        ['room1','standard_auto','osu!standard','Automatic difficulty','AUTO','circle'],
        ['room2','standard_fixed','osu!standard','Vote on difficulty','FIXED','circle'],
        ['room3','mania_7k','osu!mania','Seven-key sessions','7K','keys'],
        ['room4','mania_4k','osu!mania','Four-key sessions','4K','keys'],
    ];
    foreach ($choices as [$alias,$guide,$mode,$title,$badge,$symbol]) {
        $room = []; foreach ($rooms as $candidate) if (($candidate['alias'] ?? '') === $alias) $room = $candidate;
        $ruleset=$room['settings']['ruleset_id']??$room['ruleset_id']??null;
        $type=$room['type']??([0=>'standard',1=>'taiko',2=>'catch',3=>'mania'][$ruleset]??null);
        if ($type === 'catch') { $guide='catch'; $mode='osu!catch'; $title='Fixed Catch difficulty'; $badge='CATCH'; $symbol='circle'; }
        elseif ($type === 'taiko') { $guide='taiko'; $mode='osu!taiko'; $title='Taiko native and converted NM'; $badge='TAIKO'; $symbol='circle'; }
        $state = $room['state'] ?? 'STALE'; $id = $room['room_id'] ?? null;
        $joinable = is_int($id) && $id > 0 && !in_array($state,['OFFLINE','STALE'],true);
        $players = is_int($room['players'] ?? null) && $room['players'] >= 0 ? $room['players'].($room['players'] === 1 ? ' person' : ' people') : 'Count unavailable';
        echo '<article class="pr-home-room rh-room" data-home-room="'.$alias.'"><div class="rh-mode-icon rh-icon-'.$symbol.'" aria-hidden="true">'.($symbol==='circle'?'<i></i>':'<i></i><i></i><i></i><i></i>').'</div><div class="rh-room-info"><span class="rh-mode">'.$mode.' <b>'.$badge.'</b></span><h3><a href="'.hsc(wl('lobbies:'.$guide)).'">'.hsc($title).'</a></h3><div class="rh-room-meta"><span data-home-phase>'.hsc(prAnalyticsPhase($state)).'</span><span data-home-count>'.hsc($players).'</span></div></div><div class="rh-room-actions"><a class="pr-home-join"'.($joinable?' href="osu://room/'.$id.'"':' aria-disabled="true"').' aria-label="Join '.hsc($mode.' '.$badge).' in osu!">Join <span aria-hidden="true">↗</span></a><a class="pr-home-browser"'.($joinable?' href="https://poolrotator-91-107-237-182.nip.io/room/'.$id.'"':' hidden').' target="_blank" rel="noopener noreferrer">Browser<span class="a11y"> for '.hsc($mode.' '.$badge).' (opens a new tab)</span></a></div></article>';
    }
    echo '</div></div>';
    require_once __DIR__.'/presentation.php';
    echo '<div class="pr-home-live-grid" aria-label="Owner live maps">';
    foreach($status['rooms']??[] as $room)echo prLiveCard($room,$fresh,true);
    echo '</div><div class="rh-presence"><span><i aria-hidden="true"></i><strong data-home-people>'.$people.'</strong> people in public rooms</span><a href="'.hsc(wl('status:start')).'">Live maps & queues →</a></div></section><div class="rh-utility"><span>Lobby chat is your remote.</span><a href="'.hsc(wl('commands:start')).'">Command reference</a><a href="'.hsc(wl('troubleshooting:start')).'">Stuck? Find help</a></div>';
    // Community cards retain independent control bindings; occupancy includes both feeds.
    require_once __DIR__ . '/community.php';
    prCommunityRooms(prCommunityRead('/srv/poolrotator-wiki/public/community/rooms.json'), time(),
        array_values(array_filter(array_column($status['rooms'] ?? [], 'room_id'), fn($id)=>is_int($id) && $id>0)));
    echo '<section class="rh-record" aria-labelledby="rh-record-title"><div class="rh-record-title"><span>SESSION RECORD</span><h2 id="rh-record-title">The last 7 days</h2></div><div class="rh-totals">';
    foreach ([['players','Players recorded'],['maps','Map participations'],['pp','Recorded PP gained']] as [$key,$label]) {
        $value = $summary['available'] && isset($summary['totals'][$key]) ? ($key==='pp'?'+':'').prAnalyticsNumber($summary['totals'][$key],$key==='pp'?1:0) : '—';
        echo '<div><strong data-home-stat="'.$key.'">'.hsc($value).'</strong><span>'.$label.'</span></div>';
    }
    echo '</div><div class="rh-record-foot"><p data-home-stats-note>'.($summary['available']?'Anonymous community totals · updated every five minutes.':'Statistics are temporarily unavailable.').'</p><a href="'.hsc(wl('statistics:start',['pr_period'=>'7d'])).'">Player achievements ↗</a><a href="'.hsc(wl('statistics:how_it_works')).'">How we count</a></div></section>';
    echo '<section class="rh-reading" aria-labelledby="rh-reading-title"><div class="rh-reading-head"><span>BETWEEN ROUNDS</span><h2 id="rh-reading-title">Know what you’re playing.</h2></div><div class="rh-reading-grid"><a class="rh-finder" href="'.hsc(wl('technical:standard_map_finder')).'"><div class="rh-finder-art" aria-hidden="true"><div class="rh-map-dot">?</div><div class="rh-signal"><span>FARM</span><i></i><i></i><i></i><i></i><i></i></div><div class="rh-signal rh-stream-signal"><span>STREAM</span><i></i><i></i><i></i><i></i><i></i><i></i><i></i></div></div><span class="rh-reading-tag">OSU!STANDARD · INSIDE THE CODE</span><h3>Why this map?</h3><p>Follow a map from player scores and tapping patterns to the Farm and Stream pools.</p><span class="rh-reading-link">Open the finder’s notebook ↗</span></a><a class="rh-atlas" href="'.hsc(wl('wiki:pattern_atlas')).'"><div class="rh-key-art" aria-hidden="true"><i></i><i></i><i></i><i></i><b></b><b></b><b></b><b></b><b></b><b></b></div><span class="rh-reading-tag">OSU!MANIA · VISUAL FIELD GUIDE</span><h3>Name that pattern.</h3><p>Jacks, chordjacks, streams, holds. Learn the shapes, then choose your style.</p><span class="rh-reading-link">Explore the pattern atlas ↗</span></a></div></section>';
    $recent = $releases ?? prHomeReleases();
    echo '<div class="rh-footer"><div><span>PATCH NOTES</span>';
    if ($recent) {
        $release = $recent[0];
        echo '<a class="rh-release" href="'.hsc(wl($release['article'])).'"><strong>'.hsc($release['version']).'</strong> '.hsc($release['subject']).' <span aria-hidden="true">↗</span></a><small>'.hsc($release['status']).'</small>';
    } else echo '<a class="rh-release" href="'.hsc(wl('updates:start')).'">Browse retained release notes ↗</a>';
    echo '</div><a href="'.hsc(wl('updates:start')).'">Archive</a><a href="'.hsc(wl('community:contributing')).'">Help write the wiki ↗</a></div><noscript><p class="rh-snapshot">Counts and joins show this page’s snapshot. Reload for current information.</p></noscript></div><script type="application/json" id="pr-home-stats-data">'.json_encode($summary,JSON_HEX_TAG|JSON_HEX_AMP|JSON_HEX_APOS|JSON_HEX_QUOT).'</script>';
}
