# NDDev OpenNetwork · cleaner

AGPL-3.0-only NDS module. [NDDev OpenNetwork](https://nddev.ai).

The compiled agent adapter observes the pinned native provider's diagnostics,
policy preview and completed run. Its manifest grants read/process capabilities.
`nds-cleaner-cache` owns opt-in local development cache maintenance on Linux x86_64
and macOS arm64. Installations and configuration stay outside the source checkout.

Managed Cargo intermediates are shared across projects, separated by complete
compiler identity and host. Final artifacts have separate workspace directories.
The wrapper selects each project's Rustup compiler, including `cargo +toolchain`;
its Cargo frontend uses qualified nightly use tracking and fine-grained locks. NDS builds
retain their pinned stable compiler. A fixed compiler installation rejects a
conflicting project toolchain file. Caller-selected compilers, prepared Cargo
homes and final/intermediate staging paths are honored. Invocation-local cache
configuration and nested wrapper scopes avoid inheriting a parent final-output
lock into build-script Cargo calls. Explicit staging outside managed roots stays
with its caller. Existing project lockfiles keep their owner.
`cargo clippy ...` and `cargo nextest ...` commands enter their native
tools with the managed Cargo frontend. Their Cargo children retain the same
scope; build-script recursion still receives a separate nesting scope. Clippy
uses the selected compiler's installed component; Nextest uses the existing
native command lookup. Native global offline/lock/config/color/verbosity and
`-Z` options before those commands are forwarded. These routes require
installed cache frontends.
Caller `--config` entries before or after a Cargo command share one native
matcher with generated defaults and retain their override order. Arguments
after `--` are forwarded unchanged.

Cleanup retains active shared leases, including native children surviving Cargo's
exit. An exclusive lease permits removal only of known native unit groups whose
use fingerprint is older than 72 hours. Unknown layouts, future timestamps and
unowned roots are retained. Capability-relative filesystem operations contain
symlink traversal. Cargo's native `clean gc -Zgc` owns source/archive/index/Git
cleanup with all five age limits set to three days. An optional existing native
Cargo home keeps its own credentials, installed programs and native cache locks.

uv generation v2 and Bun generation v3 are identified by the qualified provider version,
OS/architecture, canonical workspace and exact nearest `uv.lock` or `bun.lock`.
Offline/presentation flags and other command argv do not create a new store;
native uv/Bun own package, interpreter/ABI and index compatibility inside it.
Native directory/project selectors and uv's corresponding environment select
the workspace; selectors after `--` belong to the called program. Unlocked
operations use daily generations. Arguments and lock content are never
journaled. No cache copies, timestamp rewriting or installed-environment migration is
performed. uv retains an inherited lease. Linux Bun uses an owned native
subreaper guard because Bun can close inherited descriptors before launching
descendants. Foreground exit status returns immediately; the guard holds a
shared lease until all its descendants finish, including detached children.
A durable per-invocation pending record survives guard failure and prevents
collection. The guard waits for child exits without scanning or signalling
other processes. Legacy Bun generations and platforms without a qualified
descendant scope are retained as unknown. Cleanup observes completed use under
an exclusive lease before starting the configured idle period, so a long-running
command cannot become immediately stale on exit.
A regular hourly maintenance pass bounds this conservative observation delay.
uv's native cache clean and Bun's native age prune own payload removal; Bun keeps
its native store links. Installed environments, runtimes and live data stay with
their native owners. Filesystem modification time alone never establishes shard
inactivity.

Prepare the exact native binaries using `scripts/prepare-cache-tools.py` and an
absolute versioned destination outside the cache. `cache-providers.lock.json`
binds official dated Cargo archives and official uv/Bun release digests. The
installer extracts named regular executables and refuses an unrelated existing
installation. No nightly Rust compiler is installed.

Supply a private JSON file with `schema: 1`, absolute `root`, `cargo`, `rustc`,
`rustdoc`, and optional `rustup`, `rustup_home`, `cargo_home`, `frontends`, `uv`,
`bun` paths, plus an optional `environment_root`. The root must be canonical, user-owned and mode 0700. Initialization
requires an empty directory and creates an ownership marker. Existing caches are
not silently adopted. Optional `scan_entries` can lower the default 100,000-entry
pass budget to pace maintenance; values outside 1..100,000 are rejected. Very
small budgets may not reach an atomic unit. This never raises the discovery bound.
`environment_root` is an explicitly initialized, private installed-environment
store outside the collector root and on the same filesystem. For project
`uv run/sync/add/remove`, native `uv workspace dir --offline` selects a distinct
workspace environment there. Existing `.venv` directories are preserved. Explicit
`UV_PROJECT_ENVIRONMENT`, `--active`, scripts and non-project commands retain
their native ownership. Installed environments are never cache garbage.

Provider binary paths come from the verified installation;
`frontends` selects a dedicated local tool prefix. Generate its argv-preserving
Cargo/uv/Bun wrappers with `scripts/install-cache-frontends.py CONFIG BINARY`;
the prefix refuses unrelated existing files. When a native Rustup backend is
configured, its `run TOOLCHAIN cargo ...` invocation also enters Cleaner with
that compiler selection. Other Rustup operations delegate unchanged to the
native backend. This covers SDK helpers that invoke Rustup directly.

`retention_seconds` selects the operator's idle policy in whole native days,
from 86400 through 31536000 seconds. For a one-day policy set it to `86400`.
The same value governs known Cargo units, final targets, uv/Bun generations,
all five native Cargo source-GC ages and Bun's native prune age. Reports return
the effective value. An omitted setting retains the legacy three-day default.
The boundary remains strict: equal-age, future-time, pending-use, busy and
unknown objects are retained. Byte or free-space pressure never shortens it.

```sh
nds-cleaner-cache /operator/config/cache.json init
nds-cleaner-cache /operator/config/cache.json cargo test --locked
nds-cleaner-cache /operator/config/cache.json uv sync --python 3.15.1
nds-cleaner-cache /operator/config/cache.json bun install --frozen-lockfile
nds-cleaner-cache /operator/config/cache.json clean
nds-cleaner-cache /operator/config/cache.json clean --apply
nds-cleaner-cache /operator/config/cache.json clean --apply --passes 32
```

`clean` defaults to preview. JSON reports distinguish stale/removed native units,
native GC calls, active leases and completed-use observations. Discovery keeps
its entry and time limits, without charging native cleanup or use acknowledgments
against traversal time. Native provider calls retain their own process deadline.
Collection also has a one-minute pass budget, checked between atomic units;
a native provider already started retains its 30-second deadline. Exhausting a
pass yields `scan_complete: false` and defers source GC. Apply saves an owned,
atomic `.gc-progress` cursor; the next apply resumes after completed objects or
subtrees, rechecking current leases. A complete traversal clears the cursor.
`resumed` identifies continuation passes. Preview validates but neither uses nor
changes the cursor, and reports its own scan completeness. Scheduling may run
multiple bounded passes to converge; an incomplete pass is not a full cleanup.
The explicit `--apply --passes N` mode (1..32) returns all pass reports and exits
75 if that batch remains incomplete. Schedulers can request this bounded batch
instead of silently accepting a single incomplete pass. This CLI mode and its
report fields are introduced in 0.0.1-alpha.3; existing single-pass calls remain
supported.
Cargo incremental, dependency and example payload directories are retained without
traversal and counted as unknown; collection inspects recognized native unit
groups instead. Provider, ownership and filesystem failures remain errors;
progress is never substituted for a completed traversal. Scheduling and device paths belong to the operator's estate. This CLI
adds no remote mutation command.
Frozen release artifacts must be staged outside mutable caches.

Run `just check` and `just provider-check` with the real `NDS_TEST_CLEANER` path.
The legacy provider acceptance uses isolated state. `just cache-provider-check
BINARY TOOLS RUSTC RUSTDOC` exercises real cross-project Cargo reuse, native
use timestamps, stale artifact/source GC, native child leases, and uv/Bun cleanup
while preserving installed environments. Age simulation changes only the owned
temporary fixture. CI uses public GitHub-hosted runners; native cache acceptance
runs on its qualified architectures. There is no backup/recovery implementation.

Release journals: [GitHub Releases](https://github.com/NDDev-Technocracy-OpenNetwork-NDS/nddev-device-sync-cleaner/releases).


Native owner contention is independent of a frontend lease. Before acknowledging
uv completion or running its cache GC, maintenance probes uv's real cache lock.
Observed raw native activity becomes pending use; completion and the idle period
are acknowledged only after both locks are free. Cargo source GC also probes its
global download/mutation locks, including native jobs using other target roots.
Busy owners are deferred without interruption. A timeout race is classified as
busy only while the relevant native lock is still demonstrably held; other
native failures retain their provider, operation, reason and exit category.
No provider argv, private paths or raw provider output is rendered in that error.
The process timeout and scan budgets are unchanged and do not establish a
shorter consumer RPC deadline.

The existing main workflow also retains `nds-cache-native-Linux-X64` and
`nds-cache-native-macOS-ARM64` archives after actual native release-binary
qualification. Each contains a separately pinned compatible alpha.5 execution
binary and current alpha.6 maintenance binary, their source commits, the native
frontend helper, tool/compiler identities, license and ordinary `SHA256SUMS`
inside a tar archive that preserves native executable modes.
These are finite CI artifacts, not an installed selection or host acceptance.
Consumers preserve verified bytes under their existing artifact pin before
expiry and qualify their own configuration, policy, jobs and rollback. Windows
module tests do not qualify managed-cache deployment on Windows.
