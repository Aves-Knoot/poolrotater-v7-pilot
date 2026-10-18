# v7.1.11 operator guide

## Existing healthy installations

Keep your current instance, state and saved update policy. The signed updater accepts the reviewed v7.1.10 predecessor and queues a controller transition. It waits for all configured rooms on your host to empty naturally, then preserves operator state and restores the previously running network service. Hosts on earlier releases should complete their previously offered supported transitions first.

The central hub is upgraded before the signed client offer. Both upgrade orders remain compatible. No new OAuth sign-in, enrollment or catalog rebuild is required. The root-owned network service unit gains nice 17 and membership in the shared half-core transfer slice at its next supported start. Native room workers stay outside that slice.

Use `status`, `support`, `network-support`, `ratings-support` and update status through your existing protected admin entry to review progress. Privacy/consent opt-outs and paused policies remain effective. A deliberately stopped or held host is not remotely started by publication. Retain state on an uncertain ownership outcome; use the supported ownership review instead of reinstalling, deleting journals or repeating room creation.

## Statistics and transfers

The network agent downloads a complete public player totals projection for Standard and Mania. The initial download can take longer; later unchanged checks transfer metadata only. Ordinary snapshots refresh around 20 minutes. Privacy checks occur every 30 seconds and must remain fresh within the 120-second lease; a disconnected host therefore stops serving shared totals until policy can be verified again. Local history is retained.

A negotiated new hub permits successful backlogs to drain without fixed interbatch delays. Old or unrecognized hubs retain legacy conservative upload pacing. Socket timeouts, bounded request/page sizes, concurrency limits, authorization and per-player anti-abuse checks remain. The background transfer slice shares 50% of one CPU across its participating services, at nice 17; this is not a network bandwidth cap.

## Fresh installations and recovery

Download the installer ZIP, not GitHub's source ZIP. Verify RELEASE_SHA256SUMS.txt, then use install.py with runtime.zip and follow LIVE_AUTHORIZATION.md and NETWORK_OPERATOR_GUIDE.md. Configure your own account, import the full signed catalogs, and enroll with your bound invitation. One Standard Auto room is the default. No local map research pipeline is needed.

The optional repair launcher preserves an existing supported installation and makes at most the explicitly requested startup attempt. It does not bypass unknown ownership. Retain reports and state if it refuses. Older incident guides are historical; this guide describes the current release.
