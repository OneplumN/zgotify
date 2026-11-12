package main

import (
	"bufio"
	"context"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"zgotify.local/platform/internal/platform"
)

func main() {
	if err := run(); err != nil {
		slog.Error("zgotify stopped", "reason", err.Error())
		os.Exit(1)
	}
}

func run() error {
	command := flag.String("command", "serve", "serve, migrate, bootstrap, or import")
	sources := flag.String("sources", "", "protected Gotify source configuration file (import only)")
	interval := flag.Duration("interval", 10*time.Second, "pause between complete import scans (5s-5m)")
	once := flag.Bool("once", false, "complete one import scan and exit")
	username := flag.String("username", "", "bootstrap administrator username")
	name := flag.String("name", "", "bootstrap administrator display name")
	listen := flag.String("listen", "127.0.0.1:8090", "listen address; use private interface/container port behind TLS ALB")
	flag.Parse()
	dsn, err := databaseDSN()
	if err != nil {
		return err
	}
	store, err := platform.Open(dsn)
	if err != nil {
		return err
	}
	defer store.DB.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	switch *command {
	case "migrate":
		if err := store.Migrate(ctx); err != nil {
			return errors.New("database migration failed; check connectivity, database privileges and schema")
		}
		fmt.Println("schema version 2 ready")
		return nil
	case "bootstrap":
		fmt.Fprintln(os.Stderr, "Read administrator password from stdin (pipe from a secret source; avoid typing into an echoing terminal).")
		line, err := bufio.NewReader(os.Stdin).ReadString('\n')
		if err != nil && len(line) == 0 {
			return errors.New("could not read bootstrap password")
		}
		password := strings.TrimSuffix(strings.TrimSuffix(line, "\n"), "\r")
		id, err := store.Bootstrap(ctx, platform.NewPerson{Username: *username, DisplayName: *name, Password: password})
		if err != nil {
			return errors.New("bootstrap failed; validate input, connectivity and that no users already exist")
		}
		fmt.Printf("administrator created: %d\n", id)
		return nil
	case "serve":
	case "import":
		if err := store.Ready(ctx); err != nil {
			return errors.New("database not ready; run explicit migration before importing")
		}
		stopped, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
		defer stop()
		return importMessages(stopped, store, *sources, *interval, *once)
	default:
		return errors.New("unknown command")
	}
	if err := store.Ready(ctx); err != nil {
		return errors.New("database not ready; check connectivity and run explicit migration first")
	}
	server := &http.Server{Addr: *listen, Handler: (&platform.API{Store: store}).Handler(), ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 25 * time.Second, WriteTimeout: 25 * time.Second, IdleTimeout: 60 * time.Second, MaxHeaderBytes: 16384}
	stopped, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	finished := make(chan struct{})
	go func() {
		defer close(finished)
		<-stopped.Done()
		shutdown, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		if err := server.Shutdown(shutdown); err != nil {
			server.Close()
		}
	}()
	slog.Info("zgotify internal API starting", "listen", *listen)
	err = server.ListenAndServe()
	stop()
	<-finished
	if errors.Is(err, http.ErrServerClosed) {
		return nil
	}
	return err
}
