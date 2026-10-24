set shell := ["bash", "-euo", "pipefail", "-c"]

check:
    cargo fmt -- --check
    cargo clippy --locked --all-targets -- -D warnings
    cargo test --locked

provider-check:
    python3 scripts/check-provider.py

# Native owners and compiler paths are supplied explicitly, never inferred as success.
cache-provider-check binary tools rustc rustdoc:
    python3 scripts/check-cache-provider.py --binary {{binary}} --tools {{tools}} --rustc {{rustc}} --rustdoc {{rustdoc}}
