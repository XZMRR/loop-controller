package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"time"

	"github.com/loop-controller/go/internal/models"
)

// LifecycleOutboxItem is a lifecycle audit notification awaiting delivery.
type LifecycleOutboxItem struct {
	ID       int64
	Task     models.Task
	Event    string
	Attempts int
}

// LifecycleOutboxStore persists and acknowledges lifecycle audit deliveries.
type LifecycleOutboxStore interface {
	ListDue(ctx context.Context, now time.Time, limit int) ([]LifecycleOutboxItem, error)
	MarkDelivered(ctx context.Context, id int64, deliveredAt time.Time) error
	MarkFailed(ctx context.Context, id int64, nextAttemptAt time.Time, lastError string) error
}

type lifecycleOutboxStore struct {
	db *sql.DB
}

// LifecycleAuditor delivers one lifecycle notification to the audit service.
type LifecycleAuditor interface {
	RecordLifecycle(context.Context, models.Task, string) error
}

// LifecycleOutboxDispatcher retries durable lifecycle notifications until delivery.
type LifecycleOutboxDispatcher struct {
	store        LifecycleOutboxStore
	auditor      LifecycleAuditor
	pollInterval time.Duration
}

// NewLifecycleOutboxDispatcher creates a dispatcher for a durable outbox.
func NewLifecycleOutboxDispatcher(outbox LifecycleOutboxStore, auditor LifecycleAuditor, pollInterval time.Duration) *LifecycleOutboxDispatcher {
	if pollInterval <= 0 {
		pollInterval = time.Second
	}
	return &LifecycleOutboxDispatcher{store: outbox, auditor: auditor, pollInterval: pollInterval}
}

// Run drains due notifications until ctx is cancelled.
func (d *LifecycleOutboxDispatcher) Run(ctx context.Context) {
	_ = d.RunOnce(ctx, time.Now().UTC())
	ticker := time.NewTicker(d.pollInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case now := <-ticker.C:
			_ = d.RunOnce(ctx, now.UTC())
		}
	}
}

// RunOnce attempts one bounded batch and schedules failures with exponential backoff.
func (d *LifecycleOutboxDispatcher) RunOnce(ctx context.Context, now time.Time) error {
	items, err := d.store.ListDue(ctx, now, 100)
	if err != nil {
		return err
	}
	for _, item := range items {
		if err := d.auditor.RecordLifecycle(ctx, item.Task, item.Event); err != nil {
			nextAttempt := now.Add(retryDelay(item.Attempts + 1))
			if markErr := d.store.MarkFailed(ctx, item.ID, nextAttempt, err.Error()); markErr != nil {
				return markErr
			}
			continue
		}
		if err := d.store.MarkDelivered(ctx, item.ID, now); err != nil {
			return err
		}
	}
	return nil
}

func retryDelay(attempt int) time.Duration {
	if attempt < 1 {
		attempt = 1
	}
	if attempt > 8 {
		attempt = 8
	}
	return time.Second * time.Duration(1<<(attempt-1))
}

func insertLifecycleOutbox(ctx context.Context, tx *sql.Tx, task models.Task, event string, createdAt time.Time) error {
	if task.InteractionID == "" || task.DecisionID == "" {
		return nil
	}
	payload, err := json.Marshal(task)
	if err != nil {
		return fmt.Errorf("marshal lifecycle outbox payload: %w", err)
	}
	if _, err := tx.ExecContext(ctx, `
		INSERT INTO lifecycle_outbox (task_id, event, payload_json, created_at, next_attempt_at)
		VALUES (?, ?, ?, ?, ?)
		ON CONFLICT(task_id, event) DO NOTHING
	`, task.TaskID, event, string(payload), createdAt.Format(time.RFC3339Nano), createdAt.Format(time.RFC3339Nano)); err != nil {
		return fmt.Errorf("insert lifecycle outbox: %w", err)
	}
	return nil
}

func (s *lifecycleOutboxStore) ListDue(ctx context.Context, now time.Time, limit int) ([]LifecycleOutboxItem, error) {
	if limit <= 0 {
		limit = 100
	}
	rows, err := s.db.QueryContext(ctx, `
		SELECT outbox_id, payload_json, event, attempts
		FROM lifecycle_outbox
		WHERE delivered_at IS NULL AND next_attempt_at <= ?
		ORDER BY outbox_id ASC
		LIMIT ?
	`, now.Format(time.RFC3339Nano), limit)
	if err != nil {
		return nil, fmt.Errorf("list due lifecycle outbox: %w", err)
	}
	defer rows.Close()
	var items []LifecycleOutboxItem
	for rows.Next() {
		var item LifecycleOutboxItem
		var payload string
		if err := rows.Scan(&item.ID, &payload, &item.Event, &item.Attempts); err != nil {
			return nil, fmt.Errorf("scan lifecycle outbox: %w", err)
		}
		if err := json.Unmarshal([]byte(payload), &item.Task); err != nil {
			return nil, fmt.Errorf("decode lifecycle outbox payload: %w", err)
		}
		items = append(items, item)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate lifecycle outbox: %w", err)
	}
	return items, nil
}

func (s *lifecycleOutboxStore) MarkDelivered(ctx context.Context, id int64, deliveredAt time.Time) error {
	_, err := s.db.ExecContext(ctx, `
		UPDATE lifecycle_outbox SET delivered_at = ?, attempts = attempts + 1, last_error = NULL
		WHERE outbox_id = ? AND delivered_at IS NULL
	`, deliveredAt.Format(time.RFC3339Nano), id)
	if err != nil {
		return fmt.Errorf("mark lifecycle outbox delivered: %w", err)
	}
	return nil
}

func (s *lifecycleOutboxStore) MarkFailed(ctx context.Context, id int64, nextAttemptAt time.Time, lastError string) error {
	_, err := s.db.ExecContext(ctx, `
		UPDATE lifecycle_outbox SET attempts = attempts + 1, next_attempt_at = ?, last_error = ?
		WHERE outbox_id = ? AND delivered_at IS NULL
	`, nextAttemptAt.Format(time.RFC3339Nano), lastError, id)
	if err != nil {
		return fmt.Errorf("mark lifecycle outbox failed: %w", err)
	}
	return nil
}
