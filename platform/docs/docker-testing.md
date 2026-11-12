# Isolated Docker Test Deployment

This is an internal prototype, not the production Gotify service. The user first
chose a separate RDS test database, then authorized a local MySQL container for
immediate validation. The current deployment is in `local-mysql-acceptance.md`;
the RDS instructions below remain a future path. Do not reuse existing Gotify or
Zabbix databases.

## Host Inspection: 2026-11-12

Test ECS: Ubuntu 22.04, x86_64. Public/private addresses are kept in private
operator records, not in this public repository. `TEST_ECS_HOST` below is a
placeholder for the authorized test host.
Memory: approximately 1.6 GiB, no swap. Root filesystem: 40 GiB, 14 GiB available.
Docker Compose version observed: 2.40.3.

The following running containers were stopped with user authorization, without
deleting any containers, images, configuration or data volumes:

- `zabbix8-lab-gateway-1`
- `zabbix8-lab-assistant-1`
- `zabbix8-lab-web-1`
- `zabbix8-lab-server-1`
- `zabbix8-lab-database-1`

The database was stopped after its clients. Non-secret image IDs and mount paths
were recorded in `/opt/zgotify-test/zabbix8-before-stop-20261112.txt`. Available
memory rose from about 685 MiB to 1.0 GiB. Already-stopped historical containers
were left unchanged.

Host Nginx, Fandazi content service, SSH, Docker and Zabbix Agent were left running.
Existing Nginx sites are unrelated services whose domains are omitted from this
public report; do not replace their listeners or certificates. Local HTTPS
requests to Fandazi and Regex returned 200 after stopping the laboratory stack.

## Initial Verification Before Database Setup

- `go test -race -v ./...`: 13 top-level tests passed; real MySQL integration
  explicitly skipped because no disposable test DSN was supplied.
- `go vet ./...`: passed.
- ECS `docker compose config --quiet` and `docker compose build api`: passed.
- Image `zgotify-internal:0.1.0-test` runs as `65532:65532` on Linux/amd64.
- The isolated, network-disabled `-help` runtime check exited 0. Startup without
  database configuration exited 1 with a generic error, as intended.
- SHA-256 of the transferred Linux binary matched the local build:
  `7616cebe3007d4e211476dde2f3f52af0e29a15f99e91368aba1229d7c5f0273`.
- No persistent zgotify container, database secret, migration, public API listener
  or production release has been created. Database and end-to-end acceptance
  remain pending RDS configuration. Transient image-check containers were removed.

This section records the earlier image-only stage. A later authorized local
MySQL deployment now runs the database and API and passed SQL/HTTP tests; see
`local-mysql-acceptance.md` for the current state and remaining limits.

## Prepare an Image Without Building on the Small ECS

Build using the project's pinned Go dependencies. The runtime image contains only
the static binary and a public CA bundle; it does not pull a base image or contain
database credentials. Copy just the named files, not the entire workspace.

```sh
cd backend
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -o /tmp/zgotify-linux-amd64 ./cmd/zgotify
```

On the ECS, the intended layout is:

```text
/opt/zgotify-test/
  compose.yaml
  image/Dockerfile
  image/zgotify
  image/ca-certificates.crt
  secrets/mysql_dsn     # not created until RDS is confirmed
```

Transfer the binary, `deploy/docker/Dockerfile` and `deploy/docker/compose.yaml`
over SSH. Use the host's current `/etc/ssl/certs/ca-certificates.crt` as the public
CA bundle. The server runs as UID/GID 65532, without a writable root filesystem,
with a 192 MiB memory limit and a localhost-only API port.

```sh
cd /opt/zgotify-test
docker compose config --quiet
docker compose build api
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true zgotify-internal:0.1.0-test -help
```

The help command checks Linux execution, not database or API readiness. The CA
bundle is a build-time snapshot; rebuild the image when updating trusted CAs.

## Configure the Dedicated RDS Test Database

First confirm the RDS endpoint, port, MySQL version, empty test database and
dedicated account. Allow the ECS's verified private egress in the RDS whitelist;
check VPC routing before changing rules. Do not expose MySQL to the public internet
or relax an existing production whitelist.

The migration account needs schema creation privileges in the dedicated test
database. For ongoing operation use an account restricted to application DML.
The automated integration suite needs a different fresh database named
`zgotify_test_*`; it must not run against the manually configured application DB.

Enter credentials on the ECS, not in chat or in shell command arguments:

```sh
cd /opt/zgotify-test
install -d -m 700 secrets
test ! -e secrets/mysql_dsn
```

Continue only if the last command succeeds; do not overwrite an existing secret.
Use a server-side editor to create `secrets/mysql_dsn` containing one MySQL DSN:

```text
zgotify_test_user:REPLACE_IN_EDITOR@tcp(RDS_PRIVATE_ENDPOINT:3306)/zgotify_internal?tls=true
```

`tls=true` requires a trusted certificate matching the endpoint and RDS TLS
enabled. Install the RDS CA in the image's CA bundle if required. Do not bypass
certificate verification to make a test pass.

```sh
chmod 700 /opt/zgotify-test /opt/zgotify-test/secrets
chmod 444 /opt/zgotify-test/secrets/mysql_dsn
```

The file is readable by the non-root container via a read-only secret mount;
the parent directory prevents other host users from reading it. Root and Docker
administrators can still access it. Its contents are not container environment
variables. Never print it with `cat`, `docker inspect` or `docker compose config`.

## Migrate and Start Only After Database Confirmation

```sh
cd /opt/zgotify-test
docker compose run --rm --no-deps api -command migrate
```

Stop if migration fails. Bootstrap only an empty people table, with a unique
administrator password of at least 12 characters:

```sh
read -r -s -p 'Administrator password: ' ADMIN_PASSWORD
printf '\n'
printf '%s\n' "$ADMIN_PASSWORD" | docker compose run --rm -T --no-deps api \
  -command bootstrap -username admin -name Administrator
unset ADMIN_PASSWORD
docker compose up -d --no-deps api
curl --fail --silent --show-error http://127.0.0.1:18090/healthz
```

For workstation-only access, create an SSH tunnel:

```sh
ssh -N -L 18090:127.0.0.1:18090 root@TEST_ECS_HOST
```

A USB Android test device can use `adb reverse tcp:18090 tcp:18090` and the debug
app's `http://127.0.0.1:18090` endpoint. A separate HTTPS endpoint is required for
normal phone access. No public port, DNS or existing Nginx site is changed by this
Compose configuration. Background push is not implemented in this prototype.

## Restore the Previous Lab

Do not use `docker compose down -v`, delete bind mounts, or prune Docker volumes.
If memory is needed, stop the new API first:

```sh
cd /opt/zgotify-test
docker compose stop api
docker start zabbix8-lab-database-1
docker inspect --format '{{.State.Health.Status}}' zabbix8-lab-database-1
```

When using the local MySQL overlay, stop `api database`, not only `api`, to release
memory before restoring the previous lab.

Wait until the database is healthy before starting its clients:

```sh
docker start zabbix8-lab-server-1 zabbix8-lab-web-1 zabbix8-lab-assistant-1
docker start zabbix8-lab-gateway-1
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
```

This restores the same container configuration and mounts. Application-level
health still needs checking after restoration; startup alone is not acceptance.
