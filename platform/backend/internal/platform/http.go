package platform

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"strconv"
	"strings"
	"time"
)

type API struct{ Store *Store }

func (a *API) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) {
		if err := a.Store.Ready(r.Context()); err != nil {
			writeJSON(w, 503, map[string]string{"error": "database unavailable or migration required"})
			return
		}
		writeJSON(w, 200, map[string]string{"status": "ok", "version": "0.2.0-internal"})
	})
	mux.HandleFunc("POST /v1/login", func(w http.ResponseWriter, r *http.Request) {
		var in Login
		if !readJSON(w, r, &in) {
			return
		}
		out, err := a.Store.Login(r.Context(), in)
		respond(w, out, err)
	})
	mux.HandleFunc("POST /v1/ingest", func(w http.ResponseWriter, r *http.Request) {
		var in NewMessage
		if !readJSON(w, r, &in) {
			return
		}
		if err := in.Validate(); err != nil {
			badRequest(w, err)
			return
		}
		id, created, err := a.Store.Ingest(r.Context(), bearer(r), in)
		respond(w, map[string]any{"id": id, "created": created}, err)
	})
	mux.HandleFunc("GET /v1/me", a.auth(false, func(w http.ResponseWriter, r *http.Request, s Session) { writeJSON(w, 200, s) }))
	mux.HandleFunc("DELETE /v1/session", a.auth(false, func(w http.ResponseWriter, r *http.Request, s Session) {
		respond(w, map[string]bool{"ok": true}, a.Store.Logout(r.Context(), s))
	}))
	mux.HandleFunc("POST /v1/password", a.auth(false, func(w http.ResponseWriter, r *http.Request, s Session) {
		var in struct {
			Old string `json:"old_password"`
			New string `json:"new_password"`
		}
		if !readJSON(w, r, &in) {
			return
		}
		if err := validatePassword(in.New); err != nil {
			badRequest(w, err)
			return
		}
		respond(w, map[string]bool{"reauthenticate": true}, a.Store.ChangePassword(r.Context(), s, in.Old, in.New))
	}))
	mux.HandleFunc("GET /v1/topics", a.auth(false, func(w http.ResponseWriter, r *http.Request, s Session) {
		out, err := a.Store.Topics(r.Context(), s)
		respond(w, map[string]any{"topics": out}, err)
	}))
	mux.HandleFunc("GET /v1/messages", a.auth(false, func(w http.ResponseWriter, r *http.Request, s Session) {
		before, ok := page(w, r)
		if !ok {
			return
		}
		hidden := r.URL.Query().Get("include_hidden")
		if hidden != "" && hidden != "true" && hidden != "false" {
			badRequest(w, errors.New("include_hidden must be true or false"))
			return
		}
		out, err := a.Store.Messages(r.Context(), s, before, hidden == "true")
		respond(w, map[string]any{"messages": out}, err)
	}))
	mux.HandleFunc("GET /v1/messages/{id}", a.auth(false, func(w http.ResponseWriter, r *http.Request, s Session) {
		id, ok := pathID(w, r, "id")
		if !ok {
			return
		}
		out, err := a.Store.Message(r.Context(), s, id)
		respond(w, map[string]any{"message": out}, err)
	}))
	mux.HandleFunc("PATCH /v1/messages/{id}/state", a.auth(false, func(w http.ResponseWriter, r *http.Request, s Session) {
		id, ok := pathID(w, r, "id")
		if !ok {
			return
		}
		var in StatePatch
		if !readJSON(w, r, &in) {
			return
		}
		if in.ExpectedVersion == nil || (in.Read == nil && in.Hidden == nil && in.Favorite == nil) {
			badRequest(w, errors.New("expected_version and at least one state field required"))
			return
		}
		out, err := a.Store.PatchState(r.Context(), s, id, in)
		respond(w, out, err)
	}))
	mux.HandleFunc("GET /v1/audit", a.auth(false, func(w http.ResponseWriter, r *http.Request, s Session) {
		before, ok := page(w, r)
		if !ok {
			return
		}
		out, err := a.Store.Audit(r.Context(), s, before)
		respond(w, map[string]any{"events": out}, err)
	}))
	mux.HandleFunc("POST /v1/admin/users", a.auth(true, func(w http.ResponseWriter, r *http.Request, s Session) {
		var in NewPerson
		if !readJSON(w, r, &in) {
			return
		}
		if err := in.Validate(); err != nil {
			badRequest(w, err)
			return
		}
		id, err := a.Store.CreatePerson(r.Context(), s, in)
		respond(w, map[string]any{"id": id}, err)
	}))
	mux.HandleFunc("PATCH /v1/admin/users/{id}", a.auth(true, func(w http.ResponseWriter, r *http.Request, s Session) {
		id, ok := pathID(w, r, "id")
		if !ok {
			return
		}
		var in struct {
			Enabled *bool `json:"enabled"`
		}
		if !readJSON(w, r, &in) {
			return
		}
		if in.Enabled == nil {
			badRequest(w, errors.New("enabled required"))
			return
		}
		respond(w, map[string]bool{"ok": true}, a.Store.SetEnabled(r.Context(), s, id, *in.Enabled))
	}))
	mux.HandleFunc("POST /v1/admin/topics", a.auth(true, func(w http.ResponseWriter, r *http.Request, s Session) {
		var in struct {
			Name string `json:"name"`
		}
		if !readJSON(w, r, &in) {
			return
		}
		if !validText(in.Name, 1, 128) {
			badRequest(w, errors.New("name must be 1-128 characters"))
			return
		}
		id, token, err := a.Store.CreateTopic(r.Context(), s, in.Name)
		respond(w, map[string]any{"id": id, "ingest_token": token}, err)
	}))
	for _, method := range []string{"PUT", "DELETE"} {
		mux.HandleFunc(method+" /v1/admin/topics/{topic}/members/{person}", a.auth(true, func(w http.ResponseWriter, r *http.Request, s Session) {
			topic, ok := pathID(w, r, "topic")
			if !ok {
				return
			}
			person, ok := pathID(w, r, "person")
			if !ok {
				return
			}
			respond(w, map[string]bool{"ok": true}, a.Store.Membership(r.Context(), s, topic, person, r.Method == "PUT"))
		}))
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		w.Header().Set("X-Content-Type-Options", "nosniff")
		ctx, cancel := context.WithTimeout(r.Context(), 20*time.Second)
		defer cancel()
		mux.ServeHTTP(w, r.WithContext(ctx))
	})
}

func (a *API) auth(admin bool, next func(http.ResponseWriter, *http.Request, Session)) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		s, err := a.Store.Session(r.Context(), bearer(r))
		if err != nil {
			respond(w, nil, err)
			return
		}
		if admin && !s.Person.Admin {
			respond(w, nil, ErrForbidden)
			return
		}
		next(w, r, s)
	}
}

func bearer(r *http.Request) string {
	parts := strings.Fields(r.Header.Get("Authorization"))
	if len(parts) != 2 || !strings.EqualFold(parts[0], "Bearer") {
		return ""
	}
	return parts[1]
}

func readJSON(w http.ResponseWriter, r *http.Request, out any) bool {
	if strings.TrimSpace(strings.Split(r.Header.Get("Content-Type"), ";")[0]) != "application/json" {
		badRequest(w, errors.New("Content-Type must be application/json"))
		return false
	}
	r.Body = http.MaxBytesReader(w, r.Body, 1<<20)
	decoder := json.NewDecoder(r.Body)
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(out); err != nil {
		badRequest(w, errors.New("invalid JSON or unknown field"))
		return false
	}
	if err := decoder.Decode(new(any)); err != io.EOF {
		badRequest(w, errors.New("expected exactly one JSON value"))
		return false
	}
	return true
}

func pathID(w http.ResponseWriter, r *http.Request, name string) (uint64, bool) {
	id, err := strconv.ParseUint(r.PathValue(name), 10, 64)
	if err != nil || id == 0 {
		badRequest(w, errors.New("invalid id"))
		return 0, false
	}
	return id, true
}

func page(w http.ResponseWriter, r *http.Request) (uint64, bool) {
	value := r.URL.Query().Get("before")
	if value == "" {
		return 0, true
	}
	n, err := strconv.ParseUint(value, 10, 64)
	if err != nil {
		badRequest(w, errors.New("invalid before cursor"))
		return 0, false
	}
	return n, true
}

func badRequest(w http.ResponseWriter, err error) {
	writeJSON(w, 400, map[string]string{"error": err.Error()})
}

func respond(w http.ResponseWriter, out any, err error) {
	if err == nil {
		writeJSON(w, 200, out)
		return
	}
	status := 500
	switch {
	case errors.Is(err, ErrUnauthorized):
		status = 401
	case errors.Is(err, ErrForbidden):
		status = 403
	case errors.Is(err, ErrNotFound):
		status = 404
	case errors.Is(err, ErrConflict):
		status = 409
	case errors.Is(err, ErrRateLimited):
		status = 429
		w.Header().Set("Retry-After", "300")
	}
	message := err.Error()
	if status == 500 {
		slog.Error("request failed", "error_type", "internal")
		message = "internal error"
	}
	writeJSON(w, status, map[string]string{"error": message})
}

func writeJSON(w http.ResponseWriter, status int, out any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(out)
}
