<?php
/** Public community snapshots; shared card markup preserves owner authority. */
require_once (is_file(__DIR__.'/presentation.php')?__DIR__.'/presentation.php':__DIR__.'/../live/presentation.php');
function prCommunityRead($path) {
    if(is_link($path)||!is_file($path))return null;$stream=@fopen($path,'rb');if(!$stream)return null;
    try{$info=fstat($stream);if(!$info||$info['size']>262144)return null;$raw=stream_get_contents($stream,262145);if(!is_string($raw)||strlen($raw)>262144)return null;return json_decode($raw,true,64,JSON_THROW_ON_ERROR);}catch(Throwable $error){return null;}finally{fclose($stream);}
}
function prCommunityRoomValid($room) {
    if(!is_array($room)||!is_string($room['state']??null)||!in_array($room['state'],['playing','starting','idle','draining','unavailable'],true))return false;
    foreach(['title'=>640,'operator'=>320,'preset'=>32] as $key=>$limit)if(!is_string($room[$key]??null)||strlen($room[$key])>$limit)return false;
    if(!in_array($room['preset'],['standard-auto','standard-fixed','mania-4k','mania-7k','catch-fixed','taiko-fixed','taiko-auto'],true))return false;
    foreach(['fresh_until','hide_at']as $key)if(!is_numeric($room[$key]??null)||!is_finite((float)$room[$key]))return false;
    foreach(['players','playing_players','match_participants']as $key)if(isset($room[$key])&&(!is_int($room[$key])||$room[$key]<0||$room[$key]>128))return false;
    if(isset($room['current_map'])){if(!is_array($room['current_map'])||!prLiveNumber($room['current_map']['beatmap_id']??null))return false;
      foreach(['title','artist','version']as $key)if(isset($room['current_map'][$key])&&(!is_string($room['current_map'][$key])||strlen($room['current_map'][$key])>1024))return false;}
    return true;
}
function prCommunitySnapshotValid($data,$now,$allowStale=false) {
    return is_array($data)&&in_array($data['schema']??null,[1,2],true)&&($data['available']??false)===true&&is_array($data['rooms']??null)&&count($data['rooms'])<=100&&is_numeric($data['generated_at']??null)&&is_finite((float)$data['generated_at'])&&is_numeric($data['expires_at']??null)&&is_finite((float)$data['expires_at'])&&$data['generated_at']<=$now+30&&$data['expires_at']>$data['generated_at']&&$data['expires_at']-$data['generated_at']<=60&&($allowStale?$now-$data['generated_at']<1800:$data['expires_at']>$now);
}
function prCommunityVisible($data,$now,$ownerRoomIds=[]) {
    $valid=prCommunitySnapshotValid($data,$now,true);$current=prCommunitySnapshotValid($data,$now);
    if(!$valid)return null;$out=[];$seen=[];
    foreach($data['rooms'] as $room){if(!prCommunityRoomValid($room)||!prLiveNumber($room['room_id']??null)||in_array($room['room_id'],$ownerRoomIds,true)||($room['hide_at']??0)<=$now)continue;$key=$room['key']??null;if(!is_string($key)||!preg_match('/^[a-f0-9]{24}$/D',$key)||isset($seen[$key]))continue;if(isset($room['visibility'])&&$room['visibility']!=='public')continue;
    $seen[$key]=true;$room=array_intersect_key($room,array_flip(['key','room_id','title','operator','preset','state','players','playing_players','match_participants','min_stars','max_stars','current_map','settings','playback','fresh_until','hide_at']));
    foreach(['current_map'=>['beatmap_id','title','stars','beatmapset_id','artist','version','total_length','hit_length','cover_url','metadata_source'],'settings'=>['ruleset_id','keys','auto_difficulty','selection_style','allowed_mods','required_mods','freestyle','countdown_seconds'],'playback'=>['started_at','duration_seconds','estimated_end_at','approximate','estimated','match_id','beatmap_id','clock_rate','nominal_speed']]as $field=>$keys)if(is_array($room[$field]??null))$room[$field]=array_intersect_key($room[$field],array_flip($keys));
    foreach(['min_stars','max_stars']as $key)if(isset($room[$key])&&(!is_numeric($room[$key])||!is_finite((float)$room[$key])))unset($room[$key]);
    if(is_array($room['current_map']??null))foreach($room['current_map']as $key=>$v){if(in_array($key,['beatmap_id','beatmapset_id'],true)){if(!prLiveNumber($v))unset($room['current_map'][$key]);}elseif(in_array($key,['stars','total_length','hit_length'],true)){if(!is_numeric($v)||!is_finite((float)$v))unset($room['current_map'][$key]);}elseif(!is_string($v)||strlen($v)>1024)unset($room['current_map'][$key]);}
    foreach(['settings','playback']as $field){if(!is_array($room[$field]??null)){$room[$field]=null;continue;}foreach($room[$field]as $key=>$v){if($field==='settings'&&in_array($key,['allowed_mods','required_mods'],true)){$room[$field][$key]=is_array($v)?array_values(array_filter(array_slice($v,0,32),fn($mod)=>is_string($mod)&&strlen($mod)<=16)):[];}elseif(is_array($v)||is_object($v)||($field==='playback'&&!is_bool($v)&&!is_numeric($v))||is_string($v)&&strlen($v)>160)unset($room[$field][$key]);}}
    if($room['fresh_until']<=$now||$room['state']==='unavailable'){$room['state']='unavailable';$room['fresh_until']=0;foreach(['players','playing_players','match_participants','current_map','settings','playback']as $field)$room[$field]=null;}
    $room['ownership']='community';$out[]=$room;}return $out;
}
function prCommunityRooms($data,$now=null,$ownerRoomIds=[],$surface='cards') {
    $now=$now??time();$rooms=prCommunityVisible($data,$now,$ownerRoomIds);$compact=$surface==='sidebar';$uid='pr-community-title-'.$surface;
    echo '<section class="pr-community'.($compact?' pr-community-sidebar':'').'" data-community-rooms data-community-surface="'.$surface.'" data-community-server-now="'.$now.'" aria-labelledby="'.$uid.'"><div class="pr-community-heading"><div><span class="pr-community-eyebrow">More places to play</span><h2 id="'.$uid.'">Community lobbies</h2></div>'.(!$compact?'<p class="pr-community-intro">The same rotation, hosted by the community.<br>Live status is shared by each host.</p>':'').'</div><div data-community-list>';
    foreach($rooms??[] as $room){$fresh=($room['fresh_until']??0)>$now&&($room['state']??'')!=='unavailable';$type=($room['preset']??'')==='catch-fixed'?'catch':(str_starts_with($room['preset']??'','taiko-')?'taiko':(str_starts_with($room['preset']??'','mania')?'mania':'standard'));
      echo '<article class="pr-community-room" data-community-card data-live-key="'.prLiveEscape($room['key']).'" data-room-type="'.$type.'" data-preset="'.prLiveEscape($room['preset']??'').'" data-state="'.prLiveEscape($fresh?$room['state']:'unavailable').'" id="'.($compact?'sidebar-community-':'community-').prLiveEscape($room['key']).'">'.prLiveCard($room,$fresh,$compact,$now);
      if(!$compact){echo '<div class="pr-room-body"><div class="pr-community-facts">';if($fresh&&is_numeric($room['min_stars']??null)&&is_numeric($room['max_stars']??null))echo '<p class="pr-community-fact"><span>Star range</span><strong>'.number_format((float)$room['min_stars'],2).'–'.number_format((float)$room['max_stars'],2).'★</strong></p>';echo '<p class="pr-community-fact"><span>Players in room</span><strong>'.($fresh&&is_int($room['players']??null)?$room['players'].' players':'Unavailable').'</strong></p></div>'.prLiveSettings($room['settings']??null,$fresh).'</div>';}
      echo '</article>';
    }
    echo '</div><p data-community-note>'.($rooms===null?'Community status is temporarily unavailable.':(!$rooms?'No other public rooms are currently listed.':(!prCommunitySnapshotValid($data,$now)||!empty($data['refresh_failed'])?'Status refresh delayed; showing the last received lobby information.':'Updates every 30 seconds. Progress is estimated when match timing is available.').(!empty($data['more_available'])?' Showing the first 100 public rooms; more rooms are available.':''))).'</p></section>';
    echo '<script type="application/json" data-community-initial>'.json_encode($rooms===null?null:['schema'=>1,'available'=>true,'generated_at'=>$data['generated_at'],'expires_at'=>$data['expires_at'],'rooms'=>$rooms,'more_available'=>!empty($data['more_available']),'refresh_failed'=>!empty($data['refresh_failed'])],JSON_HEX_TAG|JSON_HEX_AMP|JSON_HEX_APOS|JSON_HEX_QUOT).'</script>';
}

function prCombinedOccupancy($owner,$community,$now=null) {
    $now=$now??time();$total=0;$ids=[];$aliases=[];
    // Each unavailable room contributes zero to the live headline. Historical
    // activity retains its missing-data gaps and never inherits this policy.
    $current=is_array($owner)&&($owner['available']??false)===true&&is_numeric($owner['generated_at']??null)&&is_finite((float)$owner['generated_at'])&&is_numeric($owner['expires_at']??null)&&is_finite((float)$owner['expires_at'])&&$owner['generated_at']<=$now+30&&$owner['expires_at']>$now&&$owner['expires_at']>$owner['generated_at'];
    if($current&&is_array($owner['rooms']??null)&&count($owner['rooms'])<=4)foreach($owner['rooms'] as $room){
        if(!is_array($room))continue;$alias=$room['alias']??null;
        if(!in_array($alias,['room1','room2','room3','room4'],true)||isset($aliases[$alias]))continue;
        if(!in_array($room['state']??null,['PLAYING','STARTING','SCORING','IDLE'],true))continue;
        $id=$room['room_id']??null;$n=$room['players']??null;
        if(!is_int($n)||$n<0||$n>128||!is_int($id)||$id<=0||$id>9007199254740991||isset($ids[$id]))continue;
        $aliases[$alias]=true;$ids[$id]=true;$total+=$n;
    }
    foreach(prCommunityVisible($community,$now,array_keys($ids))??[] as $room){
        $id=$room['room_id'];$n=$room['players']??null;
        if(isset($ids[$id])||$room['fresh_until']<=$now||$room['state']==='unavailable'||!is_int($n)||$n<0||$n>128)continue;
        $ids[$id]=true;$total+=$n;
    }
    return $total;
}
