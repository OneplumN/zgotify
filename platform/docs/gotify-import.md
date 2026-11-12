# Gotify Import: Internal Test Slice

## Boundaries

The connector reads an approved Gotify account/application with a server-held
client token. It never creates or deletes upstream messages. Personal identities,
membership and state remain in the separate zgotify database. No original Gotify
server code, production containers, ALB rules or public listeners are modified.

The first slice uses REST polling, not websocket delivery or a phone push SDK.
Gotify 2.9.1 `GET /message` returns descending IDs; `since` means strictly less
than the supplied ID. The connector constructs each request itself and never
follows the upstream `paging.next` URL.

References:
- https://github.com/gotify/server/blob/v2.9.1/api/message.go
- https://github.com/gotify/server/blob/v2.9.1/api/user.go
- https://github.com/gotify/server/blob/v2.9.1/model/message.go

## Configuration

An operator places a protected JSON file and client-token files on the backend.
They are not available to Android or to public HTTP configuration endpoints.
Only explicitly approved hosts belong in this file. For a shared-database Gotify
cluster use one logical source/account, not four independently named copies.

```json
{
  "sources": [{
    "key": "approved-cluster-account-app",
    "url": "https://approved-gotify.example",
    "user_id": 7,
    "topic_id": 3,
    "application_id": 9,
    "client_token_file": "/run/secrets/gotify_client_token"
  }]
}
```

IDs above are examples, not existing production configuration. `topic_id` must
refer to an administrator-created zgotify topic with explicit personal members.
`user_id` must be the result of Gotify `/current/user`. `application_id` scopes
the feed; omitting it imports every application visible to that Gotify account.
Limit configuration to 1-16 sources; duplicate keys/logical endpoints are rejected
within a configuration file. Operators must keep source keys consistent across
replicas. Separate files/aliases of one cluster cannot be detected automatically.

TLS verification is enabled. Redirects, URL credentials, query-string tokens and
environment proxies are disabled. `allow_http: true` is only for an explicitly
approved private test network; it does not encrypt credentials or messages.
Client tokens are read on each page and sent only in `X-Gotify-Key`. Changing a
token to another account fails validation before reading messages. Token rotation
for the same account preserves progress. Recreate a container after replacing a
bind-mounted secret by rename so its mount sees the new file.

The source key is bound in MySQL to its endpoint/account/application fingerprint
and topic. Configuration drift fails rather than silently rerouting a feed.
Changing endpoints, account identity, database epoch or topic requires a reviewed
source migration; there is no automatic reset command in this slice.

## Recovery And Deduplication

- Schema v2 adds `gotify_sources` and `gotify_import_metadata`; migration is explicit.
- One page has at most 100 records. A 60-second database-time lease with an
  increasing fence protects its source. No network request runs inside the write
  transaction. The worker has a 40-second per-page context deadline.
- Imported messages, original metadata, one outbox row per new message and the
  next cursor commit atomically. Stale owners, expired leases, conflicting payloads
  and cursor mismatches cannot advance the checkpoint.
- A process restart resumes the saved descending cursor. Completing a scan
  resets it to zero. Every later scan revisits all retained upstream history.
  This recovers a lower ID that becomes visible after a higher ID has committed.
- The default worker pauses one second between pages and ten seconds between
  complete scans. Failures back off from five seconds to five minutes; lease
  contention retries without treating another owner as an error. Retry timers
  are process-local; a restart can scan earlier, but cannot bypass fencing.
- Source status records `last_success_at` and `last_scan_completed_at`. Logs
  include only the source key, record counts, scan completion and sanitized errors.
  No message bodies or tokens are printed by the importer.

Example CLI (database credentials remain in a separate file):

```bash
ZGOTIFY_MYSQL_DSN_FILE=/run/secrets/mysql_dsn ./zgotify \
  -command import -sources /run/secrets/gotify_sources.json
```

Use `-once` for one completed sweep. Cancellation/failure is not reported as a
successful one-shot run. Multiple workers are replay-safe, but this is not a
completed four-ECS high-availability acceptance.

## Content And Operational Limits

- Original title/body, signed priority, application ID, timestamp and extras are
  retained in import metadata. Inbox priority is projected to 0-10, and its
  timestamp uses the upstream time at millisecond precision.
- Titles are limited to 512 Unicode characters, bodies to 64 KiB, extras to
  16 KiB, canonical payload to 128 KiB and a page's stored payloads to 8 MiB.
  Unrepresentable timestamps/JSON and size violations stop the page without
  advancing it; an operator must resolve them. There is no quarantine UI yet.
- The existing inbox paginates by local import ID, not upstream chronological
  order. Initial descending backfill may therefore appear out of chronological
  order. Original timestamps are retained. Markdown metadata is stored but the
  prototype Android app has not gained a Markdown renderer in this change.
- Complete scans become slower as history grows. This correctness-first test
  connector needs latency/load acceptance and optimization before production.
  Upstream messages deleted before a sweep reads them cannot be recovered.
- Outbox rows are durable pending work, not proof of push delivery. Historical
  notification eligibility must be decided before enabling a push consumer.
  Vendor SDK registration, device receipt and visible notifications remain separate.

## Test Deployment

`compose.gotify-test.yaml` is an optional overlay for the dedicated ECS. It adds
an independent official Gotify 2.9.1 fixture with its own SQLite volume and an
importer. The fixture is bound only to `127.0.0.1:18091`; the zgotify API remains
on `127.0.0.1:18090`. MySQL still has no host port. This is not the user's shared
production Gotify database or iGotify bridge.

`prepare-gotify-fixture.py` generates fixture-only credentials once.
`prepare-import-test.py` generates material for a NEW empty integration schema;
the dedicated test refuses existing tables and never resets data.
`verify-gotify-import.py` preserves its credential-bearing journal under the
server's mode-0700 `secrets/` directory and emits only sanitized reports.
Do not download or publish that journal.

Local Go and Python tests are distinct from the real-ECS acceptance results.
See `gotify-import-acceptance.md` for the completed real-fixture acceptance.
