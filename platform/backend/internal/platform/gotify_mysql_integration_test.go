package platform

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"regexp"
	"strings"
	"testing"
	"time"

	"github.com/go-sql-driver/mysql"
)

// Opt-in only. Never uses the application or existing fixture DSN, drops objects,
// or cleans up evidence. Provision a new, empty zgotify_test_* database per run.
func TestMySQLGotifyImport(t *testing.T) {
	path := os.Getenv("ZGOTIFY_IMPORT_TEST_MYSQL_DSN_FILE")
	if path == "" {
		t.Skip("set ZGOTIFY_IMPORT_TEST_MYSQL_DSN_FILE to a dedicated empty zgotify_test_* database")
	}
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal("could not read import-test DSN file")
	}
	dsn := strings.TrimSpace(string(data))
	cfg, err := mysql.ParseDSN(dsn)
	if err != nil || !regexp.MustCompile(`^zgotify_test_[a-z0-9_]+$`).MatchString(cfg.DBName) {
		t.Fatal("import-test database must be named zgotify_test_* with lowercase ASCII suffix")
	}
	one, err := Open(dsn)
	if err != nil {
		t.Fatal("could not open import-test database")
	}
	defer one.DB.Close()
	two, err := Open(dsn)
	if err != nil {
		t.Fatal("could not open second import-test store")
	}
	defer two.DB.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	var objects int
	err = one.DB.QueryRowContext(ctx, `SELECT
(SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE())+
(SELECT COUNT(*) FROM information_schema.routines WHERE routine_schema=DATABASE())+
(SELECT COUNT(*) FROM information_schema.events WHERE event_schema=DATABASE())`).Scan(&objects)
	if err != nil {
		t.Fatal(err)
	}
	if objects != 0 {
		t.Fatal("import-test database must be empty; existing data will not be reset")
	}
	// Exercise the additive v1 -> v2 path against original rows, not just a fresh v2 install.
	legacy, _, found := strings.Cut(schema, "CREATE TABLE IF NOT EXISTS gotify_sources")
	if !found {
		t.Fatal("missing additive import migration")
	}
	for _, statement := range strings.Split(legacy, ";") {
		if strings.TrimSpace(statement) != "" {
			if _, err := one.DB.ExecContext(ctx, statement); err != nil {
				t.Fatal(err)
			}
		}
	}
	token := strings.Repeat("e", 64)
	result, err := one.DB.ExecContext(ctx, "INSERT INTO topics(name,ingest_token_hash) VALUES(?,?)", "import test", tokenHash(token))
	if err != nil {
		t.Fatal(err)
	}
	topic, err := result.LastInsertId()
	if err != nil {
		t.Fatal(err)
	}
	if _, _, err := one.Ingest(ctx, token, NewMessage{ExternalID: "independent", Body: "original", Priority: 4}); err != nil {
		t.Fatal(err)
	}
	for range 2 {
		if err := one.Migrate(ctx); err != nil {
			t.Fatal(err)
		}
	}
	if err := two.Ready(ctx); err != nil {
		t.Fatal(err)
	}
	counts := func(wantMessages, wantMetadata, wantOutbox int) {
		t.Helper()
		var messages, metadata, outbox int
		err := two.DB.QueryRowContext(ctx, `SELECT (SELECT COUNT(*) FROM messages),(SELECT COUNT(*) FROM gotify_import_metadata),(SELECT COUNT(*) FROM delivery_outbox)`).Scan(&messages, &metadata, &outbox)
		if err != nil || messages != wantMessages || metadata != wantMetadata || outbox != wantOutbox {
			t.Fatalf("atomic counts got %d/%d/%d want %d/%d/%d: %v", messages, metadata, outbox, wantMessages, wantMetadata, wantOutbox, err)
		}
	}
	counts(1, 0, 1)
	src := GotifySource{Key: "mysql-import", TopicID: uint64(topic), Identity: strings.Repeat("a", 64)}
	state := func(before uint64, leased, completed bool) {
		t.Helper()
		var cursor uint64
		var owner sql.NullString
		var success, completion sql.NullTime
		err := two.DB.QueryRowContext(ctx, "SELECT scan_before,lease_owner,last_success_at,last_scan_completed_at FROM gotify_sources WHERE source_key=?", src.Key).Scan(&cursor, &owner, &success, &completion)
		if err != nil || cursor != before || owner.Valid != leased || completion.Valid != completed {
			t.Fatalf("cursor/lease/completion: %d/%v/%v: %v", cursor, owner.Valid, completion.Valid, err)
		}
		if (before != 0 || completed) && !success.Valid {
			t.Fatal("missing successful-page timestamp")
		}
	}
	claim := func(store *Store) GotifyLease {
		t.Helper()
		owner, err := newToken()
		if err != nil {
			t.Fatal(err)
		}
		lease, err := store.ClaimGotify(ctx, src, owner)
		if err != nil {
			t.Fatal(err)
		}
		return lease
	}
	type claimResult struct {
		lease GotifyLease
		err   error
	}
	start, results := make(chan struct{}), make(chan claimResult, 2)
	for _, store := range []*Store{one, two} {
		owner, err := newToken()
		if err != nil {
			t.Fatal(err)
		}
		go func(s *Store, owner string) {
			<-start
			lease, err := s.ClaimGotify(ctx, src, owner)
			results <- claimResult{lease, err}
		}(store, owner)
	}
	close(start)
	var first GotifyLease
	wins, busy := 0, 0
	for range 2 {
		res := <-results
		if res.err == nil {
			wins++
			first = res.lease
		} else if errors.Is(res.err, ErrLeaseBusy) {
			busy++
		} else {
			t.Fatal(res.err)
		}
	}
	if wins != 1 || busy != 1 || first.Before != 0 || first.Fence != 1 {
		t.Fatalf("two-store contention: wins=%d busy=%d lease=%+v", wins, busy, first)
	}
	if _, err := two.ClaimGotify(ctx, src, first.Owner); !errors.Is(err, ErrLeaseBusy) {
		t.Fatalf("same-owner live claim: %v", err)
	}
	var micros int64
	if err := two.DB.QueryRowContext(ctx, "SELECT TIMESTAMPDIFF(MICROSECOND,UTC_TIMESTAMP(3),lease_until) FROM gotify_sources WHERE source_key=?", src.Key).Scan(&micros); err != nil || micros <= 50_000_000 || micros > 60_000_000 {
		t.Fatalf("DB-time lease duration %d: %v", micros, err)
	}
	for _, drift := range []GotifySource{{Key: src.Key, TopicID: src.TopicID + 100, Identity: src.Identity}, {Key: src.Key, TopicID: src.TopicID, Identity: strings.Repeat("f", 64)}} {
		if _, err := one.ClaimGotify(ctx, drift, first.Owner); !errors.Is(err, ErrConflict) {
			t.Fatalf("immutable source drift: %v", err)
		}
		forged := first
		forged.Source = drift
		if n, err := one.CommitGotify(ctx, forged, nil, 0); n != 0 || !errors.Is(err, ErrConflict) {
			t.Fatalf("forged source: %d %v", n, err)
		}
	}
	// Expire via SQL only: no sleeps and no production clock/test hooks.
	if _, err := two.DB.ExecContext(ctx, "UPDATE gotify_sources SET lease_until=UTC_TIMESTAMP(3)-INTERVAL 1 SECOND WHERE source_key=?", src.Key); err != nil {
		t.Fatal(err)
	}
	if n, err := one.CommitGotify(ctx, first, nil, 0); n != 0 || !errors.Is(err, ErrLeaseLost) {
		t.Fatalf("expired writer: %d %v", n, err)
	}
	lease := claim(two)
	if lease.Fence != first.Fence+1 {
		t.Fatal("takeover did not increment fence")
	}
	for _, stale := range []GotifyLease{first, {Source: src, Owner: lease.Owner, Fence: first.Fence, Before: first.Before}} {
		if n, err := one.CommitGotify(ctx, stale, nil, 0); n != 0 || !errors.Is(err, ErrLeaseLost) {
			t.Fatalf("stale commit: %d %v", n, err)
		}
		if err := one.ReleaseGotify(ctx, stale); !errors.Is(err, ErrLeaseLost) {
			t.Fatalf("stale release: %v", err)
		}
	}
	page := []GotifyMessage{gotifyTestMessage(300), gotifyTestMessage(200)}
	page[0].Priority, page[0].Body = -7, ""
	page[0].Date = page[0].Date.In(time.FixedZone("upstream", 8*3600))
	invalid := append([]GotifyMessage(nil), page...)
	invalid[1].AppID = 0
	if n, err := one.CommitGotify(ctx, lease, invalid, 200); n != 0 || err == nil {
		t.Fatalf("invalid later record: %d %v", n, err)
	}
	counts(1, 0, 1)
	state(0, true, false)
	if n, err := one.CommitGotify(ctx, lease, page, 200); n != 2 || err != nil {
		t.Fatalf("first page: %d %v", n, err)
	}
	counts(3, 2, 3)
	state(200, false, false)
	var createdAt time.Time
	var priority int
	var payload []byte
	var payloadHash string
	var messageID uint64
	err = two.DB.QueryRowContext(ctx, `SELECT m.id,m.created_at,m.priority,g.payload,g.payload_hash FROM gotify_import_metadata g JOIN messages m ON m.id=g.message_id WHERE g.source_key=? AND g.upstream_id=300`, src.Key).Scan(&messageID, &createdAt, &priority, &payload, &payloadHash)
	if err != nil {
		t.Fatal(err)
	}
	var original GotifyMessage
	if err := json.Unmarshal(payload, &original); err != nil {
		t.Fatal(err)
	}
	wantPayload := `{"id":300,"appid":3,"title":"alert","message":"","priority":-7,"date":"2026-11-12T01:02:03Z","extras":{"a":{"b":true},"z":9007199254740993}}`
	wantHash := sha256.Sum256([]byte(wantPayload))
	if !createdAt.Equal(gotifyTestNow) || priority != 0 || original.Priority != -7 || original.AppID != 3 || !original.Date.Equal(gotifyTestNow) || payloadHash != hex.EncodeToString(wantHash[:]) {
		t.Fatal("upstream date, priority or payload hash was not preserved")
	}
	var extras map[string]json.RawMessage
	if err := json.Unmarshal(original.Extras, &extras); err != nil || string(extras["z"]) != "9007199254740993" {
		t.Fatalf("extras precision lost: %s %v", original.Extras, err)
	}
	if err := one.DB.Close(); err != nil {
		t.Fatal(err)
	}
	one, err = Open(dsn)
	if err != nil {
		t.Fatal("could not reopen import-test store")
	}
	defer one.DB.Close()
	lease = claim(one)
	if lease.Before != 200 {
		t.Fatal("reopened store lost cursor")
	}
	forged := lease
	forged.Before = 0
	if n, err := two.CommitGotify(ctx, forged, nil, 0); n != 0 || !errors.Is(err, ErrLeaseLost) {
		t.Fatalf("forged Before: %d %v", n, err)
	}
	if err := one.ReleaseGotify(ctx, lease); err != nil {
		t.Fatal(err)
	}
	state(200, false, false)
	lease = claim(two)
	if lease.Before != 200 {
		t.Fatal("release advanced cursor")
	}
	last := gotifyTestMessage(100)
	if n, err := two.CommitGotify(ctx, lease, []GotifyMessage{last}, 0); n != 1 || err != nil {
		t.Fatalf("last page: %d %v", n, err)
	}
	counts(4, 3, 4)
	state(0, false, true)
	// ID 250 becoming visible late must be recovered by a full rescan, not a high-water filter.
	rescan := []GotifyMessage{gotifyTestMessage(400), page[0], gotifyTestMessage(250), page[1], last}
	for _, want := range []int{2, 0} {
		lease = claim(one)
		if lease.Before != 0 {
			t.Fatal("completed scan did not restart from newest")
		}
		if n, err := one.CommitGotify(ctx, lease, rescan, 0); n != want || err != nil {
			t.Fatalf("full rescan: %d %v", n, err)
		}
		counts(6, 5, 6)
	}
	lease = claim(two)
	changed := rescan[0]
	changed.Priority++
	if n, err := two.CommitGotify(ctx, lease, []GotifyMessage{gotifyTestMessage(500), changed}, 0); n != 0 || !errors.Is(err, ErrConflict) {
		t.Fatalf("late payload conflict: %d %v", n, err)
	}
	counts(6, 5, 6)
	state(0, true, true)
	if err := two.ReleaseGotify(ctx, lease); err != nil {
		t.Fatal(err)
	}
	if _, _, err := one.Ingest(ctx, token, NewMessage{ExternalID: "gotify:mysql-import:300", Title: "overwrite", Body: "changed", Priority: 1}); !errors.Is(err, ErrConflict) {
		t.Fatalf("independent ingest overwrote import: %v", err)
	}
	var persistedBody string
	if err := two.DB.QueryRowContext(ctx, "SELECT body FROM messages WHERE id=?", messageID).Scan(&persistedBody); err != nil || persistedBody != "" {
		t.Fatal("original imported message changed")
	}
	counts(6, 5, 6)
	// An authenticated message occupying the import namespace is a conflict even
	// when its display payload matches. Earlier writes in the batch must roll back.
	collision := NewMessage{ExternalID: "gotify:mysql-import:700", Title: "alert", Body: "body", Priority: 10}
	if _, _, err := one.Ingest(ctx, token, collision); err != nil {
		t.Fatal(err)
	}
	lease = claim(two)
	if n, err := two.CommitGotify(ctx, lease, []GotifyMessage{gotifyTestMessage(800), gotifyTestMessage(700)}, 0); n != 0 || !errors.Is(err, ErrConflict) {
		t.Fatalf("independent collision was adopted: %d %v", n, err)
	}
	counts(7, 5, 7)
	state(0, true, true)
	if err := two.ReleaseGotify(ctx, lease); err != nil {
		t.Fatal(err)
	}
	lease = claim(one)
	if n, err := one.CommitGotify(ctx, lease, nil, 0); n != 0 || err != nil {
		t.Fatalf("empty terminal page: %d %v", n, err)
	}
	state(0, false, true)
	counts(7, 5, 7)
}
