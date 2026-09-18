package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"time"

	"github.com/loop-controller/go/internal/models"
)

type ApprovalAuditItem struct {
	ID          int64
	EventID     string
	Approval    models.DelegationApproval
	Event       string
	Attempts    int
	ClaimToken  string
	DecodeError error
}

type ApprovalAuditor interface {
	RecordApprovalLifecycle(context.Context, models.DelegationApproval, string, string) error
}

type ApprovalAuditOutboxStore interface {
	ClaimDue(context.Context, time.Time, time.Duration, int) ([]ApprovalAuditItem, error)
	MarkDelivered(context.Context, int64, string, time.Time) error
	MarkFailed(context.Context, int64, string, time.Time, string) error
}

type approvalAuditOutboxStore struct {
	db    *sql.DB
	owner string
}

func (db *DB) ApprovalAuditOutboxStore() ApprovalAuditOutboxStore {
	return &approvalAuditOutboxStore{db: db.DB, owner: db.instanceID}
}

func insertApprovalAuditOutbox(ctx context.Context, executor interface {
	ExecContext(context.Context, string, ...any) (sql.Result, error)
}, approval models.DelegationApproval, event string, at time.Time) error {
	approval.EffectiveArgs = nil
	payload, err := json.Marshal(approval)
	if err != nil {
		return err
	}
	deliveryID := fmt.Sprintf("approval-audit:%s:%s", approval.ApprovalID, event)
	_, err = executor.ExecContext(ctx, `INSERT INTO approval_audit_outbox(delivery_id,approval_id,event,payload_json,created_at,next_attempt_at) VALUES(?,?,?,?,?,?) ON CONFLICT(approval_id,event) DO NOTHING`, deliveryID, approval.ApprovalID, event, string(payload), at.Format(time.RFC3339Nano), at.Format(time.RFC3339Nano))
	return err
}

type ApprovalAuditOutboxDispatcher struct {
	store       ApprovalAuditOutboxStore
	auditor     ApprovalAuditor
	poll, lease time.Duration
}

func NewApprovalAuditOutboxDispatcher(store ApprovalAuditOutboxStore, auditor ApprovalAuditor, poll time.Duration) *ApprovalAuditOutboxDispatcher {
	if poll <= 0 {
		poll = time.Second
	}
	return &ApprovalAuditOutboxDispatcher{store: store, auditor: auditor, poll: poll, lease: 30 * time.Second}
}
func (d *ApprovalAuditOutboxDispatcher) Run(ctx context.Context) {
	_ = d.RunOnce(ctx, time.Now().UTC())
	ticker := time.NewTicker(d.poll)
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
func (d *ApprovalAuditOutboxDispatcher) RunOnce(ctx context.Context, now time.Time) error {
	items, err := d.store.ClaimDue(ctx, now, d.lease, 1)
	if err != nil {
		return err
	}
	for _, item := range items {
		if item.DecodeError != nil {
			return d.store.MarkFailed(ctx, item.ID, item.ClaimToken, now.Add(retryDelay(item.Attempts+1)), "invalid payload: "+item.DecodeError.Error())
		}
		if err := d.auditor.RecordApprovalLifecycle(ctx, item.Approval, item.Event, item.EventID); err != nil {
			return d.store.MarkFailed(ctx, item.ID, item.ClaimToken, now.Add(retryDelay(item.Attempts+1)), err.Error())
		}
		return d.store.MarkDelivered(ctx, item.ID, item.ClaimToken, time.Now().UTC())
	}
	return nil
}
func (s *approvalAuditOutboxStore) ClaimDue(ctx context.Context, now time.Time, lease time.Duration, limit int) ([]ApprovalAuditItem, error) {
	if lease <= 0 {
		lease = 30 * time.Second
	}
	if limit <= 0 {
		limit = 100
	}
	claim := fmt.Sprintf("%s:%d", s.owner, now.UnixNano())
	rows, err := s.db.QueryContext(ctx, `UPDATE approval_audit_outbox SET claimed_by=?,claim_token=?,claim_expires_at=? WHERE outbox_id IN (SELECT outbox_id FROM approval_audit_outbox WHERE delivered_at IS NULL AND next_attempt_at<=? AND (claim_token='' OR claim_expires_at<?) ORDER BY outbox_id LIMIT ?) RETURNING outbox_id,delivery_id,approval_id,event,payload_json,attempts,claim_token`, s.owner, claim, now.Add(lease).UnixNano(), now.Format(time.RFC3339Nano), now.UnixNano(), limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var items []ApprovalAuditItem
	for rows.Next() {
		var item ApprovalAuditItem
		var payload string
		if err := rows.Scan(&item.ID, &item.EventID, &item.Approval.ApprovalID, &item.Event, &payload, &item.Attempts, &item.ClaimToken); err != nil {
			return nil, err
		}
		item.DecodeError = json.Unmarshal([]byte(payload), &item.Approval)
		items = append(items, item)
	}
	return items, rows.Err()
}
func (s *approvalAuditOutboxStore) MarkDelivered(ctx context.Context, id int64, claim string, at time.Time) error {
	return s.ack(ctx, `UPDATE approval_audit_outbox SET delivered_at=?,attempts=attempts+1,last_error='',claimed_by='',claim_token='',claim_expires_at=0 WHERE outbox_id=? AND claimed_by=? AND claim_token=? AND delivered_at IS NULL`, at.Format(time.RFC3339Nano), id, s.owner, claim)
}
func (s *approvalAuditOutboxStore) MarkFailed(ctx context.Context, id int64, claim string, next time.Time, last string) error {
	return s.ack(ctx, `UPDATE approval_audit_outbox SET attempts=attempts+1,next_attempt_at=?,last_error=?,claimed_by='',claim_token='',claim_expires_at=0 WHERE outbox_id=? AND claimed_by=? AND claim_token=? AND delivered_at IS NULL`, next.Format(time.RFC3339Nano), last, id, s.owner, claim)
}
func (s *approvalAuditOutboxStore) ack(ctx context.Context, query string, args ...any) error {
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
