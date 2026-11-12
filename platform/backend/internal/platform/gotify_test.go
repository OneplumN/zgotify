package platform

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/DATA-DOG/go-sqlmock"
	"github.com/go-sql-driver/mysql"
)

func TestGotifyReadyRequiresVersionTwo(t *testing.T) {
	for _, version := range []int{1, 2, 3} {
		store, mock := testStore(t)
		mock.ExpectQuery("SELECT MAX\\(version\\) FROM schema_versions").
			WillReturnRows(sqlmock.NewRows([]string{"version"}).AddRow(version))
		err := store.Ready(context.Background())
		if (err == nil) != (version == 2) {
			t.Errorf("Ready at version %d: %v", version, err)
		}
	}
}

var gotifyTestNow = time.Date(2026, 11, 12, 1, 2, 3, 0, time.UTC)

func gotifyTestLease() GotifyLease {
	return GotifyLease{Source: GotifySource{Key: "source-1", TopicID: 7, Identity: strings.Repeat("a", 64)}, Owner: strings.Repeat("b", 64), Fence: 4, Before: 100}
}

func gotifyTestMessage(id uint64) GotifyMessage {
	return GotifyMessage{ID: id, AppID: 3, Title: "alert", Body: "body", Priority: 14, Date: gotifyTestNow, Extras: json.RawMessage(`{"z":9007199254740993,"a":{"b":true}}`)}
}

func expectGotifyLock(m sqlmock.Sqlmock, lease GotifyLease, until time.Time) {
	m.ExpectQuery(`SELECT topic_id,identity,scan_before,lease_owner,lease_fence,lease_until FROM gotify_sources WHERE source_key=\? FOR UPDATE`).
		WithArgs(lease.Source.Key).WillReturnRows(sqlmock.NewRows([]string{"topic_id", "identity", "scan_before", "lease_owner", "lease_fence", "lease_until"}).
		AddRow(lease.Source.TopicID, lease.Source.Identity, lease.Before, lease.Owner, lease.Fence, until))
}

func expectGotifyClock(m sqlmock.Sqlmock) {
	m.ExpectQuery(`SELECT UTC_TIMESTAMP\(3\)`).WillReturnRows(sqlmock.NewRows([]string{"now"}).AddRow(gotifyTestNow))
}

func expectGotifyCommitStart(m sqlmock.Sqlmock, lease GotifyLease) {
	m.ExpectBegin()
	expectGotifyLock(m, lease, gotifyTestNow.Add(time.Minute))
	expectGotifyClock(m)
}

func expectGotifyFinish(m sqlmock.Sqlmock, lease GotifyLease, next uint64, affected int64) {
	m.ExpectExec(`UPDATE gotify_sources SET scan_before=\?,last_success_at=UTC_TIMESTAMP\(3\),\s*last_scan_completed_at=IF\(\?=0,UTC_TIMESTAMP\(3\),last_scan_completed_at\),lease_owner=NULL,lease_until=NULL WHERE source_key=\? AND lease_owner=\? AND lease_fence=\? AND scan_before=\? AND lease_until>UTC_TIMESTAMP\(3\)`).
		WithArgs(next, next, lease.Source.Key, lease.Owner, lease.Fence, lease.Before).WillReturnResult(sqlmock.NewResult(0, affected))
}

func expectGotifyMissing(m sqlmock.Sqlmock, lease GotifyLease, id uint64) {
	m.ExpectQuery(`SELECT message_id,payload_hash FROM gotify_import_metadata WHERE source_key=\? AND upstream_id=\? FOR UPDATE`).
		WithArgs(lease.Source.Key, id).WillReturnError(sql.ErrNoRows)
}

func TestGotifyClaimPersistsCursorAndUsesDBLease(t *testing.T) {
	store, m := testStore(t)
	old := gotifyTestLease()
	owner := strings.Repeat("c", 64)
	m.ExpectBegin()
	m.ExpectExec(`INSERT INTO gotify_sources\(source_key,topic_id,identity\) VALUES\(\?,\?,\?\) ON DUPLICATE KEY UPDATE source_key=VALUES\(source_key\)`).
		WithArgs(old.Source.Key, old.Source.TopicID, old.Source.Identity).WillReturnResult(sqlmock.NewResult(0, 0))
	expectGotifyLock(m, old, gotifyTestNow)
	expectGotifyClock(m)
	m.ExpectExec(`UPDATE gotify_sources SET lease_owner=\?,lease_fence=lease_fence\+1,\s*lease_until=UTC_TIMESTAMP\(3\)\+INTERVAL 60 SECOND WHERE source_key=\?`).
		WithArgs(owner, old.Source.Key).WillReturnResult(sqlmock.NewResult(0, 1))
	m.ExpectCommit()
	lease, err := store.ClaimGotify(context.Background(), old.Source, owner)
	if err != nil || lease.Source != old.Source || lease.Owner != owner || lease.Fence != 5 || lease.Before != 100 {
		t.Fatalf("claim lost persisted state: %+v %v", lease, err)
	}
}

func TestGotifyClaimRejectsBusyEvenSameOwnerAndImmutableDrift(t *testing.T) {
	for _, kind := range []string{"same owner", "other owner", "topic", "identity"} {
		t.Run(kind, func(t *testing.T) {
			store, m := testStore(t)
			old := gotifyTestLease()
			src, owner, want := old.Source, old.Owner, ErrLeaseBusy
			switch kind {
			case "other owner":
				owner = strings.Repeat("c", 64)
			case "topic":
				src.TopicID++
				want = ErrConflict
			case "identity":
				src.Identity = strings.Repeat("d", 64)
				want = ErrConflict
			}
			m.ExpectBegin()
			m.ExpectExec("INSERT INTO gotify_sources").WithArgs(src.Key, src.TopicID, src.Identity).WillReturnResult(sqlmock.NewResult(0, 0))
			expectGotifyLock(m, old, gotifyTestNow.Add(time.Second))
			if want == ErrLeaseBusy {
				expectGotifyClock(m)
			}
			m.ExpectRollback()
			if _, err := store.ClaimGotify(context.Background(), src, owner); !errors.Is(err, want) {
				t.Fatalf("wanted %v, got %v", want, err)
			}
		})
	}
}

func TestGotifyRejectsInvalidSourceAndOwnerBeforeSQL(t *testing.T) {
	for _, key := range []string{"", "UPPER", "has space", "a:b", "a/b", strings.Repeat("a", 65), "\u00e9"} {
		store, _ := testStore(t)
		lease := gotifyTestLease()
		lease.Source.Key = key
		if _, err := store.ClaimGotify(context.Background(), lease.Source, lease.Owner); err == nil {
			t.Errorf("accepted key %q", key)
		}
	}
	for _, kind := range []string{"topic", "identity", "owner"} {
		store, _ := testStore(t)
		lease := gotifyTestLease()
		switch kind {
		case "topic":
			lease.Source.TopicID = 0
		case "identity":
			lease.Source.Identity = strings.Repeat("g", 64)
		case "owner":
			lease.Owner = "worker"
		}
		if _, err := store.ClaimGotify(context.Background(), lease.Source, lease.Owner); err == nil {
			t.Errorf("accepted invalid %s", kind)
		}
	}
}

func TestGotifyCommitRejectsStaleOrForgedLease(t *testing.T) {
	for _, kind := range []string{"owner", "fence", "before", "topic", "identity", "expired", "expiry boundary", "missing"} {
		t.Run(kind, func(t *testing.T) {
			store, m := testStore(t)
			lease := gotifyTestLease()
			persisted, until, want := lease, gotifyTestNow.Add(time.Minute), ErrLeaseLost
			switch kind {
			case "owner":
				persisted.Owner = strings.Repeat("c", 64)
			case "fence":
				persisted.Fence++
			case "before":
				persisted.Before--
			case "topic":
				persisted.Source.TopicID++
				want = ErrConflict
			case "identity":
				persisted.Source.Identity = strings.Repeat("c", 64)
				want = ErrConflict
			case "expired":
				until = gotifyTestNow.Add(-time.Millisecond)
			case "expiry boundary":
				until = gotifyTestNow
			}
			m.ExpectBegin()
			if kind == "missing" {
				m.ExpectQuery("SELECT topic_id,identity,scan_before,lease_owner,lease_fence,lease_until FROM gotify_sources").WithArgs(lease.Source.Key).WillReturnError(sql.ErrNoRows)
			} else {
				expectGotifyLock(m, persisted, until)
				if kind == "expired" || kind == "expiry boundary" {
					expectGotifyClock(m)
				}
			}
			m.ExpectRollback()
			if n, err := store.CommitGotify(context.Background(), lease, nil, 0); n != 0 || !errors.Is(err, want) {
				t.Fatalf("stale/forged lease accepted: %d %v", n, err)
			}
		})
	}
}

func TestGotifyInvalidBatchCannotAdvanceCursor(t *testing.T) {
	cases := []struct {
		name string
		ids  []uint64
		next uint64
	}{
		{"ascending", []uint64{80, 90}, 80}, {"duplicate", []uint64{90, 90}, 90},
		{"at before", []uint64{100}, 100}, {"above before", []uint64{101}, 101},
		{"zero id", []uint64{0}, 0}, {"skips unseen ids", []uint64{90, 80}, 70},
		{"overlapping cursor", []uint64{90, 80}, 90}, {"empty continuation", nil, 50},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			store, m := testStore(t)
			lease := gotifyTestLease()
			batch := make([]GotifyMessage, len(tc.ids))
			for i, id := range tc.ids {
				batch[i] = gotifyTestMessage(id)
			}
			if _, err := prepareGotifyBatch(lease.Before, batch, tc.next); err == nil {
				t.Fatal("invalid page passed validation")
			}
			expectGotifyCommitStart(m, lease)
			m.ExpectRollback()
			if n, err := store.CommitGotify(context.Background(), lease, batch, tc.next); n != 0 || err == nil {
				t.Fatalf("invalid batch accepted: %d %v", n, err)
			}
		})
	}
	for _, kind := range []string{"over 100", "extras", "date", "title", "utf8", "late invalid record"} {
		t.Run(kind, func(t *testing.T) {
			store, m := testStore(t)
			lease := gotifyTestLease()
			lease.Before = 1000
			batch := []GotifyMessage{gotifyTestMessage(90)}
			switch kind {
			case "over 100":
				batch = make([]GotifyMessage, 101)
				for i := range batch {
					batch[i] = gotifyTestMessage(uint64(999 - i))
				}
			case "extras":
				batch[0].Extras = json.RawMessage(`{"broken"`)
			case "date":
				batch[0].Date = time.Time{}
			case "title":
				batch[0].Title = strings.Repeat("a", 513)
			case "utf8":
				batch[0].Body = string([]byte{0xff})
			case "late invalid record":
				batch = append(batch, gotifyTestMessage(80))
				batch[1].Extras = json.RawMessage(`oops`)
			}
			if _, err := prepareGotifyBatch(lease.Before, batch, 0); err == nil {
				t.Fatal("invalid record passed validation")
			}
			expectGotifyCommitStart(m, lease)
			m.ExpectRollback()
			if n, err := store.CommitGotify(context.Background(), lease, batch, 0); n != 0 || err == nil {
				t.Fatalf("invalid batch accepted: %d %v", n, err)
			}
		})
	}
}

func TestGotifyCommitCanonicalMetadataAndOutboxAreAtomic(t *testing.T) {
	for _, failure := range []string{"", "message", "metadata", "outbox", "expires during batch", "commit"} {
		t.Run(failure, func(t *testing.T) {
			store, m := testStore(t)
			lease, msg := gotifyTestLease(), gotifyTestMessage(90)
			msg.Date = gotifyTestNow.In(time.FixedZone("upstream", 8*3600))
			payload := []byte(`{"id":90,"appid":3,"title":"alert","message":"body","priority":14,"date":"2026-11-12T01:02:03Z","extras":{"a":{"b":true},"z":9007199254740993}}`)
			hash := sha256.Sum256(payload)
			expectGotifyCommitStart(m, lease)
			expectGotifyMissing(m, lease, msg.ID)
			insert := m.ExpectExec(`INSERT INTO messages\(topic_id,external_id,title,body,priority,created_at\) VALUES\(\?,\?,\?,\?,\?,\?\)`).
				WithArgs(uint64(7), "gotify:source-1:90", "alert", "body", 10, gotifyTestNow)
			if failure == "message" {
				insert.WillReturnError(&mysql.MySQLError{Number: 1062})
			} else {
				insert.WillReturnResult(sqlmock.NewResult(51, 1))
				meta := m.ExpectExec(`INSERT INTO gotify_import_metadata\(source_key,upstream_id,message_id,payload,payload_hash\) VALUES\(\?,\?,\?,\?,\?\)`).
					WithArgs(lease.Source.Key, msg.ID, int64(51), payload, hex.EncodeToString(hash[:]))
				if failure == "metadata" {
					meta.WillReturnError(errors.New("metadata unavailable"))
				} else {
					meta.WillReturnResult(sqlmock.NewResult(0, 1))
					outbox := m.ExpectExec(`INSERT INTO delivery_outbox\(message_id\) VALUES\(\?\)`).WithArgs(int64(51))
					if failure == "outbox" {
						outbox.WillReturnError(errors.New("outbox unavailable"))
					} else {
						outbox.WillReturnResult(sqlmock.NewResult(1, 1))
					}
				}
			}
			switch failure {
			case "", "commit":
				expectGotifyFinish(m, lease, 90, 1)
				if failure == "commit" {
					m.ExpectCommit().WillReturnError(errors.New("commit failed"))
				} else {
					m.ExpectCommit()
				}
			case "expires during batch":
				expectGotifyFinish(m, lease, 90, 0)
				m.ExpectRollback()
			default:
				m.ExpectRollback()
			}
			n, err := store.CommitGotify(context.Background(), lease, []GotifyMessage{msg}, 90)
			if failure == "" {
				if n != 1 || err != nil {
					t.Fatalf("import: %d %v", n, err)
				}
			} else if n != 0 || err == nil {
				t.Fatalf("non-atomic import: %d %v", n, err)
			}
			if failure == "message" && !errors.Is(err, ErrConflict) {
				t.Fatalf("ingest collision: %v", err)
			}
			if failure == "expires during batch" && !errors.Is(err, ErrLeaseLost) {
				t.Fatalf("expiry: %v", err)
			}
		})
	}
}

func TestGotifyCanonicalReplayAndPayloadReuseConflict(t *testing.T) {
	payload := []byte(`{"id":90,"appid":3,"title":"alert","message":"body","priority":14,"date":"2026-11-12T01:02:03Z","extras":{"a":{"b":true},"z":9007199254740993}}`)
	hash := sha256.Sum256(payload)
	for _, change := range []string{"", "appid", "title", "body", "priority", "date", "extras"} {
		t.Run(change, func(t *testing.T) {
			store, m := testStore(t)
			lease, msg := gotifyTestLease(), gotifyTestMessage(90)
			lease.Before = 0
			msg.Extras = json.RawMessage(`{ "a": {"b":true}, "z": 9007199254740993.00e0 }`)
			switch change {
			case "appid":
				msg.AppID++
			case "title":
				msg.Title += "!"
			case "body":
				msg.Body += "!"
			case "priority":
				msg.Priority = 11
			case "date":
				msg.Date = msg.Date.Add(time.Millisecond)
			case "extras":
				msg.Extras = json.RawMessage(`{"z":9007199254740992,"a":{"b":true}}`)
			}
			expectGotifyCommitStart(m, lease)
			m.ExpectQuery("SELECT message_id,payload_hash FROM gotify_import_metadata").WithArgs(lease.Source.Key, msg.ID).
				WillReturnRows(sqlmock.NewRows([]string{"message_id", "payload_hash"}).AddRow(51, hex.EncodeToString(hash[:])))
			if change == "" {
				expectGotifyFinish(m, lease, 0, 1)
				m.ExpectCommit()
			} else {
				m.ExpectRollback()
			}
			n, err := store.CommitGotify(context.Background(), lease, []GotifyMessage{msg}, 0)
			if n != 0 || (change == "" && err != nil) || (change != "" && !errors.Is(err, ErrConflict)) {
				t.Fatalf("replay/conflict: %d %v", n, err)
			}
		})
	}
}

func TestGotifyEmptyCompletionAndRelease(t *testing.T) {
	store, m := testStore(t)
	lease := gotifyTestLease()
	expectGotifyCommitStart(m, lease)
	expectGotifyFinish(m, lease, 0, 1)
	m.ExpectCommit()
	if n, err := store.CommitGotify(context.Background(), lease, nil, 0); n != 0 || err != nil {
		t.Fatalf("empty end: %d %v", n, err)
	}
	for _, affected := range []int64{1, 0} {
		m.ExpectExec(`UPDATE gotify_sources SET lease_owner=NULL,lease_until=NULL WHERE source_key=\? AND lease_owner=\? AND lease_fence=\?`).
			WithArgs(lease.Source.Key, lease.Owner, lease.Fence).WillReturnResult(sqlmock.NewResult(0, affected))
		err := store.ReleaseGotify(context.Background(), lease)
		if (affected == 1 && err != nil) || (affected == 0 && !errors.Is(err, ErrLeaseLost)) {
			t.Fatalf("release: %v", err)
		}
	}
}

func TestGotifyBoundedPayloadsStopInsteadOfSkipping(t *testing.T) {
	for _, kind := range []string{"appid", "body", "extras", "canonical payload", "batch"} {
		t.Run(kind, func(t *testing.T) {
			batch := []GotifyMessage{gotifyTestMessage(1000)}
			switch kind {
			case "appid":
				batch[0].AppID = 0
			case "body":
				batch[0].Body = strings.Repeat("x", 65537)
			case "extras":
				batch[0].Extras = json.RawMessage(`"` + strings.Repeat("x", 16383) + `"`)
			case "canonical payload":
				batch[0].Body = strings.Repeat("\x00", 65536)
			case "batch":
				batch = make([]GotifyMessage, 100)
				for i := range batch {
					batch[i] = gotifyTestMessage(uint64(1000 - i))
					batch[i].Body = strings.Repeat("\x00", 16000)
				}
			}
			store, m := testStore(t)
			lease := gotifyTestLease()
			lease.Before = 0
			if _, err := prepareGotifyBatch(0, batch, 0); err == nil {
				t.Fatal("unbounded payload passed validation")
			}
			expectGotifyCommitStart(m, lease)
			m.ExpectRollback()
			if n, err := store.CommitGotify(context.Background(), lease, batch, 0); err == nil || n != 0 {
				t.Fatalf("unbounded batch accepted: %d %v", n, err)
			}
		})
	}
}

func TestGotifyExtrasCanonicalSemanticsAndPrecision(t *testing.T) {
	for _, raw := range []string{`{"n":1,"z":0}`, `{ "z": -0.00, "n": 1.0 }`, `{"z":0e2,"n":10e-1}`} {
		msg := gotifyTestMessage(1)
		msg.Extras = json.RawMessage(raw)
		batch, err := prepareGotifyBatch(0, []GotifyMessage{msg}, 0)
		if err != nil || string(batch[0].message.Extras) != `{"n":1,"z":0}` {
			t.Fatalf("numeric spelling changed meaning: %s %v", raw, err)
		}
	}
	for _, raw := range []string{`{"n":1,"n":2}`, `1e309`, `1e-400`, `0.123456789012345678901`, `18446744073709551617`, `{} []`, `{"n":"\ud800"}`} {
		msg := gotifyTestMessage(1)
		msg.Extras = json.RawMessage(raw)
		if _, err := prepareGotifyBatch(0, []GotifyMessage{msg}, 0); err == nil {
			t.Errorf("silently changed unrepresentable extras %s", raw)
		}
	}
}

func TestGotifyHundredMessagePageAllowsEmptyBodyAndNegativePriority(t *testing.T) {
	batch := make([]GotifyMessage, 100)
	for i := range batch {
		batch[i] = gotifyTestMessage(uint64(100 - i))
		batch[i].Body = ""
		batch[i].Priority = -10
	}
	prepared, err := prepareGotifyBatch(0, batch, 1)
	if err != nil || len(prepared) != 100 || prepared[0].message.Priority != -10 {
		t.Fatalf("valid upstream page rejected: %d %v", len(prepared), err)
	}
}
