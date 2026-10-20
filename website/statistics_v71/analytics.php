<?php
if (!defined('DOKU_INC')) die();
require_once __DIR__ . '/nowplaying.php';
require_once __DIR__ . '/dashboard.php';
require_once __DIR__ . '/joinclicks.php';

function prAnalyticsData($name) {
    static $cache = [];
    if (!in_array($name, ['statistics', 'status', 'activity'], true)) return [];
    if (!isset($cache[$name])) {
        $data = json_decode(@file_get_contents('/srv/poolrotator-wiki/public/' . $name . '.json'), true);
        $cache[$name] = is_array($data) ? $data : [];
    }
    return $cache[$name];
}
function prAnalyticsFresh($data) {
    return ($data['schema'] ?? 0) === 1 && is_numeric($data['generated_at'] ?? null) && is_numeric($data['expires_at'] ?? null)
        && $data['generated_at'] <= time() + 30 && $data['expires_at'] >= time() && $data['expires_at'] - $data['generated_at'] <= 600;
}
function prAnalyticsNumber($n, $decimals = 0) { return number_format(is_numeric($n) ? (float) $n : 0, $decimals); }
function prAnalyticsHours($n) { return prAnalyticsNumber($n / 3600, 1) . ' h'; }
function prAnalyticsMap($map) {
    if (!is_array($map)) return '<span class="pr-muted">Not observed yet</span>';
    $label = hsc((string) ($map['label'] ?? 'Map details'));
    $id = $map['beatmap_id'] ?? null;
    return is_int($id) && $id > 0 ? '<a href="https://osu.ppy.sh/beatmaps/' . $id . '" target="_blank" rel="noopener noreferrer">' . $label . '<span class="a11y"> (opens a new tab)</span></a>' : $label;
}
function prAnalyticsUpdated($data) {
    return '<p class="pr-updated" data-updated="' . (int) ($data['generated_at'] ?? 0) . '">Updated ' . hsc(gmdate('M j, H:i', (int) ($data['generated_at'] ?? 0))) . ' UTC · refreshes every ' . (($data['refresh_seconds'] ?? 300) === 10 ? '10 seconds' : '5 min') . '</p>';
}
function prAnalyticsPhase($state) {
    return ['IDLE'=>'Waiting','STARTING'=>'Starting','PLAYING'=>'Playing','SCORING'=>'Between maps','OFFLINE'=>'Offline','STALE'=>'Status unavailable'][$state] ?? 'Status unavailable';
}
function prAnalyticsSidebar() { prNowPlayingPanel(); }
function prAnalyticsChoice($key, $options, $default) {
    $value = $_GET[$key] ?? null;
    return is_string($value) && isset($options[$value]) ? $value : $default;
}
function prAnalyticsValue($p, $sort) {
    if ($sort === 'gain') return $p['best_gain']['gain'] ?? 0;
    if ($sort === 'score') return $p['best_score']['score_pp'] ?? 0;
    if ($sort === 'accuracy') return ($p['accuracy_n'] ?? 0) >= 20 ? ($p['average_accuracy'] ?? -1) : -1;
    return $p[$sort] ?? 0;
}
function prAnalyticsPlayer($p) {
    $id = (int) ($p['user_id'] ?? 0);
    $out = '<a class="pr-player-name" href="https://osu.ppy.sh/users/' . $id . '" target="_blank" rel="noopener noreferrer">' . hsc($p['username'] ?? 'Player') . '</a><details class="pr-player-detail" data-player-id="' . $id . '"><summary>Details</summary><div>';
    $out .= '<p><strong>' . prAnalyticsNumber($p['scores'] ?? 0) . ' scores submitted</strong> · ' . prAnalyticsNumber($p['top_plays'] ?? 0) . ' recorded top-100 plays · ' . prAnalyticsNumber($p['active_days'] ?? 0) . ' active days (UTC)</p>';
    if (!empty($p['legacy_owner_seconds'])) $out .= '<p>Historical owner connected time: ' . prAnalyticsHours($p['legacy_owner_seconds']) . ' (included in connected time).</p>';
    if (!empty($p['legacy_community_seconds'])) $out .= '<p>Reviewed historical community connected time: ' . prAnalyticsHours($p['legacy_community_seconds']) . ' (included in connected time).</p>';
    if (!empty($p['time_by_type'])) {
        $out .= '<dl class="pr-time-types">';
        foreach (['standard'=>'Standard','catch'=>'Catch','mania'=>'Mania total','mania_7k'=>'7K tracked rooms','mania_4k'=>'4K tracked rooms'] as $key=>$label) {
            if (isset($p['time_by_type'][$key])) $out .= '<dt data-setting="' . hsc($key) . '">' . hsc($label) . '</dt><dd>' . prAnalyticsHours($p['time_by_type'][$key]) . '</dd>';
        }
        $out .= '</dl>';
    }
    foreach (['best_gain'=>'Largest recorded PP gain','best_score'=>'Highest recorded top-play PP'] as $key=>$label) {
        if (!empty($p[$key])) {
            $a = $p[$key];
            $out .= '<p><strong>' . $label . ':</strong> ' . ($key === 'best_gain' ? '+' . prAnalyticsNumber($a['gain'],2) : prAnalyticsNumber($a['score_pp'],2)) . ' PP · ' . prAnalyticsMap($a) . '</p>';
            $out .= '<p class="pr-muted">' . (isset($a['accuracy']) ? prAnalyticsNumber($a['accuracy'],2) . '% accuracy · ' : '') . 'personal best #' . (int)$a['top_rank'] . ' · ' . hsc(gmdate('M j, Y', (int)$a['at'])) . '</p>';
        }
    }
    $badges = [];
    if (($p['maps'] ?? 0)>=100) $badges[]='Century club';
    if (($p['unique_maps'] ?? 0)>=50) $badges[]='Map explorer';
    if (($p['pp'] ?? 0)>=100) $badges[]='100 PP club';
    if (($p['active_days'] ?? 0)>=7) $badges[]='Regular';
    if (!empty($p['time_by_type']['standard']) && !empty($p['time_by_type']['mania'])) $badges[]='Both rulesets';
    $out .= '<p class="pr-badges">'; foreach ($badges as $badge) $out .= '<span>' . hsc($badge) . '</span>';
    return $out . '</p></div></details>';
}
function prAnalyticsCoverage($data, $host) {
    $coverage = $data['coverage'] ?? [];
    $trackedPP = !empty($coverage['tracked_pp_supported']);
    if ($host === 'owner') return '<p class="pr-muted" role="note">Owner history includes the existing activity ledger and recorded PP achievements. Historical connected time is included in lifetime totals; recent-period time is unavailable.</p>';
    if ($host !== 'combined') {
        $row = $data['hosts'][$host] ?? [];
        $since = (int)($row['consent_at'] ?? 0); $updated = (int)($row['updated_at'] ?? 0);
        return '<p class="pr-muted" role="note">Opt-in public-room coverage since ' . hsc(gmdate('M j, Y H:i', $since)) . ' UTC. Latest host activity received ' . hsc(gmdate('M j, H:i', $updated)) . ' UTC; separately reviewed historical imports may predate opt-in. Map counts are completed-match participation; scores are included only when submitted results were observed. ' . ($trackedPP ? 'PP records are the existing tracked achievements shared by this host; no new PP attribution algorithm or complete profile history is implied.' : 'PP gains are not attributed for community hosts.') . '</p>';
    }
    $out = '<p class="pr-muted" role="note">Players are counted once across hosts by osu! ID. Connected time includes recorded owner history and community time. Duplicate room reports are counted once; overlapping community intervals are merged. Older cumulative records cannot resolve simultaneous visits. Map participation combines existing owner map starts with completed community matches; ' . ($trackedPP ? 'PP gains and top plays include existing tracked achievements shared by opted-in hosts; missing records are not inferred.' : 'PP gains and top-play achievements are owner records only.') . ' Hidden players contribute only to totals.</p>';
    if (empty($coverage['network_available'])) return $out . '<p class="pr-unavailable" role="status">Community statistics are not currently available; owner history remains visible. No community coverage is inferred before opt-in.</p>';
    return $out . '<p class="pr-updated">' . (int)($coverage['participating_hosts'] ?? 0) . ' opted-in hosts · community snapshot ' . hsc(gmdate('M j, H:i', (int)($coverage['network_generated_at'] ?? 0))) . ' UTC · ' . (!empty($coverage['pre_opt_in_community_history'])?'includes explicitly reviewed historical imports.':'no community history before opt-in.') . '</p>';
}
function prAnalyticsStatsBody($data, $mode, $period, $sort, $host = 'combined') {
    if (!prAnalyticsFresh($data) || empty($data['available'])) return '<p class="pr-unavailable">Statistics are temporarily unavailable. Please try again shortly.</p>';
    $view = $host === 'combined' ? $data : ($data['hosts'][$host] ?? []);
    $kind = $view['kind'] ?? 'combined';
    $trackedPP = !empty($data['coverage']['tracked_pp_supported']);
    $ownerPPOnly = $kind==='combined' && !$trackedPP;
    $noPP = $kind==='community' && !$trackedPP;
    $group = $view['scopes'][$mode][$period] ?? []; $totals = $group['totals'] ?? []; $players = $group['leaders'] ?? [];
    usort($players, function($a,$b) use ($sort) { return (prAnalyticsValue($b,$sort) <=> prAnalyticsValue($a,$sort)) ?: (($a['user_id'] ?? 0) <=> ($b['user_id'] ?? 0)); });
    if ($sort === 'accuracy') $players = array_values(array_filter($players,fn($p)=>($p['accuracy_n'] ?? 0)>=20));
    $out = prAnalyticsCoverage($data,$host) . '<div class="pr-metric-grid">';
    $metrics = [['Players recorded',prAnalyticsNumber($totals['players'] ?? 0)],[$kind==='owner'?'Map starts':'Map participations',prAnalyticsNumber($totals['maps'] ?? 0)],[$ownerPPOnly?'Owner recorded PP gain':'Recorded PP gain',$noPP?'Not attributed':'+' . prAnalyticsNumber($totals['pp'] ?? 0,1)],['Connected time',isset($totals['seconds']) ? prAnalyticsHours($totals['seconds']) : 'Lifetime only']];
    foreach ($metrics as [$label,$value]) {
        $out .= '<div class="pr-metric"><span>' . hsc($label) . '</span><strong>' . hsc($value) . '</strong></div>';
    }
    $out .= '</div><h2>Achievement spotlight</h2><div class="pr-spotlight-grid">';
    $all = $group['leaders'] ?? [];
    $spotlights = [['unique_maps','Map explorer'],[$period==='all'?'seconds':'active_days',$period==='all'?'Marathon regular':'Returning regular']];
    if (!$noPP) $spotlights = array_merge([['gain',$ownerPPOnly?'Biggest owner-recorded PP jump':'Biggest PP jump'],['score',$ownerPPOnly?'Highest owner-recorded top-play PP':'Highest top-play PP']],$spotlights);
    foreach ($spotlights as [$key,$title]) {
        $ranked = $all; usort($ranked,fn($a,$b)=>(prAnalyticsValue($b,$key)<=>prAnalyticsValue($a,$key))?:($a['user_id']<=>$b['user_id']));
        $p = $ranked[0] ?? null;
        $out .= '<article class="pr-spotlight"><h3>' . hsc($title) . '</h3>';
        if ($p && prAnalyticsValue($p,$key)>0) {
            $value = prAnalyticsValue($p,$key);
            $display = in_array($key,['gain','score']) ? ($key==='gain'?'+':'') . prAnalyticsNumber($value,2) . ' PP' : ($key==='seconds'?prAnalyticsHours($value):prAnalyticsNumber($value) . ($key==='unique_maps'?' different maps':' active days'));
            $out .= '<strong class="pr-spotlight-value">' . hsc($display) . '</strong><p><a href="https://osu.ppy.sh/users/' . (int)$p['user_id'] . '" target="_blank" rel="noopener noreferrer">' . hsc($p['username']) . '</a></p>';
            if (in_array($key,['gain','score'])) $out .= '<p>' . prAnalyticsMap($p[$key==='gain'?'best_gain':'best_score']) . '</p>';
        } else $out .= '<p>No qualifying public record yet.</p>';
        $out .= '</article>';
    }
    $out .= '</div><h2>Player rankings</h2><p class="pr-muted">Top 50 public players for each ranking. Hidden players contribute only to community totals. Explore a player’s Details for time by ruleset, their biggest gain and the map that earned it.</p>';
    $out .= '<div class="pr-table-scroll"><table class="pr-rankings"><thead><tr><th scope="col">#</th><th scope="col">Player</th><th scope="col">' . ($ownerPPOnly?'Owner PP gained':'PP gained') . '</th><th scope="col">' . ($kind==='owner'?'Map starts':'Map participations') . '</th><th scope="col">Different maps</th><th scope="col">' . ('Connected time') . '</th><th scope="col">Best gain</th><th scope="col">Avg. accuracy</th></tr></thead><tbody>';
    foreach (array_slice($players,0,50) as $i=>$p) {
        $out .= '<tr><td>' . ($i+1) . '</td><td>' . prAnalyticsPlayer($p) . '</td><td>' . ($noPP?'—':'+' . prAnalyticsNumber($p['pp'] ?? 0,1)) . '</td><td>' . prAnalyticsNumber($p['maps']) . '</td><td>' . prAnalyticsNumber($p['unique_maps']) . '</td><td>' . (!empty($group['time_available']) || $period==='all'?prAnalyticsHours($p['seconds'] ?? 0):'—') . '</td><td>' . (!empty($p['best_gain'])?'+' . prAnalyticsNumber($p['best_gain']['gain'],2) . ' PP':'—') . '</td><td>' . (isset($p['average_accuracy'])?prAnalyticsNumber($p['average_accuracy'],2) . '%':'—') . '</td></tr>';
    }
    return $out . '</tbody></table></div>' . (!$players?'<p>No qualifying public players in this view yet.</p>':'');
}
function prAnalyticsStatistics() {
    $modes = ['all'=>'All lobby types','standard'=>'Standard','catch'=>'Catch','mania_7k'=>'Mania 7K','mania_4k'=>'Mania 4K'];
    $periods = ['all'=>'All recorded history','7d'=>'Past 7 days','14d'=>'Past 14 days','30d'=>'Past 30 days'];
    $sorts = ['pp'=>'PP gained','maps'=>'Maps','unique_maps'=>'Different maps','seconds'=>'Connected time','gain'=>'Biggest PP gain','score'=>'Highest top-play PP','active_days'=>'Active days','accuracy'=>'Accuracy (20+ scores)'];
    $data = prAnalyticsData('statistics');
    $hosts = ['combined'=>'All participating hosts'];
    foreach (($data['hosts'] ?? []) as $key=>$row) {
        if (is_string($key) && preg_match('/^[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}$/D',$key) && is_array($row)) $hosts[$key] = (string)($row['label'] ?? 'Community host');
    }
    $requestedHost = $_GET['pr_host'] ?? null;
    $alias = is_string($requestedHost) ? ($data['host_aliases'][$requestedHost] ?? null) : null;
    $host = $alias === 'owner' ? 'owner' : prAnalyticsChoice('pr_host',$hosts,'combined');
    $mode = prAnalyticsChoice('pr_mode',$modes,'all'); $period = prAnalyticsChoice('pr_period',$periods,'all'); $sort = prAnalyticsChoice('pr_sort',$sorts,$host==='owner'?'pp':'maps');
    $view = $host === 'combined' ? $data : ($data['hosts'][$host] ?? []);
    $timeAvailable = !empty($view['scopes'][$mode][$period]['time_available']) || $period === 'all';
    if (!$timeAvailable && $sort==='seconds') $sort='pp';
    echo '<section class="pr-statistics" data-statistics data-expires="' . (int)($data['expires_at'] ?? 0) . '">' . prAnalyticsUpdated($data);
    echo '<form class="pr-stats-filters" method="get" action="' . hsc(DOKU_BASE . 'doku.php') . '"><input type="hidden" name="id" value="statistics:start">';
    foreach (['pr_host'=>['Host',$hosts,$host],'pr_mode'=>['Lobby type',$modes,$mode],'pr_period'=>['Period',$periods,$period],'pr_sort'=>['Rank by',$sorts,$sort]] as $name=>[$label,$options,$value]) {
        echo '<label>' . hsc($label) . '<select name="' . $name . '">';
        foreach ($options as $key=>$text) echo '<option value="' . hsc($key) . '"' . ($key===$value?' selected':'') . ($name==='pr_sort' && $key==='seconds' && !$timeAvailable?' disabled':'') . '>' . hsc($text) . '</option>';
        echo '</select></label>';
    }
    echo '<button type="submit">Show rankings</button></form><div class="pr-stats-body">' . prAnalyticsStatsBody($data,$mode,$period,$sort,$host) . '</div></section>';
    prJoinClickStatistics();
}
function prAnalyticsRoomCard($room,$fresh) { return prDashboardRoomCard($room,$fresh); }
function prAnalyticsGraph($samples,$index,$label,$since) { return prDashboardGraph($samples,$index,$label,$since); }
function prAnalyticsStatus($embedStatus=true) { prDashboardStatus($embedStatus); }
