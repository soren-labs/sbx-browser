# Hosted GitHub connection

The hosted Integrations page connects GitHub independently of Modal and AI.
`/hosted/connections/github/authorize` begins installation; the authenticated
callback consumes an expiring state owned by the current user. Installation
metadata uses the existing `InstallationRecord` format in PostgreSQL, scoped by
user. `/hosted/repositories` enumerates only that user's current explicit repo
selection. GitHub remains a repository connection, not a login provider.

`HostedGitHubService` reuses `GitHubAppService` for validation, repository-scoped
installation token minting and revocation. Hosted legacy GitHub routes resolve
the same user-bound service. Anonymous operator callbacks and App manifest
configuration cannot change a hosted user's installation. All-repository installs
are enumerated into explicit repository selection instead of authorizing every
future repository on an account.

Repository probes refuse repositories outside the user's installations. Existing
revision delivery, independent review and merge gates are reused, with per-repo
server credential resolution. Hosted sandbox exec replaces the operator GitHub
bridge with the current user's repo-scoped short-lived installation token.
Private App keys and longer-lived credentials stay on the VPS; connections use
the shared encryption vault. Tokens do not enter metadata or API responses.

In `SBX_CONNECTIONS_MODE=mock`, deterministic user-bound fake installations have
disjoint repositories. The fake GitHub remote stores upstream branch/PR/comment/
merge state durably for credential-free workflow validation. It exercises the
existing revision service; the review gate and exact-head comparisons still run.

For production, inject `github_factory` into `create_app`. Its client must
validate the authenticated GitHub authorization and enumerate **that user's**
verified installations, including callback installation access, rather than
returning every installation of the public SBX App. Use the existing App client
for scoped minting. Any long-lived user/broker credential must be kept in the
connection vault. Until that adapter is configured, installation is explicitly
unavailable. Real installation/permission changes, mint expiry and push/PR
acceptance require credentials; no external broker is silently used in hosted
mode.
