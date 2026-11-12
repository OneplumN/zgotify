package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestDatabaseDSN(t *testing.T) {
	t.Setenv("ZGOTIFY_MYSQL_DSN", "")
	t.Setenv("ZGOTIFY_MYSQL_DSN_FILE", "")
	if _, err := databaseDSN(); err == nil {
		t.Fatal("missing configuration accepted")
	}
	want := "test:synthetic@tcp(database:3306)/zgotify_test_config"
	t.Setenv("ZGOTIFY_MYSQL_DSN", want)
	if got, err := databaseDSN(); err != nil || got != want {
		t.Fatal("environment configuration failed")
	}
	path := filepath.Join(t.TempDir(), "mysql_dsn")
	if err := os.WriteFile(path, []byte(want+"\r\n"), 0600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("ZGOTIFY_MYSQL_DSN_FILE", path)
	if _, err := databaseDSN(); err == nil {
		t.Fatal("ambiguous configuration accepted")
	}
	t.Setenv("ZGOTIFY_MYSQL_DSN", "")
	if got, err := databaseDSN(); err != nil || got != want {
		t.Fatal("file configuration failed")
	}
	for _, input := range []string{"\n", strings.Repeat("x", 8193)} {
		if err := os.WriteFile(path, []byte(input), 0600); err != nil {
			t.Fatal(err)
		}
		if _, err := databaseDSN(); err == nil {
			t.Fatal("invalid secret accepted")
		}
	}
	t.Setenv("ZGOTIFY_MYSQL_DSN_FILE", filepath.Join(t.TempDir(), "missing"))
	if _, err := databaseDSN(); err == nil {
		t.Fatal("missing secret file accepted")
	}
}
