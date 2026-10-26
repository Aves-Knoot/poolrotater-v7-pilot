<?php
/** Shared, read-only public lobby presentation. No administration/control binding. */
function prLiveEscape($value) { return htmlspecialchars((string)$value, ENT_QUOTES | ENT_SUBSTITUTE, 'UTF-8'); }
function prLiveNumber($value) { return is_int($value) && $value > 0 && $value <= 9007199254740991; }
function prLiveClock($seconds) { $seconds=max(0,(int)$seconds); return floor($seconds/60).':'.str_pad((string)($seconds%60),2,'0',STR_PAD_LEFT); }
function prLiveRoomMode($room) {
    $room=is_array($room)?$room:[];$settings=is_array($room['settings']??null)?$room['settings']:[];
    $type=$room['type']??null;
    if($type==='mania_7k')return 'mania_7k';if($type==='mania_4k')return 'mania_4k';
    if(in_array($type,['standard','taiko','catch'],true))return $type;
    $label=strtolower((string)($room['label']??''));
    $ruleset=$settings['ruleset_id']??$room['ruleset_id']??null;
    if(is_int($ruleset)&&$ruleset>=0&&$ruleset<=3){
        if($ruleset===0)return 'standard';if($ruleset===1)return 'taiko';if($ruleset===2)return 'catch';
        $keys=$settings['keys']??$room['keys']??null;
        if($keys===7)return 'mania_7k';if($keys===4)return 'mania_4k';
        if(preg_match('/\b7k\b|seven[ -]?key/',$label))return 'mania_7k';if(preg_match('/\b4k\b|four[ -]?key/',$label))return 'mania_4k';return 'mania';
    }
    $preset=$room['preset']??$settings['preset']??null;
    if(in_array($preset,['standard-auto','standard-fixed','standard-loved'],true))return 'standard';
    if($preset==='catch-fixed')return 'catch';if(in_array($preset,['taiko-fixed','taiko-auto'],true))return 'taiko';
    if($preset==='mania-7k')return 'mania_7k';if($preset==='mania-4k')return 'mania_4k';
    if(in_array($type,['mania','mania_other'],true)){
        $keys=$settings['keys']??$room['keys']??null;
        if($keys===7)return 'mania_7k';if($keys===4)return 'mania_4k';
    }
    if(preg_match('/\btaiko\b/',$label))return 'taiko';if(preg_match('/\bcatch\b/',$label))return 'catch';
    if(preg_match('/\b7k\b|seven[ -]?key/',$label))return 'mania_7k';
    if(preg_match('/\b4k\b|four[ -]?key/',$label))return 'mania_4k';
    if(preg_match('/\bstandard\b/',$label))return 'standard';
    if($ruleset===3||in_array($type,['mania','mania_other'],true))return 'mania';
    return null;
}
function prLiveRoomLabel($room,$slot=null) {
    $room=is_array($room)?$room:[];$settings=is_array($room['settings']??null)?$room['settings']:[];
    $mode=prLiveRoomMode($room);$preset=$room['preset']??$settings['preset']??null;
    if($mode==='standard'){
        $loved=is_bool($settings['loved_mode']??null)?$settings['loved_mode']:$preset==='standard-loved';
        if($loved)return 'Standard · Loved';
        $auto=$settings['auto_difficulty']??null;
        if(!is_bool($auto))$auto=in_array($preset,['standard-auto'],true)?true:(in_array($preset,['standard-fixed'],true)?false:null);
        if($auto===true)return 'Standard · Auto';if($auto===false)return 'Standard · Fixed';
        return 'osu!standard';
    }
    if($mode==='taiko')return 'osu!taiko';if($mode==='catch')return 'osu!catch';
    if($mode==='mania_7k')return 'osu!mania · 7K';if($mode==='mania_4k')return 'osu!mania · 4K';
    if($mode==='mania')return 'osu!mania';
    if(!is_int($slot)&&preg_match('/^room([1-4])$/',(string)($room['alias']??''),$m))$slot=(int)$m[1];
    return 'Lobby'.(is_int($slot)&&$slot>=1&&$slot<=4?' '.$slot:'');
}
function prLiveRoomGuide($room) {
    $mode=prLiveRoomMode($room);$settings=is_array($room['settings']??null)?$room['settings']:[];$preset=$room['preset']??$settings['preset']??null;
    if($mode==='taiko')return 'taiko';if($mode==='catch')return 'catch';
    if($mode==='mania_7k')return 'mania_7k';if($mode==='mania_4k')return 'mania_4k';
    if($mode==='standard'){
        $loved=is_bool($settings['loved_mode']??null)?$settings['loved_mode']:$preset==='standard-loved';
        $auto=$settings['auto_difficulty']??null;if(!is_bool($auto))$auto=$preset==='standard-auto'?true:($preset==='standard-fixed'?false:null);
        return $loved?'standard_loved':($auto===true?'standard_auto':($auto===false?'standard_fixed':'start'));
    }
    return 'start';
}
function prLiveTiming($room,$fresh,$now) {
    $p=$room['playback']??null;
    if (!$fresh || !in_array($room['state']??'', ['PLAYING','playing'],true) || !is_array($p) || !is_numeric($p['started_at']??null) || !is_numeric($p['duration_seconds']??null)) return null;
    $start=(float)$p['started_at'];$duration=(float)$p['duration_seconds'];
    if(!is_finite($start)||!is_finite($duration)||$start<=0||$start>$now+5||$duration<=0||$duration>86400) return null;
    if(($room['settings']['ruleset_id']??null)===1&&isset($p['clock_rate'])&&(float)$p['clock_rate']!==1.0)return null;
    // Community timing is qualified centrally and bound to this exact map/match.
    if(($room['ownership']??'owner')==='community' && (!prLiveNumber($p['match_id']??null)||!prLiveNumber($p['beatmap_id']??null)||$p['beatmap_id']!==($room['current_map']['beatmap_id']??null))) return null;
    $elapsed=max(0,min($duration,$now-$start)); return ['start'=>$start,'duration'=>$duration,'elapsed'=>$elapsed,'ratio'=>$elapsed/$duration,'nominal_speed'=>($p['nominal_speed']??false)===true];
}
function prLiveSettings($settings,$fresh) {
    if (!$fresh || !is_array($settings) || !$settings) return '';
    $labels=['ruleset_id'=>'Ruleset','keys'=>'Keys','difficulty'=>'Difficulty mode','auto_difficulty'=>'Automatic difficulty','active_range'=>'Active difficulty','configured_range'=>'Base difficulty','selection_style'=>'Selection style','style'=>'Selection style','min_players'=>'Minimum players','countdown_seconds'=>'Countdown · seconds','auto_start_seconds'=>'Auto-start · seconds','queue_ahead'=>'Future queue target','max_length'=>'Map limit · seconds','mania_farm_enabled'=>'Farm weighting','mania_style_fallback_enabled'=>'Style fallback','freestyle'=>'Freemod','voteskip_enabled'=>'Vote skip','user_commands_enabled'=>'Player commands','required_mods'=>'Required mods','allowed_mods'=>'Allowed mods'];
    $out='<details class="pr-room-settings"><summary><span>Room settings</span><span aria-hidden="true">⌄</span></summary><dl>';
    foreach($labels as $key=>$label){$value=$settings[$key]??null;if($value===null)continue;
        if($key==='ruleset_id')$value=[0=>'osu!standard',1=>'osu!taiko',2=>'osu!catch',3=>'osu!mania'][$value]??'Unknown';
        elseif(is_bool($value))$value=$value?'Enabled':'Disabled';
        elseif(is_array($value)) {$value=array_slice($value,0,32);if(str_ends_with($key,'range'))$value=count($value)===2&&is_numeric($value[0])&&is_numeric($value[1])?number_format((float)$value[0],2).'–'.number_format((float)$value[1],2).'★':'Unknown';else {$value=array_filter($value,fn($v)=>is_string($v)&&strlen($v)<=16);$value=$value?implode(', ',$value):'None';}}
        elseif(!is_string($value)&&!is_numeric($value))continue;
        $out.='<dt>'.prLiveEscape($label).'</dt><dd>'.prLiveEscape(substr((string)$value,0,160)).'</dd>';
    }
    return $out.'</dl><p class="pr-settings-note">Public settings reported by this host. Other controls remain local to the host.</p></details>';
}
function prLiveCard($room,$fresh,$compact=false,$now=null) {
    $now=$now??time();$community=($room['ownership']??'owner')==='community';
    $alias=$room['alias']??'room1';$key=$community?($room['key']??'unknown'):$alias;
    $map=$fresh&&is_array($room['current_map']??null)?$room['current_map']:[];
    $states=['PLAYING'=>'Playing','STARTING'=>'Starting','SCORING'=>'Between maps','IDLE'=>'Waiting','OFFLINE'=>'Offline','STALE'=>'Status unavailable','playing'=>'Playing','starting'=>'Starting','idle'=>'Waiting','draining'=>'Finishing up','unavailable'=>'Status unavailable'];
    $state=$fresh?($room['state']??'STALE'):'STALE';$state=isset($states[$state])?$state:'STALE';
    $label=$community?($room['title']??'Community lobby'):prLiveRoomLabel($room);
    $id=$fresh?($room['room_id']??null):null;$join=prLiveNumber($id)&&!in_array($state,['STALE','OFFLINE','unavailable'],true);
    $set=$map['beatmapset_id']??null;$title=$map['title']??$map['label']??($state==='OFFLINE'?'Room is offline':($state==='STALE'?'Status temporarily unavailable':'Waiting for the next map'));
    $subtitle=trim(($map['artist']??'').(!empty($map['version'])?' · '.$map['version']:''));$timing=prLiveTiming($room,$fresh,$now);
    $out='<div class="pr-now-playing '.($compact?'pr-np-compact':'pr-np-full').'" data-live-card data-ownership="'.($community?'community':'owner').'" data-live-key="'.prLiveEscape($key).'"'.(!$community?' data-np-room="'.prLiveEscape($alias).'"':'').' data-state="'.prLiveEscape($state).'" data-map="'.(!empty($map)?'true':'false').'" data-timed="'.($timing?'true':'false').'" data-nominal-speed="'.($timing&&!empty($timing['nominal_speed'])?'true':'false').'" data-start="'.($timing?$timing['start']:0).'" data-duration="'.($timing?$timing['duration']:0).'">';
    $out.='<img class="pr-np-cover"'.(prLiveNumber($set)?' src="https://assets.ppy.sh/beatmaps/'.$set.'/covers/cover.jpg"':' hidden').' alt="" aria-hidden="true" loading="lazy" decoding="async" referrerpolicy="no-referrer"><div class="pr-np-shade"></div><div class="pr-np-content"><div class="pr-np-top"><a class="pr-np-room" href="/doku.php?id=status:start#'.prLiveEscape($community?'community-'.$key:$alias).'">'.prLiveEscape($label).'</a><span class="pr-np-people">'.($fresh&&is_int($room['players']??null)&&$room['players']>=0&&$room['players']<=128?$room['players'].' in room':'—').'</span></div>';
    $out.='<p class="pr-live-host">'.prLiveEscape($community?'Hosted by '.($room['operator']??'Community host'):'Hosted by Papapyrus · Owner lobby').'</p>';
    $out.='<div class="'.($compact?'pr-np-main':'pr-np-map').'"><div class="pr-np-copy"><a class="pr-np-title"'.(prLiveNumber($map['beatmap_id']??null)?' href="https://osu.ppy.sh/beatmaps/'.$map['beatmap_id'].'" target="_blank" rel="noopener noreferrer"':'').' title="'.prLiveEscape($title).'">'.prLiveEscape($title).'</a><p class="pr-np-artist">'.prLiveEscape($subtitle?:'Automatic multiplayer').'</p><div class="pr-np-status"><span class="pr-np-phase" data-phase="'.prLiveEscape($state).'"><i aria-hidden="true"></i><span>'.prLiveEscape($states[$state]).'</span></span><span class="pr-np-clock">'.($timing?'Estimated '.prLiveClock($timing['elapsed']).' / '.prLiveClock($timing['duration']):'').'</span></div></div>';
    if($compact)$out.='<a class="pr-np-join"'.($join?' href="osu://room/'.$id.'"':' aria-disabled="true"').' aria-label="Join '.prLiveEscape($label).' in osu!">Join ↗</a>';
    $out.='</div>';
    if(!$compact){$out.='<div class="pr-map-facts"><span data-np-stars>'.(is_numeric($map['stars']??null)?number_format((float)$map['stars'],2).'★':'Difficulty unknown').'</span><span data-np-length>'.(is_numeric($map['total_length']??null)?prLiveClock($map['total_length']):'Length unknown').'</span></div>';if($fresh&&is_int($room['playing_players']??null)&&$room['playing_players']>=0&&$room['playing_players']<=128)$out.='<p class="pr-live-participants">'.$room['playing_players'].' playing now</p>';if($fresh&&is_int($room['match_participants']??null)&&$room['match_participants']>=0&&$room['match_participants']<=128)$out.='<p class="pr-live-participants">'.$room['match_participants'].' players at match start</p>';}
    $out.='<div class="pr-np-progress"'.(!$timing?' hidden':'').' role="progressbar" aria-label="Estimated song progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="'.($timing?round($timing['ratio']*100):0).'"><span style="transform:scaleX('.($timing?round($timing['ratio'],4):0).')"></span></div>';
    if(!$compact)$out.='<div class="pr-np-bottom"><a class="pr-np-join"'.($join?' href="osu://room/'.$id.'"':' aria-disabled="true"').' aria-label="Join '.prLiveEscape($label).' in osu!">Join lobby ↗</a>'.($join&&$community?'<a class="pr-community-view" href="https://osu.ppy.sh/multiplayer/rooms/'.$id.'" target="_blank" rel="noopener noreferrer">View room ↗</a>':'').'</div>';
    return $out.'</div></div>';
}
