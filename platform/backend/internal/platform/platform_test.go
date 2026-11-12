package platform

import (
	"context"
	"database/sql"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/DATA-DOG/go-sqlmock"
)

func ptr[T any](v T) *T { return &v }

func testStore(t *testing.T) (*Store, sqlmock.Sqlmock) {
	t.Helper()
	db, mock, err := sqlmock.New()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := mock.ExpectationsWereMet(); err != nil {
			t.Error(err)
		}
		db.Close()
	})
	return &Store{DB: db}, mock
}

func TestStatePatchPreservesUnspecifiedFieldsAndDetectsConflict(t *testing.T) {
	initial := State{Read: true, Favorite: true, Version: 7}
	patch := StatePatch{Hidden: ptr(true), ExpectedVersion: ptr(uint64(7))}
	next, err := patch.Apply(initial)
	if err != nil || next != (State{Read: true, Hidden: true, Favorite: true, Version: 8}) {
		t.Fatalf("unexpected state %#v %v", next, err)
	}
	if _, err := patch.Apply(next); !errors.Is(err, ErrConflict) {
		t.Fatalf("stale device update accepted: %v", err)
	}
	if _, err := (StatePatch{Read: ptr(false)}).Apply(next); err == nil {
		t.Fatal("missing version accepted")
	}
}

func TestPersonalStateAndAuditUseAuthenticatedPerson(t *testing.T) {
	for _, person := range []uint64{11, 22} {
		t.Run(string(rune('A'+person)), func(t *testing.T) {
			store, m := testStore(t)
			actor := Session{Person: Person{ID: person}, DeviceName: "same-phone-label"}
			m.ExpectBegin()
			m.ExpectQuery("SELECT m.id FROM messages").WithArgs(uint64(42), person).WillReturnRows(sqlmock.NewRows([]string{"id"}).AddRow(42))
			m.ExpectExec("INSERT INTO message_states").WithArgs(person, uint64(42)).WillReturnResult(sqlmock.NewResult(0, 1))
			m.ExpectQuery("SELECT is_read,is_hidden,is_favorite,version").WithArgs(person, uint64(42)).WillReturnRows(sqlmock.NewRows([]string{"read", "hidden", "favorite", "version"}).AddRow(false, false, false, 0))
			m.ExpectExec("UPDATE message_states").WithArgs(true, false, false, uint64(1), person, uint64(42)).WillReturnResult(sqlmock.NewResult(0, 1))
			m.ExpectExec("INSERT INTO audit_events").WithArgs(person, "same-phone-label", "update_message_state", uint64(42), sqlmock.AnyArg()).WillReturnResult(sqlmock.NewResult(1, 1))
			m.ExpectCommit()
			state, err := store.PatchState(context.Background(), actor, 42, StatePatch{Read: ptr(true), ExpectedVersion: ptr(uint64(0))})
			if err != nil || !state.Read || state.Version != 1 {
				t.Fatalf("update failed: %+v %v", state, err)
			}
		})
	}
}

func TestAuditFailureRollsBackState(t *testing.T) {
	store, m := testStore(t)
	m.ExpectBegin()
	m.ExpectQuery("SELECT m.id FROM messages").WithArgs(uint64(42), uint64(11)).WillReturnRows(sqlmock.NewRows([]string{"id"}).AddRow(42))
	m.ExpectExec("INSERT INTO message_states").WillReturnResult(sqlmock.NewResult(0, 1))
	m.ExpectQuery("SELECT is_read,is_hidden,is_favorite,version").WillReturnRows(sqlmock.NewRows([]string{"read", "hidden", "favorite", "version"}).AddRow(false, false, false, 0))
	m.ExpectExec("UPDATE message_states").WillReturnResult(sqlmock.NewResult(0, 1))
	m.ExpectExec("INSERT INTO audit_events").WillReturnError(errors.New("audit storage unavailable"))
	m.ExpectRollback()
	_, err := store.PatchState(context.Background(), Session{Person: Person{ID: 11}}, 42, StatePatch{Read: ptr(true), ExpectedVersion: ptr(uint64(0))})
	if err == nil {
		t.Fatal("state reported successful without its audit record")
	}
}

func TestMissingMembershipCannotChangeState(t *testing.T) {
	store, m := testStore(t)
	m.ExpectBegin()
	m.ExpectQuery("SELECT m.id FROM messages").WithArgs(uint64(42), uint64(99)).WillReturnError(sql.ErrNoRows)
	m.ExpectRollback()
	_, err := store.PatchState(context.Background(), Session{Person: Person{ID: 99}}, 42, StatePatch{Hidden: ptr(true), ExpectedVersion: ptr(uint64(0))})
	if !errors.Is(err, ErrNotFound) {
		t.Fatalf("unauthorized message leaked: %v", err)
	}
}

func TestListMessagesUsesPersonalMembershipAndState(t *testing.T) {
	store, m := testStore(t)
	m.ExpectQuery("SELECT m.id,m.topic_id").WithArgs(uint64(22), uint64(22), uint64(0), uint64(0), false).
		WillReturnRows(sqlmock.NewRows([]string{"id", "topic_id", "title", "body", "priority", "created_at", "read", "hidden", "favorite", "version"}))
	messages, err := store.Messages(context.Background(), Session{Person: Person{ID: 22}}, 0, false)
	if err != nil || len(messages) != 0 {
		t.Fatalf("unexpected messages: %v %v", messages, err)
	}
}

func expectSession(m sqlmock.Sqlmock, token string, admin bool) {
	m.ExpectQuery("SELECT p.id,p.username,p.display_name,p.is_admin,s.device_name").WithArgs(tokenHash(token)).
		WillReturnRows(sqlmock.NewRows([]string{"id", "username", "name", "admin", "device"}).AddRow(11, "alice", "Alice", admin, "Phone A"))
}

func TestAdminEndpointRejectsOrdinaryUser(t *testing.T) {
	store, m := testStore(t)
	token := strings.Repeat("a", 64)
	expectSession(m, token, false)
	r := httptest.NewRequest(http.MethodPost, "/v1/admin/users", strings.NewReader(`{"username":"hacker"}`))
	r.Header.Set("Authorization", "Bearer "+token)
	w := httptest.NewRecorder()
	(&API{Store: store}).Handler().ServeHTTP(w, r)
	if w.Code != 403 {
		t.Fatalf("wanted 403, got %d %s", w.Code, w.Body.String())
	}
}

func TestCallerCannotSpoofAuditActor(t *testing.T) {
	store, m := testStore(t)
	token := strings.Repeat("a", 64)
	expectSession(m, token, false)
	r := httptest.NewRequest(http.MethodPatch, "/v1/messages/42/state", strings.NewReader(`{"read":true,"expected_version":0,"actor_id":22}`))
	r.Header.Set("Authorization", "Bearer "+token)
	r.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	(&API{Store: store}).Handler().ServeHTTP(w, r)
	if w.Code != 400 {
		t.Fatalf("spoofed actor accepted: %d %s", w.Code, w.Body.String())
	}
}

func TestQueryStringTokenIsNotAuthentication(t *testing.T) {
	store, _ := testStore(t)
	r := httptest.NewRequest(http.MethodGet, "/v1/me?token="+strings.Repeat("a", 64), nil)
	w := httptest.NewRecorder()
	(&API{Store: store}).Handler().ServeHTTP(w, r)
	if w.Code != 401 {
		t.Fatalf("wanted 401, got %d", w.Code)
	}
}

func TestSessionMustExistInSharedStore(t *testing.T) {
	store, m := testStore(t)
	token := strings.Repeat("a", 64)
	m.ExpectQuery("SELECT p.id,p.username").WithArgs(tokenHash(token)).WillReturnError(sql.ErrNoRows)
	_, err := store.Session(context.Background(), token)
	if !errors.Is(err, ErrUnauthorized) {
		t.Fatalf("revoked session accepted: %v", err)
	}
}

func TestPasswordAndMessageValidation(t *testing.T) {
	if (NewPerson{Username: "alice", DisplayName: "Alice", Password: "short"}).Validate() == nil {
		t.Fatal("short password accepted")
	}
	if (NewPerson{Username: "alice", DisplayName: "Alice", Password: strings.Repeat("x", 73)}).Validate() == nil {
		t.Fatal("bcrypt truncation allowed")
	}
	for _, priority := range []int{-1, 11} {
		if (NewMessage{ExternalID: "upstream:42", Body: "alert", Priority: priority}).Validate() == nil {
			t.Fatal("invalid priority accepted")
		}
	}
	if (NewMessage{ExternalID: "upstream:42", Body: "alert", Priority: 9}).Validate() != nil {
		t.Fatal("valid message rejected")
	}
}

func TestPersonalAuditCannotReadOtherPeople(t *testing.T) {
	store, m := testStore(t)
	m.ExpectQuery("SELECT id,actor_id,device_name").WithArgs(false, uint64(11), uint64(0), uint64(0)).
		WillReturnRows(sqlmock.NewRows([]string{"id", "actor_id", "device_name", "action", "object_id", "detail", "created_at"}))
	if _, err := store.Audit(context.Background(), Session{Person: Person{ID: 11}}, 0); err != nil {
		t.Fatal(err)
	}
}

func TestTokenStorageIsHashed(t *testing.T) {
	a, err := newToken()
	if err != nil {
		t.Fatal(err)
	}
	b, err := newToken()
	if err != nil {
		t.Fatal(err)
	}
	if len(a) != 64 || a == b || tokenHash(a) == a || tokenHash(a) == tokenHash(b) {
		t.Fatal("invalid token generation or hashing")
	}
}
