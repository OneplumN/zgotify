package platform

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"strings"
	"testing"

	"github.com/go-sql-driver/mysql"
)

// This suite requires a fresh, disposable database. It never connects to the
// ordinary application DSN and deliberately leaves evidence for inspection.
func TestMySQLTwoPeopleTwoDevices(t *testing.T) {
	dsn := os.Getenv("ZGOTIFY_TEST_MYSQL_DSN")
	if path := os.Getenv("ZGOTIFY_TEST_MYSQL_DSN_FILE"); path != "" {
		if dsn != "" {
			t.Fatal("set only one integration DSN source")
		}
		data, err := os.ReadFile(path)
		if err != nil {
			t.Fatal("could not read integration DSN file")
		}
		dsn = strings.TrimRight(string(data), "\r\n")
		if dsn == "" {
			t.Fatal("integration DSN file is empty")
		}
	}
	if dsn == "" {
		t.Skip("set ZGOTIFY_TEST_MYSQL_DSN to a fresh zgotify_test_* database")
	}
	cfg, err := mysql.ParseDSN(dsn)
	if err != nil || !strings.HasPrefix(cfg.DBName, "zgotify_test_") {
		t.Fatal("integration database name must start with zgotify_test_")
	}
	one, err := Open(dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer one.DB.Close()
	two, err := Open(dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer two.DB.Close()
	ctx := context.Background()
	var existingTables int
	if err := one.DB.QueryRowContext(ctx, "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE()").Scan(&existingTables); err != nil {
		t.Fatal(err)
	}
	if existingTables != 0 {
		t.Fatal("integration database must be empty; existing data will not be reset")
	}
	if err := one.Migrate(ctx); err != nil {
		t.Fatal(err)
	}
	if _, err := one.Bootstrap(ctx, NewPerson{Username: "test_admin", DisplayName: "Test Admin", Password: "test-only-password-000"}); err != nil {
		t.Fatal(err)
	}
	adminLogin, err := one.Login(ctx, Login{Username: "test_admin", Password: "test-only-password-000", DeviceName: "test-cli"})
	if err != nil {
		t.Fatal(err)
	}
	admin, err := two.Session(ctx, adminLogin.Token)
	if err != nil {
		t.Fatal(err)
	}
	aliceID, err := one.CreatePerson(ctx, admin, NewPerson{Username: "alice", DisplayName: "Alice", Password: "test-only-password-111"})
	if err != nil {
		t.Fatal(err)
	}
	bobID, err := two.CreatePerson(ctx, admin, NewPerson{Username: "bob", DisplayName: "Bob", Password: "test-only-password-222"})
	if err != nil {
		t.Fatal(err)
	}
	topic, ingestToken, err := one.CreateTopic(ctx, admin, "shared alarms")
	if err != nil {
		t.Fatal(err)
	}
	for _, id := range []uint64{aliceID, bobID} {
		if err := one.Membership(ctx, admin, topic, id, true); err != nil {
			t.Fatal(err)
		}
	}
	payload := NewMessage{ExternalID: "gotify:test-source:42", Title: "CPU alert", Body: "**load is high**", Priority: 9}
	id, created, err := one.Ingest(ctx, ingestToken, payload)
	if err != nil || !created {
		t.Fatalf("ingest: %d %v %v", id, created, err)
	}
	again, created, err := two.Ingest(ctx, ingestToken, payload)
	if err != nil || created || again != id {
		t.Fatalf("duplicate: %d %v %v", again, created, err)
	}
	payload.Body = "different payload"
	if _, _, err := two.Ingest(ctx, ingestToken, payload); !errors.Is(err, ErrConflict) {
		t.Fatal("conflicting idempotency key accepted")
	}
	loginA, err := one.Login(ctx, Login{Username: "alice", Password: "test-only-password-111", DeviceName: "phone-a"})
	if err != nil {
		t.Fatal(err)
	}
	loginA2, err := two.Login(ctx, Login{Username: "alice", Password: "test-only-password-111", DeviceName: "phone-b"})
	if err != nil {
		t.Fatal(err)
	}
	loginB, err := one.Login(ctx, Login{Username: "bob", Password: "test-only-password-222", DeviceName: "phone-a"})
	if err != nil {
		t.Fatal(err)
	}
	alice, _ := one.Session(ctx, loginA.Token)
	alice2, _ := two.Session(ctx, loginA2.Token)
	bob, _ := two.Session(ctx, loginB.Token)
	if _, err := one.PatchState(ctx, alice, id, StatePatch{Read: ptr(true), Hidden: ptr(true), ExpectedVersion: ptr(uint64(0))}); err != nil {
		t.Fatal(err)
	}
	aMessages, err := two.Messages(ctx, alice2, 0, true)
	if err != nil || len(aMessages) != 1 || !aMessages[0].State.Read || !aMessages[0].State.Hidden || aMessages[0].ContentType != "text/plain" {
		t.Fatalf("cross-device state: %#v %v", aMessages, err)
	}
	bMessages, err := one.Messages(ctx, bob, 0, false)
	if err != nil || len(bMessages) != 1 || bMessages[0].State.Read || bMessages[0].State.Hidden || bMessages[0].ContentType != "text/plain" {
		t.Fatalf("personal isolation: %#v %v", bMessages, err)
	}
	for _, actor := range []Session{alice2, bob} {
		message, err := two.Message(ctx, actor, id)
		if err != nil || message.ContentType != "text/plain" || message.Body != "**load is high**" {
			t.Fatalf("direct ingest must stay plain: %#v %v", message, err)
		}
	}
	source := GotifySource{Key: "markdown-test", TopicID: topic, Identity: strings.Repeat("a", 64)}
	lease, err := one.ClaimGotify(ctx, source, strings.Repeat("b", 64))
	if err != nil {
		t.Fatal(err)
	}
	imported := gotifyTestMessage(100)
	imported.Body = "**imported alert**"
	imported.Extras = json.RawMessage(`{"client::display":{"contentType":"text/markdown"},"private":"fixture-secret"}`)
	if n, err := one.CommitGotify(ctx, lease, []GotifyMessage{imported}, 0); err != nil || n != 1 {
		t.Fatalf("markdown import: %d %v", n, err)
	}
	mixed, err := two.Messages(ctx, alice2, 0, true)
	if err != nil || len(mixed) != 2 || mixed[0].ID <= id || mixed[0].ContentType != "text/markdown" || mixed[0].State != (State{}) || mixed[1] != aMessages[0] {
		t.Fatalf("mixed inbox order, content type or state: %#v %v", mixed, err)
	}
	markdownID := mixed[0].ID
	if _, err := one.PatchState(ctx, alice, markdownID, StatePatch{Hidden: ptr(true), Favorite: ptr(true), ExpectedVersion: ptr(uint64(0))}); err != nil {
		t.Fatal(err)
	}
	for _, actor := range []Session{alice2, bob} {
		message, err := two.Message(ctx, actor, markdownID)
		wantState := State{}
		if actor.Person.ID == aliceID {
			wantState = State{Hidden: true, Favorite: true, Version: 1}
		}
		if err != nil || message.ContentType != "text/markdown" || message.Body != imported.Body || message.State != wantState {
			t.Fatalf("imported detail and personal state: %#v %v", message, err)
		}
	}
	visible, err := one.Messages(ctx, alice2, 0, false)
	if err != nil || len(visible) != 0 {
		t.Fatalf("hidden filter changed: %#v %v", visible, err)
	}
	older, err := two.Messages(ctx, alice2, markdownID, true)
	if err != nil || len(older) != 1 || older[0] != aMessages[0] {
		t.Fatalf("before cursor changed: %#v %v", older, err)
	}
	bobInbox, err := one.Messages(ctx, bob, 0, false)
	if err != nil || len(bobInbox) != 2 || bobInbox[0].ContentType != "text/markdown" || bobInbox[0].State != (State{}) || bobInbox[1] != bMessages[0] {
		t.Fatalf("imported inbox personal isolation: %#v %v", bobInbox, err)
	}
	if _, err := one.Message(ctx, admin, markdownID); !errors.Is(err, ErrNotFound) {
		t.Fatalf("administrator bypassed import membership: %v", err)
	}
	if _, err := two.PatchState(ctx, alice2, id, StatePatch{Read: ptr(false), ExpectedVersion: ptr(uint64(0))}); !errors.Is(err, ErrConflict) {
		t.Fatal("stale device update accepted")
	}
	if err := one.Membership(ctx, admin, topic, bobID, false); err != nil {
		t.Fatal(err)
	}
	for _, messageID := range []uint64{id, markdownID} {
		if _, err := two.Message(ctx, bob, messageID); !errors.Is(err, ErrNotFound) {
			t.Fatalf("revoked membership still allowed detail: %v", err)
		}
	}
	if messages, err := two.Messages(ctx, bob, 0, true); err != nil || len(messages) != 0 {
		t.Fatalf("revoked membership still allowed inbox: %#v %v", messages, err)
	}
	if _, err := two.PatchState(ctx, bob, id, StatePatch{Read: ptr(true), ExpectedVersion: ptr(uint64(0))}); !errors.Is(err, ErrNotFound) {
		t.Fatal("revoked membership still allowed mutation")
	}
	if err := two.Logout(ctx, alice); err != nil {
		t.Fatal(err)
	}
	if _, err := one.Session(ctx, loginA.Token); !errors.Is(err, ErrUnauthorized) {
		t.Fatal("logout was not shared")
	}
	if _, err := one.Session(ctx, loginA2.Token); err != nil {
		t.Fatal("logout incorrectly revoked another device")
	}
	if err := one.SetEnabled(ctx, admin, aliceID, false); err != nil {
		t.Fatal(err)
	}
	if _, err := two.Session(ctx, loginA2.Token); !errors.Is(err, ErrUnauthorized) {
		t.Fatal("disabled user remained authenticated")
	}
	events, err := two.Audit(ctx, admin, 0)
	if err != nil {
		t.Fatal(err)
	}
	found := false
	for _, e := range events {
		if e.Action == "update_message_state" {
			if e.ActorID != aliceID || e.DeviceName != "phone-a" {
				t.Fatal("incorrect audit attribution")
			}
			found = true
		}
	}
	if !found {
		t.Fatal("missing state audit")
	}
}
