# Independent Standard styles and short remaining-time command

Included in v7.1.9. The combined historical
statistics repair makes this a controller update requiring all configured rooms
on a host to be empty, although the style changes alone were worker-compatible.
Publication, queued delivery and observed activation remain separate facts.

Standard Ranked players can mix enabled Farm, Stream and Jump styles instead of
replacing one with another. `.aim` is a new alias for the existing `.jump` command;
it uses the same verified Jump analysis, not a different classifier. Styles mix
as alternatives, so a chart need not qualify as both Stream and Jump.

```text
.stream on
.jump on       (or .aim on)
.dt on
```

This chooses from Stream and Jump maps with the existing mixed DT policy. DT is
independent: changing styles keeps its current choice, and changing DT keeps the
style selection. `.dt 100` retains its existing DT-first/NoMod-fallback behavior.
Plain `.dt` still enables mixed DT; `.dt off` and `.nm` disable DT through the
existing safe playlist/countdown handling.

Bare `.farm`, `.stream`, `.jump` and `.aim` toggle that style. Explicit `on`/`off`
forms are idempotent. `.stream off` leaves Jump enabled in the example above.
Disabling the last style returns to Variety and its last selected farm/stream
mix. `.variety` clears individual style selections using the existing 50/50 mix;
`.variety 70 30` retains its percentage syntax. Neither resets the DT choice.
These controls require an active player or moderator/admin in the room. Existing
public-command suppression, flood guards and custom prefixes still apply.

Style changes affect future picks; the running map and visible queue stay put.
The pool retains existing stars, length/age/pass limits, sparse-pool policy,
ratings, song spacing, reservations and exclusions. Enabling a style with no
eligible maps keeps the previous pool. Mixed source capacity remains bounded;
overlap maps are deduplicated separately for NM and DT. Jump's existing soft
duration/AR and strict/relaxed preferences apply when choosing its bucket. No
provider requests or concurrent mutation threads are added to style handling.
Catalog source rebuilds retain active style/DT choices when compatible.

Existing Mania styles and combinations are unchanged. Loved/fixed-mod legacy
pools keep their existing source controls; independent Standard combinations
use the normal StandardPool prepared-catalog path.

`.t` aliases `.time` in Standard and Mania, including custom prefixes. It has
the same read-only estimate, public-command permissions, flood budget and nine
personal reply translations. `.help t` and `.help aim` resolve their canonical
commands; `.help stream`/`.help jump` explain the on/off controls.

Validation receipts are under `validation/v7.1.9-style-controls/`. They include
affected command/language/selection regressions and the full prepared catalogs
(105502 Standard, 19334 4K, 4347 7K rows). Native process tests use invented chat,
referee and Discord boundaries; no production credentials or services are used.
Release packaging, signed delivery and natural worker rollout remain a separate
delivery step; this development does not replace or interrupt the queued v7.1.8
controller update.
