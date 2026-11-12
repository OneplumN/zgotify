package gotify

import (
	"context"
	"errors"
	"time"

	"zgotify.local/platform/internal/platform"
)

type ImportStore interface {
	ClaimGotify(context.Context, platform.GotifySource, string) (platform.GotifyLease, error)
	CommitGotify(context.Context, platform.GotifyLease, []platform.GotifyMessage, uint64) (int, error)
	ReleaseGotify(context.Context, platform.GotifyLease) error
}

type Result struct {
	Fetched  int
	Created  int
	Complete bool
}

// Each page and its cursor commit together. Repeated full scans deliberately
// revisit IDs below the maximum: upstream auto-increment order is not commit order.
func Step(ctx context.Context, store ImportStore, source *Source, owner string) (Result, error) {
	ctx, cancel := context.WithTimeout(ctx, 40*time.Second)
	defer cancel()
	lease, err := store.ClaimGotify(ctx, source.Descriptor(), owner)
	if err != nil {
		return Result{}, safeStoreError(err)
	}
	committed := false
	defer func() {
		if !committed {
			release, done := context.WithTimeout(context.Background(), 3*time.Second)
			defer done()
			_ = store.ReleaseGotify(release, lease)
		}
	}()
	messages, next, err := source.Page(ctx, lease.Before)
	if err != nil {
		return Result{}, err
	}
	created, err := store.CommitGotify(ctx, lease, messages, next)
	if err != nil {
		return Result{}, safeStoreError(err)
	}
	committed = true
	return Result{Fetched: len(messages), Created: created, Complete: next == 0}, nil
}

func safeStoreError(err error) error {
	for _, known := range []error{platform.ErrLeaseBusy, platform.ErrLeaseLost, platform.ErrConflict, context.Canceled, context.DeadlineExceeded} {
		if errors.Is(err, known) {
			return known
		}
	}
	return errors.New("import database operation failed; check source mapping, schema and message limits")
}
