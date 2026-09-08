package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/loop-controller/go/internal/models"
)

var ErrDispatchClaimLost = errors.New("delegation dispatch claim lost")

type DelegationDispatchItem struct {
	DeliveryID string
	ApprovalID string
	TaskID     string
	Entrypoint models.AgentEntrypoint
	Request    models.EntrypointTaskRequest
	Attempts   int
	ClaimToken string
}

type DelegationDispatchOutboxStore interface {
	ClaimDue(context.Context, time.Time, time.Duration, int) ([]DelegationDispatchItem, error)
	MarkDelivered(context.Context, string, string, time.Time) error
	MarkFailed(context.Context, string, string, time.Time, string) error
}

type delegationDispatchOutboxStore struct {
	db    *sql.DB
	owner string
}

type DelegationDispatcher interface {
	Dispatch(context.Context, models.AgentEntrypoint, models.EntrypointTaskRequest) error
}

type DelegationDispatchOutboxDispatcher struct {
	store         DelegationDispatchOutboxStore
	dispatcher    DelegationDispatcher
	pollInterval  time.Duration
	leaseDuration time.Duration
}

func NewDelegationDispatchOutboxDispatcher(outbox DelegationDispatchOutboxStore, dispatcher DelegationDispatcher, poll time.Duration) *DelegationDispatchOutboxDispatcher {
	if poll <= 0 {
		poll = time.Second
	}
	return &DelegationDispatchOutboxDispatcher{store: outbox, dispatcher: dispatcher, pollInterval: poll, leaseDuration: 30 * time.Second}
}

func (d *DelegationDispatchOutboxDispatcher) Run(ctx context.Context) {
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

func (d *DelegationDispatchOutboxDispatcher) RunOnce(ctx context.Context, now time.Time) error {
	items, err := d.store.ClaimDue(ctx, now, d.leaseDuration, 100)
	if err != nil {
		return err
	}
	for _, item := range items {
		if err := d.dispatcher.Dispatch(ctx, item.Entrypoint, item.Request); err != nil {
			if markErr := d.store.MarkFailed(ctx, item.DeliveryID, item.ClaimToken, now.Add(retryDelay(item.Attempts+1)), err.Error()); markErr != nil {
				return markErr
			}
			continue
		}
		if err := d.store.MarkDelivered(ctx, item.DeliveryID, item.ClaimToken, now); err != nil {
			return err
		}
	}
	return nil
}

func (db *DB) DelegationDispatchOutboxStore() DelegationDispatchOutboxStore {
	return &delegationDispatchOutboxStore{db: db.DB, owner: db.instanceID}
}

func (s *delegationDispatchOutboxStore) ClaimDue(ctx context.Context, now time.Time, lease time.Duration, limit int) ([]DelegationDispatchItem, error) {
	if lease <= 0 {
		lease = 30 * time.Second
	}
	if limit <= 0 {
		limit = 100
	}
	token := fmt.Sprintf("%s:%d", s.owner, now.UnixNano())
	rows, err := s.db.QueryContext(ctx, `UPDATE delegation_dispatch_outbox SET claimed_by=?,claim_token=?,claim_expires_at=? WHERE delivery_id IN (SELECT o.delivery_id FROM delegation_dispatch_outbox o JOIN tasks t ON t.task_id=o.task_id WHERE o.delivered_at IS NULL AND o.next_attempt_at<=? AND (o.claim_token='' OR o.claim_expires_at<?) AND t.status NOT IN ('cancelled','failed','completed') AND (t.deadline IS NULL OR t.deadline>?) ORDER BY o.delivery_id LIMIT ?) RETURNING delivery_id,approval_id,task_id,entrypoint_json,payload_json,attempts,claim_token`, s.owner, token, now.Add(lease).UnixNano(), now.Format(time.RFC3339Nano), now.UnixNano(), now.Format(time.RFC3339Nano), limit)
	if err != nil {
		return nil, fmt.Errorf("claim delegation dispatch: %w", err)
	}
	defer rows.Close()
	var items []DelegationDispatchItem
	for rows.Next() {
		var item DelegationDispatchItem
		var entrypoint, payload string
		if err := rows.Scan(&item.DeliveryID, &item.ApprovalID, &item.TaskID, &entrypoint, &payload, &item.Attempts, &item.ClaimToken); err != nil {
			return nil, err
		}
		if err := json.Unmarshal([]byte(entrypoint), &item.Entrypoint); err != nil {
			return nil, err
		}
		if err := json.Unmarshal([]byte(payload), &item.Request); err != nil {
			return nil, err
		}
		items = append(items, item)
	}
	return items, rows.Err()
}

func (s *delegationDispatchOutboxStore) MarkDelivered(ctx context.Context, id, claim string, at time.Time) error {
	return s.ack(ctx, `UPDATE delegation_dispatch_outbox SET delivered_at=?,attempts=attempts+1,last_error='',claimed_by='',claim_token='',claim_expires_at=0 WHERE delivery_id=? AND claimed_by=? AND claim_token=? AND delivered_at IS NULL`, at.Format(time.RFC3339Nano), id, s.owner, claim)
}

func (s *delegationDispatchOutboxStore) MarkFailed(ctx context.Context, id, claim string, next time.Time, lastError string) error {
	return s.ack(ctx, `UPDATE delegation_dispatch_outbox SET attempts=attempts+1,next_attempt_at=?,last_error=?,claimed_by='',claim_token='',claim_expires_at=0 WHERE delivery_id=? AND claimed_by=? AND claim_token=? AND delivered_at IS NULL`, next.Format(time.RFC3339Nano), lastError, id, s.owner, claim)
}

func (s *delegationDispatchOutboxStore) ack(ctx context.Context, query string, args ...any) error {
	res, err := s.db.ExecContext(ctx, query, args...)
	if err != nil {
		return err
	}
	n, err := res.RowsAffected()
	if err != nil {
		return err
	}
	if n != 1 {
		return ErrDispatchClaimLost
	}
	return nil
}
