# GitHub Synchronization

The independent application shares the existing public repository:
https://github.com/OneplumN/zgotify . Its development branch is
`feature/independent-platform`; implementation files live under `platform/`.
The original Android client and release workflows remain at the repository root.

## Boundaries

- Synchronize reviewed source, dependency locks, tests and sanitized documentation.
- The local `.github/workflows/platform-ci.yml` maps to the repository-root path
  of the same name, not to `platform/.github/`.
- Do not include artifacts, Gradle caches, local SDK paths, credentials, fixture
  journals, database snapshots, reports, AppleDouble files or signing keys.
- No release tag, deployment or production endpoint change is part of a source
  synchronization. Do not merge or rewrite the default branch automatically.
- Existing Android worktrees may contain unrelated local changes; do not stage
  or publish those changes as part of the independent platform.

## CI

The independent workflow runs Go race tests and vet, Python harness unit tests,
and an Android debug build/lint. It can retain the independent debug APK as a
one-day Actions artifact, but never creates or replaces a GitHub Release.

Database-dependent Go tests need an explicitly provisioned fresh test schema and
are skipped without its DSN. Public CI does not use the ECS, RDS, production
Gotify, real accounts or stored deployment secrets. The real MySQL/Gotify results
in `gotify-import-acceptance.md` are a separate, previously executed acceptance
stage, not proof of Android delivery or a claim that CI repeated those tests.

The independent APK has application ID `com.zgotify.app.internal`; it is not an
in-place upgrade of the original client. It still requires the independent API
and does not yet implement vendor push or reliable background delivery.
