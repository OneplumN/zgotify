# Internal API v0.2

This is the independent zgotify API, not the Gotify API. Route a dedicated test
hostname/server group to it. Do not replace the existing Gotify ALB listener.

Except for login and health, use `Authorization: Bearer <personal-session-token>`.
Ingestion uses a separate topic publishing token. Tokens in query strings are
not accepted. Use `Content-Type: application/json` for JSON request bodies.

| Method | Path | Authorization | Result |
| --- | --- | --- | --- |
| GET | `/healthz` | None | Database/schema readiness |
| POST | `/v1/login` | Username/password | 24-hour session and person |
| GET | `/v1/me` | Personal session | Authenticated person and device label |
| DELETE | `/v1/session` | Personal session | Revoke this device session |
| POST | `/v1/password` | Personal session + current password | Change password and revoke all sessions |
| GET | `/v1/topics` | Personal session | Complete list of this person's topic memberships: IDs and names only |
| GET | `/v1/messages` | Personal session | Up to 100 authorized messages with personal state |
| GET | `/v1/messages/{id}` | Personal session + topic membership | One message with personal state, including hidden messages |
| PATCH | `/v1/messages/{id}/state` | Personal session + topic membership | Atomic state/audit update |
| GET | `/v1/audit` | Personal session | Own events; administrators can see all events |
| POST | `/v1/admin/users` | Administrator | Create a personal account |
| PATCH | `/v1/admin/users/{id}` | Administrator | Enable/disable another person |
| POST | `/v1/admin/topics` | Administrator | Create topic; return publishing token once |
| PUT | `/v1/admin/topics/{topic}/members/{person}` | Administrator | Grant topic membership |
| DELETE | `/v1/admin/topics/{topic}/members/{person}` | Administrator | Revoke topic membership |
| POST | `/v1/ingest` | Topic publishing token | Idempotent import and pending delivery-outbox row |

## Login and Personal Accounts

```json
{"username":"alice","password":"a-unique-long-password","device_name":"Alice phone"}
```

Usernames are case-normalized and 3-64 ASCII characters. Passwords are 12-72
UTF-8 bytes and bcrypt-hashed. Session tokens are random 256-bit values; only
their SHA-256 hashes are stored. Device names are descriptive, never proof of
personhood. No public signup endpoint exists.

Administrator user creation additionally accepts `display_name` and `admin`
(default false). A disable request is `{"enabled":false}`. Administrators cannot
disable themselves. Password changes use `old_password` and `new_password`.
Session revocation and account disabling apply across API instances via MySQL.

## Topics and Cache Reconciliation

`GET /v1/topics` returns all current memberships, ordered by topic ID ascending:

```json
{"topics":[{"id":3,"name":"Operations"},{"id":9,"name":"Builds"}]}
```

The empty result is `{"topics":[]}`, never `null`. There is no pagination or
filtering, including by person, role, or inbox cursor. Only `id` and `name` are
returned; publishing tokens and their hashes are never selected or exposed.
Administrators receive only their own memberships, with no read-access bypass.
Ingestion tokens cannot authenticate this endpoint or message reads.

Clients can reconcile their person-scoped cache against a successful complete
topics response and remove cached messages for topic IDs absent from it. A failed
request is not an empty membership list. Do not infer revocation from a missing
message on an inbox page: pagination and hidden-state filtering can omit it.

This is a membership snapshot, not an authorization grant or revocation stream.
Membership can change between the topics query, client cleanup and a subsequent
request (TOCTOU). The server always reauthorizes each detail read and state PATCH;
it never trusts a prior topics response. Detail authorization and content/state
are read in one SQL statement, but an in-flight read may still finish after a
concurrent revocation. PATCH locks membership until commit so revocation and
mutation serialize. Neither endpoint guarantees immediate deletion of data
already delivered to an offline client.

## Message Detail

`GET /v1/messages/42` returns the same `Message` shape as an inbox item:

```json
{
  "message": {
    "id":42,
    "topic_id":3,
    "title":"CPU alert",
    "message":"High load",
    "priority":8,
    "created_at":"2026-11-12T01:02:03.456Z",
    "state":{"read":true,"hidden":true,"favorite":false,"version":7}
  }
}
```

Current topic membership is required, including for administrators. Hidden
messages remain accessible to members without `include_hidden=true`; inbox
filters/cursors do not apply to detail. State belongs only to the authenticated
person. If no state row exists, the three flags are false and `version` is zero;
reading does not create or change state. Absent messages and inaccessible or
revoked memberships all return the same HTTP 404 `{"error":"not found"}`.
IDs must be positive unsigned 64-bit decimal integers; invalid IDs return 400
after authentication. Missing, expired, revoked or disabled sessions return 401.

## Message State

```json
{"read":true,"expected_version":0}
```

The only state fields are `read`, `hidden`, and `favorite`. At least one must be
present; omitted fields retain their values. `expected_version` is required.
The response is the new complete state and incremented version. Stale versions
return HTTP 409; clients can fetch `GET /v1/messages/{id}`, including for a hidden
message, before choosing a new write using its `state.version`. Another device
can change state again before the retry, so another 409 remains possible. A 404
on detail or PATCH means the client must stop retrying and discard that cached
message; a previous successful detail response does not authorize a later write.
No user/actor ID is accepted in the body. The authenticated session determines
the person whose state changes and the audit actor. Unauthorized messages return
404. State and its before/after audit event commit or roll back together.

`GET /v1/messages?include_hidden=true` includes this person's hidden messages.
Use `before=<last-message-id>` to page toward older messages. There is no
incremental state-sync cursor in this milestone; refresh pages to get current
state. The internal Android client refreshes the first page while foregrounded
and revalidates cached content on resume. Its Room queue preserves offline personal
edits, reconciles stale versions and exposes explicit conflicts; see
`local-testing.md` for expiry, retry and revocation limits. This does not implement
background delivery or remote push.

## Import

```json
{
  "external_id":"gotify-source-1:account-1:message-42",
  "title":"CPU load warning",
  "message":"Host web-01 load is 5.2",
  "priority":8
}
```

A topic defines the authorized audience. The existing server-side Gotify REST
polling importer binds an approved upstream account/application to a topic; see
[Gotify import](gotify-import.md) for its configuration and operational limits.
The separate `/v1/ingest` endpoint accepts the payload above. The same
`(topic, external_id)` with the same
payload returns the existing message ID and `created:false`; a different payload
under the same key returns 409. An ingestion token grants publishing only, never
read access. Messages and the pending outbox row are committed atomically.

Schema v2 already includes `gotify_sources` and `gotify_import_metadata` for
restartable, fenced import scans and source-message deduplication. Each imported
page commits messages, original metadata, pending outbox rows and its cursor
atomically. The importer only reads upstream Gotify; imported content and personal
state are stored separately. No push consumer is included yet, and an accepted
import is not proof of device delivery. The new read endpoints need no schema
migration; readiness continues to require schema v2.

## Errors and Audit Limits

400 invalid input; 401 expired/invalid session; 403 insufficient role; 404 missing
or inaccessible message; 409 conflict; 429 login-attempt budget exhausted;
500 generic server error. Login allows at most ten attempts per normalized
username per five-minute window shared across replicas.

Audit covers successful login/logout, account creation/disabling, password change,
topic membership and personal-state mutations. It does not yet record failed
authorization attempts, every message view, upstream Gotify deletion, or direct
database changes. Administrators can query up to 100 events per page using
`before=<last-event-id>`. No raw passwords/tokens are included in audit records.

The internal prototype still needs session/device management UI, administrator
management UI, ingestion-token rotation, retention/cleanup jobs, data migration
versioning beyond schema v2, security review and load testing before production.
