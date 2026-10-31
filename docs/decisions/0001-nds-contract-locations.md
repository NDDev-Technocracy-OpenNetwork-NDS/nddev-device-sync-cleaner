---
status: accepted
date: '2026-10-22'
adaptation:
  standard:
    id: std-0001
  rules:
  - R-0018
  paths:
  - cache-providers.lock.json
  - contracts
  - module.yaml
  - provider.lock.json
  - standarts.lock
  reason: NDS system ADR 0013 (accepted owner direction) keeps NDS-owned material at fixed top-level locations
    in member repositories.
  result: '`cache-providers.lock.json`: NDS contract locks pinning the standards source and the native
    provider. `contracts`: NDS compiled-in runtime manifest directory owned by the module. `module.yaml`:
    NDS module descriptor owned by the product standards corpus. `provider.lock.json`: NDS contract locks
    pinning the standards source and the native provider. `standarts.lock`: NDS contract locks pinning
    the standards source and the native provider.'
  verification: Review against the NDS root ADR 0013; the root catalog check validates NDS descriptors
    and locks, and the repository's own checks exercise the remaining paths.
---
# NDS contract locations

The owner accepted NDS system [ADR 0013](https://github.com/NDDev-Technocracy-OpenNetwork-NDS/nddev-device-sync/blob/main/standarts/decisions/0013-estate-hygiene-and-standards-adoption.md). It keeps NDS-owned material at
deliberate top-level locations in member repositories. This record scopes
that decision to this repository under std-0001 R-0037 and replaces the
earlier manifest variations without changing them.

- `cache-providers.lock.json` (lock): NDS contract locks pinning the standards source and the native provider.
- `contracts` (manifest): NDS compiled-in runtime manifest directory owned by the module.
- `module.yaml` (manifest): NDS module descriptor owned by the product standards corpus.
- `provider.lock.json` (lock): NDS contract locks pinning the standards source and the native provider.
- `standarts.lock` (lock): NDS contract locks pinning the standards source and the native provider.
