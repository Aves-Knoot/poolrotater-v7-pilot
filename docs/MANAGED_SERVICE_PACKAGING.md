# v7.2.9 central service packaging repair

This is a hub/Discord assembly repair, not another room-runtime release.
Existing client installations keep the original v7.2.9 runtime and installer
digests. Community operators do not need to reinstall or run a repair command.

The compact central assemblies originally omitted `bot/mode.py` and
`bot/custom_greetings.py`. Their lazy imports are needed by managed mode
validation and greeting defaults. A valid settings upload could therefore get
HTTP 503 while ordinary heartbeats and room operation continued normally.

The repaired central service archive adds those two exact, already-audited
modules from the published runtime. All original service files remain byte
identical; each component has a new complete inventory. The owner hub and
community Discord bridge use these repaired assemblies. Lobby workers and
hosting-account credentials are unaffected. The source builder and installer
allowlist are corrected so future freshly built hub kits include these modules.

The release includes `poolrotater-v7.2.9-central-services-deps1.tar.gz`,
`CENTRAL_SERVICE_VALIDATION.json` and `CENTRAL_SERVICE_SHA256SUMS.txt`.
The archive contains separate `hub/` and `discord/` code inventories. These are
central service code, not a host installer and not an automatic client update.
Central administrators must verify the archive checksum and exact component
inventory before staging it in a fresh protected prefix; retain existing hub
state, TLS material, Discord configuration and credentials. Restarting central
services does not require restarting rooms. Do not replace host runtime files
or change the signed client-update metadata for this packaging repair.

Validation covered 16 checks against fresh isolated interpreters and the same
16 checks through actual HTTPS on the donor using protected, unprivileged
systemd services. They cover every supported catalog mode, page replay, private
password redaction and greeting defaults. A separate fresh kit built with the
corrected source passed actual `install_assets` and an unprivileged protected
bootstrap/TLS check on the donor. A source regression test prevents the missing
dependencies recurring. No actual osu! rooms or Discord messages were used in
these checks. Unchanged client suites were not repeated for a dependency-only
central assembly change.
