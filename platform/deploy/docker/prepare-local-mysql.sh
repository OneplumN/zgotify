#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
umask 077

if [[ -e secrets || -L secrets ]]; then
  printf '%s\n' 'Refusing to replace existing secrets. Inspect the current deployment first.' >&2
  exit 1
fi
if docker volume inspect zgotify-internal_mysql_test_data >/dev/null 2>&1; then
  printf '%s\n' 'Existing database volume found. Restore matching secrets instead of regenerating them.' >&2
  exit 1
fi
command -v openssl >/dev/null
mkdir -m 700 secrets

root_password=$(openssl rand -hex 32)
app_password=$(openssl rand -hex 32)
migration_password=$(openssl rand -hex 32)
test_password=$(openssl rand -hex 32)
printf '%s\n' "$root_password" > secrets/mysql_root_password
printf '%s\n' "$app_password" > secrets/mysql_app_password
printf '[client]\nuser=root\npassword=%s\nprotocol=socket\n' "$root_password" > secrets/mysql_admin_cnf
printf 'zgotify_app:%s@tcp(database:3306)/zgotify_internal\n' "$app_password" > secrets/mysql_dsn
printf 'zgotify_migrate:%s@tcp(database:3306)/zgotify_internal\n' "$migration_password" > secrets/mysql_migration_dsn
printf 'zgotify_it:%s@tcp(database:3306)/zgotify_test_store_v1\n' "$test_password" > secrets/mysql_test_dsn

# All interpolated SQL values below are generated hexadecimal strings.
{
  printf "REVOKE ALL PRIVILEGES, GRANT OPTION FROM 'zgotify_app'@'%%';\n"
  printf "GRANT SELECT,INSERT,UPDATE,DELETE ON %s.* TO 'zgotify_app'@'%%';\n" '`zgotify\_internal`'
  printf "CREATE USER 'zgotify_migrate'@'%%' IDENTIFIED BY '%s';\n" "$migration_password"
  printf "GRANT ALL PRIVILEGES ON %s.* TO 'zgotify_migrate'@'%%';\n" '`zgotify\_internal`'
  printf 'CREATE DATABASE zgotify_test_store_v1 CHARACTER SET utf8mb4;\n'
  printf "CREATE USER 'zgotify_it'@'%%' IDENTIFIED BY '%s';\n" "$test_password"
  printf "GRANT ALL PRIVILEGES ON %s.* TO 'zgotify_it'@'%%';\n" '`zgotify\_test\_store\_v1`'
} > secrets/init.sql
chmod 444 secrets/mysql_root_password secrets/mysql_app_password secrets/mysql_admin_cnf \
  secrets/mysql_dsn secrets/mysql_migration_dsn secrets/mysql_test_dsn secrets/init.sql
openssl rand -hex 24 > secrets/admin_password
chmod 600 secrets/admin_password
unset root_password app_password migration_password test_password
printf '%s\n' 'Created local-test secrets without printing credentials.'
