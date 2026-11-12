package main

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"log/slog"
	"time"

	"zgotify.local/platform/internal/gotify"
	"zgotify.local/platform/internal/platform"
)

func importMessages(ctx context.Context, store *platform.Store, path string, interval time.Duration, once bool) error {
	if interval < 5*time.Second || interval > 5*time.Minute {
		return errors.New("import interval must be 5s-5m")
	}
	sources, err := gotify.LoadConfig(path)
	if err != nil {
		return err
	}
	var random [32]byte
	if _, err := rand.Read(random[:]); err != nil {
		return errors.New("cannot create worker identity")
	}
	owner := hex.EncodeToString(random[:])
	next := make([]time.Time, len(sources))
	failures := make([]int, len(sources))
	finished := make([]bool, len(sources))
	for ctx.Err() == nil {
		allFinished := true
		for i, source := range sources {
			if finished[i] {
				continue
			}
			allFinished = false
			if time.Now().Before(next[i]) {
				continue
			}
			result, err := gotify.Step(ctx, store, source, owner)
			if ctx.Err() != nil {
				return importStopped(ctx, once)
			}
			if err != nil {
				if once {
					return err
				}
				if !errors.Is(err, platform.ErrLeaseBusy) {
					failures[i]++
					slog.Warn("Gotify import failed", "source", source.Descriptor().Key, "reason", err.Error())
				}
				next[i] = time.Now().Add(importRetryDelay(failures[i]))
				continue
			}
			failures[i] = 0
			if result.Created > 0 || result.Complete {
				slog.Info("Gotify import page committed", "source", source.Descriptor().Key, "fetched", result.Fetched, "created", result.Created, "scan_complete", result.Complete)
			}
			delay := time.Second
			if result.Complete {
				delay = interval
				finished[i] = once
			}
			next[i] = time.Now().Add(delay)
		}
		if once && allFinished {
			return nil
		}
		timer := time.NewTimer(250 * time.Millisecond)
		select {
		case <-ctx.Done():
			timer.Stop()
			return importStopped(ctx, once)
		case <-timer.C:
		}
	}
	return importStopped(ctx, once)
}

func importStopped(ctx context.Context, once bool) error {
	if once {
		return ctx.Err()
	}
	return nil
}

func importRetryDelay(failures int) time.Duration {
	if failures < 1 {
		return 5 * time.Second
	}
	if failures > 6 {
		return 5 * time.Minute
	}
	return time.Duration(1<<uint(failures-1)) * 5 * time.Second
}
