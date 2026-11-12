package main

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestCancelledOneShotCannotReportSuccess(t *testing.T) {
	path := filepath.Join(t.TempDir(), "sources.json")
	if err := os.WriteFile(path, []byte(`{"sources":[{"key":"fixture","url":"https://example.invalid","user_id":1,"topic_id":1,"client_token_file":"/not-read"}]}`), 0600); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := importMessages(ctx, nil, path, 10*time.Second, true); !errors.Is(err, context.Canceled) {
		t.Fatalf("incomplete one-shot returned %v", err)
	}
	if err := importMessages(ctx, nil, path, 10*time.Second, false); err != nil {
		t.Fatalf("continuous shutdown returned %v", err)
	}
}

func TestImportRetryDelayBounded(t *testing.T) {
	for _, tc := range []struct {
		failures int
		want     time.Duration
	}{{0, 5 * time.Second}, {1, 5 * time.Second}, {2, 10 * time.Second}, {6, 160 * time.Second}, {7, 300 * time.Second}, {1000, 300 * time.Second}} {
		if got := importRetryDelay(tc.failures); got != tc.want {
			t.Errorf("failures=%d got=%v want=%v", tc.failures, got, tc.want)
		}
	}
}
