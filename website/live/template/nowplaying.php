<?php
if (!defined('DOKU_INC')) die();
require_once __DIR__.'/presentation.php';
function prNowPlayingCard($room,$fresh,$compact=true) { return prLiveCard($room,$fresh,$compact); }
function prNowPlayingPanel() {
    $data = prAnalyticsData('status'); $fresh = prAnalyticsFresh($data);
    echo '<details class="pr-live-sidebar pr-now-panel" open><summary><span class="pr-np-live-dot" aria-hidden="true"></span><span>Live lobbies</span></summary><div class="pr-live-sidebar-body" data-status-sidebar data-expires="' . (int)($data['expires_at'] ?? 0) . '"><ul class="pr-np-list">';
    foreach (['Standard automatic','Standard fixed','Mania 7K','Mania 4K'] as $i=>$label) {
        $room=$data['rooms'][$i] ?? ['alias'=>'room'.($i+1),'label'=>$label];
        echo '<li class="pr-np-slot">' . prNowPlayingCard($room,$fresh) . '</li>';
    }
    echo '</ul>';
    require_once __DIR__.'/community.php';
    prCommunityRooms(prCommunityRead('/srv/poolrotator-wiki/public/community/rooms.json'), time(),array_column($data['rooms']??[],'room_id'),'sidebar');
    echo '<div class="pr-np-foot"><span class="pr-np-freshness a11y">Updates every 10s · progress estimated</span><a href="' . hsc(wl('status:start')) . '">View all lobbies <span aria-hidden="true">→</span></a></div><noscript><p class="pr-muted">Refresh the page for the latest status.</p></noscript></div></details>';
    echo '<script type="application/json" id="pr-now-playing-data">' . json_encode($data, JSON_HEX_TAG | JSON_HEX_AMP | JSON_HEX_APOS | JSON_HEX_QUOT) . '</script>';
}

function prCompactLobbyCard($room,$fresh) { return prLiveCard($room,$fresh,true); }
