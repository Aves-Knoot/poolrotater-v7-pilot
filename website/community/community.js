/* Community surfaces share the owner presentation, but never its control binding. */
(function(){
  'use strict';
  function init(){
    const roots=Array.from(document.querySelectorAll('[data-community-rooms]'));if(!roots.length||!window.prLivePresentation)return;
    const ui=window.prLivePresentation;let data=null,busy=false,expiry,ownerIds=new Set();let offset=Number(roots[0].dataset.communityServerNow)*1000-Date.now();if(!Number.isFinite(offset))offset=0;
    const now=()=> (Date.now()+offset)/1000;
    function ownIds(){const ids=new Set(ownerIds);document.querySelectorAll('[data-home-room] .pr-home-join[href]').forEach(a=>{const m=/^osu:\/\/room\/(\d+)$/.exec(a.getAttribute('href'));if(m)ids.add(Number(m[1]));});return ids;}
    function element(tag,text,cls){const n=document.createElement(tag);if(text!==undefined)n.textContent=String(text);if(cls)n.className=cls;return n;}
    function validRoom(r){
      if(!r||typeof r!=='object'||!['playing','starting','idle','draining','unavailable'].includes(r.state)||!['standard-auto','standard-fixed','mania-4k','mania-7k','catch-fixed','taiko-fixed','taiko-auto'].includes(r.preset))return false;
      if(typeof r.title!=='string'||r.title.length>640||typeof r.operator!=='string'||r.operator.length>320||!Number.isFinite(r.fresh_until)||!Number.isFinite(r.hide_at))return false;
      for(const key of['players','playing_players','match_participants'])if(r[key]!=null&&(!Number.isInteger(r[key])||r[key]<0||r[key]>128))return false;
      const m=r.current_map;if(m!=null){if(typeof m!=='object'||!Number.isSafeInteger(m.beatmap_id)||m.beatmap_id<=0)return false;for(const key of['title','artist','version'])if(m[key]!=null&&(typeof m[key]!=='string'||m[key].length>1024))return false;}
      return true;
    }
    function usable(value,t){return value&&[1,2].includes(value.schema)&&value.available===true&&Array.isArray(value.rooms)&&value.rooms.length<=100&&Number.isFinite(value.generated_at)&&Number.isFinite(value.expires_at)&&value.generated_at<=t+30&&value.expires_at>value.generated_at&&value.expires_at-value.generated_at<=60&&t-value.generated_at<1800;}
    function render(value){
      const t=now(),incoming=usable(value,t);
      // A valid empty refresh is authoritative. A failed fetch is not a
      // withdrawal: keep the last accepted feed without extending its clocks.
      if(incoming)data=value;
      const retained=usable(data,t),valid=retained&&data.expires_at>t,delayed=!incoming||!valid||data.refresh_failed===true;
      clearTimeout(expiry);let next=valid?data.expires_at:t+30;const own=ownIds(),seen=new Set(),rooms=[];
      for(const r of retained?data.rooms:[]){if(!validRoom(r)||(r.visibility!==undefined&&r.visibility!=='public')||own.has(r.room_id)||!Number.isSafeInteger(r.room_id)||r.room_id<=0||!Number.isFinite(r.hide_at)||r.hide_at<=t||typeof r.key!=='string'||!/^[a-f0-9]{24}$/.test(r.key)||seen.has(r.key))continue;seen.add(r.key);rooms.push({...r,ownership:'community'});next=Math.min(next,r.hide_at);if(retained&&r.fresh_until>t)next=Math.min(next,r.fresh_until);}
      if(window.prLiveOccupancyUpdate)window.prLiveOccupancyUpdate('community',retained?data:null);
      roots.forEach(root=>{
        const list=root.querySelector('[data-community-list]'),compact=root.dataset.communitySurface==='sidebar';
        const openSettings=new Set(Array.from(list.querySelectorAll('article')).filter(a=>a.querySelector('details[open]')).map(a=>a.dataset.liveKey));
        const focused=document.activeElement&&list.contains(document.activeElement)?{key:document.activeElement.closest('article').dataset.liveKey,cls:document.activeElement.className}:null;
        const nodes=[];
        for(const room of rooms){const fresh=retained&&Number.isFinite(room.fresh_until)&&room.fresh_until>t&&room.state!=='unavailable';const article=element('article',undefined,'pr-community-room');article.setAttribute('data-community-card','');article.dataset.liveKey=room.key;article.dataset.preset=room.preset||'';article.dataset.state=fresh?room.state:'unavailable';article.dataset.roomType=room.preset==='catch-fixed'?'catch':String(room.preset||'').startsWith('taiko-')?'taiko':String(room.preset||'').startsWith('mania')?'mania':'standard';article.id=(compact?'sidebar-community-':'community-')+room.key;article.append(ui.card(room,fresh,compact,t));
          if(!compact){const body=element('div',undefined,'pr-room-body'),facts=element('div',undefined,'pr-community-facts');function fact(label,text){const n=element('p',undefined,'pr-community-fact');n.append(element('span',label),element('strong',text));facts.append(n);}if(fresh&&Number.isFinite(room.min_stars)&&Number.isFinite(room.max_stars))fact('Star range',room.min_stars.toFixed(2)+'–'+room.max_stars.toFixed(2)+'★');fact('Players in room',fresh&&Number.isInteger(room.players)&&room.players>=0&&room.players<=128?room.players+' players':'Unavailable');body.append(facts);const settings=ui.settings(room.settings,fresh);if(settings){settings.open=openSettings.has(room.key);body.append(settings);}article.append(body);}
          const filter=document.querySelector('[data-room-filter][aria-pressed="true"]');if(root.dataset.communitySurface==='dashboard'&&filter)article.hidden=filter.dataset.roomFilter!=='all'&&filter.dataset.roomFilter!==article.dataset.roomType;
          nodes.push(article);
        }
        list.replaceChildren(...nodes);if(focused){const row=nodes.find(a=>a.dataset.liveKey===focused.key);if(row){const target=Array.from(row.querySelectorAll('a,summary')).find(a=>a.className===focused.cls);if(target)target.focus({preventScroll:true});}}
        root.querySelector('[data-community-note]').textContent=rooms.length?(delayed?'Status refresh delayed; showing the last received lobby information.':'Updates every 30 seconds. Progress is estimated when match timing is available.')+(data.more_available?' Showing the first 100 public rooms; more rooms are available.':''):!valid?'Community status is temporarily unavailable.':'No other public rooms are currently listed.';
      });
      expiry=setTimeout(()=>render(data),Math.max(50,(next-t)*1000+20));
    }
    async function poll(){if(busy||document.hidden)return;busy=true;const abort=new AbortController(),timeout=setTimeout(()=>abort.abort(),15000);try{const r=await fetch('/community/rooms.json',{cache:'no-store',credentials:'omit',signal:abort.signal});if(!r.ok)throw Error('unavailable');const date=Date.parse(r.headers.get('Date'));if(Number.isFinite(date))offset=date-Date.now();render(await r.json());}catch(_){render(null);}finally{clearTimeout(timeout);busy=false;}}
    document.addEventListener('pr:lobby-status',event=>{const rooms=event.detail&&event.detail.rooms;ownerIds=new Set(Array.isArray(rooms)?rooms.map(r=>r.room_id).filter(Number.isSafeInteger):[]);if(data)render(data);});
    const embedded=document.querySelector('[data-community-initial]');if(embedded){try{render(JSON.parse(embedded.textContent));}catch(_){render(null);}}
    poll();setInterval(poll,30000);setInterval(()=>{if(!document.hidden)roots.forEach(root=>root.querySelectorAll('[data-live-card]').forEach(card=>ui.tick(card,now())));},1000);
    document.addEventListener('visibilitychange',()=>{if(!document.hidden){render(data);poll();}});
  }
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();
}());
