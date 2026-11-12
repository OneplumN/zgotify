# zgotify Platform

Independent notification application built alongside the existing Gotify clients.

## Delivery Status

Internal implementation in progress. Not a production release.

- Go API implements personal accounts, topic membership, per-person state,
  cross-device session storage, idempotent ingestion and transactional audit.
- Local Go race tests and vet pass. Real MySQL 8.4.13 integration passed on the
  isolated ECS, along with ten HTTP acceptance groups and a database/API restart
  check. RDS and multi-node acceptance are still pending.
- The separate Android debug app builds and passes lint with no errors and four
  warnings. No emulator or physical-device acceptance has been completed.
- Docker test deployment is isolated from the old Gotify/iGotify projects and
  existing ECS websites. Database credentials are supplied through a secret file.
- The Gotify REST connector now implements protected source configuration,
  resumable full-history scans, fenced leases and atomic deduplicated imports.
  See `docs/gotify-import.md` for the behavior and explicit limits.
- Real Gotify 2.9.1 acceptance imported 205 messages, caught up 17 while the worker
  was stopped, and verified 222 unique imports/outbox records after replay. The
  evidence and deployment boundary are in `docs/gotify-import-acceptance.md`.
- Background push, Android offline storage and four-node failover remain
  unimplemented or unverified. The debug APK does not solve background delivery yet.

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
