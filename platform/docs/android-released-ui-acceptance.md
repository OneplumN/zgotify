# Released UI Integration Acceptance

Date: 2026-11-12. Build: `0.3.0-internal`, code 3,
package `com.zgotify.app.internal`.

## Implemented

- Adapted the released message adapter, message-row layout and Markwon renderer;
  retained the bundled Gotify icon and MIT notice. Provenance and APK/source
  limitations are in `android-source-provenance.md`.
- RecyclerView, pull-to-refresh, rendered details, date toggle, copying, and
  personal read/hide/favorite actions use the existing personal API/cache engine.
- Old account adapters and callbacks are fenced by profile and generation.
- API list/detail derives a whitelisted `content_type` from stored Gotify import
  metadata without exposing extras. Direct ingestion and absent/unknown types
  remain plain. No MySQL schema change or Gotify modification is required.
- Room schema 2 adds the type without deleting cached messages, pending writes,
  versions or account authorization. Generated schemas 1 and 2 are retained.
- Conservative Markdown complexity limits fall back to literal text; only HTTP
  and HTTPS links without embedded credentials may launch a browser.

## Local Verification

- Android: 90 tests passed, including real SQLite 1-to-2 migration, persisted
  Markdown type, personal account separation, row recycling, safe links, extreme
  nesting and bounded Markdown fallback, and pagination protection while a request
  is busy.
- Debug assembly and lint passed: 0 errors, 13 warnings. Warnings include newer
  dependency versions, backup-policy guidance and existing localization/KTX
  suggestions; this is not a warning-free or security-certified release.
- Backend: `go test -race -count=1 ./...` and `go vet ./...` passed. Dedicated
  MySQL integration tests were skipped because no test DSN was supplied this run.
- Gotify import harness: 19 unit tests passed. These are harness tests, not a
  repeat of the previous real Gotify/MySQL import acceptance.
- Deployed Android harness: 54 unit tests passed, including same-row parsing
  of the reused UI. They do not exercise the ECS or create real accounts.

Run Android builds from an APFS source mirror when the working directory is on
exFAT. AppleDouble files in Gradle intermediates caused an initial build failure;
the source checkout and other user worktrees were not cleaned or reset.

The emulator harness requires Python 3.9 or newer. One initial run accidentally
used Python 3.8, failed before acceptance and was not counted as successful.

## Emulator Verification

The final APK passed all eight checks against a loopback-only synthetic HTTP
fixture on a disposable Android 36 emulator. This is not the deployed ECS/MySQL API. The
harness covers login, rendered Markdown vs literal text, offline force-stop and
reopen, queued write acknowledgement, account isolation, cancellation, explicit
version conflict handling, membership revocation and logout. It also captures
320dp and 360dp / 1.3-font-scale layouts.

Final evidence directory: `artifacts/android-released-ui-20261112-final/`.
The evidence is local-only and excluded from the public repository.

## Distribution and Limits

Local APK SHA-256:
`462609a67a26f2f32249ab420e2b9059b488e17f201a9b237533344f58f33c1f`.

Local APK MD5 (transfer checksum, not a security signature):
`2eb8b570903f9b002e69bff40312fec5`.

The existing draft PR receives source only. No release tag, default-branch merge,
production deployment, ECS image replacement or database mutation is part of this
increment. CI produces a separately signed debug artifact; its checksum can differ.

At this UI increment's original verification, the deployed older API did not
supply `content_type`, and the app fell back to plain text. The subsequent,
separately scoped isolated API deployment and new Android 0.3 acceptance are
recorded in `android-render-deployed-acceptance.md`. Existing personal-state
endpoints remain compatible.
Remote images/source icons, legacy settings/log migration, vendor push, physical
phones, reliable background notification delivery and four-node acceptance remain
unfinished. The earlier 0.2 deployed-device results are not claimed for this UI.
