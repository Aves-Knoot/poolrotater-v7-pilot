<?php
if (!defined('DOKU_INC')) die();
function prDocumentationData($name) {
    $path = $name === 'reviewed' ? '/srv/poolrotator-wiki/documentation/reviewed-release.json' : ($name === 'status' ? '/var/lib/poolrotator-wiki/documentation/status.json' : ($name === 'published' ? '/var/lib/poolrotator-wiki/documentation/published.json' : '/srv/poolrotator-wiki/documentation/release.json'));
    if (!is_readable($path) || filesize($path) > 1048576) return null;
    $data = json_decode(file_get_contents($path), true);
    return is_array($data) ? $data : null;
}
function prReviewedDocumentation() {
    $path='/srv/poolrotator-wiki/documentation/reviewed-release.json';
    if(is_link($path)||!is_file($path)||fileowner($path)!==0||(fileperms($path)&0022)!==0)return null;
    $review=prDocumentationData('reviewed');$version=$review['version']??null;
    if(!is_string($version)||!preg_match('/^v7\.[0-9]+\.[0-9]+$/D',$version)||!is_array($review['pages']??null)||count($review['pages'])>100)return null;
    $manifest='/srv/poolrotator-wiki/documentation/manifests/'.$version.'.json';
    if(is_link($manifest)||!is_file($manifest)||fileowner($manifest)!==0||(fileperms($manifest)&0022)!==0||hash_file('sha256',$manifest)!==($review['manifest_sha256']??null))return null;
    foreach($review['pages'] as $id=>$hash){if(!is_string($id)||!preg_match('/^[a-z0-9_]+(?::[a-z0-9_]+)*$/D',$id)||!is_string($hash)||!preg_match('/^[a-f0-9]{64}$/D',$hash))return null;$page='/var/lib/poolrotator-wiki/data/pages/'.str_replace(':','/',$id).'.txt';if(!is_file($page)||is_link($page)||hash_file('sha256',$page)!==$hash)return null;}
    return $review;
}
function prDocumentationFreshness() {
    global $ID, $ACT;
    if ($ACT !== 'show' || !($ID === 'start' || $ID === 'updates:start' || $ID === 'wiki:feature_inventory' || $ID === 'wiki:documentation_updates' || strpos($ID, 'commands:') === 0)) return;
    $published = prDocumentationData('published'); $release = prDocumentationData('release');
    $fresh = $release && isset($release['observed_at']) && time() - $release['observed_at'] <= 600 && time() >= $release['observed_at'] - 30;
    $version = $published['version'] ?? 'unavailable';
    $publication = prDocumentationData('status');
    $current = (!isset($publication['ok']) || $publication['ok']) && $fresh && !empty($release['confirmed']) && !empty($release['ready']) && ($release['active_version'] ?? '') === $version;
    $rolling = ($release['source_kind'] ?? '') === 'rolling_worker';
    $reviewed=prReviewedDocumentation();
    echo '<div class="pr-doc-freshness" role="note"><strong>' . ($reviewed ? 'Documentation reviewed for '.hsc($reviewed['version']) : ($current ? ($rolling ? 'Room-worker inventory verified' : 'Command inventory current') : 'Documentation verification pending')) . '</strong> · ';
    echo ($current ? 'Facts verified for ' : 'Last verified command inventory: ') . hsc($version);
    if ($fresh && $rolling) {
        $roomVersions = is_array($release['room_versions'] ?? null) ? $release['room_versions'] : [];
        $versions = array_values(array_filter($roomVersions, fn($v) => is_string($v) && preg_match('/^v\d+\.\d+\.\d+$/D', $v)));
        usort($versions, fn($a, $b) => version_compare(substr($a, 1), substr($b, 1)));
        if ($versions) echo ' · Room workers ' . hsc($versions[0] . ($versions[0] === end($versions) ? '' : '–' . end($versions)));
        echo ' · Shared supervisor ' . hsc($release['supervisor_version'] ?? 'unavailable');
        echo '. Availability depends on the room version';
    }
    elseif ($fresh && ($release['source_kind'] ?? '') === 'owner_v7_registered') {
        if (!empty($release['active_version'])) echo ' · Bot ' . hsc($release['active_version']);
        else echo ' · v7 inventory source verification pending (not a lobby availability check)';
        echo ' · <a href="' . hsc(wl($reviewed ? str_replace('.','_','updates:'.$reviewed['version']) : 'updates:v7_0_1')) . '">Reviewed v7 release notes</a>';
    }
    elseif ($fresh) echo ' · Bot ' . hsc($release['active_version'] ?? 'unavailable');
    else echo ' · Live version check unavailable';
    echo '. <a href="' . hsc(wl('wiki:documentation_updates')) . '">How updates work</a></div>';
    if ($ID === 'commands:start' && $published && isset($published['manifest']['commands'])) {
        echo '<script type="application/json" id="pr-command-catalog">' . json_encode($published['manifest']['commands'], JSON_HEX_TAG | JSON_HEX_AMP | JSON_HEX_APOS | JSON_HEX_QUOT) . '</script>';
    }
}
function prDocumentationFacts() {
    global $ID, $ACT;
    if ($ACT !== 'show' || strpos($ID, 'commands:') !== 0 || $ID === 'commands:start') return;
    $published = prDocumentationData('published');
    if (!$published || !isset($published['manifest']['commands'])) return;
    foreach ($published['manifest']['commands'] as $command) {
        if ($command['article'] !== $ID) continue;
        echo '<details class="pr-command-details"><summary>Verified syntax, aliases and limits</summary><section class="pr-command-facts" aria-label="Verified command facts"><h2>Verified command facts</h2>';
        echo '<p>Inventory checked for ' . hsc($published['version']) . '. The article’s Behavior reference dates its explanatory text.</p>';
        echo '<dl><dt>Syntax and examples</dt><dd>';
        foreach ($command['usage'] as $usage) {
            echo '<code>' . hsc($usage) . '</code>';
            if ($command['usage_rulesets'][$usage] !== $command['rulesets']) echo ' (' . hsc(implode(', ', $command['usage_rulesets'][$usage])) . ')';
            echo ' ';
        }
        echo '</dd><dt>Aliases</dt><dd>';
        if (!$command['aliases']) echo 'No additional direct aliases.';
        foreach ($command['aliases'] as $alias) {
            echo '<code>' . hsc($alias['command']) . '</code>';
            if ($alias['rulesets'] !== $command['rulesets']) echo ' (Mania)';
            echo ' ';
        }
        if ($command['id'] === 'style') echo '<p>Supported normalized arguments: ' . hsc(implode(', ', array_keys($published['manifest']['style_arguments']))) . '. Case, spaces, hyphens and underscores are normalized for supported names.</p>';
        echo '</dd><dt>Availability</dt><dd>' . hsc($command['availability']) . '</dd><dt>Who can use it</dt><dd>' . hsc($command['permissions']) . '</dd><dt>Conditions</dt><dd>' . hsc(implode(' ', $command['gates'])) . '</dd><dt>Timing and limits</dt><dd>' . hsc($command['timing']) . '</dd></dl></section></details>';
        return;
    }
}

function prDocumentationHealthReport() {
    global $ACT, $ID;
    if ($ACT !== 'show' || $ID !== 'wiki:documentation_health') return;
    $path = '/var/lib/poolrotator-wiki/documentation/health.json';
    if (!is_readable($path) || filesize($path) > 1048576) {
        echo '<p class="pr-doc-health-unavailable">The documentation audit has not run yet.</p>';
        return;
    }
    $data = json_decode(file_get_contents($path), true);
    if (!is_array($data) || ($data['schema'] ?? null) !== 1 || !isset($data['scanned_at'], $data['issues']) || !is_array($data['issues'])) {
        echo '<p class="pr-doc-health-unavailable">The documentation audit is unavailable.</p>';
        return;
    }
    $age = time() - (int)$data['scanned_at'];
    $fresh = $age >= -30 && $age <= 900;
    $labels = [
        'verification_pending'=>'Release verification or inventory publication needs review',
        'broken_link'=>'Internal page link points to a missing article',
        'missing_media'=>'Local image or file is missing',
        'missing_inventory_article'=>'Verified inventory points to a missing article',
        'missing_review_reference'=>'Article is missing its Behavior reference',
        'invalid_review_date'=>'Behavior reference has an invalid review date',
        'review_ahead_of_release'=>'Article review version is ahead of the verified release',
        'current_claim_older_review'=>'Article mentions the current release but its review label is older',
        'older_review_version'=>'Review label predates the verified release; check when affected',
        'duplicate_alias'=>'Command alias is claimed more than once for a ruleset',
        'inventory_invalid'=>'Verified inventory has invalid article metadata',
        'usage_annotation'=>'Verified command input contains an editorial mode note',
        'example_not_in_inventory'=>'Article example is not listed in the verified command inputs',
        'page_limit'=>'Page scan limit was reached',
        'page_too_large'=>'Page exceeds the scan limit',
        'page_unreadable'=>'Page could not be read',
    ];
    echo '<section class="pr-doc-health" aria-label="Documentation audit">';
    echo '<p class="pr-doc-health-status'.($fresh?'':' pr-doc-health-delayed').'"><strong>'.($fresh?'Audit current':'Audit delayed').'</strong> · Scanned '.hsc(gmdate('M j, H:i', (int)$data['scanned_at'])).' UTC · Verified inventory '.hsc($data['inventory_version'] ?? 'unavailable').'.</p>';
    if (empty($data['verification_current'])) echo '<p class="pr-doc-health-unavailable">Release verification is pending. The audit can still check links, but its version comparisons need review.</p>';
    echo '<div class="pr-doc-health-metrics">';
    foreach ([['Pages', $data['pages_scanned'] ?? 0], ['Links', $data['links_checked'] ?? 0], ['Media', $data['media_checked'] ?? 0], ['Action items', $data['action_count'] ?? 0], ['Review candidates', $data['review_count'] ?? 0]] as [$name, $number]) {
        echo '<div><span>'.hsc($name).'</span><strong>'.(int)$number.'</strong></div>';
    }
    echo '</div>';
    foreach (['action'=>'Action items', 'review'=>'Review candidates'] as $severity=>$heading) {
        $items = array_values(array_filter($data['issues'], fn($row)=>is_array($row) && ($row['severity'] ?? null) === $severity));
        echo '<details class="pr-doc-health-group"'.($severity === 'action'?' open':'').'><summary>'.$heading.' <span>'.count($items).'</span></summary>';
        if (!$items) echo '<p>No '.$heading.' found in this scan.</p>';
        else {
            echo '<ul>';
            foreach (array_slice($items, 0, 1000) as $item) {
                $code = $item['code'] ?? '';
                $page = $item['page'] ?? null;
                $target = $item['target'] ?? null;
                echo '<li><span>'.hsc($labels[$code] ?? 'Documentation check needs review').'</span>';
                if (is_string($page) && preg_match('/^[a-z0-9_:]+$/D', $page)) echo ' · <a href="'.hsc(wl($page)).'">'.hsc($page).'</a>';
                if (is_string($target)) echo ' · target <code>'.hsc($target).'</code>';
                echo '</li>';
            }
            echo '</ul>';
        }
        echo '</details>';
    }
    echo '<p class="pr-doc-health-note">Older review labels are a review queue, not proof that the article is wrong. This audit does not rewrite contributor prose or verify every gameplay claim.</p></section>';
}
