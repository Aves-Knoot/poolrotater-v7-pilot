<?php
// Invented fixture only; maintained templates copied alongside it by test/preview.
define('DOKU_INC','/invented/');define('DOKU_BASE','/');
function hsc($v){return htmlspecialchars((string)$v,ENT_QUOTES|ENT_SUBSTITUTE,'UTF-8');}
function wl($v,$options=[]){return '/doku.php?id='.$v;}
function prAnalyticsFresh($v){return ($v['expires_at']??0)>time();}
function prAnalyticsNumber($v,$digits=0){return number_format($v,$digits);}
function prAnalyticsPhase($v){return ['PLAYING'=>'Playing','IDLE'=>'Waiting','STARTING'=>'Starting','OFFLINE'=>'Offline'][$v]??'Status unavailable';}
function prAnalyticsChoice($key,$choices,$default){return $default;}
function prAnalyticsUpdated($v){return '<p class="pr-updated">Invented fixture snapshot</p>';}
function rawWiki($v){return '';}
$input=json_decode(stream_get_contents(STDIN),true);$GLOBALS['fixture']=$input;
function prAnalyticsData($name){return $GLOBALS['fixture'][$name]??[];}
require __DIR__.'/nowplaying.php';require __DIR__.'/dashboard.php';require __DIR__.'/home.php';
if(($argv[1]??'dashboard')==='sidebar')prNowPlayingPanel();
elseif(($argv[1]??'dashboard')==='home')prHomePage($input['status'],[],[]);
else prDashboardStatus();
