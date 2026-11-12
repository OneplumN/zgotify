package platform

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
)

func (s *Store) CreateTopic(ctx context.Context, actor Session, name string) (uint64, string, error) {
	if !actor.Person.Admin {
		return 0, "", ErrForbidden
	}
	token, err := newToken()
	if err != nil {
		return 0, "", err
	}
	tx, err := s.DB.BeginTx(ctx, nil)
	if err != nil {
		return 0, "", err
	}
	defer tx.Rollback()
	result, err := tx.ExecContext(ctx, "INSERT INTO topics(name,ingest_token_hash) VALUES(?,?)", name, tokenHash(token))
	if err != nil {
		return 0, "", err
	}
	id, err := result.LastInsertId()
	if err != nil {
		return 0, "", err
	}
	if err := audit(ctx, tx, actor, "create_topic", uint64(id), map[string]string{"name": name}); err != nil {
		return 0, "", err
	}
	return uint64(id), token, tx.Commit()
}

func (s *Store) Membership(ctx context.Context, actor Session, topic, person uint64, add bool) error {
	if !actor.Person.Admin {
		return ErrForbidden
	}
	tx, err := s.DB.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if add {
		_, err = tx.ExecContext(ctx, "INSERT INTO memberships(topic_id,person_id) VALUES(?,?) ON DUPLICATE KEY UPDATE person_id=VALUES(person_id)", topic, person)
	} else {
		_, err = tx.ExecContext(ctx, "DELETE FROM memberships WHERE topic_id=? AND person_id=?", topic, person)
	}
	if err != nil {
		return mapDatabaseError(err)
	}
	if err := audit(ctx, tx, actor, "set_membership", topic, map[string]any{"person_id": person, "member": add}); err != nil {
		return err
	}
	return tx.Commit()
}

func (s *Store) Ingest(ctx context.Context, token string, in NewMessage) (uint64, bool, error) {
	if len(token) != 64 {
		return 0, false, ErrUnauthorized
	}
	tx, err := s.DB.BeginTx(ctx, nil)
	if err != nil {
		return 0, false, err
	}
	defer tx.Rollback()
	var topic uint64
	if err := tx.QueryRowContext(ctx, "SELECT id FROM topics WHERE ingest_token_hash=?", tokenHash(token)).Scan(&topic); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return 0, false, ErrUnauthorized
		}
		return 0, false, err
	}
	result, err := tx.ExecContext(ctx, "INSERT INTO messages(topic_id,external_id,title,body,priority) VALUES(?,?,?,?,?)", topic, in.ExternalID, in.Title, in.Body, in.Priority)
	if errors.Is(mapDatabaseError(err), ErrConflict) {
		var existing NewMessage
		var id uint64
		err = tx.QueryRowContext(ctx, "SELECT id,title,body,priority FROM messages WHERE topic_id=? AND external_id=? FOR UPDATE", topic, in.ExternalID).
			Scan(&id, &existing.Title, &existing.Body, &existing.Priority)
		if err != nil {
			return 0, false, err
		}
		if existing.Title != in.Title || existing.Body != in.Body || existing.Priority != in.Priority {
			return 0, false, ErrConflict
		}
		return id, false, tx.Commit()
	}
	if err != nil {
		return 0, false, err
	}
	id, err := result.LastInsertId()
	if err != nil {
		return 0, false, err
	}
	if _, err := tx.ExecContext(ctx, "INSERT INTO delivery_outbox(message_id) VALUES(?)", id); err != nil {
		return 0, false, err
	}
	return uint64(id), true, tx.Commit()
}

func (s *Store) Topics(ctx context.Context, actor Session) ([]Topic, error) {
	rows, err := s.DB.QueryContext(ctx, `SELECT t.id,t.name FROM topics t
JOIN memberships mb ON mb.topic_id=t.id
WHERE mb.person_id=? ORDER BY t.id`, actor.Person.ID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := make([]Topic, 0)
	for rows.Next() {
		var topic Topic
		if err := rows.Scan(&topic.ID, &topic.Name); err != nil {
			return nil, err
		}
		out = append(out, topic)
	}
	return out, rows.Err()
}

func (s *Store) Message(ctx context.Context, actor Session, id uint64) (Message, error) {
	var out Message
	var payload []byte
	err := s.DB.QueryRowContext(ctx, `SELECT m.id,m.topic_id,m.title,m.body,m.priority,m.created_at,
COALESCE(st.is_read,FALSE),COALESCE(st.is_hidden,FALSE),COALESCE(st.is_favorite,FALSE),COALESCE(st.version,0),g.payload
FROM messages m JOIN memberships mb ON mb.topic_id=m.topic_id AND mb.person_id=?
LEFT JOIN message_states st ON st.message_id=m.id AND st.person_id=?
LEFT JOIN gotify_import_metadata g ON g.message_id=m.id
WHERE m.id=?`, actor.Person.ID, actor.Person.ID, id).
		Scan(&out.ID, &out.TopicID, &out.Title, &out.Body, &out.Priority, &out.CreatedAt, &out.State.Read, &out.State.Hidden, &out.State.Favorite, &out.State.Version, &payload)
	out.ContentType = messageContentType(payload)
	return out, mapDatabaseError(err)
}

func (s *Store) Messages(ctx context.Context, actor Session, before uint64, includeHidden bool) ([]Message, error) {
	rows, err := s.DB.QueryContext(ctx, `SELECT m.id,m.topic_id,m.title,m.body,m.priority,m.created_at,
COALESCE(st.is_read,FALSE),COALESCE(st.is_hidden,FALSE),COALESCE(st.is_favorite,FALSE),COALESCE(st.version,0),g.payload
FROM messages m JOIN memberships mb ON mb.topic_id=m.topic_id AND mb.person_id=?
LEFT JOIN message_states st ON st.message_id=m.id AND st.person_id=?
LEFT JOIN gotify_import_metadata g ON g.message_id=m.id
WHERE (?=0 OR m.id<?) AND (? OR COALESCE(st.is_hidden,FALSE)=FALSE)
ORDER BY m.id DESC LIMIT 100`, actor.Person.ID, actor.Person.ID, before, before, includeHidden)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := make([]Message, 0)
	for rows.Next() {
		var m Message
		var payload []byte
		if err := rows.Scan(&m.ID, &m.TopicID, &m.Title, &m.Body, &m.Priority, &m.CreatedAt, &m.State.Read, &m.State.Hidden, &m.State.Favorite, &m.State.Version, &payload); err != nil {
			return nil, err
		}
		m.ContentType = messageContentType(payload)
		out = append(out, m)
	}
	return out, rows.Err()
}

func messageContentType(payload []byte) string {
	// Map lookups keep the metadata keys case-sensitive; unrelated values stay private.
	for _, key := range []string{"extras", "client::display", "contentType"} {
		var object map[string]json.RawMessage
		if err := json.Unmarshal(payload, &object); err != nil {
			return "text/plain"
		}
		payload = object[key]
	}
	var contentType string
	if err := json.Unmarshal(payload, &contentType); err == nil && contentType == "text/markdown" {
		return "text/markdown"
	}
	return "text/plain"
}

func (s *Store) PatchState(ctx context.Context, actor Session, id uint64, patch StatePatch) (State, error) {
	var previous State
	tx, err := s.DB.BeginTx(ctx, nil)
	if err != nil {
		return previous, err
	}
	defer tx.Rollback()
	var authorized uint64
	// Lock the membership until commit, so revocation and state mutation serialize.
	err = tx.QueryRowContext(ctx, `SELECT m.id FROM messages m JOIN memberships mb ON mb.topic_id=m.topic_id
WHERE m.id=? AND mb.person_id=? FOR UPDATE`, id, actor.Person.ID).Scan(&authorized)
	if err != nil {
		return previous, mapDatabaseError(err)
	}
	_, err = tx.ExecContext(ctx, "INSERT INTO message_states(person_id,message_id) VALUES(?,?) ON DUPLICATE KEY UPDATE message_id=VALUES(message_id)", actor.Person.ID, id)
	if err != nil {
		return previous, err
	}
	err = tx.QueryRowContext(ctx, "SELECT is_read,is_hidden,is_favorite,version FROM message_states WHERE person_id=? AND message_id=? FOR UPDATE", actor.Person.ID, id).
		Scan(&previous.Read, &previous.Hidden, &previous.Favorite, &previous.Version)
	if err != nil {
		return previous, err
	}
	next, err := patch.Apply(previous)
	if err != nil {
		return previous, err
	}
	_, err = tx.ExecContext(ctx, "UPDATE message_states SET is_read=?,is_hidden=?,is_favorite=?,version=?,updated_at=UTC_TIMESTAMP(3) WHERE person_id=? AND message_id=?", next.Read, next.Hidden, next.Favorite, next.Version, actor.Person.ID, id)
	if err != nil {
		return previous, err
	}
	if err := audit(ctx, tx, actor, "update_message_state", id, map[string]State{"before": previous, "after": next}); err != nil {
		return previous, err
	}
	return next, tx.Commit()
}

func (s *Store) Audit(ctx context.Context, actor Session, before uint64) ([]Audit, error) {
	rows, err := s.DB.QueryContext(ctx, `SELECT id,actor_id,device_name,action,object_id,detail,created_at
FROM audit_events WHERE (? OR actor_id=?) AND (?=0 OR id<?) ORDER BY id DESC LIMIT 100`, actor.Person.Admin, actor.Person.ID, before, before)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := make([]Audit, 0)
	for rows.Next() {
		var a Audit
		if err := rows.Scan(&a.ID, &a.ActorID, &a.DeviceName, &a.Action, &a.ObjectID, &a.Detail, &a.CreatedAt); err != nil {
			return nil, err
		}
		out = append(out, a)
	}
	return out, rows.Err()
}
