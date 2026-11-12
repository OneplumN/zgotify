package gotify

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func testSource(t *testing.T, handler http.HandlerFunc) *Source {
	t.Helper()
	server := httptest.NewServer(handler)
	t.Cleanup(server.Close)
	path := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(path, []byte("test-client-secret\n"), 0600); err != nil {
		t.Fatal(err)
	}
	source, err := NewSource(SourceConfig{Key: "test-source", URL: server.URL + "/gotify", UserID: 7, TopicID: 3, ClientTokenFile: path, AllowHTTP: true})
	if err != nil {
		t.Fatal(err)
	}
	return source
}

func TestPageUsesHeaderAndDescendingCursor(t *testing.T) {
	source := testSource(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "GET" || r.Header.Get("X-Gotify-Key") != "test-client-secret" || r.URL.Query().Get("token") != "" {
			t.Error("unsafe upstream request")
		}
		if r.URL.Path == "/gotify/current/user" {
			fmt.Fprint(w, `{"id":7,"name":"fixture"}`)
			return
		}
		if r.URL.Path != "/gotify/message" || r.URL.Query().Get("since") != "200" || r.URL.Query().Get("limit") != "100" {
			t.Errorf("wrong cursor request: %s", r.URL)
		}
		fmt.Fprint(w, `{"messages":[{"id":199,"appid":2,"title":"CPU","message":"**high**","priority":11,"date":"2026-11-12T01:00:00Z","extras":{"client::display":{"contentType":"text/markdown"}}}],"paging":{"next":"https://untrusted.invalid/message?token=stolen"}}`)
	})
	page, next, err := source.Page(context.Background(), 200)
	if err != nil || len(page) != 1 || next != 0 {
		t.Fatalf("page = %#v next=%d err=%v", page, next, err)
	}
	if page[0].ID != 199 || page[0].Priority != 11 || !strings.Contains(string(page[0].Extras), "text/markdown") {
		t.Fatal("upstream metadata lost")
	}
}

func TestFullPageAdvancesFromLastIDNotNextURL(t *testing.T) {
	source := testSource(t, func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "current/user") {
			fmt.Fprint(w, `{"id":7}`)
			return
		}
		messages := make([]map[string]any, 100)
		for i := range messages {
			messages[i] = map[string]any{"id": 500 - i, "appid": 2, "message": "body", "date": "2026-11-12T01:00:00Z"}
		}
		json.NewEncoder(w).Encode(map[string]any{"messages": messages})
	})
	page, next, err := source.Page(context.Background(), 0)
	if err != nil || len(page) != 100 || next != 401 {
		t.Fatalf("full page: len=%d next=%d err=%v", len(page), next, err)
	}
}

func TestRejectsWrongAccountBeforeReadingMessages(t *testing.T) {
	calls := 0
	source := testSource(t, func(w http.ResponseWriter, r *http.Request) { calls++; fmt.Fprint(w, `{"id":8}`) })
	_, _, err := source.Page(context.Background(), 0)
	if err == nil || calls != 1 {
		t.Fatalf("wrong user accepted, calls=%d err=%v", calls, err)
	}
}

func TestRejectsRedirectAndRedactsErrorBody(t *testing.T) {
	for _, status := range []int{302, 401, 500} {
		t.Run(fmt.Sprint(status), func(t *testing.T) {
			calls := 0
			source := testSource(t, func(w http.ResponseWriter, r *http.Request) {
				calls++
				w.Header().Set("Location", "/leaked")
				w.WriteHeader(status)
				fmt.Fprint(w, "test-client-secret")
			})
			_, _, err := source.Page(context.Background(), 0)
			if err == nil || calls != 1 || strings.Contains(err.Error(), "test-client-secret") {
				t.Fatalf("unsafe error/redirect: calls=%d err=%v", calls, err)
			}
		})
	}
}

func TestRejectsMalformedOrNonProgressingPages(t *testing.T) {
	for _, body := range []string{
		`{"error":"not a page"}`, `{"messages":null}`, `{"messages":[]} {}`,
		`{"messages":[{"id":9,"appid":1},{"id":9,"appid":1}]}`,
		`{"messages":[{"id":10,"appid":1}]}`, `{"messages":[{"id":0,"appid":1}]}`,
	} {
		t.Run(body, func(t *testing.T) {
			source := testSource(t, func(w http.ResponseWriter, r *http.Request) {
				if strings.HasSuffix(r.URL.Path, "current/user") {
					fmt.Fprint(w, `{"id":7}`)
					return
				}
				fmt.Fprint(w, body)
			})
			if _, _, err := source.Page(context.Background(), 10); err == nil {
				t.Fatal("invalid page advanced")
			}
		})
	}
}

func TestSourceConfigRejectsUnsafeURLsAndDuplicates(t *testing.T) {
	base := SourceConfig{Key: "alarms", URL: "https://gotify.example/base/", UserID: 7, TopicID: 3, ClientTokenFile: "/run/secrets/client"}
	for _, value := range []string{"http://gotify.example", "https://user:pass@gotify.example", "https://gotify.example?token=secret", "https://gotify.example/#x", "file:///etc/passwd", "https://gotify.example/a/../b", "https://gotify.example/%2e%2e/a"} {
		cfg := base
		cfg.URL = value
		if _, err := NewSource(cfg); err == nil {
			t.Errorf("unsafe URL accepted: %s", value)
		}
	}
	path := filepath.Join(t.TempDir(), "sources.json")
	data, _ := json.Marshal(Config{Sources: []SourceConfig{base, base}})
	os.WriteFile(path, data, 0600)
	if _, err := LoadConfig(path); err == nil {
		t.Fatal("duplicate source keys accepted")
	}
	alias := base
	alias.Key = "another-key"
	data, _ = json.Marshal(Config{Sources: []SourceConfig{base, alias}})
	os.WriteFile(path, data, 0600)
	if _, err := LoadConfig(path); err == nil {
		t.Fatal("same logical source accepted with two keys")
	}
	first, err := NewSource(base)
	if err != nil {
		t.Fatal(err)
	}
	base.URL = "https://gotify.example/base"
	base.ClientTokenFile = "/run/secrets/rotated"
	second, err := NewSource(base)
	if err != nil {
		t.Fatal(err)
	}
	if first.Descriptor() != second.Descriptor() {
		t.Fatal("trailing slash/token path changed source identity")
	}
	base.UserID = 8
	third, _ := NewSource(base)
	if first.Descriptor().Identity == third.Descriptor().Identity {
		t.Fatal("account drift not distinguished")
	}
}

func TestTokenRotationAndApplicationScope(t *testing.T) {
	var tokens []string
	source := testSource(t, func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "current/user") {
			tokens = append(tokens, r.Header.Get("X-Gotify-Key"))
			fmt.Fprint(w, `{"id":7}`)
			return
		}
		if r.URL.Path != "/gotify/application/9/message" {
			t.Error("application scope not requested")
		}
		fmt.Fprint(w, `{"messages":[]}`)
	})
	source.config.AppID = 9
	for _, token := range []string{"first-client", "rotated-client"} {
		if err := os.WriteFile(source.config.ClientTokenFile, []byte(token), 0600); err != nil {
			t.Fatal(err)
		}
		if _, _, err := source.Page(context.Background(), 0); err != nil {
			t.Fatal(err)
		}
	}
	if strings.Join(tokens, ",") != "first-client,rotated-client" {
		t.Fatal("token rotation did not take effect")
	}
}

func TestInvalidUTF8IsNotSilentlyReplaced(t *testing.T) {
	source := testSource(t, func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "current/user") {
			fmt.Fprint(w, `{"id":7}`)
			return
		}
		w.Write([]byte("{\"messages\":[{\"id\":1,\"appid\":1,\"message\":\"\xff\"}]}"))
	})
	if _, _, err := source.Page(context.Background(), 0); err == nil {
		t.Fatal("corrupt UTF-8 accepted")
	}
}
