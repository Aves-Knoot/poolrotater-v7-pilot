/* Shared owner/community presentation. Public display only; no control commands. */
(function(global){
  'use strict';
  const states={PLAYING:'Playing',STARTING:'Starting',SCORING:'Between maps',IDLE:'Waiting',OFFLINE:'Offline',STALE:'Status unavailable',playing:'Playing',starting:'Starting',idle:'Waiting',draining:'Finishing up',unavailable:'Status unavailable'};
  const validId=n=>Number.isSafeInteger(n)&&n>0;
  const clock=n=>Math.floor(Math.max(0,n)/60)+':'+String(Math.floor(Math.max(0,n)%60)).padStart(2,'0');
  const node=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=String(text);if(cls)n.className=cls;return n;};
  function timing(room,fresh,now){
    const p=room.playback;if(!fresh||!['PLAYING','playing'].includes(room.state)||!p||!Number.isFinite(p.started_at)||!Number.isFinite(p.duration_seconds)||p.started_at<=0||p.started_at>now+5||p.duration_seconds<=0||p.duration_seconds>86400)return null;
    if(room.ownership==='community'&&(!validId(p.match_id)||!validId(p.beatmap_id)||p.beatmap_id!==(room.current_map||{}).beatmap_id))return null;
    if((room.settings||{}).ruleset_id===1&&p.clock_rate!=null&&p.clock_rate!==1)return null;
    return p;
  }
  function put(card,selector,text){const n=card.querySelector(selector);if(n)n.textContent=String(text);}
  function tick(card,now){
    const start=Number(card.dataset.start),duration=Number(card.dataset.duration),running=['PLAYING','playing'].includes(card.dataset.state)&&start>0&&duration>0&&start<=now+5;
    const elapsed=running?Math.max(0,Math.min(duration,now-start)):0,ratio=running?elapsed/duration:0;
    const bar=card.querySelector('.pr-np-progress');if(!bar)return;
    bar.hidden=!running;bar.firstElementChild.style.transform='scaleX('+ratio+')';bar.setAttribute('aria-valuenow',String(Math.round(ratio*100)));bar.setAttribute('aria-valuetext',running?'Estimated '+clock(elapsed)+' of '+clock(duration):'Timing unavailable');
    card.dataset.timed=String(running);card.dataset.finished=String(ratio>=1);put(card,'.pr-np-clock',running?'Estimated '+clock(elapsed)+' / '+clock(duration):'');
  }
  function update(card,room,fresh,now){
    room=room||{};const state=fresh&&states[room.state]?room.state:'STALE',map=fresh?(room.current_map||{}):{},p=timing(room,fresh,now),community=room.ownership==='community';
    card.dataset.state=state;card.dataset.nominalSpeed=String(Boolean(p&&p.nominal_speed));card.dataset.start=p?p.started_at:0;card.dataset.duration=p?p.duration_seconds:0;card.dataset.map=String(Boolean(map.title||map.label));
    put(card,'.pr-np-people',fresh&&Number.isInteger(room.players)&&room.players>=0&&room.players<=128?room.players+' in room':'—');
    const title=String(map.title||map.label||(state==='OFFLINE'?'Room is offline':state==='STALE'?'Status temporarily unavailable':'Waiting for the next map')).slice(0,160),link=card.querySelector('.pr-np-title');
    link.textContent=title;link.title=title;if(validId(map.beatmap_id))link.href='https://osu.ppy.sh/beatmaps/'+map.beatmap_id;else link.removeAttribute('href');
    put(card,'.pr-np-artist',[map.artist,map.version].filter(v=>typeof v==='string').join(' · ').slice(0,240)||'Automatic multiplayer');
    put(card,'[data-np-stars]',Number.isFinite(map.stars)?map.stars.toFixed(2)+'★':'Difficulty unknown');put(card,'[data-np-length]',Number.isFinite(map.total_length)?clock(map.total_length):'Length unknown');
    const img=card.querySelector('.pr-np-cover');if(img){if(!img.dataset.watched){img.dataset.watched='1';img.addEventListener('error',()=>{img.hidden=true;});img.addEventListener('load',()=>{img.hidden=false;});}const src=validId(map.beatmapset_id)?'https://assets.ppy.sh/beatmaps/'+map.beatmapset_id+'/covers/cover.jpg':null;if(src&&img.getAttribute('src')!==src){img.hidden=false;img.src=src;}else if(!src){img.hidden=true;img.removeAttribute('src');}}
    const phase=card.querySelector('.pr-np-phase');if(phase){phase.dataset.phase=state;phase.lastElementChild.textContent=states[state];}
    const join=card.querySelector('.pr-np-join');if(fresh&&validId(room.room_id)&&!['OFFLINE','STALE','unavailable'].includes(state)){join.href='osu://room/'+room.room_id;join.removeAttribute('aria-disabled');}else{join.removeAttribute('href');join.setAttribute('aria-disabled','true');}
    if(community)put(card,'.pr-live-host','Hosted by '+String(room.operator||'Community host').slice(0,80));tick(card,now);
  }
  function card(room,fresh,compact,now){
    const el=node('div',undefined,'pr-now-playing '+(compact?'pr-np-compact':'pr-np-full'));
    el.setAttribute('data-live-card','');el.setAttribute('data-ownership',room.ownership||'community');el.setAttribute('data-live-key',room.key||'');
    const image=node('img',undefined,'pr-np-cover');image.alt='';image.setAttribute('aria-hidden','true');image.loading='lazy';image.decoding='async';image.referrerPolicy='no-referrer';
    const content=node('div',undefined,'pr-np-content'),top=node('div',undefined,'pr-np-top'),label=node('a',String(room.title||'Community lobby').slice(0,160),'pr-np-room');label.href='/doku.php?id=status:start#community-'+room.key;top.append(label,node('span','','pr-np-people'));
    const main=node('div',undefined,compact?'pr-np-main':'pr-np-map'),copy=node('div',undefined,'pr-np-copy'),title=node('a','','pr-np-title'),phase=node('span',undefined,'pr-np-phase');phase.append(node('i'),node('span',''));
    const status=node('div',undefined,'pr-np-status');status.append(phase,node('span','','pr-np-clock'));copy.append(title,node('p','','pr-np-artist'),status);main.append(copy);
    const join=node('a',compact?'Join ↗':'Join lobby ↗','pr-np-join');join.setAttribute('aria-label','Join '+String(room.title||'community lobby').slice(0,160)+' in osu!');if(compact)main.append(join);
    const bar=node('div',undefined,'pr-np-progress');bar.setAttribute('role','progressbar');bar.setAttribute('aria-label','Estimated song progress');bar.setAttribute('aria-valuemin','0');bar.setAttribute('aria-valuemax','100');bar.append(node('span'));
    content.append(top,node('p','','pr-live-host'),main);
    if(!compact){const facts=node('div',undefined,'pr-map-facts'),stars=node('span',''),length=node('span','');stars.setAttribute('data-np-stars','');length.setAttribute('data-np-length','');facts.append(stars,length);content.append(facts);if(fresh&&Number.isInteger(room.playing_players)&&room.playing_players>=0&&room.playing_players<=128)content.append(node('p',room.playing_players+' playing now','pr-live-participants'));if(fresh&&Number.isInteger(room.match_participants)&&room.match_participants>=0&&room.match_participants<=128)content.append(node('p',room.match_participants+' players at match start','pr-live-participants'));}
    content.append(bar);if(!compact){const actions=node('div',undefined,'pr-np-bottom');actions.append(join);if(fresh&&validId(room.room_id)){const view=node('a','View room ↗','pr-community-view');view.href='https://osu.ppy.sh/multiplayer/rooms/'+room.room_id;view.target='_blank';view.rel='noopener noreferrer';actions.append(view);}content.append(actions);}
    el.append(image,node('div',undefined,'pr-np-shade'),content);update(el,room,fresh,now);return el;
  }
  const labels={ruleset_id:'Ruleset',keys:'Keys',auto_difficulty:'Automatic difficulty',selection_style:'Selection style',freestyle:'Freemod',countdown_seconds:'Countdown · seconds',required_mods:'Required mods',allowed_mods:'Allowed mods'};
  function settings(value,fresh){if(!fresh||!value||typeof value!=='object')return null;const details=node('details',undefined,'pr-room-settings'),summary=node('summary','Room settings ⌄'),dl=node('dl');for(const [key,label]of Object.entries(labels)){let v=value[key];if(v==null)continue;if(key==='ruleset_id')v=({0:'osu!standard',1:'osu!taiko',2:'osu!catch',3:'osu!mania'})[v]||'Unknown';else if(typeof v==='boolean')v=v?'Enabled':'Disabled';else if(Array.isArray(v))v=v.filter(n=>typeof n==='string'&&n.length<=16).slice(0,32).join(', ')||'None';else if(!['string','number'].includes(typeof v))continue;dl.append(node('dt',label),node('dd',String(v).slice(0,160)));}if(!dl.children.length)return null;details.append(summary,dl,node('p','Public settings reported by this host. Other controls remain local to the host.','pr-settings-note'));return details;}
  global.prLivePresentation={card,update,tick,timing,settings,clock};
}(typeof window!=='undefined'?window:globalThis));
