set shell := ["bash", "-euo", "pipefail", "-c"]

# List the project commands.
default:
    @just --list

check:
    just --fmt --check
    cargo deny --locked check licenses sources
    cargo fmt -- --check

# Preview by default; add --apply to select, --rollback RECEIPT to restore.
select-profile artifact base *args:
    python3 scripts/select-profile.py --artifact {{ artifact }} --base-profile {{ base }} {{ args }}

provider-check:
    python3 scripts/check-provider.py

# Native owners and compiler paths are supplied explicitly, never inferred as success.
cache-provider-check binary tools rustc rustdoc:
    python3 scripts/check-cache-provider.py --binary {{ binary }} --tools {{ tools }} --rustc {{ rustc }} --rustdoc {{ rustdoc }}

# Fetch locked source dependencies without compiling.
setup:
    cargo fetch --locked

# Run the native workspace and tooling regressions.
test: build audit
    cargo clippy --locked --all-targets -- -D warnings
    cargo test --locked
    python3 -B -m unittest discover -s scripts -p 'test_*.py'

# Compile the locked release workspace.
build:
    cargo build --locked --workspace --release

# Check current RustSec advisories.
audit:
    cargo audit --deny warnings

# Run source checks, native tests and the advisory audit.
verify: check test audit build
