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

Cleanup retains active shared leases, including native children surviving Cargo's
exit. An exclusive lease permits removal only of known native unit groups whose
use fingerprint is older than 72 hours. Unknown layouts, future timestamps and
unowned roots are retained. Capability-relative filesystem operations contain
symlink traversal. Cargo's native `clean gc -Zgc` owns source/archive/index/Git
cleanup with all five age limits set to three days. An optional existing native
Cargo home keeps its own credentials, installed programs and native cache locks.

uv and Bun cache generations are identified by workspace, exact lock content and
command arguments; unlocked operations use daily generations. Arguments are
hashed, never journaled. Each generation has an inherited lease. Cleanup observes
completed foreground use under an exclusive lease before starting its three-day
idle period, so a long-running command cannot become immediately stale on exit.
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
`bun` paths. The root must be canonical, user-owned and mode 0700. Initialization
requires an empty directory and creates an ownership marker. Existing caches are
not silently adopted. Provider binary paths come from the verified installation;
`frontends` selects a dedicated local tool prefix. Generate its argv-preserving
Cargo/uv/Bun wrappers with `scripts/install-cache-frontends.py CONFIG BINARY`;
the prefix refuses unrelated existing files. When a native Rustup backend is
configured, its `run TOOLCHAIN cargo ...` invocation also enters Cleaner with
that compiler selection. Other Rustup operations delegate unchanged to the
native backend. This covers SDK helpers that invoke Rustup directly.

```sh
nds-cleaner-cache /operator/config/cache.json init
nds-cleaner-cache /operator/config/cache.json cargo test --locked
nds-cleaner-cache /operator/config/cache.json uv sync --python 3.15.1
nds-cleaner-cache /operator/config/cache.json bun install --frozen-lockfile
nds-cleaner-cache /operator/config/cache.json clean
nds-cleaner-cache /operator/config/cache.json clean --apply
```

`clean` defaults to preview. JSON reports distinguish stale/removed native units,
native GC calls, active leases and completed-use observations. Discovery keeps
its entry and time limits, without charging native cleanup or use acknowledgments
against traversal time. Native provider calls retain their own process deadline.
Cargo incremental, dependency and example payload directories are retained without
traversal and counted as unknown; collection inspects recognized native unit
groups instead. A bounded failure is an error, never a fabricated successful
cleanup. Scheduling and device paths belong to the operator's estate. This CLI
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
