
(function(global){
  const sources={owner:null,community:null};
  global.prLiveOccupancyUpdate=function(kind,value){
    sources[kind]=value;const now=Date.now()/1000,o=sources.owner,c=sources.community;
    let total=null;
    if(o&&c&&o.available===true&&c.available===true&&Array.isArray(o.rooms)&&o.rooms.length===4&&Array.isArray(c.rooms)&&c.rooms.length<=100&&!c.more_available&&o.generated_at<=now+30&&o.expires_at>now&&c.generated_at<=now+30&&c.expires_at>now){
      let n=0,known=true;const seen=new Set(),aliases=new Set();
      for(const r of o.rooms){if(!/^room[1-4]$/.test(r.alias)||aliases.has(r.alias)){known=false;break;}aliases.add(r.alias);if(r.state==='OFFLINE')continue;if(!Number.isSafeInteger(r.room_id)||r.room_id<=0||!Number.isInteger(r.players)||r.players<0||r.players>128){known=false;break;}if(!seen.has(r.room_id)){seen.add(r.room_id);n+=r.players;}}
      for(const r of c.rooms){if(r.visibility!==undefined&&r.visibility!=='public'||seen.has(r.room_id))continue;if(!Number.isSafeInteger(r.room_id)||r.room_id<=0||!Number.isInteger(r.players)||r.players<0||r.players>128||!['playing','starting','idle','draining'].includes(r.state)||!(r.fresh_until>now)||!(r.hide_at>now)){known=false;break;}seen.add(r.room_id);n+=r.players;}
      if(known)total=n;
    }
    if(typeof document!=='undefined')for(const el of document.querySelectorAll('[data-live-metric="players"],[data-home-people],[data-chart-current="total"]'))el.textContent=total===null?'—':new Intl.NumberFormat('en-US').format(total)+(el.hasAttribute('data-chart-current')?' here now':'');
    return total;
  };
  if(typeof setInterval==='function')setInterval(function(){global.prLiveOccupancyUpdate('owner',sources.owner);},1000);
})(typeof window!=='undefined'?window:globalThis);
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
    card.dataset.state=state;card.dataset.start=p?p.started_at:0;card.dataset.duration=p?p.duration_seconds:0;card.dataset.map=String(Boolean(map.title||map.label));
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
  const labels={ruleset_id:'Ruleset',keys:'Keys',auto_difficulty:'Automatic difficulty',selection_style:'Selection style',freestyle:'Freestyle',countdown_seconds:'Countdown · seconds',required_mods:'Required mods',allowed_mods:'Allowed mods'};
  function settings(value,fresh){if(!fresh||!value||typeof value!=='object')return null;const details=node('details',undefined,'pr-room-settings'),summary=node('summary','Room settings ⌄'),dl=node('dl');for(const [key,label]of Object.entries(labels)){let v=value[key];if(v==null)continue;if(key==='ruleset_id')v=({0:'osu!standard',1:'osu!taiko',2:'osu!catch',3:'osu!mania'})[v]||'Unknown';else if(typeof v==='boolean')v=v?'Enabled':'Disabled';else if(Array.isArray(v))v=v.filter(n=>typeof n==='string'&&n.length<=16).slice(0,32).join(', ')||'None';else if(!['string','number'].includes(typeof v))continue;dl.append(node('dt',label),node('dd',String(v).slice(0,160)));}if(!dl.children.length)return null;details.append(summary,dl,node('p','Public settings reported by this host. Other controls remain local to the host.','pr-settings-note'));return details;}
  global.prLivePresentation={card,update,tick,timing,settings,clock};
}(typeof window!=='undefined'?window:globalThis));



/**
 *  We handle several device classes based on browser width.
 *
 *  - desktop:   > __tablet_width__ (as set in style.ini)
 *  - mobile:
 *    - tablet   <= __tablet_width__
 *    - phone    <= __phone_width__
 */
var device_class = ''; // not yet known
var device_classes = 'desktop mobile tablet phone';

function tpl_dokuwiki_mobile(){

    // the z-index in mobile.css is (mis-)used purely for detecting the screen mode here
    var screen_mode = jQuery('#screen__mode').css('z-index') + '';

    // determine our device pattern
    // TODO: consider moving into dokuwiki core
    switch (screen_mode) {
        case '1':
            if (device_class.match(/tablet/)) return;
            device_class = 'mobile tablet';
            break;
        case '2':
            if (device_class.match(/phone/)) return;
            device_class = 'mobile phone';
            break;
        default:
            if (device_class == 'desktop') return;
            device_class = 'desktop';
    }

    jQuery('html').removeClass(device_classes).addClass(device_class);

    // handle some layout changes based on change in device
    var $handle = jQuery('#dokuwiki__aside h3.toggle');
    var $toc = jQuery('#dw__toc h3');

    if (device_class == 'desktop') {
        // reset for desktop mode
        if($handle.length) {
            $handle[0].setState(1);
            $handle.hide();
        }
        if($toc.length) {
            $toc[0].setState(1);
        }
    }
    if (device_class.match(/mobile/)){
        // toc and sidebar hiding
        if($handle.length) {
            $handle.show();
            $handle[0].setState(-1);
        }
        if($toc.length) {
            $toc[0].setState(-1);
        }
    }
}

jQuery(function(){
    var resizeTimer;
    dw_page.makeToggle('#dokuwiki__aside h3.toggle','#dokuwiki__aside div.content');

    tpl_dokuwiki_mobile();
    jQuery(window).on('resize',
        function(){
            if (resizeTimer) clearTimeout(resizeTimer);
            resizeTimer = setTimeout(tpl_dokuwiki_mobile,200);
        }
    );

    // increase sidebar length to match content (desktop mode only)
    var sidebar_height = jQuery('.desktop #dokuwiki__aside').height();
    var pagetool_height = jQuery('.desktop #dokuwiki__pagetools ul:first').height();
    // pagetools div has no height; ul has a height
    var content_min = Math.max(sidebar_height || 0, pagetool_height || 0);

    var content_height = jQuery('#dokuwiki__content div.page').height();
    if(content_min && content_min > content_height) {
        var $content = jQuery('#dokuwiki__content div.page');
        $content.css('min-height', content_min);
    }

    // blur when clicked
    jQuery('#dokuwiki__pagetools div.tools>ul>li>a').on('click', function(){
        this.blur();
    });
});

/* PoolRotater reading tools: no API calls, storage, or additional libraries. */
(function () {
    'use strict';
    function node(tag, className, text) {
        var el = document.createElement(tag);
        if (className) el.className = className;
        if (text) el.textContent = text;
        return el;
    }
    function copyButton(command, status) {
        var button = node('button', 'pr-copy', 'Copy');
        button.type = 'button';
        button.setAttribute('aria-label', 'Copy ' + command);
        button.addEventListener('click', function () {
            function failed() {
                status.textContent = 'Clipboard unavailable. Select the command and copy it manually.';
            }
            if (!navigator.clipboard || !navigator.clipboard.writeText) { failed(); return; }
            navigator.clipboard.writeText(command).then(function () {
                button.textContent = 'Copied';
                status.textContent = 'Copied ' + command + '.';
                window.setTimeout(function () { button.textContent = 'Copy'; }, 1800);
            }, failed);
        });
        return button;
    }
    function field(labelText, control) {
        var label = node('label', 'pr-finder-field');
        label.appendChild(node('span', '', labelText));
        label.appendChild(control);
        return label;
    }
    function select(id, options) {
        var control = node('select');
        control.id = id;
        options.forEach(function (item) {
            var option = node('option', '', item[1]);
            option.value = item[0]; control.appendChild(option);
        });
        return control;
    }
    function finder(content) {
        // Keep release-owned and explicitly authored worker-command tables separate.
        var tables = Array.prototype.filter.call(content.querySelectorAll('table.inline'), function (candidate) {
            var headers = candidate.querySelectorAll('tr:first-child th');
            return headers.length === 3 && Array.prototype.map.call(headers, function (header) {
                return header.textContent.trim();
            }).join('|') === 'Command|Syntax and aliases|Availability';
        });
        var table = tables[0];
        if (!table) return;
        var groups = {
            help: 'help', discord: 'help', ai: 'help',
            queue: 'session', pool: 'selection', start_match: 'session', wait: 'session',
            skip: 'session', replay: 'session', harder: 'selection', easier: 'selection',
            good: 'feedback', bad: 'feedback', ok: 'feedback',
            pp: 'stats', stats: 'stats', lobby: 'stats', hidden: 'stats', show: 'stats',
            farm: 'selection', stream: 'selection', variety: 'selection', style: 'selection',
            map: 'selection', jack: 'selection', chordjack: 'selection', ln: 'selection', technical: 'selection'
        };
        var meanings = {
            help: 'See the short and expanded lobby help.', queue: 'See the current map and upcoming picks.',
            pool: 'Inspect the active map range and filters.', start_match: 'Request the normal countdown.',
            wait: 'Ask for ten more seconds before the map starts.', skip: 'Vote to skip the current map.',
            replay: 'Vote to replay the last completed map.', harder: 'Vote for harder future picks.',
            easier: 'Vote for easier future picks.', good: 'Rate the last completed map positively.',
            bad: 'Rate the last completed map negatively.', ok: 'Leave neutral map feedback.',
            pp: 'Estimate PP with supported mods and accuracies.', stats: 'View recorded AutoHost statistics.',
            lobby: 'View activity recorded for this lobby.', hidden: 'Hide your statistics from public lookups.',
            show: 'Restore public visibility of your statistics.', discord: 'Get the configured community invite.',
            ai: 'Ask the optional bot chat feature when enabled.', farm: 'Choose Farm for future selections.',
            stream: 'Choose Stream for future selections.', variety: 'Return to a broad selection or Standard mix.',
            style: 'Inspect or select a Mania style.', map: 'Show Mania map details and its link.',
            jack: 'Prefer repeated-note native Mania charts.', chordjack: 'Prefer repeated-chord native Mania charts.',
            ln: 'Prefer long-note native Mania charts.', technical: 'Prefer technical native Mania charts.'
        };
        var synonyms = {
            start_match: 'ready countdown play begin', wait: 'download downloading delay countdown',
            replay: 'again repeat redo', skip: 'skip next', harder: 'difficulty stars difficult',
            easier: 'difficulty stars easy', ln: 'long notes holds', technical: 'tech',
            hidden: 'privacy private hide', show: 'privacy public', pp: 'performance points accuracy mods',
            good: 'rating feedback like', bad: 'rating feedback dislike', ok: 'rating feedback neutral',
            style: 'mania pattern chart', farm: 'efficiency', discord: 'community support invite'
        };
        // Release-owned metadata replaces the historical fallback maps when verified.
        var catalogNode = document.getElementById('pr-command-catalog');
        var catalog = null;
        try { catalog = catalogNode ? JSON.parse(catalogNode.textContent) : null; } catch (ignore) {}
        if (Array.isArray(catalog) && catalog.length) {
            groups = {}; meanings = {}; synonyms = {};
            catalog.forEach(function (command) {
                groups[command.id] = command.purpose;
                meanings[command.id] = command.summary;
                synonyms[command.id] = (command.search_terms || []).join(' ');
            });
        }
        // Reviewed editorial supplements may precede the shared inventory's release.
        // Never replace verified metadata if a newer inventory already owns a command.
        [
            ['time', 'session', 'Check approximate time left in the running map.', 'remaining timing wait'],
            ['jump', 'selection', 'Prefer circle-heavy Standard spatial jumps.', 'circles sliders distance'],
            ['rice', 'selection', 'Prefer native Mania taps with few holds and streams.', 'tap composition chords'],
            ['noodle', 'selection', 'Prefer hold-dominant native Mania; Stream is the exhaustion fallback.', 'holds composition long notes'],
            ['lang', 'help', 'Choose personal English-first bilingual guidance.', 'language mandarin chinese korean spanish russian english'],
            ['dt', 'selection', 'Allow verified Standard DT mixed with NoMod for the shared room.', 'double time speed mods'],
            ['nm', 'selection', 'Disable future mixed DT safely without interrupting the running match.', 'nomod no mod disable double time']
        ].forEach(function (item) {
            if (!groups[item[0]]) {
                groups[item[0]] = item[1]; meanings[item[0]] = item[2]; synonyms[item[0]] = item[3];
            }
        });
        var entries = [];
        var seen = {}, invalid = false;
        tables.forEach(function (sourceTable) {
        var parsed = 0;
        Array.prototype.forEach.call(sourceTable.querySelectorAll('tr'), function (row) {
            var cells = row.querySelectorAll('td');
            if (cells.length !== 3) return;
            var link = cells[0].querySelector('a[data-wiki-id]');
            if (!link) return;
            var key = link.getAttribute('data-wiki-id').replace(/^commands:/, '');
            if (!groups[key]) return;
            parsed++;
            if (seen[key]) return;
            seen[key] = true;
            var availability = cells[2].textContent.trim();
            entries.push({
                key: key, link: link, syntax: cells[1].textContent.trim(), availability: availability,
                mode: /^Mania/.test(availability) ? 'mania' : /^Standard/.test(availability) && !/Mania/.test(availability) ? 'standard' : 'both',
                category: groups[key],
                search: (row.textContent + ' ' + meanings[key] + ' ' + (synonyms[key] || '')).toLowerCase()
            });
        });
        if (parsed !== sourceTable.querySelectorAll('tr').length - 1) invalid = true;
        });
        // Keep the original reference if its schema is changed by a maintainer.
        if (!entries.length || invalid) return;
        var section = node('section', 'pr-command-finder');
        section.setAttribute('aria-label', 'Find a player command');
        var controls = node('div', 'pr-finder-controls');
        var query = node('input'); query.type = 'search'; query.id = 'pr-command-search';
        query.placeholder = 'Try .s, long notes, or privacy';
        var mode = select('pr-command-mode', [['all', 'All lobbies'], ['standard', 'Standard'], ['mania', 'Mania 4K / 7K']]);
        var category = select('pr-command-purpose', [['all', 'All purposes'], ['session', 'Session controls'], ['selection', 'Map selection'], ['feedback', 'Map feedback'], ['stats', 'PP and statistics'], ['help', 'Help and community']]);
        controls.appendChild(field('Search commands and aliases', query));
        controls.appendChild(field('Lobby mode', mode));
        controls.appendChild(field('Purpose', category));
        var reset = node('button', 'pr-reset', 'Reset filters'); reset.type = 'button';
        controls.appendChild(reset); section.appendChild(controls);
        var count = node('p', 'pr-finder-count'); count.setAttribute('role', 'status'); section.appendChild(count);
        var grid = node('div', 'pr-command-grid'); section.appendChild(grid);
        var empty = node('p', 'pr-finder-empty', 'No commands match. Try a shorter term or reset the filters.');
        empty.hidden = true; section.appendChild(empty);
        var copyStatus = node('p', 'pr-copy-status'); copyStatus.setAttribute('role', 'status'); section.appendChild(copyStatus);
        entries.forEach(function (entry) {
            var card = node('article', 'pr-command-card');
            var heading = node('h2'); heading.appendChild(entry.link.cloneNode(true)); card.appendChild(heading);
            card.appendChild(node('p', 'pr-command-meaning', meanings[entry.key]));
            card.appendChild(node('p', 'pr-command-availability', entry.availability));
            var command = entry.syntax.split(' / ')[0].trim();
            var action = node('div', 'pr-command-action');
            action.appendChild(node('code', '', command)); action.appendChild(copyButton(command, copyStatus));
            card.appendChild(action);
            if (entry.syntax !== command) card.appendChild(node('p', 'pr-command-aliases', 'Examples and aliases: ' + entry.syntax));
            entry.card = card; grid.appendChild(card);
        });
        function filter() {
            var term = query.value.toLowerCase().trim();
            var tokens = term.split(/\s+/).filter(Boolean);
            var exact = /^\.[a-z][a-z0-9]*$/.test(term) && entries.some(function (e) {
                return (e.syntax.match(/\.[a-z][a-z0-9]*\b/g) || []).indexOf(term) !== -1;
            });
            var visible = 0;
            entries.forEach(function (entry) {
                var matched = (mode.value === 'all' || entry.mode === 'both' || entry.mode === mode.value)
                    && (category.value === 'all' || entry.category === category.value)
                    && (exact ? (entry.syntax.match(/\.[a-z][a-z0-9]*\b/g) || []).indexOf(term) !== -1
                        : tokens.every(function (token) { return entry.search.indexOf(token) !== -1; }));
                entry.card.hidden = !matched; if (matched) visible++;
            });
            count.textContent = visible + ' of ' + entries.length + ' commands shown';
            empty.hidden = visible !== 0;
        }
        query.addEventListener('input', filter); mode.addEventListener('change', filter);
        category.addEventListener('change', filter);
        reset.addEventListener('click', function () { query.value = ''; mode.value = 'all'; category.value = 'all'; filter(); query.focus(); });
        // The authored table remains the readable, printable, no-JavaScript reference.
        table.parentNode.insertBefore(section, table);
        tables.forEach(function (sourceTable) { sourceTable.classList.add('pr-command-source'); });
        filter();
    }
    function examples(content) {
        var code = content.querySelector('pre.code');
        if (!code) return;
        var lines = code.textContent.split('\n').map(function (line) { return line.trim(); }).filter(function (line) { return /^\.[a-z]/i.test(line); });
        if (!lines.length) return;
        var panel = node('div', 'pr-copy-examples');
        panel.appendChild(node('p', '', 'Copy an example, then paste it into lobby chat.'));
        var status = node('p', 'pr-copy-status'); status.setAttribute('role', 'status');
        lines.forEach(function (line) {
            var row = node('div', 'pr-command-action'); row.appendChild(node('code', '', line));
            row.appendChild(copyButton(line, status)); panel.appendChild(row);
        });
        panel.appendChild(status); code.parentNode.insertBefore(panel, code.nextSibling);
    }
    function init() {
        var info = window.JSINFO || {};
        if (info.ACT !== 'show') return;
        var content = document.querySelector('#dokuwiki__content .page');
        if (!content) return;
        if (info.id === 'commands:start') finder(content);
        else if (/^commands:/.test(info.id || '')) examples(content);
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
}());

/* Count website Join clicks without delaying navigation or identifying visitors. */
(function () {
    'use strict';
    document.addEventListener('click', function (event) {
        if (event.defaultPrevented || event.button !== 0) return;
        var link = event.target.closest('a.pr-room-join, a.pr-room-browser, a.pr-home-join, a.pr-home-browser, a.pr-np-join, a.pr-browser-room');
        if (!link || !link.hasAttribute('href') || link.getAttribute('aria-disabled') === 'true') return;
        var scope = link.closest('[data-room], [data-home-room], [data-np-room], .pr-dash-room');
        if (!scope) return;
        var room = scope.dataset.room || scope.dataset.homeRoom || scope.dataset.npRoom || scope.id;
        if (!/^room[1-4]$/.test(room || '')) return;
        var href = link.getAttribute('href');
        var kind = /^osu:\/\/room\/[1-9][0-9]*$/.test(href) ? 'osu' : null;
        if (!kind) {
            try {
                var url = new URL(href, window.location.href);
                if (url.protocol === 'https:' && url.hostname === 'poolrotator-91-107-237-182.nip.io' && /^\/room\/[1-9][0-9]*$/.test(url.pathname)) kind = 'browser';
            } catch (_) { return; }
        }
        if (!kind) return;
        var data = new URLSearchParams({ room: room, kind: kind });
        var endpoint = '/join-click.php';
        if (navigator.sendBeacon && navigator.sendBeacon(endpoint, data)) return;
        if (window.fetch) fetch(endpoint, { method: 'POST', body: data, credentials: 'omit', keepalive: true }).catch(function () {});
    });
}());

/* Refresh public room IDs when opening the menu; no osu! API or analytics. */
(function () {
    'use strict';
    function init() {
        var bar = document.querySelector('.pr-joinbar');
        if (!bar) return;
        var menu = bar.querySelector('details');
        var note = bar.querySelector('.pr-lobby-note');
        if (!menu || !note) return;
        var fallback = 'https://poolrotator-91-107-237-182.nip.io/room/';
        var controller;
        var expiryTimer;
        function unavailable() {
            bar.querySelectorAll('[data-room]').forEach(function (row) {
                var join = row.querySelector('.pr-room-join');
                join.removeAttribute('href');
                join.setAttribute('aria-disabled', 'true');
                join.textContent = 'Link unavailable';
                row.querySelector('.pr-room-browser').hidden = true;
                row.querySelector('.pr-room-browser').removeAttribute('href');
            });
            note.textContent = 'Room links are unavailable right now. Check Discord or the lobby guides.';
        }
        function armExpiry() {
            clearTimeout(expiryTimer);
            var remaining = Number(bar.dataset.expires) * 1000 - Date.now();
            if (remaining <= 0) unavailable();
            else expiryTimer = setTimeout(unavailable, remaining);
        }
        function render(data) {
            var now = Date.now() / 1000;
            if (!data || data.schema !== 1 || !Array.isArray(data.rooms) ||
                !Number.isFinite(data.expires_at) || !Number.isFinite(data.generated_at ?? data.observed_at) ||
                data.expires_at < now || (data.generated_at ?? data.observed_at) > now + 30 ||
                data.expires_at - (data.generated_at ?? data.observed_at) > 600) {
                unavailable();
                return;
            }
            bar.dataset.expires = String(data.expires_at);
            var count = 0;
            bar.querySelectorAll('[data-room]').forEach(function (row) {
                var room = data.rooms.find(function (r) { return r && r.alias === row.dataset.room; });
                var id = room && room.room_id;
                var valid = Number.isSafeInteger(id) && id > 0;
                var join = row.querySelector('.pr-room-join');
                var browser = row.querySelector('.pr-room-browser');
                if (valid) {
                    join.href = 'osu://room/' + id;
                    join.removeAttribute('aria-disabled');
                    join.textContent = 'Join in osu!';
                    browser.href = fallback + id;
                    count++;
                } else {
                    join.removeAttribute('href');
                    join.setAttribute('aria-disabled', 'true');
                    join.textContent = 'Link unavailable';
                    browser.removeAttribute('href');
                }
                browser.hidden = !valid;
            });
            if (typeof data.discord_url === 'string' && /^https:\/\/(?:discord\.gg\/|discord\.com\/invite\/)[A-Za-z0-9_-]{2,100}$/.test(data.discord_url)) {
                bar.querySelector('.pr-discord').href = data.discord_url;
            }
            note.textContent = count ? 'Join opens osu!lazer. If it does not open, try the browser link. Lobby status refreshes every 10 seconds.' : 'Room links are unavailable right now. Check Discord or the lobby guides.';
            armExpiry();
        }
        document.addEventListener('pr:lobby-status', function (e) { render(e.detail); });
        menu.addEventListener('toggle', async function () {
            if (!menu.open) return;
            armExpiry();
            if (controller) controller.abort();
            controller = new AbortController();
            var request = controller;
            var timeout = setTimeout(function () { request.abort(); }, 4000);
            try {
                var response = await fetch('/status.json', { cache: 'no-store', credentials: 'omit', signal: request.signal });
                if (!response.ok) throw new Error('Unavailable');
                render(await response.json());
            } catch (_) {
                // Keep the server-rendered links only while their snapshot is fresh.
                armExpiry();
            } finally { clearTimeout(timeout); }
        });
        document.addEventListener('click', function (event) {
            if (menu.open && !menu.contains(event.target)) menu.open = false;
        });
        document.addEventListener('keydown', function (event) {
            if (event.key === 'Escape' && menu.open) {
                menu.open = false;
                menu.querySelector('summary').focus();
            }
        });
        armExpiry();
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
}());

/* Language links work without JavaScript; enhance dismissal only. */
(function () {
    'use strict';
    function init() {
        var menu = document.querySelector('.pr-language-menu');
        if (!menu) return;
        document.addEventListener('click', function (event) {
            if (menu.open && !menu.contains(event.target)) menu.open = false;
        });
        document.addEventListener('keydown', function (event) {
            if (event.key === 'Escape' && menu.open) {
                menu.open = false;
                menu.querySelector('summary').focus();
            }
        });
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
}());

/* Five-minute read-only views; native HTML is the no-JavaScript fallback. */
(function () {
    'use strict';
    function init() {
        var labels = ['Standard automatic', 'Standard fixed', 'Mania 7K', 'Mania 4K'];
        var phases = { IDLE: 'Waiting', STARTING: 'Starting', PLAYING: 'Playing', SCORING: 'Between maps', OFFLINE: 'Offline', STALE: 'Status unavailable' };
        var sidebar = document.querySelector('[data-status-sidebar]');
        var stats = document.querySelector('[data-statistics]');
        var live = document.querySelector('[data-status-page]');
        var activity = null;
        var lastRefresh = 0;
        var statsRequest = null;
        var statsSerial = 0;
        var timers = new Map();
        var sidebarDisclosure = document.querySelector('.pr-live-sidebar');
        if (sidebarDisclosure && window.matchMedia('(max-width: 800px)').matches) sidebarDisclosure.open = false;
        function element(tag, text, cls) {
            var e = document.createElement(tag);
            if (text !== undefined) e.textContent = String(text);
            if (cls) e.className = cls;
            return e;
        }
        function finite(n) { return typeof n === 'number' && Number.isFinite(n); }
        function fresh(data) {
            var now = Date.now() / 1000;
            return data && data.schema === 1 && finite(data.generated_at) && finite(data.expires_at) &&
                data.generated_at <= now + 30 && data.expires_at >= now && data.expires_at - data.generated_at <= 600;
        }
        function timeText(timestamp, full) {
            return new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', timeZoneName: full ? 'short' : undefined }).format(new Date(timestamp * 1000));
        }
        function updated(root, stamp) {
            var e = root.querySelector('.pr-updated');
            if (e && finite(stamp)) { e.dataset.updated = String(stamp); e.textContent = 'Updated ' + timeText(stamp, true) + ' · refreshes every ' + (root.matches('[data-status-page]') ? '10s' : '5 min'); }
        }
        document.querySelectorAll('[data-updated]').forEach(function (e) { updated(e.parentElement, Number(e.dataset.updated)); });
        function expire(root, callback) {
            if (!root) return;
            clearTimeout(timers.get(root));
            var delay = Number(root.dataset.expires) * 1000 - Date.now();
            if (!(delay > 0)) callback();
            else timers.set(root, setTimeout(callback, Math.min(delay, 610000)));
        }
        async function get(url, signal) {
            var result = await fetch(url, { cache: 'no-store', credentials: 'omit', signal: signal });
            if (!result.ok) throw new Error('Unavailable');
            return result;
        }
        function expiredStats() {
            if (stats) stats.querySelector('.pr-stats-body').replaceChildren(element('p', 'Statistics are temporarily unavailable. Please try again shortly.', 'pr-unavailable'));
        }
        var statsForm = stats && stats.querySelector('form');
        function statsURL() {
            var url = new URL('/doku.php', window.location.origin);
            url.search = new URLSearchParams(new FormData(statsForm)).toString();
            return url;
        }
        async function refreshStats() {
            if (!stats) return;
            expire(stats, expiredStats);
            var period = statsForm.elements.pr_period.value;
            var timeOption = statsForm.elements.pr_sort.querySelector('[value=seconds]');
            timeOption.disabled = period !== 'all';
            if (period !== 'all' && statsForm.elements.pr_sort.value === 'seconds') statsForm.elements.pr_sort.value = 'pp';
            if (statsRequest) statsRequest.abort();
            statsRequest = new AbortController();
            var request = statsRequest; var serial = ++statsSerial;
            var timeout = setTimeout(function () { request.abort(); }, 15000);
            stats.setAttribute('aria-busy', 'true');
            try {
                // Reuse the server renderer: it escapes player names and gives
                // identical filters/details with or without JavaScript.
                var response = await get(statsURL(), request.signal);
                var doc = new DOMParser().parseFromString(await response.text(), 'text/html');
                var incoming = doc.querySelector('[data-statistics]');
                if (!incoming || serial !== statsSerial) return;
                var open = Array.from(stats.querySelectorAll('.pr-player-detail[open]')).map(function (d) { return d.dataset.playerId; });
                var body = incoming.querySelector('.pr-stats-body');
                if (!body) throw new Error('Unavailable');
                stats.querySelector('.pr-stats-body').replaceChildren.apply(stats.querySelector('.pr-stats-body'), Array.from(body.childNodes));
                open.forEach(function (id) {
                    var d = Array.from(stats.querySelectorAll('.pr-player-detail')).find(function (e) { return e.dataset.playerId === id; });
                    if (d) d.open = true;
                });
                stats.dataset.expires = incoming.dataset.expires;
                var stamp = Number(incoming.querySelector('[data-updated]').dataset.updated); updated(stats, stamp);
                history.replaceState(null, '', statsURL());
                expire(stats, expiredStats);
            } catch (_) { expire(stats, expiredStats); }
            finally { clearTimeout(timeout); if (serial === statsSerial) stats.removeAttribute('aria-busy'); }
        }
        if (statsForm) {
            statsForm.addEventListener('submit', function (e) { e.preventDefault(); refreshStats(); });
            statsForm.addEventListener('change', refreshStats);
            expire(stats, expiredStats);
        }
        var chartSelect = live && live.querySelector('[name=pr_chart_window]');
        function svgNode(tag, attrs, text) {
            var n = document.createElementNS('http://www.w3.org/2000/svg', tag);
            Object.keys(attrs || {}).forEach(function (key) { n.setAttribute(key, String(attrs[key])); });
            if (text !== undefined) n.textContent = String(text);
            return n;
        }
        function activityTotal(row, states) {
            var total = 0, roomStates = states.get(row[0]) || [];
            for (var i = 1; i <= 4; i++) {
                if (Number.isInteger(row[i]) && row[i] >= 0) total += row[i];
                else if (row[i] === null && roomStates[i] === 0) continue;
                else return null;
            }
            return total;
        }
        function graphs() {
            if (!live || !activity || !Array.isArray(activity.samples)) return;
            var seconds = Number(chartSelect.value), end = Date.now()/1000, start = end-seconds;
            var samples = activity.samples.filter(function(r){return Array.isArray(r)&&r.length===6&&finite(r[0])&&r[0]>=start&&r[0]<=end;});
            var states = new Map((activity.room_states||[]).filter(function(r){return Array.isArray(r)&&r.length===5;}).map(function(r){return [r[0],r];}));
            var series = labels.map(function(label,i){return {label:label,index:i,key:i,total:false};});
            series.unshift({label:'All public rooms',index:0,key:'total',total:true});
            series.forEach(function(item){
                var label=item.label,i=item.index,total=item.total;
                var rows=total?(activity.network_totals||[]).filter(function(r){return Array.isArray(r)&&r.length===2&&finite(r[0])&&r[0]>=start&&r[0]<=end;}).map(function(r){return [r[0],r[1],null,null,null,null];}):samples;
                var root=live.querySelector('[data-chart="'+item.key+'"]');if(!root)return;
                var known=rows.filter(function(r){return Number.isInteger(r[i+1])&&r[i+1]>=0;});
                var offline=total?[]:rows.filter(function(r){return !Number.isInteger(r[i+1])&&(states.get(r[0])||[])[i+1]===0;});
                var unavailable=rows.length-known.length-offline.length;
                if(!known.length){root.replaceChildren(element('div',total?'No complete network readings in this window. '+unavailable+' unavailable.':'No player-count readings in this window. '+offline.length+' offline, '+unavailable+' unavailable.','pr-chart-empty'));return;}
                var peak=Math.max.apply(null,known.map(function(r){return r[i+1];})),scale=Math.max(1,peak),average=known.reduce(function(sum,r){return sum+r[i+1];},0)/known.length;
                var svg=svgNode('svg',{'class':'pr-activity-chart',viewBox:'0 0 680 205',role:'img','aria-label':label+': player count over time'});
                svg.append(svgNode('title',{},label+' player count'),svgNode('desc',{},'Five-minute observations. Peak '+peak+' players. '+(total?'All reporting public room counts combined, including spectators. Confirmed offline rooms contribute zero; unknown room readings and gaps over 15 minutes break the total.':'Red marks indicate confirmed offline rooms; other gaps indicate unavailable readings.')));
                [35,100,165].forEach(function(y){svg.append(svgNode('path',{'class':'pr-chart-grid',d:'M40 '+y+'H640'}));});
                svg.append(svgNode('text',{x:12,y:39},scale),svgNode('text',{x:20,y:169},'0'));
                var segments=[],segment=[],last=0;
                rows.forEach(function(r){
                    if(!Number.isInteger(r[i+1])||r[i+1]<0||last&&r[0]-last>900){if(segment.length)segments.push(segment);segment=[];last=0;}
                    if(!Number.isInteger(r[i+1])||r[i+1]<0)return;
                    segment.push([(40+600*(r[0]-start)/seconds).toFixed(2),(165-130*r[i+1]/scale).toFixed(2)]);last=r[0];
                });
                if(segment.length)segments.push(segment);
                segments.forEach(function(points){var path='';points.forEach(function(point){path+=path?' H '+point[0]+' V '+point[1]:'M '+point[0]+' '+point[1];});svg.append(svgNode('path',{'class':'pr-chart-area',d:path+' L '+points[points.length-1][0]+' 165 L '+points[0][0]+' 165 Z'}),svgNode('path',{'class':'pr-chart-line',d:path}));});
                if(total)segments.filter(function(points){return points.length===1;}).forEach(function(points){var point=svgNode('circle',{'class':'pr-chart-point',cx:points[0][0],cy:points[0][1],r:3});point.append(svgNode('title',{},'Single complete four-room reading'));svg.append(point);});
                offline.forEach(function(r){var dot=svgNode('circle',{'class':'pr-chart-offline',cx:(40+600*(r[0]-start)/seconds).toFixed(2),cy:165,r:4});dot.append(svgNode('title',{},'Room offline'));svg.append(dot);});
                svg.append(svgNode('text',{x:40,y:194},timeText(start,false)),svgNode('text',{x:640,y:194,'text-anchor':'end'},timeText(end,false)));
                var summary=element('div',undefined,'pr-chart-summary');
                [['Peak',peak],['Average',average.toFixed(1)]].forEach(function(row){var s=element('span');s.append(document.createTextNode(row[0]+' '),element('b',row[1]));summary.append(s);});summary.append(element('span',known.length+(total?' complete readings':' readings')));if(!total)summary.append(element('span',offline.length+' offline'));summary.append(element('span',unavailable+' unavailable'));root.replaceChildren(svg,summary);
            });
        }
        function historyTable() {
            if(!live||!activity||!Array.isArray(activity.samples))return;
            var samples=activity.samples.filter(function(r){return Array.isArray(r)&&r.length===6&&finite(r[0]);});
            var states=new Map((activity.room_states||[]).filter(function(r){return Array.isArray(r)&&r.length===5;}).map(function(r){return [r[0],r];}));
            var body=live.querySelector('.pr-chart-data tbody'), rows=[];
            samples.slice(-24).reverse().forEach(function(r){var tr=element('tr');tr.append(element('td',new Intl.DateTimeFormat('en-GB',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit',timeZone:'UTC'}).format(new Date(r[0]*1000))));r.slice(1,5).forEach(function(n,i){tr.append(element('td',Number.isInteger(n)?n:(states.get(r[0])||[])[i+1]===0?'Offline':'—'));});tr.append(element('td',new Map(activity.network_totals||[]).get(r[0])??'—'));rows.push(tr);});
            if(body)body.replaceChildren.apply(body,rows);
            var stamp=live.querySelector('[data-history-updated]');if(stamp)stamp.textContent=samples.length?'Last observation '+timeText(samples[samples.length-1][0],true):'No observations yet';
        }
        if(chartSelect){
            chartSelect.addEventListener('change',graphs);
            live.querySelector('.pr-chart-form').addEventListener('submit',function(e){e.preventDefault();graphs();});
            try{var embedded=document.getElementById('pr-activity-data');if(embedded){activity=JSON.parse(embedded.textContent);graphs();}}catch(_){}
        }
        async function refreshLive() {
            if(!live)return;
            var controller=new AbortController(),timeout=setTimeout(function(){controller.abort();},10000);
            try{var data=await(await get('/activity.json',controller.signal)).json();if(data.schema!==1||!Array.isArray(data.samples))throw Error('Unavailable');activity=data;graphs();historyTable();}
            catch(_){/* Retain the dated historical observations while current status refreshes independently. */}
            finally{clearTimeout(timeout);}
        }
        async function refresh() {
            lastRefresh = Date.now();
            await Promise.allSettled([refreshStats(), refreshLive()]);
        }
        refresh();
        setInterval(function () { if (!document.hidden) refresh(); }, 300000);
        document.addEventListener('visibilitychange', function () { if (!document.hidden && Date.now() - lastRefresh > 30000) refresh(); });
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
}());

/* Lightweight current-room updates; match starts anchor estimated progress. */
(function () {
    'use strict';
    function init() {
        var panel = document.querySelector('.pr-now-panel');
        if (!panel && !document.querySelector('[data-status-page]') && !document.querySelector('[data-home]')) return;
        var last = null, busy = false, expiry, boundary, boundaryKey = '', offset = 0;
        var phases = {PLAYING:'Playing',STARTING:'Starting',SCORING:'Between maps',IDLE:'Waiting',OFFLINE:'Offline',STALE:'Status unavailable'};
        var wide = window.matchMedia('(min-width: 900px)');
        function now() { return (Date.now() + offset) / 1000; }
        function integer(x) { return Number.isSafeInteger(x) && x > 0; }
        function text(root, selector, value) { var el = root.querySelector(selector); if (el) el.textContent = value; }
        function node(tag, value) { var el = document.createElement(tag); if (value !== undefined) el.textContent = value; return el; }
        function fresh(data) { return data && data.schema === 1 && Array.isArray(data.rooms) && data.rooms.length === 4 && Number.isFinite(data.generated_at) && Number.isFinite(data.expires_at) && data.generated_at <= now()+30 && data.expires_at > now() && data.expires_at-data.generated_at <= 90; }
        function clock(seconds) { seconds = Math.floor(Math.max(0,seconds)); return Math.floor(seconds/60)+':'+String(seconds%60).padStart(2,'0'); }
        var reducedMotion=window.matchMedia('(prefers-reduced-motion: reduce)');
        function progress(force) {
            document.querySelectorAll('.pr-now-playing[data-ownership="owner"]').forEach(function(card){window.prLivePresentation.tick(card,now());});
        }
        reducedMotion.addEventListener('change',function(){progress(true);});
        function queue(article,room,valid) {
            var root=article.querySelector('.pr-queue-view');if(!root)return;
            var signature=JSON.stringify([valid,room.queue_known,room.queue]);if(root.dataset.signature===signature)return;root.dataset.signature=signature;
            if(!valid||!room.queue_known){var pending=node('p',valid?'The next map is being prepared.':'Waiting for a fresh room update.');pending.className='pr-queue-empty';root.replaceChildren(pending);return;}
            if(!room.queue.length){var empty=node('p','No upcoming maps in the observed queue.');empty.className='pr-queue-empty';root.replaceChildren(empty);return;}
            var list=node('ol');list.className='pr-up-next';
            room.queue.forEach(function(map,i){
                var li=node('li'),art=node('span');art.className='pr-queue-art';art.setAttribute('aria-hidden','true');
                if(integer(map.beatmapset_id)){var img=node('img');img.alt='';img.loading='lazy';img.decoding='async';img.referrerPolicy='no-referrer';img.addEventListener('error',function(){this.hidden=true;});img.src='https://assets.ppy.sh/beatmaps/'+map.beatmapset_id+'/covers/cover.jpg';art.append(img);}
                art.append(node('span',String(i+1).padStart(2,'0')));
                var copy=node('div');copy.className='pr-queue-copy';var a=node('a',map.title||map.label||'Upcoming map');if(integer(map.beatmap_id)){a.href='https://osu.ppy.sh/beatmaps/'+map.beatmap_id;a.target='_blank';a.rel='noopener noreferrer';}copy.append(a,node('span',[map.artist,map.version].filter(Boolean).join(' · ')));li.append(art,copy);
                if(Number.isFinite(map.stars)){var stars=node('span',map.stars.toFixed(2)+'★');stars.className='pr-queue-stars';li.append(stars);}list.append(li);
            });root.replaceChildren(list);
        }
        function settings(article, room, valid) {
            var root=article.querySelector('.pr-room-settings dl'); if (!root) return;
            root.querySelectorAll('dt[data-setting]').forEach(function (dt) {
                var key=dt.dataset.setting, value=valid?(room.settings||{})[key]:null;
                dt.nextElementSibling.textContent=value==null?'Unknown':key.endsWith('range')?(Array.isArray(value)&&value.length===2?value.map(function(n){return Number(n).toFixed(2);}).join('–')+'★':'Not recorded'):Array.isArray(value)?(value.length?value.join(', '):'None'):typeof value==='boolean'?(value?'Enabled':'Disabled'):String(value);
            });
        }
        function dashboard(data,valid) {
            var live=document.querySelector('[data-status-page]');if(!live)return;
            var rooms=valid?data.rooms:[],service=valid?data.service||{}:{},availability=valid?data.availability||{}:{};
            function metric(key,value){text(live,'[data-live-metric="'+key+'"]',value);}
            var counts=rooms.filter(function(r){return Number.isInteger(r.players);});
            metric('players',valid&&counts.length?counts.reduce(function(n,r){return n+r.players;},0):'—');
            window.prLiveOccupancyUpdate('owner',valid?data:null);
            var peak=valid?data.peak_players:null;
            if(valid){
                var peakOk=peak&&Number.isInteger(peak.count)&&peak.count>=0&&peak.count<=2048&&Number.isInteger(peak.tracking_since)&&peak.tracking_since>=1600000000&&peak.tracking_since<=now()+30;
                text(live,'[data-live-peak]',peakOk?peak.count:'—');
                text(live,'[data-live-peak-since]',peakOk?'since '+new Date(peak.tracking_since*1000).toLocaleDateString('en-US',{month:'short',day:'numeric',year:'numeric',timeZone:'UTC'}):'');
            }
            metric('rooms',valid&&data.available?rooms.filter(function(r){return integer(r.room_id)&&r.state!=='OFFLINE'&&r.state!=='STALE';}).length+' / 4':'—');
            var seconds=service.uptime_seconds,minutes=Number.isFinite(seconds)?Math.floor(seconds/60):null;
            metric('uptime',minutes===null?'—':minutes>=1440?Math.floor(minutes/1440)+'d '+Math.floor(minutes%1440/60)+'h':minutes>=60?Math.floor(minutes/60)+'h '+minutes%60+'m':minutes+'m');
            if(Number.isFinite(availability.percent))metric('availability',availability.percent.toFixed(2)+'%');
            var health=live.querySelector('.pr-dashboard-health'),status=typeof service.online==='boolean'?(service.online?'online':'offline'):'unknown';health.dataset.health=status;text(health,'[data-service-label]','Bot '+status);
            text(live,'[data-service-restarts]',Number.isInteger(service.restarts)?String(service.restarts):'Unknown');
            if(Number.isInteger(availability.samples))text(live,'.pr-availability-note','Availability is based on '+availability.samples+' service observations in the past 24 hours. Short outages between observations can be missed.');
            live.querySelectorAll('[data-chart-current]').forEach(function(el){
                if(el.dataset.chartCurrent==='total'){
                    var complete=rooms.length===4&&rooms.every(function(r){return Number.isInteger(r.players)&&r.players>=0||r.players==null&&r.state==='OFFLINE';});
                    window.prLiveOccupancyUpdate('owner',valid?data:null);
                }else{var room=rooms.find(function(r){return r.alias===el.dataset.chartCurrent;});el.textContent=room&&Number.isInteger(room.players)?room.players+' here now':'—';}
            });
            live.querySelectorAll('.pr-dash-room').forEach(function(article){var room=rooms.find(function(r){return r.alias===article.id;})||{},map=room.current_map||{},settings=room.settings||{},range=settings.active_range;
                text(article,'[data-room-range]',Array.isArray(range)&&range.length===2?range.map(function(n){return Number(n).toFixed(2);}).join('–')+'★':'Unknown');
                text(article,'[data-room-limit]',Number.isFinite(settings.max_length)?clock(settings.max_length):'Unknown');
                text(article,'[data-np-stars]',Number.isFinite(map.stars)?map.stars.toFixed(2)+'★':'Difficulty unknown');
                text(article,'[data-np-length]',Number.isFinite(map.total_length)?clock(map.total_length):'Length unknown');
            });
        }
        function update(data) {
            var valid=fresh(data); if (valid && last && data.generated_at < last.generated_at) return;
            if (valid) last=data; else last=null;
            clearTimeout(expiry);
            document.querySelectorAll('.pr-now-playing[data-ownership="owner"]').forEach(function (card) {
                var room=valid?data.rooms.find(function(r){return r.alias===card.dataset.npRoom;}):null; room=room||{};
                window.prLivePresentation.update(card,room,valid,now());
                var article=card.closest('.pr-room-card'); if(article){queue(article,room,valid);settings(article,room,valid);var browser=article.querySelector('.pr-browser-room');if(browser){if(integer(room.room_id)){browser.href='https://poolrotator-91-107-237-182.nip.io/room/'+room.room_id;browser.removeAttribute('aria-disabled');}else{browser.removeAttribute('href');browser.setAttribute('aria-disabled','true');}}}
            });
            document.querySelectorAll('[data-status-sidebar],[data-status-page]').forEach(function(root){root.dataset.expires=valid?data.expires_at:0;var e=root.querySelector('.pr-updated');if(e&&valid){e.dataset.updated=data.generated_at;e.textContent='Updated '+new Date(data.generated_at*1000).toLocaleTimeString()+' · refreshes every 10s';}});
            if(panel)text(panel,'.pr-np-freshness',valid?'Updates every 10s · progress estimated':'Reconnecting · current status unavailable');
            dashboard(data,valid);
            document.querySelectorAll('.pr-lobby-menu [data-room]').forEach(function(row){var r=valid?data.rooms.find(function(r){return r.alias===row.dataset.room;}):null;var p=row.querySelector('.pr-join-status');if(!p){p=node('p');p.className='pr-join-status';row.querySelector('.pr-room-heading').after(p);}p.textContent=r?(phases[r.state]||phases.STALE)+(Number.isInteger(r.players)?' · '+r.players+' players':''):phases.STALE;});
            document.dispatchEvent(new CustomEvent('pr:lobby-status',{detail:valid?data:null}));progress();
            if(valid)expiry=setTimeout(function(){update(null);},Math.max(1,(data.expires_at-now())*1000));
            var endings=valid?data.rooms.filter(function(r){return r.state==='PLAYING'&&r.playback&&r.playback.estimated_end_at>now();}).sort(function(a,b){return a.playback.estimated_end_at-b.playback.estimated_end_at;}):[];
            var next=endings[0], key=next?next.alias+':'+next.playback.started_at:'';
            if(key!==boundaryKey){boundaryKey=key;clearTimeout(boundary);if(next)boundary=setTimeout(function(){poll();},Math.max(500,(next.playback.estimated_end_at-now())*1000+500));}
        }
        async function poll(){if(busy||document.hidden)return;busy=true;var controller=new AbortController(),timeout=setTimeout(function(){controller.abort();},5000);try{var response=await fetch('/status.json',{cache:'no-store',credentials:'omit',signal:controller.signal});if(!response.ok)throw Error('Unavailable');var date=Date.parse(response.headers.get('Date'));if(Number.isFinite(date))offset=date-Date.now();update(await response.json());}catch(_){if(!fresh(last))update(null);}finally{clearTimeout(timeout);busy=false;}}
        function fitWidget(){
            if(!panel)return;var body=panel.querySelector('.pr-live-sidebar-body');
            if(wide.matches||!panel.open){body.style.maxHeight='';body.style.top='';body.style.bottom='';return;}
            var rect=panel.getBoundingClientRect(),below=window.innerHeight-rect.bottom-20,above=rect.top-76;
            if(below<180&&above>below){body.style.top='auto';body.style.bottom='52px';body.style.maxHeight=Math.max(80,above-12)+'px';}
            else{body.style.top='52px';body.style.bottom='auto';body.style.maxHeight=Math.max(80,below)+'px';}
        }
        function layout(){if(panel)panel.open=wide.matches;fitWidget();}layout();wide.addEventListener('change',layout);
        if(panel){panel.addEventListener('toggle',fitWidget);window.addEventListener('resize',fitWidget);var fitPending=false;window.addEventListener('scroll',function(){if(!fitPending){fitPending=true;requestAnimationFrame(function(){fitPending=false;fitWidget();});}},{passive:true});}
        document.addEventListener('keydown',function(e){if(e.key==='Escape'&&panel&&!wide.matches&&panel.open){panel.open=false;panel.querySelector('summary').focus();}});
        document.addEventListener('click',function(e){if(panel&&!wide.matches&&panel.open&&!panel.contains(e.target))panel.open=false;});
        document.addEventListener('pr:np-rescan',function(){update(last);});
        document.addEventListener('visibilitychange',function(){document.documentElement.classList.toggle('pr-tab-hidden',document.hidden);if(document.hidden){document.querySelectorAll('.pr-np-progress > span').forEach(function(fill){if(fill._songAnimation)fill._songAnimation.pause();});}else{if(!fresh(last))update(null);progress(true);poll();}});
        try{var initial=document.getElementById('pr-now-playing-data');if(initial)update(JSON.parse(initial.textContent));}catch(_){update(null);}
        poll();setInterval(function(){if(!document.hidden)poll();},10000);setInterval(function(){if(!document.hidden)progress();},1000);
    }
    if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();
}());

/* Room filtering leaves the live cards and their focus/state intact. */
(function(){
    function init(){var live=document.querySelector('[data-status-page]');if(!live)return;
        live.querySelectorAll('[data-room-filter]').forEach(function(button){button.addEventListener('click',function(){
            live.querySelectorAll('[data-room-filter]').forEach(function(other){other.setAttribute('aria-pressed',String(other===button));});
            live.querySelectorAll('.pr-dash-room,[data-community-card]').forEach(function(room){room.hidden=button.dataset.roomFilter!=='all'&&room.dataset.roomType!==button.dataset.roomFilter;});
        });});
        document.addEventListener('click',function(e){var link=e.target.closest('a[href="#about_status"]');if(link){var notes=document.getElementById('about_status');if(notes)notes.open=true;}});
    }
    if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();
}());

/* Homepage consumes the shared live feed; its totals use the existing 5m export. */
(function () {
    'use strict';
    function init() {
        var home=document.querySelector('[data-home]'); if(!home)return;
        var phases={PLAYING:'Playing',STARTING:'Starting',SCORING:'Between maps',IDLE:'Waiting',OFFLINE:'Offline',STALE:'Status unavailable'};
        function put(root,selector,value){var el=root.querySelector(selector);if(el)el.textContent=String(value);}
        function rooms(data){
            var valid=data&&data.available===true&&Array.isArray(data.rooms),list=valid?data.rooms:[];
            var counts=list.map(function(r){return r.players;});
            window.prLiveOccupancyUpdate('owner',valid?data:null);
            home.querySelectorAll('[data-home-room]').forEach(function(card){
                var room=list.find(function(r){return r.alias===card.dataset.homeRoom;})||{},state=phases[room.state]?room.state:'STALE';
                put(card,'[data-home-phase]',phases[state]);put(card,'[data-home-count]',Number.isInteger(room.players)&&room.players>=0?room.players+(room.players===1?' person':' people'):'Count unavailable');
                var join=card.querySelector('.pr-home-join'),browser=card.querySelector('.pr-home-browser');
                if(Number.isSafeInteger(room.room_id)&&room.room_id>0&&state!=='OFFLINE'&&state!=='STALE'){
                    join.href='osu://room/'+room.room_id;join.removeAttribute('aria-disabled');browser.href='https://poolrotator-91-107-237-182.nip.io/room/'+room.room_id;browser.hidden=false;
                }else{join.removeAttribute('href');join.setAttribute('aria-disabled','true');browser.removeAttribute('href');browser.hidden=true;}
            });
        }
        document.addEventListener('pr:lobby-status',function(e){rooms(e.detail);});
        var stats=null,busy=false,expiry,lastAttempt=0,offset=Number(home.dataset.serverNow)*1000-Date.now();
        function fresh(data){var now=(Date.now()+offset)/1000;return data&&data.schema===1&&data.available===true&&Number.isFinite(data.generated_at)&&Number.isFinite(data.expires_at)&&data.generated_at<=now+30&&data.expires_at>now&&data.expires_at-data.generated_at<=600;}
        function render(data){
            var valid=fresh(data),totals=valid?data.totals||{}:{};stats=valid?data:null;clearTimeout(expiry);
            ['players','maps','pp'].forEach(function(key){var n=totals[key];put(home,'[data-home-stat="'+key+'"]',Number.isFinite(n)&&n>=0?(key==='pp'?'+':'')+new Intl.NumberFormat('en-US',{maximumFractionDigits:key==='pp'?1:0}).format(n):'—');});
            put(home,'[data-home-stats-note]',valid?'Anonymous community totals · updated every five minutes.':'Statistics are temporarily unavailable.');
            if(valid)expiry=setTimeout(function(){render(null);},Math.max(1,data.expires_at*1000-Date.now()-offset));
        }
        async function refresh(){
            if(busy||document.hidden)return;busy=true;lastAttempt=Date.now();var controller=new AbortController(),timeout=setTimeout(function(){controller.abort();},5000);
            try{var response=await fetch('/statistics.json',{cache:'no-store',credentials:'omit',signal:controller.signal});if(!response.ok)throw Error('Unavailable');var date=Date.parse(response.headers.get('Date'));if(Number.isFinite(date))offset=date-Date.now();var data=await response.json();render({schema:data.schema,available:data.available,generated_at:data.generated_at,expires_at:data.expires_at,totals:((data.scopes||{}).all||{})['7d']?.totals||{}});}
            catch(_){if(!fresh(stats))render(null);}finally{clearTimeout(timeout);busy=false;}
        }
        try{render(JSON.parse(document.getElementById('pr-home-stats-data').textContent));}catch(_){render(null);}
        refresh();setInterval(function(){if(!document.hidden)refresh();},300000);
        document.addEventListener('visibilitychange',function(){if(!document.hidden){if(!fresh(stats)){render(null);refresh();}else if(Date.now()-lastAttempt>300000)refresh();}});
    }
    if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();
}());

/* Theme controls use the early head bootstrap; no requests or account changes. */
(function () {
    'use strict';
    function init() {
        if (!window.prTheme) return;
        document.querySelectorAll('[data-theme-toggle]').forEach(function (button) {
            button.addEventListener('click', function () { window.prTheme.toggle(); });
        });
        window.prTheme.sync();
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
}());

/* Player-facing illustration of the active v6.6.18 MapPool._rating_weight. */
(function () {
    'use strict';
    function init() {
        var info = window.JSINFO || {};
        if (info.ACT !== 'show' || info.id !== 'wiki:ratings' || info.rev) return;
        var page = document.querySelector('#dokuwiki__content .page');
        if (!page) return;
        var heading = Array.prototype.find.call(page.querySelectorAll('h2'), function (el) {
            return el.textContent.trim() === 'Try your own vote mix';
        });
        if (!heading || !heading.nextElementSibling) return;
        var lead = heading.nextElementSibling;
        var paragraphs = Array.prototype.slice.call(lead.querySelectorAll(':scope > p'));
        var intro = paragraphs.find(function (el) { return el.textContent.trim().indexOf('Enter good and bad totals') === 0; });
        var fallback = paragraphs.find(function (el) { return el.textContent.trim().indexOf('The calculator needs JavaScript.') === 0; });
        if (!intro || !fallback) return;
        var panel = document.createElement('section');
        panel.className = 'pr-rating-tool';
        panel.setAttribute('aria-label', 'Map feedback weight calculator');
        panel.innerHTML = '<div class="pr-rating-intro"><span class="pr-rating-eyebrow">PICK WEIGHT LAB</span><strong>How far can feedback move a map?</strong><p>Try a vote mix. This models one eligible map against 19 otherwise identical unrated maps.</p></div>' +
            '<div class="pr-rating-presets" aria-label="Example vote mixes"><button type="button" data-good="0" data-bad="0">Unrated</button><button type="button" data-good="3" data-bad="0">Popular</button><button type="button" data-good="0" data-bad="2">Disliked</button><button type="button" data-good="2" data-bad="1">Mixed</button></div>' +
            '<div class="pr-rating-inputs"><label>Good votes<input type="number" min="0" max="99" step="1" inputmode="numeric" value="2" data-rating-good></label><label>Bad votes<input type="number" min="0" max="99" step="1" inputmode="numeric" value="1" data-rating-bad></label></div>' +
            '<p class="pr-rating-error" data-rating-error hidden></p>' +
            '<div class="pr-rating-results" aria-live="polite" aria-atomic="true"><div><span>Relative pick weight</span><strong data-rating-weight></strong><small data-rating-change></small></div><div><span>Illustrative pick chance</span><strong data-rating-chance></strong><small>Among 20 equally eligible maps; the other 19 are unrated.</small></div></div>' +
            '<div class="pr-rating-bars" aria-hidden="true"><div><span>Unrated map</span><i><b style="width:40%"></b></i><em>1.00×</em></div><div><span>Your mix</span><i><b data-rating-bar></b></i><em data-rating-bar-label></em></div></div>' +
            '<p class="pr-rating-caveat">A real lobby can have different candidates, ratings, source rules, and repeat spacing. This is an illustration for one future pick.</p>';
        lead.insertBefore(panel, fallback);
        intro.hidden = true;
        fallback.hidden = true;

        var goodInput = panel.querySelector('[data-rating-good]');
        var badInput = panel.querySelector('[data-rating-bad]');
        var error = panel.querySelector('[data-rating-error]');
        var results = panel.querySelector('.pr-rating-results');
        function read(input) {
            var value = input.value.trim();
            if (!/^(0|[1-9][0-9]*)$/.test(value)) return null;
            var n = Number(value);
            return Number.isSafeInteger(n) && n <= 99 ? n : null;
        }
        function percent(n) {
            return (n < 1 ? n.toFixed(2) : n.toFixed(1)) + '%';
        }
        function update() {
            var good = read(goodInput), bad = read(badInput);
            if (good === null || bad === null) {
                error.textContent = 'Enter whole vote counts from 0 to 99.';
                error.hidden = false;
                results.hidden = true;
                panel.querySelector('.pr-rating-bars').hidden = true;
                return;
            }
            error.hidden = true;
            results.hidden = false;
            panel.querySelector('.pr-rating-bars').hidden = false;
            var goodFactor = Math.min(2.5, 1 + 0.25 * good);
            var badFactor = bad === 0 ? 1 : 0.85 * Math.pow(0.35, bad - 1);
            var weight = Math.max(0.03, goodFactor * badFactor);
            var chance = 100 * weight / (weight + 19);
            var weightLabel = weight.toFixed(4).replace(/0$/, '');
            panel.querySelector('[data-rating-weight]').textContent = weightLabel + '×';
            panel.querySelector('[data-rating-chance]').textContent = percent(chance);
            var delta = (weight - 1) * 100;
            panel.querySelector('[data-rating-change]').textContent = delta === 0 ? 'Same weight as an unrated map' :
                percent(Math.abs(delta)) + (delta > 0 ? ' higher' : ' lower') + ' than an unrated map';
            panel.querySelector('[data-rating-bar]').style.width = (weight / 2.5 * 100).toFixed(2) + '%';
            panel.querySelector('[data-rating-bar-label]').textContent = (Math.round((weight + 1e-9) * 100) / 100).toFixed(2) + '×';
        }
        goodInput.addEventListener('input', update);
        badInput.addEventListener('input', update);
        panel.querySelectorAll('.pr-rating-presets button').forEach(function (button) {
            button.addEventListener('click', function () {
                goodInput.value = button.dataset.good;
                badInput.value = button.dataset.bad;
                update();
            });
        });
        update();
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
}());


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
      if(!r||typeof r!=='object'||!['playing','starting','idle','draining','unavailable'].includes(r.state)||!['standard-auto','standard-fixed','mania-4k','mania-7k'].includes(r.preset))return false;
      if(typeof r.title!=='string'||r.title.length>640||typeof r.operator!=='string'||r.operator.length>320||!Number.isFinite(r.fresh_until)||!Number.isFinite(r.hide_at))return false;
      for(const key of['players','playing_players','match_participants'])if(r[key]!=null&&(!Number.isInteger(r[key])||r[key]<0||r[key]>128))return false;
      const m=r.current_map;if(m!=null){if(typeof m!=='object'||!Number.isSafeInteger(m.beatmap_id)||m.beatmap_id<=0)return false;for(const key of['title','artist','version'])if(m[key]!=null&&(typeof m[key]!=='string'||m[key].length>1024))return false;}
      return true;
    }
    function render(value){
      const t=now(),valid=value&&[1,2].includes(value.schema)&&value.available===true&&Array.isArray(value.rooms)&&value.rooms.length<=100&&Number.isFinite(value.generated_at)&&Number.isFinite(value.expires_at)&&value.generated_at<=t+30&&value.expires_at>t&&value.expires_at-value.generated_at<=60;
      data=valid?value:null;clearTimeout(expiry);let next=valid?value.expires_at:t+30;const own=ownIds(),seen=new Set(),rooms=[];
      for(const r of valid?value.rooms:[]){if(!validRoom(r)||(r.visibility!==undefined&&r.visibility!=='public')||own.has(r.room_id)||!Number.isSafeInteger(r.room_id)||r.room_id<=0||!Number.isFinite(r.hide_at)||r.hide_at<=t||typeof r.key!=='string'||!/^[a-f0-9]{24}$/.test(r.key)||seen.has(r.key))continue;seen.add(r.key);rooms.push({...r,ownership:'community'});next=Math.min(next,r.hide_at);if(r.fresh_until>t)next=Math.min(next,r.fresh_until);}
      if(window.prLiveOccupancyUpdate)window.prLiveOccupancyUpdate('community',valid?value:null);
      roots.forEach(root=>{
        const list=root.querySelector('[data-community-list]'),compact=root.dataset.communitySurface==='sidebar';
        const openSettings=new Set(Array.from(list.querySelectorAll('article')).filter(a=>a.querySelector('details[open]')).map(a=>a.dataset.liveKey));
        const focused=document.activeElement&&list.contains(document.activeElement)?{key:document.activeElement.closest('article').dataset.liveKey,cls:document.activeElement.className}:null;
        const nodes=[];
        for(const room of rooms){const fresh=Number.isFinite(room.fresh_until)&&room.fresh_until>t&&room.state!=='unavailable';const article=element('article',undefined,'pr-community-room');article.setAttribute('data-community-card','');article.dataset.liveKey=room.key;article.dataset.preset=room.preset||'';article.dataset.state=fresh?room.state:'unavailable';article.dataset.roomType=String(room.preset||'').startsWith('mania')?'mania':'standard';article.id=(compact?'sidebar-community-':'community-')+room.key;article.append(ui.card(room,fresh,compact,t));
          if(!compact){const body=element('div',undefined,'pr-room-body'),facts=element('div',undefined,'pr-community-facts');function fact(label,text){const n=element('p',undefined,'pr-community-fact');n.append(element('span',label),element('strong',text));facts.append(n);}if(fresh&&Number.isFinite(room.min_stars)&&Number.isFinite(room.max_stars))fact('Star range',room.min_stars.toFixed(2)+'–'+room.max_stars.toFixed(2)+'★');fact('Players in room',fresh&&Number.isInteger(room.players)&&room.players>=0&&room.players<=128?room.players+' players':'Unavailable');body.append(facts);const settings=ui.settings(room.settings,fresh);if(settings){settings.open=openSettings.has(room.key);body.append(settings);}article.append(body);}
          const filter=document.querySelector('[data-room-filter][aria-pressed="true"]');if(root.dataset.communitySurface==='dashboard'&&filter)article.hidden=filter.dataset.roomFilter!=='all'&&filter.dataset.roomFilter!==article.dataset.roomType;
          nodes.push(article);
        }
        list.replaceChildren(...nodes);if(focused){const row=nodes.find(a=>a.dataset.liveKey===focused.key);if(row){const target=Array.from(row.querySelectorAll('a,summary')).find(a=>a.className===focused.cls);if(target)target.focus({preventScroll:true});}}
        root.querySelector('[data-community-note]').textContent=!valid?'Community status is temporarily unavailable.':rooms.length?'Updates every 30 seconds. Progress is estimated when match timing is available.'+(value.more_available?' Showing the first 100 public rooms; more rooms are available.':''):'No other public rooms are currently listed.';
      });
      expiry=setTimeout(()=>render(data),Math.max(50,(next-t)*1000+20));
    }
    async function poll(){if(busy||document.hidden)return;busy=true;const abort=new AbortController(),timeout=setTimeout(()=>abort.abort(),4000);try{const r=await fetch('/community/rooms.json',{cache:'no-store',credentials:'omit',signal:abort.signal});if(!r.ok)throw Error('unavailable');const date=Date.parse(r.headers.get('Date'));if(Number.isFinite(date))offset=date-Date.now();render(await r.json());}catch(_){render(null);}finally{clearTimeout(timeout);busy=false;}}
    document.addEventListener('pr:lobby-status',event=>{const rooms=event.detail&&event.detail.rooms;ownerIds=new Set(Array.isArray(rooms)?rooms.map(r=>r.room_id).filter(Number.isSafeInteger):[]);if(data)render(data);});
    const embedded=document.querySelector('[data-community-initial]');if(embedded){try{render(JSON.parse(embedded.textContent));}catch(_){render(null);}}
    poll();setInterval(poll,30000);setInterval(()=>{if(!document.hidden)roots.forEach(root=>root.querySelectorAll('[data-live-card]').forEach(card=>ui.tick(card,now())));},1000);
    document.addEventListener('visibilitychange',()=>{if(!document.hidden){render(data);poll();}});
  }
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();
}());
