#!/usr/bin/env php
<?php
/* Revision-safe wiki-only publisher: fixed page allowlist, no bot execution. */
define('DOKU_INC', '/srv/poolrotator-wiki/dokuwiki/');
define('NOSESSION', 1);
require DOKU_INC . 'inc/init.php';
set_exception_handler(static function (Throwable $e) { fwrite(STDERR, $e->getMessage() . PHP_EOL); exit(1); });
$_SERVER['REMOTE_USER'] = 'wikidocs';
$plan = json_decode(file_get_contents($argv[1]), true, 64, JSON_THROW_ON_ERROR);
if (!preg_match('/^v\d+\.\d+\.\d+$/D', $plan['version']) || count($plan['changes']) > 2) throw new RuntimeException('Invalid plan');
$locked = [];
function currentRelease() {
    global $plan;
    if (($plan['source_kind'] ?? null) === 'owner_v7_registered') {
        $path='/srv/poolrotator-wiki/documentation/release.json';$stat=stat($path);
        if(is_link($path)||!$stat||$stat['uid']!==0||($stat['mode']&0022)||$stat['size']>1048576)throw new RuntimeException('Invalid registered-source attestation');
        $proof=json_decode(file_get_contents($path),true,64,JSON_THROW_ON_ERROR);$age=time()-($proof['observed_at']??0);
        if($age < -30 || $age > 60 || ($proof['confirmed']??null)!==true || ($proof['ready']??null)!==true
            || ($proof['source_kind']??null)!=='owner_v7_registered'
            || ($proof['active_version']??null)!==$plan['version']
            || ($proof['manifest_digest']??null)!==($plan['manifest_digest']??null)
            || ($proof['runtime_sha256']??null)!==($plan['runtime_sha256']??null)
            || ($proof['source_files_sha256']??null)!==($plan['source_files_sha256']??null))throw new RuntimeException('Registered-source proof changed or expired');
        return $proof['active_version'];
    }
    $target = readlink('/opt/poolrotator');
    if (!preg_match('#^/opt/poolrotator-releases/(v\d+\.\d+\.\d+)$#D', $target, $match)) throw new RuntimeException('Unexpected release link');
    if (($plan['source_kind'] ?? null) === 'rolling_worker') {
        $path = '/srv/poolrotator-wiki/documentation/release.json';
        $stat = stat($path);
        if (is_link($path) || !$stat || $stat['uid'] !== 0 || ($stat['mode'] & 0022) || $stat['size'] > 1048576) throw new RuntimeException('Invalid deployment attestation');
        $proof = json_decode(file_get_contents($path), true, 64, JSON_THROW_ON_ERROR);
        $age = time() - ($proof['observed_at'] ?? 0);
        if ($age < -30 || $age > 60 || ($proof['confirmed'] ?? null) !== true || ($proof['ready'] ?? null) !== true
            || ($proof['source_kind'] ?? null) !== 'rolling_worker'
            || ($proof['supervisor_version'] ?? null) !== $match[1]
            || ($proof['supervisor_version'] ?? null) !== ($plan['supervisor_version'] ?? null)
            || ($proof['manifest_digest'] ?? null) !== ($plan['manifest_digest'] ?? null)
            || ($proof['active_version'] ?? null) !== $plan['version']) throw new RuntimeException('Worker deployment proof changed or expired');
        return $proof['active_version'];
    }
    return $match[1];
}
try {
    if (currentRelease() !== $plan['version']) throw new RuntimeException('Release changed');
    foreach ($plan['changes'] as $change) {
        $id = $change['id'];
        if (!in_array($id, ['commands:start', 'wiki:feature_inventory'], true)) throw new RuntimeException('Page outside allowlist');
        if (strlen($change['text']) > 262144) throw new RuntimeException('Page limit');
        if (checklock($id)) throw new RuntimeException('Contributor is editing ' . $id);
        lock($id);
        if (checklock($id)) throw new RuntimeException('Could not lock ' . $id);
        $locked[] = $id;
        $file = wikiFN($id);
        $hash = is_file($file) ? hash_file('sha256', $file) : null;
        if ($hash !== $change['expected_sha256'] && $hash !== hash('sha256', $change['text'])) throw new RuntimeException('Concurrent edit to ' . $id);
    }
    if (currentRelease() !== $plan['version']) throw new RuntimeException('Release changed');
    foreach ($plan['changes'] as $change) {
        $file = wikiFN($change['id']);
        if (is_file($file) && hash_file('sha256', $file) === hash('sha256', $change['text'])) continue;
        saveWikiText($change['id'], $change['text'], 'Verified release inventory ' . $plan['version'], false);
        if (!is_file($file) || hash_file('sha256', $file) !== hash('sha256', $change['text'])) throw new RuntimeException('Revision write failed');
    }
} finally {
    foreach ($locked as $id) unlock($id);
}
echo 'Revision checks and commit completed.' . PHP_EOL;
