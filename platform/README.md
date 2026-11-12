# zgotify Platform

Independent notification application built alongside the existing Gotify clients.

## Delivery Status

Internal implementation in progress. Not a production release.

Upstream compatibility and acceptance baseline: **Gotify Server 2.9.1**. The
independent application does not require a Gotify 3.x upgrade or changes to
Gotify's database. Existing production Gotify/iGotify deployments stay unchanged.

- Go API implements personal accounts, topic membership, per-person state,
  cross-device session storage, idempotent ingestion and transactional audit.
- Local Go race tests and vet pass. Real MySQL 8.4.13 integration passed on the
  isolated ECS, along with ten HTTP acceptance groups and a database/API restart
  check. RDS and multi-node acceptance are still pending.
- The separate Android 0.2 internal app has person-scoped Room storage, a durable
  offline state queue, bounded foreground retries and explicit conflict handling.
  All 77 Android unit tests pass; build/lint pass with no errors and seven warnings.
  Seven Android 36 emulator UI workflows passed against a synthetic API fixture.
  A later two-emulator run passed eight checks against the deployed Go API/MySQL,
  including same-person sync, different-person isolation, offline queue recovery
  and authorization revocation. See `docs/android-deployed-acceptance.md`.
  Physical-phone and background delivery acceptance remain pending.
- Docker test deployment is isolated from the old Gotify/iGotify projects and
  existing ECS websites. Database credentials are supplied through a secret file.
- The Gotify REST connector now implements protected source configuration,
  resumable full-history scans, fenced leases and atomic deduplicated imports.
  See `docs/gotify-import.md` for the behavior and explicit limits.
- Real Gotify 2.9.1 acceptance imported 205 messages, caught up 17 while the worker
  was stopped, and verified 222 unique imports/outbox records after replay. The
  evidence and deployment boundary are in `docs/gotify-import-acceptance.md`.
- Background push and four-node failover remain unimplemented or unverified.
  The debug APK does not solve background delivery yet. Offline behavior and
  emulator verification boundaries are documented in `docs/android-offline-acceptance.md`.

## Version 1 Scope

- Administrator-created personal accounts; no public sign-up.
- Shared notification topics with server-enforced membership.
- Per-person read, hidden and favorite state shared across devices.
- Server-recorded state changes attributed to the authenticated person.
- Android account isolation, offline state and notification diagnostics.
- Gotify integration and separate validation of four-node delivery.

Alarm ownership, incident resolution, escalation and on-call scheduling stay in
the existing alarm platform. Reading a notification never resolves an incident.

The original Gotify Android client remains at the repository root. This independent
implementation is synchronized under `platform/` on the
`feature/independent-platform` branch of `OneplumN/zgotify`. It does not replace the
old client or its release workflow. See `docs/github-sync.md` for synchronization
and CI boundaries.

See `docs/plans/2026-11-12-independent-notifications.md` for delivery and acceptance.
See `docs/api.md` for the API contract, `docs/local-testing.md` for core testing,
and `docs/docker-testing.md` for the isolated ECS setup and old-service rollback.
The current temporary MySQL deployment and acceptance scope are documented in
`docs/local-mysql-acceptance.md`.
