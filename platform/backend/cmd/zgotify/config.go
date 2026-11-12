package main

import (
	"errors"
	"io"
	"os"
	"strings"
)

// File-backed secrets keep credentials out of container environment inspection.
func databaseDSN() (string, error) {
	dsn := os.Getenv("ZGOTIFY_MYSQL_DSN")
	path := os.Getenv("ZGOTIFY_MYSQL_DSN_FILE")
	if dsn != "" && path != "" {
		return "", errors.New("set only one of ZGOTIFY_MYSQL_DSN and ZGOTIFY_MYSQL_DSN_FILE")
	}
	if path != "" {
		file, err := os.Open(path)
		if err != nil {
			return "", errors.New("could not open database secret file")
		}
		defer file.Close()
		data, err := io.ReadAll(io.LimitReader(file, 8193))
		if err != nil || len(data) > 8192 {
			return "", errors.New("could not read database secret file or file exceeds 8192 bytes")
		}
		dsn = strings.TrimRight(string(data), "\r\n")
	}
	if dsn == "" {
		return "", errors.New("database DSN is required; use a separate zgotify database")
	}
	return dsn, nil
}
