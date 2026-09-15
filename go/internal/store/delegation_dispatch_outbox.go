package store

import (
	"bytes"
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
	DeliveryID   string
	AssignmentID string
	ApprovalID   string
	TaskID       string
	Entrypoint   models.AgentEntrypoint
	Request      models.EntrypointTaskRequest
	Attempts     int
	ClaimToken   string
}

type OutboundDelegationEnqueue struct {
	Task       models.Task
	Entrypoint models.AgentEntrypoint
	Request    models.EntrypointTaskRequest
	ApprovalID string
	Now        time.Time
}

type DelegationDispatchOutboxStore interface {
	EnqueueOutboundDelegation(context.Context, OutboundDelegationEnqueue) (models.Task, models.TaskAssignment, error)
	LoadOutboundDelegation(context.Context, string) (DelegationDispatchItem, error)
	CommitOutboundDispatchResult(context.Context, AssignmentResult) (models.Task, models.TaskAssignment, error)
	CommitOutboundTaskResult(context.Context, string, string, json.RawMessage, string, models.DelegationBudget, time.Time) (models.Task, error)
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

func (s *delegationDispatchOutboxStore) EnqueueOutboundDelegation(ctx context.Context, p OutboundDelegationEnqueue) (models.Task, models.TaskAssignment, error) {
	now := p.Now.UTC()
	if now.IsZero() {
		now = time.Now().UTC()
	}
	if p.Task.TaskID == "" || p.Task.TargetAgentID == "" {
		return models.Task{}, models.TaskAssignment{}, ErrAssignmentConflict
	}
	assignmentID, deliveryID := "outbound-assignment-"+p.Task.TaskID, "delegation-dispatch-"+p.Task.TaskID
	p.Task.DelegationToken = p.Request.DelegationToken
	p.Request.TaskID = p.Task.TaskID
	p.Request.DeliveryID = deliveryID
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return models.Task{}, models.TaskAssignment{}, err
	}
	defer tx.Rollback()
	ep, err := json.Marshal(p.Entrypoint)
	if err != nil {
		return models.Task{}, models.TaskAssignment{}, err
	}
	req, err := json.Marshal(p.Request)
	if err != nil {
		return models.Task{}, models.TaskAssignment{}, err
	}
	var existingEntrypoint, existingRequest string
	lookupErr := tx.QueryRowContext(ctx, `SELECT entrypoint_json,payload_json FROM delegation_dispatch_outbox WHERE task_id=?`, p.Task.TaskID).Scan(&existingEntrypoint, &existingRequest)
	if lookupErr == nil {
		existingTask, taskErr := getTaskTx(ctx, tx, p.Task.TaskID)
		existingAssignment, assignmentErr := getAssignment(ctx, tx, `task_id=? AND assignment_kind=?`, p.Task.TaskID, models.AssignmentKindOutboundDelegation)
		if taskErr != nil || assignmentErr != nil || !sameOutboundTask(existingTask, p.Task) || existingEntrypoint != string(ep) || existingRequest != string(req) {
			return models.Task{}, models.TaskAssignment{}, ErrAssignmentConflict
		}
		return existingTask, existingAssignment, nil
	}
	if !errors.Is(lookupErr, sql.ErrNoRows) {
		return models.Task{}, models.TaskAssignment{}, lookupErr
	}
	if p.Task.ParentTaskID != "" {
		res, e := tx.ExecContext(ctx, `UPDATE tasks SET reserved_token_count=reserved_token_count+?,reserved_payment_amount=reserved_payment_amount+? WHERE task_id=? AND status IN ('pending','accepted','running') AND budget_currency=? AND budget_token_count-reserved_token_count-consumed_token_count>=? AND budget_payment_amount-reserved_payment_amount-consumed_payment_amount>=?`, p.Task.Budget.TokenCount, p.Task.Budget.PaymentAmount, p.Task.ParentTaskID, p.Task.Budget.Currency, p.Task.Budget.TokenCount, p.Task.Budget.PaymentAmount)
		if e != nil {
			return models.Task{}, models.TaskAssignment{}, e
		}
		if n, _ := res.RowsAffected(); n != 1 {
			return models.Task{}, models.TaskAssignment{}, ErrBudgetExceeded
		}
	}
	if _, err = insertTask(ctx, tx, p.Task); err != nil {
		return models.Task{}, models.TaskAssignment{}, err
	}
	payload, err := json.Marshal(p.Task)
	if err != nil {
		return models.Task{}, models.TaskAssignment{}, err
	}
	event := models.TaskEvent{EventID: "ev-outbound-" + p.Task.TaskID, TaskID: p.Task.TaskID, EventType: "task_created", Payload: payload, PublishedAt: now}
	if err = appendEvent(ctx, tx, &event); err != nil {
		return models.Task{}, models.TaskAssignment{}, err
	}
	if err = insertLifecycleOutbox(ctx, tx, p.Task, "created", now); err != nil {
		return models.Task{}, models.TaskAssignment{}, err
	}
	policy := normalizedRetryPolicy(nil, p.Task.Deadline)
	policyJSON, _ := json.Marshal(policy)
	a := models.TaskAssignment{AssignmentID: assignmentID, Kind: models.AssignmentKindOutboundDelegation, TenantID: p.Task.TenantID, TaskID: p.Task.TaskID, AgentID: p.Task.TargetAgentID, State: models.AssignmentStateQueued, Revision: 1, DeliveryID: deliveryID, IdempotencyKey: deliveryID, Deadline: p.Task.Deadline, RetryPolicy: &policy, CreatedAt: now, UpdatedAt: now}
	_, err = tx.ExecContext(ctx, `INSERT INTO task_assignments(assignment_id,assignment_kind,tenant_id,task_id,agent_id,state,revision,attempt,execution_fence,lease_owner,claim_token,lease_expires_at,deadline,delivery_id,idempotency_key,retry_policy_json,created_at,updated_at) VALUES(?,?,?,?,?,'queued',1,0,0,'','',0,?,?,?,?,?,?)`, a.AssignmentID, a.Kind, a.TenantID, a.TaskID, a.AgentID, timeText(a.Deadline), a.DeliveryID, a.IdempotencyKey, string(policyJSON), formatTime(now), formatTime(now))
	if err != nil {
		return models.Task{}, models.TaskAssignment{}, err
	}
	var approval any
	if p.ApprovalID != "" {
		approval = p.ApprovalID
	}
	_, err = tx.ExecContext(ctx, `INSERT INTO delegation_dispatch_outbox(delivery_id,approval_id,task_id,assignment_id,entrypoint_json,payload_json,next_attempt_at) VALUES(?,?,?,?,?,?,?)`, deliveryID, approval, p.Task.TaskID, assignmentID, string(ep), string(req), formatTime(now))
	if err != nil {
		return models.Task{}, models.TaskAssignment{}, err
	}
	if err = tx.Commit(); err != nil {
		return models.Task{}, models.TaskAssignment{}, err
	}
	return p.Task, a, nil
}

func sameOutboundTask(existing, requested models.Task) bool {
	if existing.DelegationToken != requested.DelegationToken {
		return false
	}
	existing.CreatedAt, requested.CreatedAt = time.Time{}, time.Time{}
	existing.UpdatedAt, requested.UpdatedAt = time.Time{}, time.Time{}
	existing.CompletedAt, requested.CompletedAt = nil, nil
	existing.ReservedBudget, requested.ReservedBudget = models.DelegationBudget{}, models.DelegationBudget{}
	existing.ConsumedBudget, requested.ConsumedBudget = models.DelegationBudget{}, models.DelegationBudget{}
	existingJSON, err := json.Marshal(existing)
	if err != nil {
		return false
	}
	requestedJSON, err := json.Marshal(requested)
	return err == nil && bytes.Equal(existingJSON, requestedJSON)
}

func (s *delegationDispatchOutboxStore) LoadOutboundDelegation(ctx context.Context, assignmentID string) (DelegationDispatchItem, error) {
	var item DelegationDispatchItem
	var ep, req string
	var approval sql.NullString
	err := s.db.QueryRowContext(ctx, `SELECT delivery_id,assignment_id,approval_id,task_id,entrypoint_json,payload_json,attempts FROM delegation_dispatch_outbox WHERE assignment_id=?`, assignmentID).Scan(&item.DeliveryID, &item.AssignmentID, &approval, &item.TaskID, &ep, &req, &item.Attempts)
	if err != nil {
		return item, err
	}
	item.ApprovalID = approval.String
	if err = json.Unmarshal([]byte(ep), &item.Entrypoint); err != nil {
		return item, err
	}
	err = json.Unmarshal([]byte(req), &item.Request)
	return item, err
}

func (s *delegationDispatchOutboxStore) CommitOutboundDispatchResult(ctx context.Context, p AssignmentResult) (models.Task, models.TaskAssignment, error) {
	if p.Kind != models.AssignmentKindOutboundDelegation {
		return models.Task{}, models.TaskAssignment{}, ErrAssignmentConflict
	}
	now := p.Now.UTC()
	if now.IsZero() {
		now = time.Now().UTC()
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return models.Task{}, models.TaskAssignment{}, err
	}
	defer tx.Rollback()
	a, err := getAssignment(ctx, tx, `assignment_id=?`, p.AssignmentID)
	if err != nil {
		return models.Task{}, a, err
	}
	if a.Kind != p.Kind {
		return models.Task{}, a, ErrAssignmentClaimLost
	}
	if a.State == models.AssignmentStateSettled {
		if a.Attempt != p.Attempt || a.ExecutionFence != p.Fence {
			return models.Task{}, a, ErrAssignmentClaimLost
		}
		var owner, token string
		if err = tx.QueryRowContext(ctx, `SELECT lease_owner,claim_token FROM execution_attempts WHERE assignment_id=? AND attempt=? AND execution_fence=?`, p.AssignmentID, p.Attempt, p.Fence).Scan(&owner, &token); err != nil || owner != p.Owner || token != p.ClaimToken {
			return models.Task{}, a, ErrAssignmentClaimLost
		}
		if !sameResult(a, p) {
			return models.Task{}, a, ErrAssignmentResultConflict
		}
		t, loadErr := getTaskTx(ctx, tx, a.TaskID)
		return t, a, loadErr
	}
	res, err := tx.ExecContext(ctx, `UPDATE task_assignments SET state='settled',revision=revision+1,result_status=?,failure_class=?,error_code=?,lease_owner='',claim_token='',lease_expires_at=0,updated_at=? WHERE assignment_id=? AND assignment_kind=? AND revision=? AND attempt=? AND execution_fence=? AND lease_owner=? AND claim_token=? AND lease_expires_at>? AND state='dispatched'`, p.Status, p.FailureClass, p.ErrorCode, formatTime(now), p.AssignmentID, p.Kind, p.Revision, p.Attempt, p.Fence, p.Owner, p.ClaimToken, now.UnixNano())
	if err != nil {
		return models.Task{}, a, err
	}
	if err = requireOneRow(res); err != nil {
		return models.Task{}, a, classifyGuard(ctx, tx, p.AssignmentTransition, models.AssignmentStateDispatched, now)
	}
	attemptRes, err := tx.ExecContext(ctx, `UPDATE execution_attempts SET state='settled',result_status=?,failure_class=?,error_code=?,finished_at=? WHERE assignment_id=? AND attempt=? AND execution_fence=?`, p.Status, p.FailureClass, p.ErrorCode, formatTime(now), p.AssignmentID, p.Attempt, p.Fence)
	if err != nil {
		return models.Task{}, a, err
	}
	if err = requireOneRow(attemptRes); err != nil {
		return models.Task{}, a, err
	}
	var outboxRes sql.Result
	if p.Status == "delivered" {
		outboxRes, err = tx.ExecContext(ctx, `UPDATE delegation_dispatch_outbox SET delivered_at=?,attempts=attempts+1,last_error='' WHERE assignment_id=? AND delivered_at IS NULL AND terminal_at IS NULL`, formatTime(now), p.AssignmentID)
	} else {
		outboxRes, err = tx.ExecContext(ctx, `UPDATE delegation_dispatch_outbox SET terminal_at=?,attempts=attempts+1,failure_class=?,last_error=? WHERE assignment_id=? AND delivered_at IS NULL AND terminal_at IS NULL`, formatTime(now), p.FailureClass, p.ErrorCode, p.AssignmentID)
	}
	if err != nil {
		return models.Task{}, a, err
	}
	if err = requireOneRow(outboxRes); err != nil {
		return models.Task{}, a, ErrAssignmentClaimLost
	}
	if p.Status == "failed" || p.Status == "outcome_unknown" {
		res, err = tx.ExecContext(ctx, `UPDATE tasks SET status=?,error_code=?,updated_at=?,completed_at=CASE WHEN ?='failed' THEN ? ELSE completed_at END WHERE task_id=? AND status IN ('pending','accepted','running')`, p.Status, p.ErrorCode, formatTime(now), p.Status, formatTime(now), a.TaskID)
		if err != nil {
			return models.Task{}, a, err
		}
		if err = requireOneRow(res); err != nil {
			return models.Task{}, a, ErrStatusConflict
		}
		state, reason := "released", "dispatch_failed"
		if p.Status == "outcome_unknown" {
			state, reason = "uncertain", "sent_unacknowledged"
		}
		if err = settleTaskBudgetTx(ctx, tx, a.TaskID, p.Status, models.DelegationBudget{}, now); err != nil {
			return models.Task{}, a, err
		}
		reservationRes, updateErr := tx.ExecContext(ctx, `UPDATE agent_capacity_reservations SET state=?,revision=revision+1,release_reason=?,released_at=CASE WHEN ?='released' THEN ? ELSE NULL END WHERE assignment_id=? AND state='held'`, state, reason, state, formatTime(now), a.AssignmentID)
		if updateErr != nil {
			return models.Task{}, a, updateErr
		}
		if n, rowsErr := reservationRes.RowsAffected(); rowsErr != nil {
			return models.Task{}, a, rowsErr
		} else if n > 1 {
			return models.Task{}, a, ErrReservationConflict
		}
		task, loadErr := getTaskTx(ctx, tx, a.TaskID)
		if loadErr != nil {
			return models.Task{}, a, loadErr
		}
		queue := executionQueueStore{owner: s.owner}
		if err = queue.insertTaskTransition(ctx, tx, task, p.Status, now); err != nil {
			return models.Task{}, a, err
		}
	}
	a, err = getAssignment(ctx, tx, `assignment_id=?`, p.AssignmentID)
	if err != nil {
		return models.Task{}, a, err
	}
	t, err := getTaskTx(ctx, tx, a.TaskID)
	if err != nil {
		return t, a, err
	}
	if err = tx.Commit(); err != nil {
		return t, a, err
	}
	return t, a, nil
}

func (s *delegationDispatchOutboxStore) CommitOutboundTaskResult(ctx context.Context, taskID, status string, outcome json.RawMessage, errorCode string, consumed models.DelegationBudget, now time.Time) (models.Task, error) {
	if status != "completed" && status != "failed" {
		return models.Task{}, ErrStatusConflict
	}
	if consumed.TokenCount < 0 || consumed.PaymentAmount < 0 {
		return models.Task{}, ErrBudgetExceeded
	}
	now = now.UTC()
	if now.IsZero() {
		now = time.Now().UTC()
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return models.Task{}, err
	}
	defer tx.Rollback()
	a, err := getAssignment(ctx, tx, `task_id=? AND assignment_kind=?`, taskID, models.AssignmentKindOutboundDelegation)
	if errors.Is(err, ErrAssignmentNotFound) {
		return models.Task{}, ErrAssignmentNotFound
	}
	if err != nil {
		return models.Task{}, err
	}
	if a.State != models.AssignmentStateSettled || a.ResultStatus != "delivered" {
		return models.Task{}, ErrAssignmentInvalidTransition
	}
	current, err := getTaskTx(ctx, tx, taskID)
	if err != nil {
		return models.Task{}, err
	}
	if current.Status == "completed" || current.Status == "failed" {
		if current.Status != status || string(current.Outcome) != string(outcome) || current.ErrorCode != errorCode || a.ConsumedBudget != consumed {
			return models.Task{}, ErrAssignmentResultConflict
		}
		return current, nil
	}
	if current.Status != "running" && current.Status != "outcome_unknown" && current.Status != "pending" && current.Status != "accepted" {
		return models.Task{}, ErrStatusConflict
	}
	res, err := tx.ExecContext(ctx, `UPDATE tasks SET status=?,updated_at=?,completed_at=?,outcome=?,error_code=? WHERE task_id=? AND status=?`, status, formatTime(now), formatTime(now), rawNull(outcome), errorCode, taskID, current.Status)
	if err != nil {
		return models.Task{}, err
	}
	if err = requireOneRow(res); err != nil {
		return models.Task{}, ErrStatusConflict
	}
	if err = settleTaskBudgetTx(ctx, tx, taskID, status, consumed, now); err != nil {
		return models.Task{}, err
	}
	res, err = tx.ExecContext(ctx, `UPDATE task_assignments SET outcome_json=?,error_code=?,consumed_token_count=?,consumed_payment_amount=?,consumed_currency=?,updated_at=? WHERE assignment_id=? AND state='settled' AND result_status='delivered'`, rawNull(outcome), errorCode, consumed.TokenCount, consumed.PaymentAmount, consumed.Currency, formatTime(now), a.AssignmentID)
	if err != nil {
		return models.Task{}, err
	}
	if err = requireOneRow(res); err != nil {
		return models.Task{}, ErrAssignmentConflict
	}
	res, err = tx.ExecContext(ctx, `UPDATE agent_capacity_reservations SET state='released',revision=revision+1,release_reason='outbound_result',released_at=? WHERE assignment_id=? AND state IN ('held','uncertain')`, formatTime(now), a.AssignmentID)
	if err != nil {
		return models.Task{}, err
	}
	if n, rowsErr := res.RowsAffected(); rowsErr != nil {
		return models.Task{}, rowsErr
	} else if n > 1 {
		return models.Task{}, ErrReservationConflict
	}
	updated, err := getTaskTx(ctx, tx, taskID)
	if err != nil {
		return models.Task{}, err
	}
	queue := executionQueueStore{owner: s.owner}
	if err = queue.insertTaskTransition(ctx, tx, updated, status, now); err != nil {
		return models.Task{}, err
	}
	if err = tx.Commit(); err != nil {
		return models.Task{}, err
	}
	return updated, nil
}

func (s *delegationDispatchOutboxStore) ClaimDue(ctx context.Context, now time.Time, lease time.Duration, limit int) ([]DelegationDispatchItem, error) {
	if lease <= 0 {
		lease = 30 * time.Second
	}
	if limit <= 0 {
		limit = 100
	}
	token := fmt.Sprintf("%s:%d", s.owner, now.UnixNano())
	rows, err := s.db.QueryContext(ctx, `UPDATE delegation_dispatch_outbox SET claimed_by=?,claim_token=?,claim_expires_at=? WHERE delivery_id IN (SELECT o.delivery_id FROM delegation_dispatch_outbox o JOIN tasks t ON t.task_id=o.task_id WHERE o.assignment_id IS NULL AND o.delivered_at IS NULL AND o.next_attempt_at<=? AND (o.claim_token='' OR o.claim_expires_at<?) AND t.status NOT IN ('cancelled','failed','completed') AND (t.deadline IS NULL OR t.deadline>?) ORDER BY o.delivery_id LIMIT ?) RETURNING delivery_id,approval_id,task_id,entrypoint_json,payload_json,attempts,claim_token`, s.owner, token, now.Add(lease).UnixNano(), now.Format(time.RFC3339Nano), now.UnixNano(), now.Format(time.RFC3339Nano), limit)
	if err != nil {
		return nil, fmt.Errorf("claim delegation dispatch: %w", err)
	}
	defer rows.Close()
	var items []DelegationDispatchItem
	for rows.Next() {
		var item DelegationDispatchItem
		var entrypoint, payload string
		var approval sql.NullString
		if err := rows.Scan(&item.DeliveryID, &approval, &item.TaskID, &entrypoint, &payload, &item.Attempts, &item.ClaimToken); err != nil {
			return nil, err
		}
		item.ApprovalID = approval.String
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
