package gotify

import (
	"bytes"
	"context"
	"crypto/sha256"
	"crypto/tls"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"zgotify.local/platform/internal/platform"
)

const pageSize = 100

var sourceKey = regexp.MustCompile(`^[a-z0-9][a-z0-9._-]{0,63}$`)

type Config struct {
	Sources []SourceConfig `json:"sources"`
}
type SourceConfig struct {
	Key             string `json:"key"`
	URL             string `json:"url"`
	UserID          uint64 `json:"user_id"`
	TopicID         uint64 `json:"topic_id"`
	AppID           uint64 `json:"application_id,omitempty"`
	ClientTokenFile string `json:"client_token_file"`
	AllowHTTP       bool   `json:"allow_http,omitempty"`
}

type Source struct {
	config SourceConfig
	base   *url.URL
	client *http.Client
}

func LoadConfig(path string) ([]*Source, error) {
	data, err := readLimitedFile(path, 65536)
	if err != nil {
		return nil, errors.New("cannot read sources configuration")
	}
	var config Config
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if decoder.Decode(&config) != nil || decoder.Decode(new(any)) != io.EOF || len(config.Sources) == 0 || len(config.Sources) > 16 {
		return nil, errors.New("invalid sources configuration (1-16 sources required)")
	}
	seen := map[string]bool{}
	identities := map[string]bool{}
	out := make([]*Source, 0, len(config.Sources))
	for _, c := range config.Sources {
		if seen[c.Key] {
			return nil, errors.New("duplicate source key")
		}
		s, err := NewSource(c)
		if err != nil {
			return nil, err
		}
		if identities[s.Descriptor().Identity] {
			return nil, errors.New("duplicate logical upstream source")
		}
		seen[c.Key] = true
		identities[s.Descriptor().Identity] = true
		out = append(out, s)
	}
	return out, nil
}

func NewSource(config SourceConfig) (*Source, error) {
	if !sourceKey.MatchString(config.Key) || config.UserID == 0 || config.TopicID == 0 || config.ClientTokenFile == "" {
		return nil, errors.New("source requires valid key, user_id, topic_id and client_token_file")
	}
	u, err := url.Parse(config.URL)
	if err != nil || u.Hostname() == "" || u.User != nil || u.RawQuery != "" || u.ForceQuery || u.Fragment != "" || u.Opaque != "" || (u.Scheme != "https" && !(u.Scheme == "http" && config.AllowHTTP)) {
		return nil, errors.New("source URL requires HTTPS without credentials, query or fragment; isolated HTTP must be explicitly allowed")
	}
	if strings.ContainsAny(u.Path, "\\\x00\r\n") || u.RawPath != "" {
		return nil, errors.New("source URL contains unsafe path")
	}
	for _, part := range strings.Split(u.Path, "/") {
		if part == "." || part == ".." {
			return nil, errors.New("source URL contains unsafe path")
		}
	}
	u.Path = strings.TrimRight(u.Path, "/")
	config.URL = u.String()
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = nil
	transport.TLSClientConfig = &tls.Config{MinVersion: tls.VersionTLS12}
	transport.ResponseHeaderTimeout = 10 * time.Second
	return &Source{config: config, base: u, client: &http.Client{Transport: transport, Timeout: 15 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}}, nil
}

func (s *Source) Descriptor() platform.GotifySource {
	identity := sha256.Sum256([]byte(fmt.Sprintf("%s|user=%d|app=%d", s.config.URL, s.config.UserID, s.config.AppID)))
	return platform.GotifySource{Key: s.config.Key, TopicID: s.config.TopicID, Identity: hex.EncodeToString(identity[:])}
}

func readLimitedFile(path string, limit int64) ([]byte, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	b, err := io.ReadAll(io.LimitReader(f, limit+1))
	if err != nil || int64(len(b)) > limit {
		return nil, errors.New("file exceeds limit or is unreadable")
	}
	return b, nil
}

func (s *Source) get(ctx context.Context, path string, query url.Values, token string, out any) error {
	u := *s.base
	u.Path += path
	u.RawQuery = query.Encode()
	r, err := http.NewRequestWithContext(ctx, http.MethodGet, u.String(), nil)
	if err != nil {
		return errors.New("invalid upstream request")
	}
	r.Header.Set("X-Gotify-Key", token)
	r.Header.Set("Accept", "application/json")
	resp, err := s.client.Do(r)
	if err != nil {
		return errors.New("upstream request unavailable (network, TLS or timeout)")
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("upstream HTTP status %d", resp.StatusCode)
	}
	const maxPageBytes = 32 << 20
	b, err := io.ReadAll(io.LimitReader(resp.Body, maxPageBytes+1))
	if err != nil || len(b) > maxPageBytes || !utf8.Valid(b) {
		return errors.New("upstream response unreadable or too large")
	}
	decoder := json.NewDecoder(bytes.NewReader(b))
	if decoder.Decode(out) != nil || decoder.Decode(new(any)) != io.EOF {
		return errors.New("invalid upstream JSON")
	}
	return nil
}

// Page constructs pagination locally. Upstream paging.next is never followed:
// it may point at an external ALB hostname or carry credentials in its query.
func (s *Source) Page(ctx context.Context, before uint64) ([]platform.GotifyMessage, uint64, error) {
	secret, err := readLimitedFile(s.config.ClientTokenFile, 4096)
	if err != nil {
		return nil, 0, errors.New("cannot read upstream client token file")
	}
	token := strings.TrimRight(string(secret), "\r\n")
	if token == "" {
		return nil, 0, errors.New("empty upstream client token")
	}
	for _, c := range token {
		if c < 33 || c > 126 {
			return nil, 0, errors.New("invalid upstream client token")
		}
	}
	var user struct {
		ID uint64 `json:"id"`
	}
	if err := s.get(ctx, "/current/user", nil, token, &user); err != nil {
		return nil, 0, err
	}
	if user.ID != s.config.UserID {
		return nil, 0, errors.New("upstream account does not match configured user_id")
	}
	path := "/message"
	if s.config.AppID != 0 {
		path = fmt.Sprintf("/application/%d/message", s.config.AppID)
	}
	query := url.Values{"limit": {strconv.Itoa(pageSize)}}
	if before != 0 {
		query.Set("since", strconv.FormatUint(before, 10))
	}
	var page struct {
		Messages []platform.GotifyMessage `json:"messages"`
	}
	if err := s.get(ctx, path, query, token, &page); err != nil {
		return nil, 0, err
	}
	if page.Messages == nil || len(page.Messages) > pageSize {
		return nil, 0, errors.New("invalid upstream message page")
	}
	previous := before
	for _, m := range page.Messages {
		if m.ID == 0 || (previous != 0 && m.ID >= previous) || m.AppID == 0 || (s.config.AppID != 0 && m.AppID != s.config.AppID) {
			return nil, 0, errors.New("upstream page has invalid identity or non-descending IDs")
		}
		previous = m.ID
	}
	if len(page.Messages) < pageSize {
		return page.Messages, 0, nil
	}
	return page.Messages, previous, nil
}
