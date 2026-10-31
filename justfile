set shell := ["bash", "-euo", "pipefail", "-c"]

check:
    cargo fmt -- --check
    cargo clippy --locked --all-targets -- -D warnings
    cargo test --locked
    python3 -B -m unittest discover -s scripts -p 'test_select_profile.py'

# Preview by default; add --apply to select, --rollback RECEIPT to restore.
select-profile artifact base *args:
    python3 scripts/select-profile.py --artifact {{artifact}} --base-profile {{base}} {{args}}

provider-check:
    python3 scripts/check-provider.py

# Native owners and compiler paths are supplied explicitly, never inferred as success.
cache-provider-check binary tools rustc rustdoc:
    python3 scripts/check-cache-provider.py --binary {{binary}} --tools {{tools}} --rustc {{rustc}} --rustdoc {{rustdoc}}
