package platform

import (
	"context"
	"database/sql"
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/go-sql-driver/mysql"
	"golang.org/x/crypto/bcrypt"
)

//go:embed schema.sql
var schema string

type Store struct{ DB *sql.DB }

func Open(dsn string) (*Store, error) {
	cfg, err := mysql.ParseDSN(dsn)
	if err != nil {
		return nil, errors.New("invalid MYSQL_DSN")
	}
	cfg.ParseTime = true
	cfg.Loc = time.UTC
	cfg.MultiStatements = false
	cfg.Timeout = 5 * time.Second
	cfg.ReadTimeout = 15 * time.Second
	cfg.WriteTimeout = 15 * time.Second
	if cfg.Params == nil {
		cfg.Params = map[string]string{}
	}
	cfg.Params["time_zone"] = "'+00:00'"
	db, err := sql.Open("mysql", cfg.FormatDSN())
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(10)
	db.SetMaxIdleConns(5)
	db.SetConnMaxLifetime(3 * time.Minute)
	return &Store{DB: db}, nil
}

func (s *Store) Migrate(ctx context.Context) error {
	conn, err := s.DB.Conn(ctx)
	if err != nil {
		return err
	}
	defer conn.Close()
	var locked int
	if err = conn.QueryRowContext(ctx, "SELECT GET_LOCK('zgotify_schema_migration', 10)").Scan(&locked); err != nil {
		return err
	}
	if locked != 1 {
		return errors.New("migration lock unavailable")
	}
	defer conn.ExecContext(context.Background(), "SELECT RELEASE_LOCK('zgotify_schema_migration')")
	for _, stmt := range strings.Split(schema, ";") {
		if strings.TrimSpace(stmt) == "" {
			continue
		}
		if _, err := conn.ExecContext(ctx, stmt); err != nil {
			return err
		}
	}
	return nil
}

func (s *Store) Bootstrap(ctx context.Context, p NewPerson) (uint64, error) {
	if err := p.Validate(); err != nil {
		return 0, err
	}
	hash, err := bcrypt.GenerateFromPassword([]byte(p.Password), bcrypt.DefaultCost)
	if err != nil {
		return 0, err
	}
	conn, err := s.DB.Conn(ctx)
	if err != nil {
		return 0, err
	}
	defer conn.Close()
	var locked, count int
	if err := conn.QueryRowContext(ctx, "SELECT GET_LOCK('zgotify_bootstrap', 10)").Scan(&locked); err != nil {
		return 0, err
	}
	if locked != 1 {
		return 0, errors.New("bootstrap lock unavailable")
	}
	defer conn.ExecContext(context.Background(), "SELECT RELEASE_LOCK('zgotify_bootstrap')")
	if err := conn.QueryRowContext(ctx, "SELECT COUNT(*) FROM people").Scan(&count); err != nil {
		return 0, err
	}
	if count != 0 {
		return 0, errors.New("bootstrap is only allowed on an empty people table")
	}
	tx, err := conn.BeginTx(ctx, nil)
	if err != nil {
		return 0, err
	}
	defer tx.Rollback()
	result, err := tx.ExecContext(ctx, "INSERT INTO people(username,display_name,password_hash,is_admin) VALUES(?,?,?,TRUE)", normalizeUsername(p.Username), p.DisplayName, hash)
	if err != nil {
		return 0, err
	}
	id, err := result.LastInsertId()
	if err != nil {
		return 0, err
	}
	actor := Session{Person: Person{ID: uint64(id)}, DeviceName: "bootstrap-cli"}
	if err := audit(ctx, tx, actor, "bootstrap_admin", uint64(id), map[string]string{"username": normalizeUsername(p.Username)}); err != nil {
		return 0, err
	}
	return uint64(id), tx.Commit()
}

func (s *Store) Session(ctx context.Context, token string) (Session, error) {
	var out Session
	if len(token) != 64 {
		return out, ErrUnauthorized
	}
	out.TokenHash = tokenHash(token)
	err := s.DB.QueryRowContext(ctx, `SELECT p.id,p.username,p.display_name,p.is_admin,s.device_name
FROM sessions s JOIN people p ON p.id=s.person_id
WHERE s.token_hash=? AND s.expires_at>UTC_TIMESTAMP(3) AND p.enabled=TRUE`, out.TokenHash).
		Scan(&out.Person.ID, &out.Person.Username, &out.Person.DisplayName, &out.Person.Admin, &out.DeviceName)
	if errors.Is(err, sql.ErrNoRows) {
		return out, ErrUnauthorized
	}
	return out, err
}

func (s *Store) Login(ctx context.Context, in Login) (LoginResult, error) {
	var out LoginResult
	username := normalizeUsername(in.Username)
	if !usernamePattern.MatchString(username) || len(in.Password) > 72 || !validText(in.DeviceName, 1, 128) {
		return out, ErrUnauthorized
	}
	// Store attempt budgets in MySQL so changing ALB replicas cannot reset them.
	tx, err := s.DB.BeginTx(ctx, nil)
	if err != nil {
		return out, err
	}
	defer tx.Rollback()
	key := tokenHash(username)
	_, err = tx.ExecContext(ctx, `INSERT INTO login_attempts(username_hash,window_start,attempts) VALUES(?,UTC_TIMESTAMP(3),1)
ON DUPLICATE KEY UPDATE attempts=IF(window_start<UTC_TIMESTAMP(3)-INTERVAL 5 MINUTE,1,attempts+1),
window_start=IF(window_start<UTC_TIMESTAMP(3)-INTERVAL 5 MINUTE,UTC_TIMESTAMP(3),window_start)`, key)
	if err != nil {
		return out, err
	}
	var attempts int
	if err := tx.QueryRowContext(ctx, "SELECT attempts FROM login_attempts WHERE username_hash=?", key).Scan(&attempts); err != nil {
		return out, err
	}
	if err := tx.Commit(); err != nil {
		return out, err
	}
	if attempts > 10 {
		return out, ErrRateLimited
	}
	var hash []byte
	var enabled bool
	err = s.DB.QueryRowContext(ctx, "SELECT id,username,display_name,is_admin,password_hash,enabled FROM people WHERE username=?", username).
		Scan(&out.Person.ID, &out.Person.Username, &out.Person.DisplayName, &out.Person.Admin, &hash, &enabled)
	if errors.Is(err, sql.ErrNoRows) {
		bcrypt.CompareHashAndPassword(dummyPasswordHash, []byte(in.Password))
		return LoginResult{}, ErrUnauthorized
	}
	if err != nil {
		return out, err
	}
	if bcrypt.CompareHashAndPassword(hash, []byte(in.Password)) != nil || !enabled {
		return LoginResult{}, ErrUnauthorized
	}
	out.Token, err = newToken()
	if err != nil {
		return out, err
	}
	out.ExpiresAt = time.Now().UTC().Add(24 * time.Hour)
	tx, err = s.DB.BeginTx(ctx, nil)
	if err != nil {
		return out, err
	}
	defer tx.Rollback()
	var latestHash []byte
	if err := tx.QueryRowContext(ctx, "SELECT enabled,password_hash FROM people WHERE id=? FOR UPDATE", out.Person.ID).Scan(&enabled, &latestHash); err != nil {
		return out, err
	}
	if !enabled || string(latestHash) != string(hash) {
		return LoginResult{}, ErrUnauthorized
	}
	_, err = tx.ExecContext(ctx, "INSERT INTO sessions(token_hash,person_id,device_name,expires_at) VALUES(?,?,?,?)", tokenHash(out.Token), out.Person.ID, in.DeviceName, out.ExpiresAt)
	if err != nil {
		return out, err
	}
	if err = audit(ctx, tx, Session{Person: out.Person, DeviceName: in.DeviceName}, "login", out.Person.ID, map[string]any{}); err != nil {
		return out, err
	}
	return out, tx.Commit()
}

var dummyPasswordHash = func() []byte {
	h, err := bcrypt.GenerateFromPassword([]byte("unused-password-for-timing"), bcrypt.DefaultCost)
	if err != nil {
		panic(err)
	}
	return h
}()

func (s *Store) Logout(ctx context.Context, actor Session) error {
	tx, err := s.DB.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if _, err := tx.ExecContext(ctx, "DELETE FROM sessions WHERE token_hash=?", actor.TokenHash); err != nil {
		return err
	}
	if err := audit(ctx, tx, actor, "logout", actor.Person.ID, map[string]any{}); err != nil {
		return err
	}
	return tx.Commit()
}

func (s *Store) CreatePerson(ctx context.Context, actor Session, in NewPerson) (uint64, error) {
	if !actor.Person.Admin {
		return 0, ErrForbidden
	}
	if err := in.Validate(); err != nil {
		return 0, err
	}
	hash, err := bcrypt.GenerateFromPassword([]byte(in.Password), bcrypt.DefaultCost)
	if err != nil {
		return 0, err
	}
	tx, err := s.DB.BeginTx(ctx, nil)
	if err != nil {
		return 0, err
	}
	defer tx.Rollback()
	result, err := tx.ExecContext(ctx, "INSERT INTO people(username,display_name,password_hash,is_admin) VALUES(?,?,?,?)", normalizeUsername(in.Username), in.DisplayName, hash, in.Admin)
	if err != nil {
		return 0, mapDatabaseError(err)
	}
	id, err := result.LastInsertId()
	if err != nil {
		return 0, err
	}
	if err := audit(ctx, tx, actor, "create_person", uint64(id), map[string]any{"username": normalizeUsername(in.Username), "admin": in.Admin}); err != nil {
		return 0, err
	}
	return uint64(id), tx.Commit()
}

func (s *Store) SetEnabled(ctx context.Context, actor Session, id uint64, enabled bool) error {
	if !actor.Person.Admin || actor.Person.ID == id {
		return ErrForbidden
	}
	tx, err := s.DB.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	var exists uint64
	if err := tx.QueryRowContext(ctx, "SELECT id FROM people WHERE id=? FOR UPDATE", id).Scan(&exists); err != nil {
		return mapDatabaseError(err)
	}
	if _, err := tx.ExecContext(ctx, "UPDATE people SET enabled=? WHERE id=?", enabled, id); err != nil {
		return err
	}
	if !enabled {
		if _, err := tx.ExecContext(ctx, "DELETE FROM sessions WHERE person_id=?", id); err != nil {
			return err
		}
	}
	if err := audit(ctx, tx, actor, "set_person_enabled", id, map[string]bool{"enabled": enabled}); err != nil {
		return err
	}
	return tx.Commit()
}

func (s *Store) ChangePassword(ctx context.Context, actor Session, old, password string) error {
	if err := validatePassword(password); err != nil {
		return err
	}
	if len(old) > 72 {
		return ErrUnauthorized
	}
	tx, err := s.DB.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	var previous []byte
	if err := tx.QueryRowContext(ctx, "SELECT password_hash FROM people WHERE id=? FOR UPDATE", actor.Person.ID).Scan(&previous); err != nil {
		return err
	}
	if bcrypt.CompareHashAndPassword(previous, []byte(old)) != nil {
		return ErrUnauthorized
	}
	hash, err := bcrypt.GenerateFromPassword([]byte(password), bcrypt.DefaultCost)
	if err != nil {
		return err
	}
	if _, err := tx.ExecContext(ctx, "UPDATE people SET password_hash=? WHERE id=?", hash, actor.Person.ID); err != nil {
		return err
	}
	if _, err := tx.ExecContext(ctx, "DELETE FROM sessions WHERE person_id=?", actor.Person.ID); err != nil {
		return err
	}
	if err := audit(ctx, tx, actor, "change_password", actor.Person.ID, map[string]any{}); err != nil {
		return err
	}
	return tx.Commit()
}

func audit(ctx context.Context, tx *sql.Tx, actor Session, action string, object uint64, detail any) error {
	b, err := json.Marshal(detail)
	if err != nil {
		return err
	}
	_, err = tx.ExecContext(ctx, "INSERT INTO audit_events(actor_id,device_name,action,object_id,detail) VALUES(?,?,?,?,?)", actor.Person.ID, actor.DeviceName, action, object, b)
	return err
}

func mapDatabaseError(err error) error {
	if errors.Is(err, sql.ErrNoRows) {
		return ErrNotFound
	}
	var e *mysql.MySQLError
	if errors.As(err, &e) {
		if e.Number == 1062 {
			return ErrConflict
		}
		if e.Number == 1452 {
			return ErrNotFound
		}
	}
	return err
}

func (s *Store) Ready(ctx context.Context) error {
	var version int
	if err := s.DB.QueryRowContext(ctx, "SELECT MAX(version) FROM schema_versions").Scan(&version); err != nil {
		return err
	}
	if version != 2 {
		return fmt.Errorf("unsupported schema version %d", version)
	}
	return nil
}
