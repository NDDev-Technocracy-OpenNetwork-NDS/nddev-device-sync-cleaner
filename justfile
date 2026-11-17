set shell := ["bash", "-euo", "pipefail", "-c"]

# List the maintained source and native commands.
default:
    @just --list

# Check command and Rust formatting without compilation or test execution.
check:
    just --fmt --check
    cargo fmt -- --check

# Preview by default; add --apply to select, --rollback RECEIPT to restore.
select-profile artifact base *args:
    python3 scripts/select-profile.py --artifact {{ artifact }} --base-profile {{ base }} {{ args }}

# Run the isolated acceptance against the explicit real provider binary.
provider-check:
    python3 scripts/check-provider.py

# Native owners and compiler paths are supplied explicitly, never inferred as success.
cache-provider-check binary tools rustc rustdoc:
    python3 scripts/check-cache-provider.py --binary {{ binary }} --tools {{ tools }} --rustc {{ rustc }} --rustdoc {{ rustdoc }}

# Run the original Python cases and complete workspace compiler/test scope.
test:
    python3 -B -m unittest discover -s scripts -p 'test_*.py'
    cargo clippy --locked --workspace --all-targets -- -D warnings
    cargo test --locked --workspace

# Retain blocking advisory checks for the full CI gate.
ci-extras:
    cargo audit --deny warnings

# Retain source and license policy for the full CI gate.
ci-policy:
    cargo deny --locked check licenses sources

# Build the retained workspace release artifact.
ci-build:
    cargo build --locked --workspace --release

# Run source, native, policy, advisory and release-build checks once.
verify:
    just check
    just test
    just ci-policy
    just ci-extras
    just ci-build

# Format only the command interface.
fix:
    just --fmt
