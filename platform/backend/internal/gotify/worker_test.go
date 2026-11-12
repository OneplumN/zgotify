package gotify

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"testing"

	"zgotify.local/platform/internal/platform"
)

type fakeStore struct {
	before    uint64
	commits   int
	released  int
	claimErr  error
	commitErr error
	seen      []uint64
}

func (s *fakeStore) ClaimGotify(_ context.Context, src platform.GotifySource, owner string) (platform.GotifyLease, error) {
	return platform.GotifyLease{Source: src, Owner: owner, Fence: 1, Before: s.before}, s.claimErr
}
func (s *fakeStore) CommitGotify(_ context.Context, _ platform.GotifyLease, m []platform.GotifyMessage, next uint64) (int, error) {
	if s.commitErr != nil {
		return 0, s.commitErr
	}
	s.commits++
	s.before = next
	for _, message := range m {
		s.seen = append(s.seen, message.ID)
	}
	return len(m), nil
}

func TestRestartRescanRecoversLateIDAndDeletedCursor(t *testing.T) {
	late := false
	source := testSource(t, func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "current/user") {
			fmt.Fprint(w, `{"id":7}`)
			return
		}
		var before uint64
		fmt.Sscan(r.URL.Query().Get("since"), &before)
		messages := make([]map[string]any, 0)
		for id := uint64(301); id >= 199; id-- {
			if (id == 201 && !late) || (id == 202 && late) || (before != 0 && id >= before) {
				continue
			}
			messages = append(messages, map[string]any{"id": id, "appid": 1, "message": "test", "date": "2026-11-12T00:00:00Z"})
			if len(messages) == 100 {
				break
			}
		}
		json.NewEncoder(w).Encode(map[string]any{"messages": messages, "paging": map[string]any{"next": "https://untrusted.invalid/ignored"}})
	})
	store := &fakeStore{}
	owner := strings.Repeat("a", 64)
	first, err := Step(context.Background(), store, source, owner)
	if err != nil || first.Complete || store.before != 202 {
		t.Fatalf("first page: %#v %v", first, err)
	}
	second, err := Step(context.Background(), store, source, strings.Repeat("b", 64))
	if err != nil || !second.Complete || store.before != 0 {
		t.Fatalf("resumed tail: %#v %v", second, err)
	}
	late = true
	for i := 0; i < 2; i++ {
		if _, err := Step(context.Background(), store, source, owner); err != nil {
			t.Fatal(err)
		}
	}
	found := false
	for _, id := range store.seen {
		if id == 201 {
			found = true
		}
	}
	if !found || store.before != 0 {
		t.Fatal("late lower ID was skipped on rescan")
	}
}
func (s *fakeStore) ReleaseGotify(context.Context, platform.GotifyLease) error {
	s.released++
	return nil
}

func TestStepDoesNotAdvanceOnFetchFailure(t *testing.T) {
	source := testSource(t, func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(503) })
	store := &fakeStore{before: 42}
	_, err := Step(context.Background(), store, source, strings.Repeat("a", 64))
	if err == nil || store.before != 42 || store.commits != 0 || store.released != 1 {
		t.Fatalf("failure advanced or leaked lease: %#v %v", store, err)
	}
}

func TestStepDoesNotFetchWithoutLease(t *testing.T) {
	calls := 0
	source := testSource(t, func(http.ResponseWriter, *http.Request) { calls++ })
	store := &fakeStore{claimErr: platform.ErrLeaseBusy}
	_, err := Step(context.Background(), store, source, strings.Repeat("a", 64))
	if !errors.Is(err, platform.ErrLeaseBusy) || calls != 0 || store.released != 0 {
		t.Fatalf("lease contention: calls=%d err=%v", calls, err)
	}
}

func TestStepCommitsEndOfScanAndSanitizesStoreErrors(t *testing.T) {
	source := testSource(t, func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "current/user") {
			fmt.Fprint(w, `{"id":7}`)
		} else {
			fmt.Fprint(w, `{"messages":[]}`)
		}
	})
	store := &fakeStore{before: 42}
	result, err := Step(context.Background(), store, source, strings.Repeat("a", 64))
	if err != nil || !result.Complete || store.before != 0 || store.commits != 1 {
		t.Fatalf("endofscan: %#v %v", result, err)
	}
	store.commitErr = errors.New("secret-database-detail")
	_, err = Step(context.Background(), store, source, strings.Repeat("a", 64))
	if err == nil || strings.Contains(err.Error(), "secret-database-detail") || store.released != 1 {
		t.Fatalf("unsafe error: %v", err)
	}
}
