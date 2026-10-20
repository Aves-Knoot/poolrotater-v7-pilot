#!/usr/bin/env php
<?php
/* Create only a new private release review page, using DokuWiki revisions. */
define('DOKU_INC', '/srv/poolrotator-wiki/dokuwiki/');
define('NOSESSION', 1);
require DOKU_INC . 'inc/init.php';
set_exception_handler(static function (Throwable $e) { fwrite(STDERR, $e->getMessage() . PHP_EOL); exit(1); });
$_SERVER['REMOTE_USER'] = 'wikidocs';
$plan = json_decode(file_get_contents($argv[1]), true, 64, JSON_THROW_ON_ERROR);
if (!preg_match('/^v\d+\.\d+\.\d+$/D', $plan['version']) ||
    $plan['page_id'] !== 'drafts:release_' . str_replace('.', '_', $plan['version']) ||
    strlen($plan['text']) > 262144) throw new RuntimeException('Invalid draft plan');
$target = readlink('/opt/poolrotator');
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


} elseif (($plan['source_kind'] ?? null) === 'rolling_worker') {
    $path = '/srv/poolrotator-wiki/documentation/release.json';
    $stat = stat($path);
    if (is_link($path) || !$stat || $stat['uid'] !== 0 || ($stat['mode'] & 0022) || $stat['size'] > 1048576) throw new RuntimeException('Invalid deployment attestation');
    $proof = json_decode(file_get_contents($path), true, 64, JSON_THROW_ON_ERROR);
    $age = time() - ($proof['observed_at'] ?? 0);
    if ($age < -30 || $age > 60 || ($proof['confirmed'] ?? null) !== true || ($proof['ready'] ?? null) !== true
        || ($proof['source_kind'] ?? null) !== 'rolling_worker' || ($proof['active_version'] ?? null) !== $plan['version']
        || ($proof['manifest_digest'] ?? null) !== $plan['manifest_digest']
        || ($proof['supervisor_version'] ?? null) !== ($plan['supervisor_version'] ?? null)
        || $target !== '/opt/poolrotator-releases/' . ($plan['supervisor_version'] ?? '')) throw new RuntimeException('Worker deployment proof changed or expired');
} elseif ($target !== '/opt/poolrotator-releases/' . $plan['version']) throw new RuntimeException('Release changed');
$id = $plan['page_id'];
if (checklock($id)) throw new RuntimeException('Draft is being edited');
lock($id);
try {
    if (checklock($id)) throw new RuntimeException('Could not lock draft');
    $file = wikiFN($id);
    if (is_file($file)) {
        if (hash_file('sha256', $file) !== hash('sha256', $plan['text'])) throw new RuntimeException('Existing draft preserved for review');
    } else {
        saveWikiText($id, $plan['text'], 'Prepare verified release review packet ' . $plan['version'], false);
        if (!is_file($file) || hash_file('sha256', $file) !== hash('sha256', $plan['text'])) throw new RuntimeException('Draft revision failed');
    }
} finally { unlock($id); }
echo "Private draft ready.\n";
