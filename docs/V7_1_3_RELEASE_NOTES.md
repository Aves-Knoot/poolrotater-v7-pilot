# v7.1.3 — room-worker responsiveness

Live chat already uses the osu! WebSocket. This patch reduces avoidable waiting
after messages have arrived at a room worker:

- Process queued live commands before a due HTTP chat keepalive.
- Handle room chat before routine AI-result delivery, Discord command draining,
  queued outgoing messages, deferred profile enrichment and public-status work.
- Stop that loop pass's background work if a chat command stops the room.
- Add bounded, sampled command-handler duration summaries to identify slow
  processing without including player identifiers or command text.

Referee events, configuration, exclusions and shutdown requests are checked before
chat. Reconnect history catch-up still precedes the new live batch; message order,
deduplication, vote/map context, fallback polling, failure handling and API pacing
are preserved. No new concurrent room mutations or background API threads are
introduced. Shared scheduling and the separate unproven refresh failure are not
changed; this is not a guarantee of instantaneous starts or replies.

The runtime changes only `bot/bot.py` over exact v7.1.2. Controller/provider code,
storage, dependencies and credentials are unchanged. Compatible room workers
update independently once safely empty; occupied siblings continue running.
See [the operator guide](V7_1_3_OPERATOR_GUIDE.md) for prerequisites and reporting.

Validation evidence and final publication hashes are recorded alongside the
release. Real-world response-time improvements require observation after rollout.
