package platform

import (
	"context"
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
	payload := NewMessage{ExternalID: "gotify:test-source:42", Title: "CPU alert", Body: "load is high", Priority: 9}
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
	if err != nil || len(aMessages) != 1 || !aMessages[0].State.Read || !aMessages[0].State.Hidden {
		t.Fatalf("cross-device state: %#v %v", aMessages, err)
	}
	bMessages, err := one.Messages(ctx, bob, 0, false)
	if err != nil || len(bMessages) != 1 || bMessages[0].State.Read || bMessages[0].State.Hidden {
		t.Fatalf("personal isolation: %#v %v", bMessages, err)
	}
	if _, err := two.PatchState(ctx, alice2, id, StatePatch{Read: ptr(false), ExpectedVersion: ptr(uint64(0))}); !errors.Is(err, ErrConflict) {
		t.Fatal("stale device update accepted")
	}
	if err := one.Membership(ctx, admin, topic, bobID, false); err != nil {
		t.Fatal(err)
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
