# Independent zgotify Implementation Plan

> Execute incrementally in this isolated project. Existing production services
> and the two reference checkouts must not be changed by local implementation.

**Goal:** Deliver an Android notification application with administrator-created
personal identities, authorized shared feeds, per-person state and auditability.

**Architecture:** Gotify remains an upstream message system. A companion Go API
owns people, topic membership, device sessions, imported messages, personal state
and audit records in a separate MySQL database. Four API instances can share this
database; production notification delivery additionally requires durable outbox
workers and device push integration. Android uses the companion API for identity
and personal state, never a shared Gotify credential as a person's identity.

**Tech Stack:** Go, database/sql, MySQL/InnoDB, Kotlin Android. Reuse proven Android
networking/rendering components after aligning the deployed zgotify source.

## Decisions

- Confirmed on 2026-11-12: Gotify Server 2.9.1 remains the upstream compatibility
  and acceptance baseline. This project does not upgrade existing Gotify instances
  to 3.x or migrate their databases. Any later 3.x support is a separate change.
- Confirmed by the user: administrators create personal accounts for the first
  internal release. No public registration. Corporate SSO is a later adapter.
- Keep incident acknowledgement, resolution, escalation and on-call assignments
  in the existing alarm platform. A read flag does not resolve an incident.
- Personal state is keyed by authenticated user and canonical message ID. A
  session's device label is descriptive and does not establish personal identity.
- Message feeds are shared by explicit topic membership, not password sharing.
- Hide is reversible personal state; it never deletes an upstream message.
- Use a new database/schema and database account; do not alter Gotify tables.
- On 2026-11-12 the user authorized a separate local MySQL container on the test
  ECS for immediate validation. RDS migration remains a later, separate check.
- Target HTTPS behind ALB and independent backend/server-group configuration.
  Production DNS, listener rules and data remain unchanged. The user authorized
  preparing a separate ECS Docker test environment and stopping its old lab stack;
  that isolated deployment is recorded in `../docker-testing.md`.
- Ordinary HTTP reads/writes must work through any API instance without sticky
  sessions. A MySQL table alone does not broadcast websocket events.

## Milestones

### M1: Identity, authorization and state backend

Files: `backend/cmd/zgotify/main.go`, `backend/internal/platform/*.go`,
`backend/internal/platform/schema.sql`, `docs/api.md`, `docs/local-testing.md`.

1. Add MySQL schema for users, sessions, topics, memberships, messages, state,
   audit and durable delivery outbox. Use InnoDB and foreign keys.
2. Add an explicit migration command and explicit administrator bootstrap command.
   Normal server startup must not run migrations or recreate accounts.
3. Add password-hashed login, expiring random bearer sessions, logout, account
   disabling and password changes. Store token hashes, never raw tokens.
4. Add administrator-only user creation, topic creation and membership changes.
5. Add topic-scoped, idempotent message ingestion for the future Gotify connector.
6. Add membership-filtered message reads and per-person read/hidden/favorite flags.
7. Add optimistic state versions. A stale device must receive 409 and refresh,
   rather than silently overwriting another device's state.
8. Commit state updates and audit records atomically. Actor identity must come
   from the validated session; reject caller-supplied actor fields.
9. Test authorization, state isolation, session revocation, duplicate ingestion,
   stale updates and audit failures. Run MySQL integration tests when a disposable
   test database is available; clearly report skipped tests.

### M2: Android internal build

Files: isolated `android/` app; retain reference checkouts unchanged.

1. Establish signed-build identity and upgrade strategy before public distribution.
2. Add server profiles with tokens protected using Android Keystore-backed keys.
3. Add personal login, account switching and cancellation without losing the
   current profile. Isolate all state and caches by server/profile identity.
4. Add authorized inbox, local Room storage, offline state operations and retry.
5. On 409, fetch server state and ask/merge according to explicit per-field rules;
   never treat an HTTP failure as a successful state update.
6. Add notification permission/channel diagnostics and a local test notification.
7. Verify two people see the same topic but keep independent read/hidden state.
8. Verify one person on two devices sees the same server-backed state after sync.

### M3: Gotify import and push

1. Align the actual deployed server versions and Android APK/source revision.
2. Add administrator-managed Gotify source credentials in protected server storage.
   Allow only approved upstream hosts; do not expose arbitrary URL fetching.
3. Ingest using a stable source/account/message identity. Periodic REST catch-up
   is authoritative for gaps; websocket events are a latency optimization.
4. Account for Gotify 2.9.1 process-local websocket distribution: either subscribe
   to all required instances or use a verified upstream fan-out mechanism.
5. Use leases/fencing for source workers and transactional outbox claims for push.
   Deduplicate imports and device notifications; retry with bounded backoff.
6. Select push providers based on actual Android devices and network reachability.
   Obtain project credentials and verify SDK/app-signing requirements before
   calling a push integration complete.
7. Bind push device registrations to authenticated users. Revoke on logout and
   re-check current topic membership before delivery and message detail access.

### M4: Four-node acceptance and stable release

1. Exercise requests across four replicas against disposable MySQL, including
   membership revocation, logout and concurrent state writes across nodes.
2. Kill API/import/push workers at transaction boundaries. Verify durable recovery,
   bounded duplicate attempts and no silently lost accepted messages.
3. Test the target phone matrix: foreground, Home/background, screen off, Doze,
   process reclamation, force stop, reboot and Wi-Fi/mobile switching separately.
4. Record send, import, provider-acceptance, app-receipt and notification-posting
   milestones. Do not equate provider acceptance with a visible heads-up banner.
5. Verify migrations, backups/restores, retained audit records, account disabling,
   log redaction, credential rotation and message retention policies.
6. Pin dependencies/images and publish release notes, checksums, support matrix
   and rollback instructions only after the agreed acceptance criteria pass.

## Status

- [x] Scope and initial account-management choice agreed.
- [x] Isolated project directory created.
- [x] M1 backend implementation, local race tests and static checks.
- [x] M1 real MySQL 8.4.13 integration on a dedicated test schema.
- [ ] RDS-specific connectivity and integration acceptance.
- [x] M2 Android 0.2 internal APK build and lint (0 errors, 7 warnings).
- [x] M2 Room offline storage, durable state queue and 77 Android unit tests.
- [x] M2 Android 36 emulator UI acceptance against an isolated API fixture (7 groups).
- [x] M2 deployed Go API/MySQL and two-emulator verification (eight checks).
- [x] M2 Android 0.3 released-UI reuse and updated test API: eight deployed checks
  plus real Gotify Markdown/plain-text rendering acceptance; see
  `../android-render-deployed-acceptance.md` for failed-run and verification limits.
- [ ] M2 two-physical-device verification.
- [x] Dedicated ECS inventory and authorized old-lab shutdown with data retained.
- [x] Isolated Docker configuration and file-backed database secret support.
- [x] Linux image build and isolated CLI execution on the authorized ECS.
- [x] Dedicated-container migration, live HTTP acceptance and database/API restart on the ECS.
- [ ] RDS configuration and migration rehearsal.
- [x] M3 REST import implementation, source pinning, page checkpoints and fenced transactions.
- [x] M3 local race tests and real MySQL import/upgrade integration.
- [x] M3 real Gotify 2.9.1-to-personal-inbox acceptance (205 messages, three pages).
- [x] M3 stopped-worker catch-up (+17 messages) and replay deduplication (222 total).
- [ ] M3 remote push provider integration and background phone delivery.
- [ ] M4 four-node and physical-device acceptance.

## Explicit Limits Until Verified

An internal implementation is not a stable release. RDS/ALB access, real upstream
configuration, device-provider credentials and target phones are not present in
this workspace. Local tests must not imply production or physical-device success.
