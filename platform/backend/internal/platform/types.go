package platform

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"regexp"
	"strings"
	"time"
	"unicode/utf8"
)

var (
	ErrUnauthorized = errors.New("invalid or expired credentials")
	ErrForbidden    = errors.New("permission denied")
	ErrNotFound     = errors.New("not found")
	ErrConflict     = errors.New("conflict: refresh current server state")
	ErrRateLimited  = errors.New("too many login attempts; retry later")
	usernamePattern = regexp.MustCompile(`^[a-z0-9][a-z0-9._-]{2,63}$`)
)

type Person struct {
	ID          uint64 `json:"id"`
	Username    string `json:"username"`
	DisplayName string `json:"display_name"`
	Admin       bool   `json:"admin"`
}

type Session struct {
	Person     Person `json:"person"`
	TokenHash  string `json:"-"`
	DeviceName string `json:"device_name"`
}

type Login struct {
	Username   string `json:"username"`
	Password   string `json:"password"`
	DeviceName string `json:"device_name"`
}

type LoginResult struct {
	Token     string    `json:"token"`
	ExpiresAt time.Time `json:"expires_at"`
	Person    Person    `json:"person"`
}

type NewPerson struct {
	Username    string `json:"username"`
	DisplayName string `json:"display_name"`
	Password    string `json:"password"`
	Admin       bool   `json:"admin"`
}

type State struct {
	Read     bool   `json:"read"`
	Hidden   bool   `json:"hidden"`
	Favorite bool   `json:"favorite"`
	Version  uint64 `json:"version"`
}

type StatePatch struct {
	Read            *bool   `json:"read,omitempty"`
	Hidden          *bool   `json:"hidden,omitempty"`
	Favorite        *bool   `json:"favorite,omitempty"`
	ExpectedVersion *uint64 `json:"expected_version"`
}

type Topic struct {
	ID   uint64 `json:"id"`
	Name string `json:"name"`
}

type Message struct {
	ID        uint64    `json:"id"`
	TopicID   uint64    `json:"topic_id"`
	Title     string    `json:"title"`
	Body      string    `json:"message"`
	Priority  int       `json:"priority"`
	CreatedAt time.Time `json:"created_at"`
	State     State     `json:"state"`
}

type NewMessage struct {
	ExternalID string `json:"external_id"`
	Title      string `json:"title"`
	Body       string `json:"message"`
	Priority   int    `json:"priority"`
}

type Audit struct {
	ID         uint64          `json:"id"`
	ActorID    uint64          `json:"actor_id"`
	DeviceName string          `json:"device_name"`
	Action     string          `json:"action"`
	ObjectID   uint64          `json:"object_id"`
	Detail     json.RawMessage `json:"detail"`
	CreatedAt  time.Time       `json:"created_at"`
}

func normalizeUsername(s string) string { return strings.ToLower(strings.TrimSpace(s)) }

func (p NewPerson) Validate() error {
	if !usernamePattern.MatchString(normalizeUsername(p.Username)) {
		return errors.New("username must be 3-64 ASCII letters, digits, dots, hyphens or underscores")
	}
	if !validText(p.DisplayName, 1, 128) {
		return errors.New("display_name must be 1-128 characters")
	}
	return validatePassword(p.Password)
}

func validatePassword(p string) error {
	if len(p) < 12 || len(p) > 72 {
		return errors.New("password must be 12-72 UTF-8 bytes")
	}
	return nil
}

func validText(s string, min, max int) bool {
	return utf8.ValidString(s) && utf8.RuneCountInString(strings.TrimSpace(s)) >= min && utf8.RuneCountInString(s) <= max
}

func (p StatePatch) Apply(s State) (State, error) {
	if p.ExpectedVersion == nil || (p.Read == nil && p.Hidden == nil && p.Favorite == nil) {
		return s, errors.New("expected_version and at least one state field are required")
	}
	if *p.ExpectedVersion != s.Version {
		return s, ErrConflict
	}
	if p.Read != nil {
		s.Read = *p.Read
	}
	if p.Hidden != nil {
		s.Hidden = *p.Hidden
	}
	if p.Favorite != nil {
		s.Favorite = *p.Favorite
	}
	s.Version++
	return s, nil
}

func (m NewMessage) Validate() error {
	if len(m.ExternalID) == 0 || len(m.ExternalID) > 191 {
		return errors.New("external_id must be 1-191 ASCII characters")
	}
	for _, c := range m.ExternalID {
		if c < 33 || c > 126 {
			return errors.New("external_id must contain printable non-space ASCII")
		}
	}
	if !validText(m.Title, 0, 512) || !validText(m.Body, 1, 65536) {
		return errors.New("invalid title or message length")
	}
	if m.Priority < 0 || m.Priority > 10 {
		return errors.New("priority must be 0-10")
	}
	return nil
}

func newToken() (string, error) {
	b := make([]byte, 32)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	return hex.EncodeToString(b), nil
}

func tokenHash(s string) string {
	h := sha256.Sum256([]byte(s))
	return hex.EncodeToString(h[:])
}
