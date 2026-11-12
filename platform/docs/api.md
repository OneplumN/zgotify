# Internal API v0.1

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
| GET | `/v1/messages` | Personal session | Up to 100 authorized messages with personal state |
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

## Message State

```json
{"read":true,"expected_version":0}
```

The only state fields are `read`, `hidden`, and `favorite`. At least one must be
present; omitted fields retain their values. `expected_version` is required.
The response is the new complete state and incremented version. Stale versions
return HTTP 409; clients must fetch current state before choosing a new write.
No user/actor ID is accepted in the body. The authenticated session determines
the person whose state changes and the audit actor. Unauthorized messages return
404. State and its before/after audit event commit or roll back together.

`GET /v1/messages?include_hidden=true` includes this person's hidden messages.
Use `before=<last-message-id>` to page toward older messages. There is no
incremental state-sync cursor in this milestone; refresh pages to get current
state. The internal Android client refreshes the first page while foregrounded.
This does not implement background delivery or offline mutation queuing.

## Import

```json
{
  "external_id":"gotify-source-1:account-1:message-42",
  "title":"CPU load warning",
  "message":"Host web-01 load is 5.2",
  "priority":8
}
```

A topic defines the authorized audience. Bind an upstream source/account to that
topic in the future connector. The same `(topic, external_id)` with the same
payload returns the existing message ID and `created:false`; a different payload
under the same key returns 409. An ingestion token grants publishing only, never
read access. Messages and the pending outbox row are committed atomically.

No push consumer or Gotify connector is included yet. An accepted import is not
proof of device delivery. Imported content is stored separately from Gotify.

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
versioning beyond schema v1, security review and load testing before production.
