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
	ID         int64
	EventID    string
	Task       models.Task
	Event      string
	Attempts   int
	ClaimToken string
}

// LifecycleOutboxStore persists and acknowledges lifecycle audit deliveries.
type LifecycleOutboxStore interface {
	// ClaimDue atomically claims a bounded batch of due notifications for this
	// instance. The claim is held by the instance until the lease expires, so
	// concurrent instances sharing the same database cannot deliver the same
	// notification twice.
	ClaimDue(ctx context.Context, now time.Time, leaseDuration time.Duration, limit int) ([]LifecycleOutboxItem, error)
	MarkDelivered(ctx context.Context, id int64, claimToken string, deliveredAt time.Time) error
	MarkFailed(ctx context.Context, id int64, claimToken string, nextAttemptAt time.Time, lastError string) error
}

type lifecycleOutboxStore struct {
	db    *sql.DB
	owner string
}

// LifecycleAuditor delivers one lifecycle notification to the audit service.
type LifecycleAuditor interface {
	RecordLifecycle(context.Context, models.Task, string, string) error
}

// LifecycleOutboxDispatcher retries durable lifecycle notifications until delivery.
type LifecycleOutboxDispatcher struct {
	store         LifecycleOutboxStore
	auditor       LifecycleAuditor
	pollInterval  time.Duration
	leaseDuration time.Duration
}

// NewLifecycleOutboxDispatcher creates a dispatcher for a durable outbox.
func NewLifecycleOutboxDispatcher(outbox LifecycleOutboxStore, auditor LifecycleAuditor, pollInterval time.Duration) *LifecycleOutboxDispatcher {
	if pollInterval <= 0 {
		pollInterval = time.Second
	}
	return &LifecycleOutboxDispatcher{
		store:         outbox,
		auditor:       auditor,
		pollInterval:  pollInterval,
		leaseDuration: 30 * time.Second,
	}
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
	items, err := d.store.ClaimDue(ctx, now, d.leaseDuration, 100)
	if err != nil {
		return err
	}
	for _, item := range items {
		if err := d.auditor.RecordLifecycle(ctx, item.Task, item.Event, item.EventID); err != nil {
			nextAttempt := now.Add(retryDelay(item.Attempts + 1))
			if markErr := d.store.MarkFailed(ctx, item.ID, item.ClaimToken, nextAttempt, err.Error()); markErr != nil {
				return markErr
			}
			continue
		}
		if err := d.store.MarkDelivered(ctx, item.ID, item.ClaimToken, now); err != nil {
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

func (s *lifecycleOutboxStore) ClaimDue(ctx context.Context, now time.Time, leaseDuration time.Duration, limit int) ([]LifecycleOutboxItem, error) {
	if limit <= 0 {
		limit = 100
	}
	if leaseDuration <= 0 {
		leaseDuration = 30 * time.Second
	}
	claimExpiresAt := now.Add(leaseDuration).UnixNano()
	claimToken := fmt.Sprintf("%s:%d", s.owner, now.UnixNano())
	rows, err := s.db.QueryContext(ctx, `
		UPDATE lifecycle_outbox
		SET claimed_by = ?, claim_token = ?, claim_expires_at = ?
		WHERE outbox_id IN (
			SELECT outbox_id FROM lifecycle_outbox
			WHERE delivered_at IS NULL
			  AND next_attempt_at <= ?
			  AND (claimed_by = '' OR claim_expires_at < ?)
			ORDER BY outbox_id ASC
			LIMIT ?
		)
		RETURNING outbox_id, payload_json, event, attempts, claim_token
	`, s.owner, claimToken, claimExpiresAt, now.Format(time.RFC3339Nano), now.UnixNano(), limit)
	if err != nil {
		return nil, fmt.Errorf("claim due lifecycle outbox: %w", err)
	}
	defer rows.Close()
	var items []LifecycleOutboxItem
	for rows.Next() {
		var item LifecycleOutboxItem
		var payload string
		if err := rows.Scan(&item.ID, &payload, &item.Event, &item.Attempts, &item.ClaimToken); err != nil {
			return nil, fmt.Errorf("scan lifecycle outbox: %w", err)
		}
		if err := json.Unmarshal([]byte(payload), &item.Task); err != nil {
			return nil, fmt.Errorf("decode lifecycle outbox payload: %w", err)
		}
		item.EventID = fmt.Sprintf("lifecycle:%s:%s", item.Task.TaskID, item.Event)
		items = append(items, item)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate lifecycle outbox: %w", err)
	}
	return items, nil
}

func (s *lifecycleOutboxStore) MarkDelivered(ctx context.Context, id int64, claimToken string, deliveredAt time.Time) error {
	res, err := s.db.ExecContext(ctx, `
		UPDATE lifecycle_outbox SET delivered_at = ?, attempts = attempts + 1, last_error = NULL, claimed_by = '', claim_token = '', claim_expires_at = 0
		WHERE outbox_id = ? AND claimed_by = ? AND claim_token = ? AND delivered_at IS NULL
	`, deliveredAt.Format(time.RFC3339Nano), id, s.owner, claimToken)
	if err != nil {
		return fmt.Errorf("mark lifecycle outbox delivered: %w", err)
	}
	if n, _ := res.RowsAffected(); n != 1 {
		return ErrDispatchClaimLost
	}
	return nil
}

func (s *lifecycleOutboxStore) MarkFailed(ctx context.Context, id int64, claimToken string, nextAttemptAt time.Time, lastError string) error {
	res, err := s.db.ExecContext(ctx, `
		UPDATE lifecycle_outbox SET attempts = attempts + 1, next_attempt_at = ?, last_error = ?, claimed_by = '', claim_token = '', claim_expires_at = 0
		WHERE outbox_id = ? AND claimed_by = ? AND claim_token = ? AND delivered_at IS NULL
	`, nextAttemptAt.Format(time.RFC3339Nano), lastError, id, s.owner, claimToken)
	if err != nil {
		return fmt.Errorf("mark lifecycle outbox failed: %w", err)
	}
	if n, _ := res.RowsAffected(); n != 1 {
		return ErrDispatchClaimLost
	}
	return nil
}
