package platform

import (
	"bytes"
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"regexp"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"
)

type GotifySource struct {
	Key      string
	TopicID  uint64
	Identity string
}

type GotifyLease struct {
	Source GotifySource
	Owner  string
	Fence  uint64
	Before uint64
}

type GotifyMessage struct {
	ID       uint64          `json:"id"`
	AppID    uint64          `json:"appid"`
	Title    string          `json:"title"`
	Body     string          `json:"message"`
	Priority int             `json:"priority"`
	Date     time.Time       `json:"date"`
	Extras   json.RawMessage `json:"extras,omitempty"`
}

var (
	ErrLeaseBusy     = errors.New("gotify import lease is busy")
	ErrLeaseLost     = errors.New("gotify import lease is lost")
	gotifyKeyPattern = regexp.MustCompile(`^[a-z0-9._-]{1,64}$`)
	gotifyHexPattern = regexp.MustCompile(`^[0-9a-fA-F]{64}$`)
)

func validateGotifySource(src GotifySource, owner string) error {
	if !gotifyKeyPattern.MatchString(src.Key) || src.TopicID == 0 || !gotifyHexPattern.MatchString(src.Identity) || !gotifyHexPattern.MatchString(owner) {
		return errors.New("invalid gotify source or lease owner")
	}
	return nil
}

type gotifySourceState struct {
	topic    uint64
	identity string
	before   uint64
	owner    sql.NullString
	fence    uint64
	until    sql.NullTime
}

func lockGotifySource(ctx context.Context, tx *sql.Tx, key string) (gotifySourceState, error) {
	var state gotifySourceState
	err := tx.QueryRowContext(ctx, `SELECT topic_id,identity,scan_before,lease_owner,lease_fence,lease_until
FROM gotify_sources WHERE source_key=? FOR UPDATE`, key).
		Scan(&state.topic, &state.identity, &state.before, &state.owner, &state.fence, &state.until)
	return state, err
}

func gotifyDBTime(ctx context.Context, tx *sql.Tx) (time.Time, error) {
	// A separate statement samples time after the row lock, not before its wait.
	var now time.Time
	err := tx.QueryRowContext(ctx, "SELECT UTC_TIMESTAMP(3)").Scan(&now)
	return now, err
}

func (s *Store) ClaimGotify(ctx context.Context, src GotifySource, owner string) (GotifyLease, error) {
	if err := validateGotifySource(src, owner); err != nil {
		return GotifyLease{}, err
	}
	tx, err := s.DB.BeginTx(ctx, nil)
	if err != nil {
		return GotifyLease{}, err
	}
	defer tx.Rollback()
	_, err = tx.ExecContext(ctx, `INSERT INTO gotify_sources(source_key,topic_id,identity) VALUES(?,?,?)
ON DUPLICATE KEY UPDATE source_key=VALUES(source_key)`, src.Key, src.TopicID, src.Identity)
	if err != nil {
		return GotifyLease{}, mapDatabaseError(err)
	}
	state, err := lockGotifySource(ctx, tx, src.Key)
	if err != nil {
		return GotifyLease{}, err
	}
	if state.topic != src.TopicID || state.identity != src.Identity {
		return GotifyLease{}, ErrConflict
	}
	now, err := gotifyDBTime(ctx, tx)
	if err != nil {
		return GotifyLease{}, err
	}
	if state.owner.Valid && state.until.Valid && state.until.Time.After(now) {
		return GotifyLease{}, ErrLeaseBusy
	}
	if state.fence == ^uint64(0) {
		return GotifyLease{}, errors.New("gotify fence exhausted")
	}
	_, err = tx.ExecContext(ctx, `UPDATE gotify_sources SET lease_owner=?,lease_fence=lease_fence+1,
lease_until=UTC_TIMESTAMP(3)+INTERVAL 60 SECOND WHERE source_key=?`, owner, src.Key)
	if err != nil {
		return GotifyLease{}, err
	}
	if err := tx.Commit(); err != nil {
		return GotifyLease{}, err
	}
	return GotifyLease{Source: src, Owner: owner, Fence: state.fence + 1, Before: state.before}, nil
}

func (s *Store) CommitGotify(ctx context.Context, lease GotifyLease, messages []GotifyMessage, nextBefore uint64) (int, error) {
	if err := validateGotifySource(lease.Source, lease.Owner); err != nil {
		return 0, err
	}
	if lease.Fence == 0 {
		return 0, ErrLeaseLost
	}
	tx, err := s.DB.BeginTx(ctx, nil)
	if err != nil {
		return 0, err
	}
	defer tx.Rollback()
	state, err := lockGotifySource(ctx, tx, lease.Source.Key)
	if errors.Is(err, sql.ErrNoRows) {
		return 0, ErrLeaseLost
	}
	if err != nil {
		return 0, err
	}
	if state.topic != lease.Source.TopicID || state.identity != lease.Source.Identity {
		return 0, ErrConflict
	}
	if !state.owner.Valid || state.owner.String != lease.Owner || state.fence != lease.Fence || state.before != lease.Before {
		return 0, ErrLeaseLost
	}
	now, err := gotifyDBTime(ctx, tx)
	if err != nil {
		return 0, err
	}
	if !state.until.Valid || !state.until.Time.After(now) {
		return 0, ErrLeaseLost
	}
	batch, err := prepareGotifyBatch(lease.Before, messages, nextBefore)
	if err != nil {
		return 0, err
	}
	created := 0
	for _, item := range batch {
		var existingID uint64
		var existingHash string
		err := tx.QueryRowContext(ctx, `SELECT message_id,payload_hash FROM gotify_import_metadata
WHERE source_key=? AND upstream_id=? FOR UPDATE`, lease.Source.Key, item.message.ID).Scan(&existingID, &existingHash)
		if err == nil {
			if existingHash != item.hash {
				return 0, ErrConflict
			}
			continue
		}
		if !errors.Is(err, sql.ErrNoRows) {
			return 0, err
		}
		priority := max(0, min(10, item.message.Priority))
		externalID := "gotify:" + lease.Source.Key + ":" + strconv.FormatUint(item.message.ID, 10)
		result, err := tx.ExecContext(ctx, `INSERT INTO messages(topic_id,external_id,title,body,priority,created_at)
VALUES(?,?,?,?,?,?)`, lease.Source.TopicID, externalID, item.message.Title, item.message.Body, priority, item.message.Date.Truncate(time.Millisecond))
		// A collision without import metadata belongs to independent ingest. Never adopt it.
		if err != nil {
			return 0, mapDatabaseError(err)
		}
		id, err := result.LastInsertId()
		if err != nil {
			return 0, err
		}
		if _, err := tx.ExecContext(ctx, `INSERT INTO gotify_import_metadata(source_key,upstream_id,message_id,payload,payload_hash)
VALUES(?,?,?,?,?)`, lease.Source.Key, item.message.ID, id, item.payload, item.hash); err != nil {
			return 0, mapDatabaseError(err)
		}
		if _, err := tx.ExecContext(ctx, "INSERT INTO delivery_outbox(message_id) VALUES(?)", id); err != nil {
			return 0, err
		}
		created++
	}
	// Recheck expiry at the write boundary: a long-running batch must not advance.
	result, err := tx.ExecContext(ctx, `UPDATE gotify_sources SET scan_before=?,last_success_at=UTC_TIMESTAMP(3),
last_scan_completed_at=IF(?=0,UTC_TIMESTAMP(3),last_scan_completed_at),lease_owner=NULL,lease_until=NULL
WHERE source_key=? AND lease_owner=? AND lease_fence=? AND scan_before=? AND lease_until>UTC_TIMESTAMP(3)`,
		nextBefore, nextBefore, lease.Source.Key, lease.Owner, lease.Fence, lease.Before)
	if err != nil {
		return 0, err
	}
	if err := gotifyRequireMatch(result); err != nil {
		return 0, err
	}
	if err := tx.Commit(); err != nil {
		return 0, err
	}
	return created, nil
}

func (s *Store) ReleaseGotify(ctx context.Context, lease GotifyLease) error {
	if err := validateGotifySource(lease.Source, lease.Owner); err != nil {
		return err
	}
	if lease.Fence == 0 {
		return ErrLeaseLost
	}
	result, err := s.DB.ExecContext(ctx, `UPDATE gotify_sources SET lease_owner=NULL,lease_until=NULL
WHERE source_key=? AND lease_owner=? AND lease_fence=?`, lease.Source.Key, lease.Owner, lease.Fence)
	if err != nil {
		return err
	}
	return gotifyRequireMatch(result)
}

func gotifyRequireMatch(result sql.Result) error {
	n, err := result.RowsAffected()
	if err != nil {
		return err
	}
	if n != 1 {
		return ErrLeaseLost
	}
	return nil
}

type gotifyImportRecord struct {
	message GotifyMessage
	payload []byte
	hash    string
}

func prepareGotifyBatch(before uint64, messages []GotifyMessage, next uint64) ([]gotifyImportRecord, error) {
	if len(messages) > 100 {
		return nil, errors.New("gotify batch exceeds 100 messages")
	}
	if next != 0 && (len(messages) == 0 || next != messages[len(messages)-1].ID || (before != 0 && next >= before)) {
		return nil, errors.New("gotify continuation must equal the lowest page ID and descend")
	}
	batch := make([]gotifyImportRecord, 0, len(messages))
	previous := before
	payloadBytes := 0
	for _, message := range messages {
		if message.ID == 0 || (previous != 0 && message.ID >= previous) {
			return nil, errors.New("gotify IDs must strictly descend below Before")
		}
		previous = message.ID
		message.Date = message.Date.UTC()
		// Conservative first-slice limits: stop the page rather than truncate or skip.
		if message.AppID == 0 || !utf8.ValidString(message.Title) || utf8.RuneCountInString(message.Title) > 512 || !utf8.ValidString(message.Body) || len(message.Body) > 64<<10 || len(message.Extras) > 16<<10 || message.Date.Unix() < 1 || message.Date.Unix() > 2147483647 {
			return nil, fmt.Errorf("gotify message %d cannot be represented by the message schema", message.ID)
		}
		if len(message.Extras) != 0 {
			if !utf8.Valid(message.Extras) || !gotifyValidSurrogates(message.Extras) {
				return nil, errors.New("gotify extras must be UTF-8 JSON")
			}
			decoder := json.NewDecoder(bytes.NewReader(message.Extras))
			decoder.UseNumber()
			extra, err := decodeGotifyJSON(decoder, 0)
			if err != nil {
				return nil, fmt.Errorf("gotify message %d extras: %w", message.ID, err)
			}
			if _, err := decoder.Token(); err != io.EOF {
				return nil, errors.New("gotify extras must contain one JSON value")
			}
			message.Extras, err = json.Marshal(extra)
			if err != nil {
				return nil, err
			}
		}
		payload, err := json.Marshal(message)
		if err != nil {
			return nil, err
		}
		payloadBytes += len(payload)
		if len(payload) > 128<<10 || payloadBytes > 8<<20 {
			return nil, errors.New("gotify canonical payload exceeds 128 KiB per message or 8 MiB per batch")
		}
		hash := sha256.Sum256(payload)
		batch = append(batch, gotifyImportRecord{message: message, payload: payload, hash: hex.EncodeToString(hash[:])})
	}
	return batch, nil
}

// encoding/json replaces unpaired UTF-16 escapes with U+FFFD; reject that loss.
// All remaining JSON syntax is validated by the standard decoder below.
func gotifyValidSurrogates(raw []byte) bool {
	for i := 0; i < len(raw); i++ {
		if raw[i] != '\\' {
			continue
		}
		i++
		if i >= len(raw) || raw[i] != 'u' {
			continue
		}
		if i+4 >= len(raw) {
			return false
		}
		value, err := strconv.ParseUint(string(raw[i+1:i+5]), 16, 16)
		if err != nil {
			return false
		}
		i += 4
		if value >= 0xdc00 && value <= 0xdfff {
			return false
		}
		if value < 0xd800 || value > 0xdbff {
			continue
		}
		if i+6 >= len(raw) || raw[i+1] != '\\' || raw[i+2] != 'u' {
			return false
		}
		low, err := strconv.ParseUint(string(raw[i+3:i+7]), 16, 16)
		if err != nil || low < 0xdc00 || low > 0xdfff {
			return false
		}
		i += 6
	}
	return true
}

// Decode without float64 coercion or silent duplicate-key loss. Marshal sorts object keys.
func decodeGotifyJSON(decoder *json.Decoder, depth int) (any, error) {
	if depth > 128 {
		return nil, errors.New("gotify extras nesting exceeds 128")
	}
	token, err := decoder.Token()
	if err != nil {
		return nil, err
	}
	switch value := token.(type) {
	case json.Number:
		canonical, err := canonicalGotifyNumber(string(value))
		if err != nil {
			return nil, err
		}
		// MySQL JSON stores integers as int64/uint64 and other numbers as doubles.
		if _, err := strconv.ParseInt(canonical, 10, 64); err != nil {
			if _, err := strconv.ParseUint(canonical, 10, 64); err != nil {
				f, err := strconv.ParseFloat(canonical, 64)
				if err != nil {
					return nil, errors.New("gotify extras number is not representable")
				}
				roundtrip, err := canonicalGotifyNumber(strconv.FormatFloat(f, 'g', -1, 64))
				if err != nil || roundtrip != canonical {
					return nil, errors.New("gotify extras number would lose precision")
				}
			}
		}
		return json.Number(canonical), nil
	case json.Delim:
		switch value {
		case '{':
			object := make(map[string]any)
			for decoder.More() {
				key, err := decoder.Token()
				if err != nil {
					return nil, err
				}
				name, ok := key.(string)
				if !ok {
					return nil, errors.New("invalid gotify extras object key")
				}
				if _, exists := object[name]; exists {
					return nil, errors.New("duplicate gotify extras object key")
				}
				item, err := decodeGotifyJSON(decoder, depth+1)
				if err != nil {
					return nil, err
				}
				object[name] = item
			}
			if _, err := decoder.Token(); err != nil {
				return nil, err
			}
			return object, nil
		case '[':
			array := make([]any, 0)
			for decoder.More() {
				item, err := decodeGotifyJSON(decoder, depth+1)
				if err != nil {
					return nil, err
				}
				array = append(array, item)
			}
			if _, err := decoder.Token(); err != nil {
				return nil, err
			}
			return array, nil
		}
		return nil, errors.New("unexpected gotify extras delimiter")
	default:
		return value, nil
	}
}

// Normalize decimal spelling exactly, including exponent forms, without rounding.
func canonicalGotifyNumber(raw string) (string, error) {
	negative := strings.HasPrefix(raw, "-")
	raw = strings.TrimPrefix(raw, "-")
	exponent := 0
	if i := strings.IndexAny(raw, "eE"); i >= 0 {
		n, err := strconv.Atoi(raw[i+1:])
		if err != nil || n < -10000 || n > 10000 {
			return "", errors.New("gotify extras exponent is not representable")
		}
		exponent, raw = n, raw[:i]
	}
	if i := strings.IndexByte(raw, '.'); i >= 0 {
		exponent -= len(raw) - i - 1
		raw = raw[:i] + raw[i+1:]
	}
	raw = strings.TrimLeft(raw, "0")
	if raw == "" {
		return "0", nil
	}
	trimmed := strings.TrimRight(raw, "0")
	exponent += len(raw) - len(trimmed)
	raw = trimmed
	point := len(raw) + exponent
	var out string
	switch {
	case point > 0 && point <= 21:
		if point >= len(raw) {
			out = raw + strings.Repeat("0", point-len(raw))
		} else {
			out = raw[:point] + "." + raw[point:]
		}
	case point <= 0 && point >= -5:
		out = "0." + strings.Repeat("0", -point) + raw
	default:
		out = raw[:1]
		if len(raw) > 1 {
			out += "." + raw[1:]
		}
		out += "e" + strconv.Itoa(point-1)
	}
	if negative {
		out = "-" + out
	}
	return out, nil
}
