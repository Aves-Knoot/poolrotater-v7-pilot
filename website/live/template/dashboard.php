<?php
/* Public dashboard renderer. All data comes from existing sanitized exports. */
if (!defined('DOKU_INC')) die();
function prDashboardIcon($name) {
    $paths = [
        'people'=>'<circle cx="9" cy="7" r="3"/><path d="M3 21v-3a6 6 0 0 1 12 0v3M16 4a3 3 0 0 1 0 6m3 11v-3a6 6 0 0 0-3-5"/>',
        'rooms'=>'<rect x="3" y="3" width="7" height="7" rx="2"/><rect x="14" y="3" width="7" height="7" rx="2"/><rect x="3" y="14" width="7" height="7" rx="2"/><rect x="14" y="14" width="7" height="7" rx="2"/>',
        'clock'=>'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
        'activity'=>'<path d="M2 12h5l3-8 4 16 3-8h5"/>',
        'settings'=>'<path d="M4 6h16M4 12h16M4 18h16"/><circle cx="8" cy="6" r="2"/><circle cx="16" cy="12" r="2"/><circle cx="10" cy="18" r="2"/>',
    ];
    return '<svg class="pr-dash-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' . ($paths[$name] ?? $paths['activity']) . '</svg>';
}
function prDashboardTime($seconds) {
    $seconds=max(0,(int)$seconds);
    return $seconds>=86400 ? floor($seconds/86400).'d '.floor($seconds%86400/3600).'h' : ($seconds>=3600 ? floor($seconds/3600).'h '.floor($seconds%3600/60).'m' : floor($seconds/60).'m');
}
function prDashboardLength($seconds) { return floor($seconds/60).':'.str_pad((string)floor($seconds%60),2,'0',STR_PAD_LEFT); }
function prDashboardQueue($room,$fresh) {
    if (!$fresh || empty($room['queue_known'])) return '<p class="pr-queue-empty">' . ($fresh?'The next map is being prepared.':'Waiting for a fresh room update.') . '</p>';
    if (empty($room['queue'])) return '<p class="pr-queue-empty">No upcoming maps in the observed queue.</p>';
    $out='<ol class="pr-up-next">';
    foreach ($room['queue'] as $i=>$map) {
        $id=$map['beatmap_id'] ?? null;$set=$map['beatmapset_id'] ?? null;
        $out.='<li><span class="pr-queue-art" aria-hidden="true">';
        if (is_int($set)&&$set>0) $out.='<img alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer" src="https://assets.ppy.sh/beatmaps/'.$set.'/covers/cover.jpg">';
        $out.='<span>'.str_pad((string)($i+1),2,'0',STR_PAD_LEFT).'</span></span><div class="pr-queue-copy"><a'.(is_int($id)&&$id>0?' href="https://osu.ppy.sh/beatmaps/'.$id.'" target="_blank" rel="noopener noreferrer"':'').'>'.hsc($map['title']??$map['label']??'Upcoming map').'</a><span>'.hsc(implode(' · ',array_filter([$map['artist']??null,$map['version']??null]))).'</span></div>';
        if (is_numeric($map['stars']??null)) $out.='<span class="pr-queue-stars">'.prAnalyticsNumber($map['stars'],2).'★</span>';
        $out.='</li>';
    }
    return $out.'</ol>';
}
function prDashboardRoomCard($room,$fresh) {
    $alias=$room['alias'];$settings=$room['settings']??[];
    $descriptions=['room1'=>'Difficulty follows the room. Keep the rounds flowing.','room2'=>'A steady difficulty range, with player votes.','room3'=>'Seven keys. Find your next Mania challenge.','room4'=>'Four keys. A fresh mix of Mania maps.'];
    $out='<article class="pr-room-card pr-dash-room" id="'.hsc($alias).'" data-room-type="'.(in_array($alias,['room1','room2'],true)?'standard':'mania').'">'.prNowPlayingCard($room,$fresh,false);
    $out.='<div class="pr-room-body"><p class="pr-room-description">'.hsc($descriptions[$alias]??'Automatic multiplayer').'</p><div class="pr-room-pills"><span><span class="pr-pill-label">Difficulty</span> <b data-room-range>';
    $range=$fresh?($settings['active_range']??[]):[];
    $out.=count($range)===2?prAnalyticsNumber($range[0],2).'–'.prAnalyticsNumber($range[1],2).'★':'Unknown';
    $out.='</b></span><span><span class="pr-pill-label">Map limit</span> <b data-room-limit>'.($fresh&&is_numeric($settings['max_length']??null)?prDashboardLength($settings['max_length']):'Unknown').'</b></span></div>';
    $out.='<div class="pr-queue-heading"><h3>Up next</h3><span>Observed queue</span></div><div class="pr-queue-view">'.prDashboardQueue($room,$fresh).'</div>';
    $out.='<details class="pr-room-settings"><summary>'.prDashboardIcon('settings').'<span>Room settings</span><span class="pr-disclosure-arrow" aria-hidden="true">⌄</span></summary><dl>';
    foreach (['difficulty'=>'Difficulty mode','active_range'=>'Active difficulty','configured_range'=>'Base difficulty','min_players'=>'Minimum players','countdown_seconds'=>'Countdown · seconds','auto_start_seconds'=>'Auto-start · seconds','queue_ahead'=>'Future queue target','max_length'=>'Map limit · seconds','mania_farm_enabled'=>'Farm weighting','mania_style_fallback_enabled'=>'Style fallback','freestyle'=>'Freestyle','voteskip_enabled'=>'Vote skip','user_commands_enabled'=>'Player commands','required_mods'=>'Required mods','allowed_mods'=>'Allowed mods'] as $key=>$label) {
        if (!isset($settings[$key])) continue;
        $value=$fresh?$settings[$key]:null;
        if ($value===null) $value='Unknown';
        elseif (str_ends_with($key,'range')) $value=count($value)===2?prAnalyticsNumber($value[0],2).'–'.prAnalyticsNumber($value[1],2).'★':'Not recorded';
        elseif (is_bool($value)) $value=$value?'Enabled':'Disabled';
        elseif (is_array($value)) $value=$value?implode(', ',$value):'None';
        $out.='<dt data-setting="'.hsc($key).'">'.hsc($label).'</dt><dd>'.hsc((string)$value).'</dd>';
    }
    $id=$fresh?($room['room_id']??null):null;
    $out.='</dl><p class="pr-settings-note">Active difficulty follows the room title. Other values describe configured settings.</p></details><div class="pr-live-room-links"><a href="'.hsc(wl($room['guide'])).'">Lobby guide <span aria-hidden="true">→</span></a><a class="pr-browser-room"'.(is_int($id)&&$id>0?' href="https://poolrotator-91-107-237-182.nip.io/room/'.$id.'"':' aria-disabled="true"').'>Open in browser <span aria-hidden="true">↗</span></a></div></div></article>';
    return $out;
}
function prDashboardTotalCount($row,$states=[]) {
    if (!is_array($row) || count($row)!==6) return null;
    $total=0;
    for ($i=1;$i<=4;$i++) {
        if (is_int($row[$i]) && $row[$i]>=0) $total+=$row[$i];
        elseif ($row[$i]===null && ($states[$row[0]][$i]??null)===0) continue;
        else return null;
    }
    return $total;
}
function prDashboardTotalSamples($samples,$states=[]) {
    $totals=[];
    foreach ($samples as $row) {
        if (!is_array($row) || count($row)!==6) continue;
        $total=prDashboardTotalCount($row,$states);
        $totals[]=[$row[0],$total,null,null,null,$row[5]];
    }
    return $totals;
}
function prDashboardGraph($samples,$index,$label,$since,$states=[],$total=false) {
    $end=time();$samples=array_values(array_filter($samples,fn($r)=>is_array($r)&&count($r)===6&&is_numeric($r[0])&&$r[0]>=$since&&$r[0]<=$end));
    $known=array_values(array_filter($samples,fn($r)=>is_int($r[$index+1])&&$r[$index+1]>=0));
    $offline=array_values(array_filter($samples,fn($r)=>!is_int($r[$index+1])&&($states[$r[0]][$index+1]??null)===0));
    $unavailable=count($samples)-count($known)-count($offline);
    if (!$known) return '<div class="pr-chart-empty">'.($total?'No complete network readings in this window. '.$unavailable.' unavailable.':'No player-count readings in this window. '.count($offline).' offline, '.$unavailable.' unavailable.').'</div>';
    $peak=max(array_column($known,$index+1));$scale=max(1,$peak);$average=array_sum(array_column($known,$index+1))/count($known);
    $description=$total?'All reporting public room counts combined, including spectators. Confirmed offline rooms contribute zero; unknown room readings and gaps over 15 minutes break the total.':'Red marks indicate confirmed offline rooms; other gaps indicate unavailable readings.';
    $out='<svg class="pr-activity-chart" viewBox="0 0 680 205" role="img" aria-label="'.hsc($label.': player count over time').'"><title>'.hsc($label.' player count').'</title><desc>Five-minute observations. Peak '.$peak.' players. '.hsc($description).'</desc>';
    foreach ([35,100,165] as $y) $out.='<path class="pr-chart-grid" d="M40 '.$y.'H640"/>';
    $out.='<text x="12" y="39">'.$scale.'</text><text x="20" y="169">0</text>';
    $segments=[];$segment=[];$last=0;
    foreach ($samples as $r) {
        if (!is_int($r[$index+1])||$r[$index+1]<0||($last&&$r[0]-$last>900)) {if($segment)$segments[]=$segment;$segment=[];$last=0;}
        if (!is_int($r[$index+1])||$r[$index+1]<0) continue;
        $segment[]=[round(40+600*($r[0]-$since)/max(1,$end-$since),2),round(165-130*$r[$index+1]/$scale,2)];$last=$r[0];
    }
    if ($segment) $segments[]=$segment;
    foreach ($segments as $segment) {
        $path='';foreach($segment as [$x,$y])$path.=($path?' H '.$x.' V '.$y:'M '.$x.' '.$y);
        $area=$path.' L '.$segment[count($segment)-1][0].' 165 L '.$segment[0][0].' 165 Z';
        $out.='<path class="pr-chart-area" d="'.$area.'"/><path class="pr-chart-line" d="'.$path.'"/>';
        if ($total && count($segment)===1) $out.='<circle class="pr-chart-point" cx="'.$segment[0][0].'" cy="'.$segment[0][1].'" r="3"><title>Single complete network reading</title></circle>';
    }
    foreach ($offline as $r) $out.='<circle class="pr-chart-offline" cx="'.round(40+600*($r[0]-$since)/max(1,$end-$since),2).'" cy="165" r="4"><title>Room offline</title></circle>';
    $out.='<text x="40" y="194">'.hsc(gmdate('M j H:i',$since)).'</text><text x="640" y="194" text-anchor="end">'.hsc(gmdate('M j H:i',$end)).' UTC</text></svg>';
    return $out.'<div class="pr-chart-summary"><span>Peak <b>'.$peak.'</b></span><span>Average <b>'.prAnalyticsNumber($average,1).'</b></span><span>'.count($known).($total?' complete readings':' readings').'</span>'.($total?'':'<span>'.count($offline).' offline</span>').'<span>'.$unavailable.' unavailable</span></div>';
}
function prDashboardStatus($embedStatus=true) {
    require_once __DIR__.'/community.php';
    $community=prCommunityRead('/srv/poolrotator-wiki/public/community/rooms.json');
    $data=prAnalyticsData('status');$activity=prAnalyticsData('activity');$fresh=prAnalyticsFresh($data);$service=$data['service']??[];$rooms=$data['rooms']??[];
    if(count($rooms)!==4){$rooms=[];foreach(['standard_automatic','standard_fixed','mania_7k','mania_4k'] as $i=>$guide)$rooms[]=['alias'=>'room'.($i+1),'label'=>['Standard automatic','Standard fixed','Mania 7K','Mania 4K'][$i],'state'=>'STALE','players'=>null,'room_id'=>null,'queue'=>[],'queue_known'=>false,'settings'=>[],'guide'=>'lobbies:'.$guide];}
    $availability=$data['availability']??[];
    $knownCounts=array_filter($rooms,fn($r)=>is_int($r['players']??null));
    $players=prCombinedOccupancy($data,$community);
    $open=$fresh&&!empty($data['available'])?count(array_filter($rooms,fn($r)=>is_int($r['room_id']??null)&&!in_array($r['state']??'STALE',['STALE','OFFLINE'],true))):null;
    $state=$fresh&&isset($service['online'])?($service['online']?'online':'offline'):'unknown';
    echo '<section class="pr-live-status pr-dashboard" data-status-page data-expires="'.(int)($data['expires_at']??0).'"><header class="pr-dashboard-hero"><div><span class="pr-dashboard-eyebrow">PLAY TOGETHER</span><h1>Live lobbies</h1><p>Find your next map. Jump into the round.</p></div><div class="pr-dashboard-connection"><span class="pr-dashboard-health" data-health="'.hsc($state).'"><i aria-hidden="true"></i><span data-service-label>Bot '.hsc($state).'</span></span>'.(!empty($data['generated_at'])?prAnalyticsUpdated($data):'<p class="pr-updated">Waiting for fresh status</p>').'</div></header>';
    echo '<div class="pr-service-metrics pr-metric-grid">';
    $metrics=[['players','People here',$players===null?'—':(string)$players,'Across owner and community rooms','people'],['rooms','Rooms open',$open===null?'—':$open.' / 4','Pick a ruleset below','rooms'],['uptime','Current uptime',$fresh&&isset($service['uptime_seconds'])?prDashboardTime($service['uptime_seconds']):'—','This uninterrupted run','clock'],['availability','Availability · 24h',isset($availability['percent'])?prAnalyticsNumber($availability['percent'],2).'%':'—','Based on observed samples','activity']];
    $peak=$data['peak_players']??null;
    $peakValid=is_array($peak)&&is_int($peak['count']??null)&&$peak['count']>=0&&$peak['count']<=2048&&is_int($peak['tracking_since']??null)&&$peak['tracking_since']>=1600000000&&$peak['tracking_since']<=time()+30;
    foreach ($metrics as [$key,$label,$value,$hint,$icon]) {
        echo '<div class="pr-metric"><div class="pr-metric-label">'.prDashboardIcon($icon).'<span>'.hsc($label).'</span></div><strong data-live-metric="'.$key.'">'.hsc($value).'</strong><small>'.hsc($hint).'</small>';
        if ($key==='players') echo '<small class="pr-peak-record"><span>Peak observed <b data-live-peak>'.($peakValid?(int)$peak['count']:'—').'</b></span><span data-live-peak-since>'.($peakValid?'since '.hsc(gmdate('M j, Y',$peak['tracking_since'])):'').'</span></small>';
        echo '</div>';
    }
    echo '</div><div class="pr-room-toolbar" id="rooms"><div><h2>Choose your room</h2><p>Standard or Mania. There’s a round for you.</p></div><div class="pr-room-filters" role="group" aria-label="Filter lobbies"><button type="button" data-room-filter="all" aria-pressed="true">All rooms</button><button type="button" data-room-filter="standard" aria-pressed="false">Standard</button><button type="button" data-room-filter="mania" aria-pressed="false">Mania</button></div></div><div class="pr-room-grid">';
    foreach ($rooms as $room) echo prDashboardRoomCard($room,$fresh);
    echo '</div><p class="pr-dashboard-timing">Status updates every 10 seconds. Song progress is approximate. <a href="#about_status">How it works ↓</a></p>';
    require_once __DIR__.'/community.php';
    prCommunityRooms(prCommunityRead('/srv/poolrotator-wiki/public/community/rooms.json'),time(),array_column($rooms,'room_id'),'dashboard');
    $windows=['3600'=>'Past hour','86400'=>'Past 24 hours','604800'=>'Past 7 days','2592000'=>'Past 30 days'];$window=prAnalyticsChoice('pr_chart_window',$windows,'86400');
    echo '<section class="pr-dashboard-activity" id="activity"><div class="pr-activity-toolbar"><div><span class="pr-dashboard-eyebrow">THE ROOM RHYTHM</span><h2>Lobby activity</h2><p>See when each room has company.</p></div><form class="pr-chart-form" method="get" action="'.hsc(DOKU_BASE.'doku.php').'"><input type="hidden" name="id" value="status:start"><label class="pr-chart-window"><span class="a11y">Activity time window</span><select name="pr_chart_window">';
    foreach ($windows as $value=>$label) echo '<option value="'.$value.'"'.((string)$value===(string)$window?' selected':'').'>'.$label.'</option>';
    echo '</select></label><button class="pr-chart-apply" type="submit">Show activity</button></form></div><div class="pr-activity-grid">';
    $labels=['Standard automatic','Standard fixed','Mania 7K','Mania 4K'];$roomStates=[];
    foreach ($activity['room_states']??[] as $stateRow) if (is_array($stateRow)&&count($stateRow)===5) $roomStates[$stateRow[0]]=$stateRow;
    $currentRow=[0];$currentStates=[0];
    foreach ($rooms as $room) {$currentRow[]=$room['players']??null;$currentStates[]=($room['state']??null)==='OFFLINE'?0:null;}
    $currentRow[]=null;
    $totalPlayers=prCombinedOccupancy($data,$community);
    echo '<article class="pr-activity-panel pr-activity-total" data-series="total"><div class="pr-chart-heading"><h3><i aria-hidden="true"></i>Total players · all public rooms</h3><span data-chart-current="total">'.($totalPlayers!==null?$totalPlayers.' here now':'—').'</span></div><p class="pr-chart-context">Owner and community room occupancy, including spectators. Unknown or stale community readings leave a gap; history starts when network observation begins.</p><div data-chart="total">'.prDashboardGraph(array_map(fn($r)=>[$r[0],$r[1],null,null,null,null],$activity['network_totals']??[]),0,'All public rooms',time()-(int)$window,[],true).'</div></article>';
    foreach ($labels as $i=>$label) echo '<article class="pr-activity-panel" data-series="'.$i.'"><div class="pr-chart-heading"><h3><i aria-hidden="true"></i>'.hsc($label).'</h3><span data-chart-current="room'.($i+1).'">'.($fresh&&is_int($rooms[$i]['players']??null)?$rooms[$i]['players'].' here now':'—').'</span></div><div data-chart="'.$i.'">'.prDashboardGraph($activity['samples']??[],$i,$label,time()-(int)$window,$roomStates).'</div></article>';
    echo '</div><div class="pr-activity-foot"><p>Five-minute readings, including spectators · per-room zero means an empty open room · red marks mean confirmed offline · other gaps mean unavailable data. Network totals include listed community rooms and offline owner rooms as zero; incomplete or stale coverage leaves a gap.</p><span data-history-updated>'.(!empty($activity['samples'])?'Last observation '.hsc(gmdate('M j H:i',(int)$activity['samples'][count($activity['samples'])-1][0])).' UTC':'No observations yet').'</span></div><details class="pr-chart-data"><summary>View recent observations <span aria-hidden="true">⌄</span></summary><div class="pr-table-scroll"><table><thead><tr><th>Time (UTC)</th><th>Standard auto</th><th>Standard fixed</th><th>7K</th><th>4K</th><th>Total · all rooms</th></tr></thead><tbody>';
    foreach (array_reverse(array_slice($activity['samples']??[],-24)) as $row) {echo '<tr><td>'.hsc(gmdate('M j H:i',(int)$row[0])).'</td>';foreach(array_slice($row,1,4) as $i=>$count) echo '<td>'.(is_int($count)?$count:(($roomStates[$row[0]][$i+1]??null)===0?'Offline':'—')).'</td>';echo '<td>'.(array_column($activity['network_totals']??[],1,0)[$row[0]]??'—').'</td></tr>';}
    echo '</tbody></table></div></details></section><details class="pr-service-details"><summary>Service health &amp; observation details <span aria-hidden="true">⌄</span></summary><div><p>Automatic restarts this activation: <b data-service-restarts>'.($fresh&&isset($service['restarts'])?(int)$service['restarts']:'Unknown').'</b>.</p><p class="pr-availability-note">Availability is based on '.(int)($availability['samples']??0).' service observations in the past 24 hours. Short outages between observations can be missed.</p><p>Chart history begins '.hsc(gmdate('M j, Y',(int)($data['history_since']??time()))).'.</p></div></details><noscript><p class="pr-unavailable">This is a snapshot. Reload for current room status; use Show activity to change the chart window.</p></noscript></section>';
    if($embedStatus) echo '<script type="application/json" id="pr-now-playing-data">'.json_encode($data,JSON_HEX_TAG|JSON_HEX_AMP|JSON_HEX_APOS|JSON_HEX_QUOT).'</script>';
    echo '<script type="application/json" id="pr-activity-data">'.json_encode($activity,JSON_HEX_TAG|JSON_HEX_AMP|JSON_HEX_APOS|JSON_HEX_QUOT).'</script>';
}
