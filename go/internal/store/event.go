package store

import (
	"context"
	"database/sql"
	"fmt"
	"strings"
	"time"

	"github.com/loop-controller/go/internal/models"
)

// EventStore persists and queries task events for SSE delivery.
type EventStore interface {
	Append(ctx context.Context, ev models.TaskEvent) error
	ListPending(ctx context.Context, taskID string) ([]models.TaskEvent, error)
	ListAfter(ctx context.Context, taskID, afterEventID string) ([]models.TaskEvent, error)
	MarkPublished(ctx context.Context, eventIDs []string) error
}

type eventStore struct {
	db *sql.DB
}

func (s *eventStore) Append(ctx context.Context, ev models.TaskEvent) error {
	if ev.EventID == "" {
		return fmt.Errorf("event_id is required")
	}
	if ev.TaskID == "" {
		return fmt.Errorf("task_id is required")
	}
	published := 0
	if ev.Published {
		published = 1
	}
	_, err := s.db.ExecContext(ctx, `
		INSERT INTO events (event_id, task_id, event_type, payload_json, published_at, published)
		VALUES (?, ?, ?, ?, ?, ?)
	`, ev.EventID, ev.TaskID, ev.EventType, string(ev.Payload), ev.PublishedAt.Format(time.RFC3339), published)
	if err != nil {
		return fmt.Errorf("insert event: %w", err)
	}
	return nil
}

func (s *eventStore) ListPending(ctx context.Context, taskID string) ([]models.TaskEvent, error) {
	rows, err := s.db.QueryContext(ctx, `
		SELECT event_id, task_id, event_type, payload_json, published_at, published
		FROM events
		WHERE task_id = ? AND published = 0
		ORDER BY published_at ASC
	`, taskID)
	if err != nil {
		return nil, fmt.Errorf("list pending events: %w", err)
	}
	defer rows.Close()
	return scanEvents(rows)
}

func (s *eventStore) ListAfter(ctx context.Context, taskID, afterEventID string) ([]models.TaskEvent, error) {
	query := `
		SELECT event_id, task_id, event_type, payload_json, published_at, published
		FROM events
		WHERE task_id = ?
	`
	args := []any{taskID}
	if afterEventID != "" {
		query += ` AND rowid > COALESCE((SELECT rowid FROM events WHERE event_id = ? AND task_id = ?), 0)`
		args = append(args, afterEventID, taskID)
	}
	query += ` ORDER BY rowid ASC`
	rows, err := s.db.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, fmt.Errorf("list task events: %w", err)
	}
	defer rows.Close()
	return scanEvents(rows)
}

func (s *eventStore) MarkPublished(ctx context.Context, eventIDs []string) error {
	if len(eventIDs) == 0 {
		return nil
	}
	placeholders := make([]string, len(eventIDs))
	args := make([]any, len(eventIDs))
	for i, id := range eventIDs {
		placeholders[i] = "?"
		args[i] = id
	}
	query := fmt.Sprintf(`
		UPDATE events
		SET published = 1
		WHERE event_id IN (%s)
	`, strings.Join(placeholders, ","))
	_, err := s.db.ExecContext(ctx, query, args...)
	if err != nil {
		return fmt.Errorf("mark events published: %w", err)
	}
	return nil
}

func scanEvents(rows *sql.Rows) ([]models.TaskEvent, error) {
	var out []models.TaskEvent
	for rows.Next() {
		var ev models.TaskEvent
		var ts string
		var payload string
		var published int
		if err := rows.Scan(&ev.EventID, &ev.TaskID, &ev.EventType, &payload, &ts, &published); err != nil {
			return nil, fmt.Errorf("scan event: %w", err)
		}
		t, err := time.Parse(time.RFC3339, ts)
		if err != nil {
			return nil, fmt.Errorf("parse event timestamp: %w", err)
		}
		ev.PublishedAt = t
		ev.Payload = []byte(payload)
		ev.Published = published == 1
		out = append(out, ev)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate events: %w", err)
	}
	return out, nil
}
