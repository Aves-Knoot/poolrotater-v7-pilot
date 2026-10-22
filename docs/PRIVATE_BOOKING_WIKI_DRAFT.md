# Private lobby bookings

**Draft for review. Private bookings are not currently published or enabled.**
The current released baseline is v7.2.13. The v7.3.0 candidate has been built reproducibly and passed isolated/donor synthetic qualification. It has not been signed, published or accepted through a real osu! pilot. This page is
not a promise that private bookings are available on any host.

Private bookings let an osu! player request an explicitly designated private
host room for a session. They do not reserve a public room or make a public
lobby private. Both the booking website and the individual host must be
explicitly enabled before a slot can appear as available. Legacy hosts and
hosts without the booking opt-in remain disabled; there is no fallback to a
public room.

## Reserving and joining

When enabled, the website lists only safe slot names, availability, supported
presets, and whether the host report is fresh. Sign in with osu! identify, pick
an available slot and preset, and reserve it. A reservation is private to its
owner. The room may need time to prepare; its status remains unavailable to
other players until the host confirms it is ready.

Once ready, the owner or the friend explicitly designated during reservation
must join within 10 minutes. If neither arrives, the host requests closure and
the slot remains held until cleanup is confirmed. The designated friend can
claim the room but is still a Player until the owner grants Controller access.
Other friends may join and play after the room is claimed.

An owner can create a revocable invite for joining. Invitees see only the join
details. They do not receive the settings panel, roster, booking controls, or
controller role. A replacement invite revokes the previous one; invitations
expire after 30 minutes. Do not post a private invitation publicly or share it
beyond the people the owner intends to invite.

## Roles and controls

| Role | What the role can do |
| --- | --- |
| Owner | Configure supported settings; start rotation; abort or skip a map; grant or revoke Controller access; create invitations; end the booking. |
| Controller | Configure supported settings; start rotation; abort or skip a map. The owner retains role and booking control. |
| Player | Join and play, use ordinary player commands and votes. No settings, booking, or role controls. |
| Invited guest | View join details from the invite. No roster, settings, or control panel. |

The existing native operator and global moderation controls remain in force.
Booking roles apply only to that booking and do not create global bot admins
or moderators. Revoking a Controller takes effect immediately, including for
queued actions that have not started.

Owners and Controllers can use the website settings panel or supported in-game
controls. In-game rotation controls include `.start`, `.abort`, `.skip`, and
owner-only `.end`. Depending on the preset, supported settings commands
include `.range <min> <max>`, `.easier`, `.harder`, `.dt`, `.nm`, `.style
<name>`, `.countdown <seconds>`, `.autostart <seconds>`, `.minplayers <count>`,
and `.length <seconds>`. Unsupported commands remain unavailable in a private
booking. The website accepts only the preset's server-provided settings
inventory. Edits remain a draft until Apply. Apply has a shared 10-second
cooldown, and stale settings revisions are rejected instead of overwriting
newer settings.

## Session timers

After the owner or designated friend claims the ready room, ordinary verified
gameplay can continue the session. A completed map or at least 60 seconds of
verified participation renews the gameplay timer. Quickly starting and
aborting a map does not count as continued gameplay.

After 30 minutes continuously empty or without meaningful gameplay, the host
warns the room and requests closure. A verified map already in progress may
finish within its bounded expected duration and a 120-second grace period; a
stuck playing state cannot extend the room indefinitely. Unknown or stale host
observations pause expiry and keep the slot reserved. The room is not released
or reused until closure and cleanup are confirmed.

## Presets and settings

The planned presets are Standard Auto, Standard Fixed, Standard Loved, Mania
4K, Mania 7K, and Catch. Each is offered only when that host has the complete,
verified catalog required for it. A missing or incomplete catalog means that
preset is unavailable.

Settings vary by preset. Standard Auto and Fixed can expose Standard DT mode
through the dedicated selector. Standard does not offer clock-rate mods or
Mania-only FI/MR choices. Catch uses its fixed ruleset settings. Available
selection filters are limited to metadata that the complete catalog qualifies:
map length is supported for all planned presets; AR filtering is unavailable
for Mania and current Catch catalogs; OD and BPM are not available. The portal
does not estimate or invent missing metadata.

## Privacy and statistics

The public website and public status panels never show a private room ID, join
link, password, roster, current map, or queue. The owner and Controllers see
the private session panel. Invited guests see only the information needed to
join. Private session details are not a public community announcement.

Private bookings do not change the existing statistics collector or its
consent and visibility rules. Booking information is not added to public
`.stats` results.

## When status is uncertain

If the host report is stale or room ownership, roster, closure, or cleanup is
uncertain, the slot stays unavailable. The operator retains the booking and
ownership records while investigating. Do not ask an operator to delete state,
stop a live room, or reuse the alias to clear a warning.

## Version availability

| Version state | Private booking behavior |
| --- | --- |
| Released v7.2.13 baseline | Private bookings are not part of the published feature set. |
| Proposed v7.3.0 candidate | Development target only; unavailable until candidate qualification, human approval, and separate publication approval. |
| Older or mixed-version host without the booking capability | Booking remains disabled or the slot is unavailable. No public-room fallback is used. |
| Future approved release with explicit host and portal opt-ins | The feature may be offered only for compatible hosts, complete catalogs, and configured private slots. |

This draft should be published only after the exact released candidate and
pilot behavior are accepted, the site owner authorizes publication, and the
version table is updated to match the artifact that is actually available.
