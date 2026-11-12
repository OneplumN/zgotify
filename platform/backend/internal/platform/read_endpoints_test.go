package platform

import (
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"reflect"
	"regexp"
	"strings"
	"testing"
	"time"

	"github.com/DATA-DOG/go-sqlmock"
)

const topicsReadSQL = `SELECT t.id,t.name FROM topics t
JOIN memberships mb ON mb.topic_id=t.id
WHERE mb.person_id=? ORDER BY t.id`

const messageReadSQL = `SELECT m.id,m.topic_id,m.title,m.body,m.priority,m.created_at,
COALESCE(st.is_read,FALSE),COALESCE(st.is_hidden,FALSE),COALESCE(st.is_favorite,FALSE),COALESCE(st.version,0),g.payload
FROM messages m JOIN memberships mb ON mb.topic_id=m.topic_id AND mb.person_id=?
LEFT JOIN message_states st ON st.message_id=m.id AND st.person_id=?
LEFT JOIN gotify_import_metadata g ON g.message_id=m.id
WHERE m.id=?`

const messagesReadSQL = `SELECT m.id,m.topic_id,m.title,m.body,m.priority,m.created_at,
COALESCE(st.is_read,FALSE),COALESCE(st.is_hidden,FALSE),COALESCE(st.is_favorite,FALSE),COALESCE(st.version,0),g.payload
FROM messages m JOIN memberships mb ON mb.topic_id=m.topic_id AND mb.person_id=?
LEFT JOIN message_states st ON st.message_id=m.id AND st.person_id=?
LEFT JOIN gotify_import_metadata g ON g.message_id=m.id
WHERE (?=0 OR m.id<?) AND (? OR COALESCE(st.is_hidden,FALSE)=FALSE)
ORDER BY m.id DESC LIMIT 100`

func expectReadSession(m sqlmock.Sqlmock, token string, person uint64, admin bool) {
	m.ExpectQuery(regexp.QuoteMeta(`SELECT p.id,p.username,p.display_name,p.is_admin,s.device_name
FROM sessions s JOIN people p ON p.id=s.person_id
WHERE s.token_hash=? AND s.expires_at>UTC_TIMESTAMP(3) AND p.enabled=TRUE`)).
		WithArgs(tokenHash(token)).WillReturnRows(sqlmock.NewRows([]string{"id", "username", "name", "admin", "device"}).
		AddRow(person, "reader", "Reader", admin, "same-device-label"))
}

func readEndpoint(h http.Handler, target, authorization string) *httptest.ResponseRecorder {
	r := httptest.NewRequest(http.MethodGet, target, nil)
	r.Header.Set("Authorization", authorization)
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	return w
}

func assertReadJSON(t *testing.T, w *httptest.ResponseRecorder, status int, want string) {
	t.Helper()
	if w.Code != status {
		t.Fatalf("status = %d, want %d; body = %s", w.Code, status, w.Body.String())
	}
	if w.Header().Get("Cache-Control") != "no-store" || w.Header().Get("Content-Type") != "application/json; charset=utf-8" {
		t.Fatalf("unexpected response headers: %v", w.Header())
	}
	var gotJSON, wantJSON any
	if err := json.Unmarshal(w.Body.Bytes(), &gotJSON); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal([]byte(want), &wantJSON); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(gotJSON, wantJSON) {
		t.Fatalf("body = %s, want %s", w.Body.String(), want)
	}
}

func readMessageRows() *sqlmock.Rows {
	return sqlmock.NewRows([]string{"id", "topic_id", "title", "body", "priority", "created_at", "read", "hidden", "favorite", "version", "payload"})
}

func TestReadTopicsOnlyPersonalMemberships(t *testing.T) {
	for _, admin := range []bool{false, true} {
		t.Run(fmt.Sprintf("admin=%t", admin), func(t *testing.T) {
			store, m := testStore(t)
			token := strings.Repeat("a", 64)
			expectReadSession(m, token, 11, admin)
			m.ExpectQuery("^" + regexp.QuoteMeta(topicsReadSQL) + "$").WithArgs(uint64(11)).
				WillReturnRows(sqlmock.NewRows([]string{"id", "name"}).AddRow(3, "Operations").AddRow(9, "Builds")).RowsWillBeClosed()
			w := readEndpoint((&API{Store: store}).Handler(), "/v1/topics?person_id=22&admin=true&before=3&limit=1", "Bearer "+token)
			assertReadJSON(t, w, 200, `{"topics":[{"id":3,"name":"Operations"},{"id":9,"name":"Builds"}]}`)
		})
	}
}

func TestReadTopicsCompleteSnapshotAndEmptyArray(t *testing.T) {
	for _, count := range []int{0, 101} {
		t.Run(fmt.Sprintf("memberships=%d", count), func(t *testing.T) {
			store, m := testStore(t)
			token := strings.Repeat("a", 64)
			expectReadSession(m, token, 22, false)
			rows := sqlmock.NewRows([]string{"id", "name"})
			want := make([]string, 0, count)
			for id := 1; id <= count; id++ {
				rows.AddRow(id, "Member topic")
				want = append(want, fmt.Sprintf(`{"id":%d,"name":"Member topic"}`, id))
			}
			m.ExpectQuery("^" + regexp.QuoteMeta(topicsReadSQL) + "$").WithArgs(uint64(22)).WillReturnRows(rows).RowsWillBeClosed()
			w := readEndpoint((&API{Store: store}).Handler(), "/v1/topics", "Bearer "+token)
			assertReadJSON(t, w, 200, `{"topics":[`+strings.Join(want, ",")+`]}`)
		})
	}
}

func TestReadMessageUsesOwnStateIncludingHiddenAndInboxShape(t *testing.T) {
	for _, tc := range []struct {
		name   string
		person uint64
		admin  bool
		state  State
		want   string
	}{
		{"hidden member", 11, false, State{Read: true, Hidden: true, Favorite: true, Version: 7}, `{"read":true,"hidden":true,"favorite":true,"version":7}`},
		{"other person defaults", 22, false, State{}, `{"read":false,"hidden":false,"favorite":false,"version":0}`},
		{"admin member", 33, true, State{Favorite: true, Version: 2}, `{"read":false,"hidden":false,"favorite":true,"version":2}`},
	} {
		t.Run(tc.name, func(t *testing.T) {
			store, m := testStore(t)
			token := strings.Repeat("a", 64)
			created := time.Date(2026, 11, 12, 1, 2, 3, 456000000, time.UTC)
			rows := func() *sqlmock.Rows {
				return readMessageRows().AddRow(42, 3, "CPU alert", "High load", 8, created, tc.state.Read, tc.state.Hidden, tc.state.Favorite, tc.state.Version, nil)
			}
			expectReadSession(m, token, tc.person, tc.admin)
			m.ExpectQuery("^"+regexp.QuoteMeta(messageReadSQL)+"$").WithArgs(tc.person, tc.person, uint64(42)).WillReturnRows(rows()).RowsWillBeClosed()
			h := (&API{Store: store}).Handler()
			w := readEndpoint(h, "/v1/messages/42?include_hidden=false&person_id=99&topic_id=99&before=1", "Bearer "+token)
			wantMessage := `{"id":42,"topic_id":3,"title":"CPU alert","message":"High load","content_type":"text/plain","priority":8,"created_at":"2026-11-12T01:02:03.456Z","state":` + tc.want + `}`
			assertReadJSON(t, w, 200, `{"message":`+wantMessage+`}`)
			expectReadSession(m, token, tc.person, tc.admin)
			m.ExpectQuery("^"+regexp.QuoteMeta(messagesReadSQL)+"$").WithArgs(tc.person, tc.person, uint64(0), uint64(0), true).WillReturnRows(rows())
			w = readEndpoint(h, "/v1/messages?include_hidden=true", "Bearer "+token)
			assertReadJSON(t, w, 200, `{"messages":[`+wantMessage+`]}`)
		})
	}
}

func TestReadEndpointsRequirePersonalSession(t *testing.T) {
	token := strings.Repeat("a", 64)
	for _, target := range []string{"/v1/topics", "/v1/messages", "/v1/messages/42", "/v1/messages/bad-id"} {
		for _, tc := range []struct {
			name, header, query string
			lookup              bool
		}{
			{name: "missing"},
			{name: "query token", query: "?token=" + token},
			{name: "wrong scheme", header: "Basic " + token},
			{name: "short token", header: "Bearer short"},
			{name: "extra field", header: "Bearer " + token + " extra"},
			{name: "expired revoked or disabled", header: "Bearer " + token, lookup: true},
			{name: "ingest token", header: "Bearer " + strings.Repeat("b", 64), lookup: true},
		} {
			t.Run(target+"/"+tc.name, func(t *testing.T) {
				store, m := testStore(t)
				if tc.lookup {
					m.ExpectQuery("SELECT p.id,p.username,p.display_name,p.is_admin,s.device_name").
						WithArgs(tokenHash(strings.TrimPrefix(tc.header, "Bearer "))).WillReturnError(sql.ErrNoRows)
				}
				w := readEndpoint((&API{Store: store}).Handler(), target+tc.query, tc.header)
				assertReadJSON(t, w, 401, `{"error":"invalid or expired credentials"}`)
			})
		}
	}
}

func TestReadMessageRejectsInvalidID(t *testing.T) {
	for _, id := range []string{"0", "-1", "not-a-number", "1.5", "18446744073709551616", "+1", "%2042"} {
		t.Run(id, func(t *testing.T) {
			store, m := testStore(t)
			token := strings.Repeat("a", 64)
			expectReadSession(m, token, 11, false)
			w := readEndpoint((&API{Store: store}).Handler(), "/v1/messages/"+id, "Bearer "+token)
			assertReadJSON(t, w, 400, `{"error":"invalid id"}`)
		})
	}
}

func TestReadMessageMissingOrInaccessibleIsNotFound(t *testing.T) {
	for _, admin := range []bool{false, true} {
		for _, reason := range []string{"absent", "not a member", "revoked"} {
			t.Run(fmt.Sprintf("admin=%t/%s", admin, reason), func(t *testing.T) {
				store, m := testStore(t)
				token := strings.Repeat("a", 64)
				expectReadSession(m, token, 11, admin)
				m.ExpectQuery("^"+regexp.QuoteMeta(messageReadSQL)+"$").WithArgs(uint64(11), uint64(11), uint64(42)).WillReturnRows(readMessageRows())
				w := readEndpoint((&API{Store: store}).Handler(), "/v1/messages/42", "Bearer "+token)
				assertReadJSON(t, w, 404, `{"error":"not found"}`)
			})
		}
	}
}

func TestReadEndpointsDatabaseErrorsAreSanitized(t *testing.T) {
	for _, tc := range []struct {
		name, target string
		setup        func(sqlmock.Sqlmock)
	}{
		{"topics query", "/v1/topics", func(m sqlmock.Sqlmock) {
			m.ExpectQuery(regexp.QuoteMeta(topicsReadSQL)).WithArgs(uint64(11)).WillReturnError(errors.New("private database detail"))
		}},
		{"topics scan", "/v1/topics", func(m sqlmock.Sqlmock) {
			m.ExpectQuery(regexp.QuoteMeta(topicsReadSQL)).WithArgs(uint64(11)).WillReturnRows(sqlmock.NewRows([]string{"id", "name"}).AddRow(3, "Valid").AddRow("bad-id", "Invalid")).RowsWillBeClosed()
		}},
		{"topics iteration", "/v1/topics", func(m sqlmock.Sqlmock) {
			m.ExpectQuery(regexp.QuoteMeta(topicsReadSQL)).WithArgs(uint64(11)).WillReturnRows(sqlmock.NewRows([]string{"id", "name"}).AddRow(3, "Valid").AddRow(9, "Unread").RowError(1, errors.New("private row error"))).RowsWillBeClosed()
		}},
		{"message query", "/v1/messages/42", func(m sqlmock.Sqlmock) {
			m.ExpectQuery(regexp.QuoteMeta(messageReadSQL)).WithArgs(uint64(11), uint64(11), uint64(42)).WillReturnError(errors.New("private database detail"))
		}},
		{"message scan", "/v1/messages/42", func(m sqlmock.Sqlmock) {
			m.ExpectQuery(regexp.QuoteMeta(messageReadSQL)).WithArgs(uint64(11), uint64(11), uint64(42)).WillReturnRows(readMessageRows().AddRow(42, 3, "Title", "Body", 8, "bad-date", false, false, false, 0, nil)).RowsWillBeClosed()
		}},
		{"message row", "/v1/messages/42", func(m sqlmock.Sqlmock) {
			m.ExpectQuery(regexp.QuoteMeta(messageReadSQL)).WithArgs(uint64(11), uint64(11), uint64(42)).WillReturnRows(readMessageRows().AddRow(42, 3, "Title", "Body", 8, time.Now(), false, false, false, 0, nil).RowError(0, errors.New("private row error"))).RowsWillBeClosed()
		}},
		{"messages query", "/v1/messages", func(m sqlmock.Sqlmock) {
			m.ExpectQuery(regexp.QuoteMeta(messagesReadSQL)).WithArgs(uint64(11), uint64(11), uint64(0), uint64(0), false).WillReturnError(errors.New("private database detail"))
		}},
		{"messages scan", "/v1/messages", func(m sqlmock.Sqlmock) {
			m.ExpectQuery(regexp.QuoteMeta(messagesReadSQL)).WithArgs(uint64(11), uint64(11), uint64(0), uint64(0), false).WillReturnRows(readMessageRows().AddRow(42, 3, "Title", "Body", 8, "bad-date", false, false, false, 0, nil)).RowsWillBeClosed()
		}},
		{"messages iteration", "/v1/messages", func(m sqlmock.Sqlmock) {
			m.ExpectQuery(regexp.QuoteMeta(messagesReadSQL)).WithArgs(uint64(11), uint64(11), uint64(0), uint64(0), false).WillReturnRows(readMessageRows().AddRow(42, 3, "Title", "Body", 8, time.Now(), false, false, false, 0, nil).AddRow(41, 3, "Title", "Body", 8, time.Now(), false, false, false, 0, nil).RowError(1, errors.New("private row error"))).RowsWillBeClosed()
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			store, m := testStore(t)
			token := strings.Repeat("a", 64)
			expectReadSession(m, token, 11, false)
			tc.setup(m)
			w := readEndpoint((&API{Store: store}).Handler(), tc.target, "Bearer "+token)
			assertReadJSON(t, w, 500, `{"error":"internal error"}`)
		})
	}
	for _, target := range []string{"/v1/topics", "/v1/messages/42"} {
		t.Run("session failure"+target, func(t *testing.T) {
			store, m := testStore(t)
			token := strings.Repeat("a", 64)
			m.ExpectQuery("SELECT p.id,p.username").WithArgs(tokenHash(token)).WillReturnError(errors.New("private session database detail"))
			w := readEndpoint((&API{Store: store}).Handler(), target, "Bearer "+token)
			assertReadJSON(t, w, 500, `{"error":"internal error"}`)
		})
	}
}

func TestReadInboxFiltersRemainValidated(t *testing.T) {
	for _, tc := range []struct {
		query, want string
	}{
		{"include_hidden=1", `{"error":"include_hidden must be true or false"}`},
		{"include_hidden=TRUE", `{"error":"include_hidden must be true or false"}`},
		{"before=-1", `{"error":"invalid before cursor"}`},
		{"before=abc", `{"error":"invalid before cursor"}`},
		{"before=18446744073709551616", `{"error":"invalid before cursor"}`},
	} {
		t.Run(tc.query, func(t *testing.T) {
			store, m := testStore(t)
			token := strings.Repeat("a", 64)
			expectReadSession(m, token, 11, false)
			w := readEndpoint((&API{Store: store}).Handler(), "/v1/messages?"+tc.query, "Bearer "+token)
			assertReadJSON(t, w, 400, tc.want)
		})
	}
}

func TestReadTopicsSnapshotDoesNotAuthorizeLaterDetailOrPatch(t *testing.T) {
	store, m := testStore(t)
	token := strings.Repeat("a", 64)
	h := (&API{Store: store}).Handler()
	expectReadSession(m, token, 11, true)
	m.ExpectQuery(regexp.QuoteMeta(topicsReadSQL)).WithArgs(uint64(11)).WillReturnRows(sqlmock.NewRows([]string{"id", "name"}).AddRow(3, "Operations"))
	assertReadJSON(t, readEndpoint(h, "/v1/topics", "Bearer "+token), 200, `{"topics":[{"id":3,"name":"Operations"}]}`)

	// Membership was revoked after the successful topics snapshot.
	expectReadSession(m, token, 11, true)
	m.ExpectQuery("^"+regexp.QuoteMeta(messageReadSQL)+"$").WithArgs(uint64(11), uint64(11), uint64(42)).WillReturnRows(readMessageRows())
	assertReadJSON(t, readEndpoint(h, "/v1/messages/42", "Bearer "+token), 404, `{"error":"not found"}`)

	expectReadSession(m, token, 11, true)
	m.ExpectBegin()
	m.ExpectQuery(regexp.QuoteMeta(`SELECT m.id FROM messages m JOIN memberships mb ON mb.topic_id=m.topic_id
WHERE m.id=? AND mb.person_id=? FOR UPDATE`)).WithArgs(uint64(42), uint64(11)).WillReturnError(sql.ErrNoRows)
	m.ExpectRollback()
	r := httptest.NewRequest(http.MethodPatch, "/v1/messages/42/state", strings.NewReader(`{"read":true,"expected_version":7}`))
	r.Header.Set("Authorization", "Bearer "+token)
	r.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	assertReadJSON(t, w, 404, `{"error":"not found"}`)
}
