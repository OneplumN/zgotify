package platform

import (
	"database/sql/driver"
	"fmt"
	"net/http"
	"net/http/httptest"
	"regexp"
	"strings"
	"testing"
	"time"
)

func TestMessageContentTypeFromImportMetadata(t *testing.T) {
	for _, tc := range []struct {
		name    string
		payload driver.Value
		want    string
	}{
		{"no metadata", nil, "text/plain"},
		{"empty metadata", "", "text/plain"},
		{"markdown", []byte(`{"id":42,"extras":{"client::display":{"contentType":"text/markdown"}}}`), "text/markdown"},
		{"unrelated metadata stays private", `{"extras":{"client::display":{"contentType":"text/markdown","url":"https://private.invalid/?token=fixture-secret"},"client::notification":{"click":{"url":"https://private.invalid/"}},"secret":"fixture-secret","number":9007199254740993}}`, "text/markdown"},
		{"escaped value", `{"extras":{"client::display":{"contentType":"text\u002fmarkdown"}}}`, "text/markdown"},
		{"missing extras", `{}`, "text/plain"},
		{"null payload", `null`, "text/plain"},
		{"array payload", `[]`, "text/plain"},
		{"scalar payload", `true`, "text/plain"},
		{"null extras", `{"extras":null}`, "text/plain"},
		{"array extras", `{"extras":[]}`, "text/plain"},
		{"string extras", `{"extras":"text/markdown"}`, "text/plain"},
		{"missing display", `{"extras":{}}`, "text/plain"},
		{"null display", `{"extras":{"client::display":null}}`, "text/plain"},
		{"array display", `{"extras":{"client::display":[]}}`, "text/plain"},
		{"string display", `{"extras":{"client::display":"text/markdown"}}`, "text/plain"},
		{"missing content type", `{"extras":{"client::display":{}}}`, "text/plain"},
		{"null content type", `{"extras":{"client::display":{"contentType":null}}}`, "text/plain"},
		{"boolean content type", `{"extras":{"client::display":{"contentType":true}}}`, "text/plain"},
		{"number content type", `{"extras":{"client::display":{"contentType":1}}}`, "text/plain"},
		{"array content type", `{"extras":{"client::display":{"contentType":["text/markdown"]}}}`, "text/plain"},
		{"object content type", `{"extras":{"client::display":{"contentType":{"value":"text/markdown"}}}}`, "text/plain"},
		{"plain", `{"extras":{"client::display":{"contentType":"text/plain"}}}`, "text/plain"},
		{"html", `{"extras":{"client::display":{"contentType":"text/html"}}}`, "text/plain"},
		{"url value", `{"extras":{"client::display":{"contentType":"https://private.invalid/?token=fixture-secret"}}}`, "text/plain"},
		{"value case mismatch", `{"extras":{"client::display":{"contentType":"TEXT/MARKDOWN"}}}`, "text/plain"},
		{"value whitespace", `{"extras":{"client::display":{"contentType":" text/markdown "}}}`, "text/plain"},
		{"mime parameters", `{"extras":{"client::display":{"contentType":"text/markdown; charset=utf-8"}}}`, "text/plain"},
		{"extras case mismatch", `{"Extras":{"client::display":{"contentType":"text/markdown"}}}`, "text/plain"},
		{"display case mismatch", `{"extras":{"Client::display":{"contentType":"text/markdown"}}}`, "text/plain"},
		{"key case mismatch", `{"extras":{"client::display":{"ContentType":"text/markdown"}}}`, "text/plain"},
		{"wrong nesting", `{"client::display":{"contentType":"text/markdown"},"content_type":"text/markdown"}`, "text/plain"},
		{"malformed JSON", `{"extras":`, "text/plain"},
		{"truncated after value", `{"extras":{"client::display":{"contentType":"text/markdown"}}`, "text/plain"},
		{"trailing JSON", `{"extras":{"client::display":{"contentType":"text/markdown"}}} {}`, "text/plain"},
	} {
		for _, detail := range []bool{false, true} {
			t.Run(fmt.Sprintf("%s/detail=%t", tc.name, detail), func(t *testing.T) {
				store, mock := testStore(t)
				token := strings.Repeat("a", 64)
				created := time.Date(2026, 11, 12, 1, 2, 3, 456000000, time.UTC)
				rows := readMessageRows().AddRow(42, 3, "CPU alert", "**High load**", 8, created, true, true, true, 7, tc.payload)
				wantMessage := `{"id":42,"topic_id":3,"title":"CPU alert","message":"**High load**","content_type":"` + tc.want + `","priority":8,"created_at":"2026-11-12T01:02:03.456Z","state":{"read":true,"hidden":true,"favorite":true,"version":7}}`
				expectReadSession(mock, token, 11, false)
				target, want := "/v1/messages/42", `{"message":`+wantMessage+`}`
				if detail {
					mock.ExpectQuery("^"+regexp.QuoteMeta(messageReadSQL)+"$").WithArgs(uint64(11), uint64(11), uint64(42)).WillReturnRows(rows).RowsWillBeClosed()
				} else {
					// A following direct-ingest row must not inherit the imported row's type.
					rows.AddRow(40, 3, "CPU alert", "**High load**", 8, created, false, false, false, 0, nil)
					mock.ExpectQuery("^"+regexp.QuoteMeta(messagesReadSQL)+"$").WithArgs(uint64(11), uint64(11), uint64(50), uint64(50), true).WillReturnRows(rows).RowsWillBeClosed()
					target = "/v1/messages?before=50&include_hidden=true&person_id=99"
					want = `{"messages":[` + wantMessage + `,{"id":40,"topic_id":3,"title":"CPU alert","message":"**High load**","content_type":"text/plain","priority":8,"created_at":"2026-11-12T01:02:03.456Z","state":{"read":false,"hidden":false,"favorite":false,"version":0}}]}`
				}
				assertReadJSON(t, readEndpoint((&API{Store: store}).Handler(), target, "Bearer "+token), 200, want)
			})
		}
	}
}

func TestIngestDoesNotAcceptContentTypeOrExtras(t *testing.T) {
	for _, extra := range []string{`"content_type":"text/markdown"`, `"extras":{"client::display":{"contentType":"text/markdown"}}`} {
		t.Run(extra, func(t *testing.T) {
			store, _ := testStore(t)
			r := httptest.NewRequest(http.MethodPost, "/v1/ingest", strings.NewReader(`{"external_id":"synthetic:42","message":"**High load**","priority":8,`+extra+`}`))
			r.Header.Set("Authorization", "Bearer "+strings.Repeat("b", 64))
			r.Header.Set("Content-Type", "application/json")
			w := httptest.NewRecorder()
			(&API{Store: store}).Handler().ServeHTTP(w, r)
			assertReadJSON(t, w, 400, `{"error":"invalid JSON or unknown field"}`)
		})
	}
}
