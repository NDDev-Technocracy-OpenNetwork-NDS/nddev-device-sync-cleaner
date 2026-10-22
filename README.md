# NDDev OpenNetwork · cleaner

AGPL-3.0-only NDS adapter. [NDDev OpenNetwork](https://nddev.ai).

Read native diagnostics, preview the owning policy and inspect the last completed run. No cleanup operation is exposed.

`contracts/module.json` is the canonical compiled-in runtime descriptor;
`module.yaml` identifies its repository package. Caller-owned local/process
capability handles share a bounded agent admission budget. Native responses,
private paths and content are never Debug or telemetry payloads.

Run `just check`, then `just provider-check` with the explicit `NDS_TEST_*`
provider paths used by CI. Acceptance exercises the real pinned native owner
in isolated temporary state. Linux provider acceptance and portable source
checks are distinct; an absent provider is unavailable, not a successful stub.
There is no backup/recovery implementation or private estate configuration.

Release journals live in [GitHub Releases](https://github.com/NDDev-Technocracy-OpenNetwork-NDS/nddev-device-sync-cleaner/releases).
