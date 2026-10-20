# v7.2.8 — matching owner and community Discord presentation

Owner Discord slots1–4 now match the newer community presentation. Channel names
and panel titles reflect Catch, Standard automatic/fixed difficulty or4K/7K Mania.
Existing channels, status message IDs, role permissions and command routes stay
in place. This changes Discord presentation, not osu! room titles or settings.

Owner panels add central public artwork, room settings, the next queued map and
qualified estimated map progress. The selected room/map must match the fresh
public feed; delayed, missing, malformed or mismatched data suppresses enrichment.
Actual gameplay authority never comes from this display feed. Fixed-mod timing
uses the existing adjusted website duration. Playing panels refresh at most once
per20seconds; unchanged idle panels refresh once perminute. No extra osu! API
requests, Discord connections, tokens or remote control routes are added.

All v7.2.7 community Discord integration remains: reserved owner slots1–4,
persistent host/alias community slots5+, host/type labels, dynamic discovery of
new lobby-N-community channels, authenticated expiring native-room commands,
credential separation and isolated background delivery. See COMMUNITY_DISCORD_GUIDE.md.

This is a controller update, reviewed for both v7.2.6 and v7.2.7 predecessors.
Normal updates wait until all configured rooms on THAT host are empty. No
occupied room is forced down. Configuration, OAuth, catalogs, history, enrollment,
explicit sharing/update choices and custom osu! titles remain unchanged.

Validation:20 focused presentation/Discord regressions and protected full-catalog
owner27→28, owner26→28 and community26→28 upgrades passed. The parent release's
117 relay/network tests and real installed native command/gameplay qualification
remain applicable; unchanged long fixtures/full repository suites were not
repeated. See VALIDATION.json for exact artifacts and limitations. Production
player commands are not injected as acceptance tests.
