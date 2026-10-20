# Existing-hub enrollment and website integration — v7.2.6

This connects the operator to the existing poolrotater.com hub. Do not initialize
or serve a separate hub. For a new Loved installation, follow LOVED_OPERATOR_GUIDE.md
in order. Existing enrolled hosts keep their identity and do not enroll again.

1. Install the selected presets and import their full signed catalogs. Complete
   your own local authorization using LIVE_AUTHORIZATION.md. No room starts yet.
2. Return the hosting username/ID, public label, instance/version/runtime SHA,
   installation_id and generation, and directory/sharing/update preferences.
3. Obtain the maintainer's expiring PRIVATE download link. The package contains
   public hub trust/configuration plus a private invitation bound to that account,
   installation, generation, operator identity and directory choice. Never send
   it, tokens, OAuth credentials or raw private configuration in support output.
4. Follow the package README using the current protected administrator entry:
   network-configure, then network-enroll with the bounded JSON on private stdin.
   Do not put invitations in argv, environment variables, Git or shell tracing.
   A lost reply uses network-status/network-resume, not a new identity or reset.
5. Return host_id and redacted network-support. The maintainer checks the binding
   and grants ratings contribution separately. That grant is not implied by
   directory enrollment; fresh votes need no historical-seed permission.
6. Finish the package's local ratings capture/sharing/reader and network-agent
   setup. Reporting defaults include shared statistics, statistics consumption,
   sanitized operational logs and public-room chat/commands; credentials are
   excluded. Private-room suppression and player privacy choices remain.
   Preserve explicit opt-outs. Automatic signed updates default on when enrolled
   network-start bootstraps a missing updater; existing pauses/stopped intent win.
7. Configure/install the bot unit, check clean ownership, then start once. Return
   public room links and redacted support/network-support/ratings-support/update-status.
   Verify actual website listing; a remembered heartbeat alone is not that proof.

The website is the existing central site, not a web server installed on the
operator machine. Loved uses the compatible Standard fixed wire type and is
identified by its title and Loved selection style. Existing hub protocols,
canonical map feedback and Standard player statistics continue to apply.

The network agent is independent of gameplay. Its support output and operational
logs explain connection/retry state. Do not restart rooms merely to refresh the
website. Choose boot enablement explicitly; installation alone starts/enables no
bot. Controller updates wait for all configured local rooms to empty; compatible
worker-only updates use their existing independent empty-room gates.

For an already enrolled account changing machines, coordinate a bound transfer/
revocation instead of creating a second active host. No OAuth credentials are
needed centrally. When ownership or startup is uncertain, preserve state and send
redacted diagnostics before any repeated room-creation attempt.
