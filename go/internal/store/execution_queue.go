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

type EnqueueAcceptedTaskParams struct {
	Kind           models.AssignmentKind
	AssignmentID   string
	DeliveryID     string
	IdempotencyKey string
	TaskID         string
	TenantID       string
	TargetAgentID  string
	Deadline       *time.Time
	NotBefore      *time.Time
	RetryPolicy    *models.RetryPolicy
	Now            time.Time
}

type ExecutionQueueStore interface {
	EnqueueAcceptedTask(context.Context, EnqueueAcceptedTaskParams) (models.Task, models.TaskAssignment, error)
	CommitExecutionResult(context.Context, AssignmentResult) (models.Task, models.TaskAssignment, error)
	CancelAssignmentAndRelease(context.Context, CancelAssignmentParams) (models.Task, models.TaskAssignment, error)
}

type executionQueueStore struct {
	db    *sql.DB
	owner string
}

func (s *executionQueueStore) EnqueueAcceptedTask(ctx context.Context, p EnqueueAcceptedTaskParams) (models.Task, models.TaskAssignment, error) {
	if p.Kind != models.AssignmentKindTargetExecution || p.AssignmentID == "" || p.DeliveryID == "" || p.TaskID == "" || p.TargetAgentID == "" {
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
	t, err := getTaskTx(ctx, tx, p.TaskID)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return models.Task{}, models.TaskAssignment{}, ErrAssignmentNotFound
		}
		return models.Task{}, models.TaskAssignment{}, err
	}
	if t.TenantID != p.TenantID || t.TargetAgentID != p.TargetAgentID || !equalTimePtr(t.Deadline, p.Deadline) {
		return models.Task{}, models.TaskAssignment{}, ErrAssignmentConflict
	}
	if existing, getErr := getAssignment(ctx, tx, `task_id=?`, p.TaskID); getErr == nil {
		want := models.TaskAssignment{Kind: p.Kind, AssignmentID: p.AssignmentID, TenantID: p.TenantID, TaskID: p.TaskID, AgentID: p.TargetAgentID, DeliveryID: p.DeliveryID, IdempotencyKey: p.IdempotencyKey, NotBefore: p.NotBefore, Deadline: p.Deadline}
		if t.Status == "running" && sameCreation(existing, want) {
			return t, existing, nil
		}
		return models.Task{}, models.TaskAssignment{}, ErrAssignmentConflict
	} else if !errors.Is(getErr, ErrAssignmentNotFound) {
		return models.Task{}, models.TaskAssignment{}, getErr
	}
	if t.Status != "accepted" {
		return models.Task{}, models.TaskAssignment{}, ErrStatusConflict
	}
	policy := normalizedRetryPolicy(p.RetryPolicy, p.Deadline)
	policyJSON, _ := json.Marshal(policy)
	a := models.TaskAssignment{Kind: p.Kind, AssignmentID: p.AssignmentID, TenantID: p.TenantID, TaskID: p.TaskID, AgentID: p.TargetAgentID, State: models.AssignmentStateQueued, Revision: 1, DeliveryID: p.DeliveryID, IdempotencyKey: p.IdempotencyKey, NotBefore: p.NotBefore, Deadline: p.Deadline, RetryPolicy: &policy, CreatedAt: now, UpdatedAt: now}
	if _, err = tx.ExecContext(ctx, `INSERT INTO task_assignments (assignment_id,assignment_kind,tenant_id,task_id,agent_id,state,revision,attempt,execution_fence,lease_owner,claim_token,lease_expires_at,not_before,deadline,delivery_id,idempotency_key,retry_policy_json,failure_class,result_status,outcome_json,error_code,consumed_token_count,consumed_payment_amount,consumed_currency,execution_receipt_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`, a.AssignmentID, a.Kind, a.TenantID, a.TaskID, a.AgentID, a.State, 1, 0, 0, "", "", 0, timeText(a.NotBefore), timeText(a.Deadline), a.DeliveryID, a.IdempotencyKey, string(policyJSON), "", "", nil, "", 0, 0, "", nil, formatTime(now), formatTime(now)); err != nil {
		return models.Task{}, models.TaskAssignment{}, fmt.Errorf("insert assignment: %w", err)
	}
	res, err := tx.ExecContext(ctx, `UPDATE tasks SET status='running',updated_at=?,exec_owner='',exec_lease_expires_at=0 WHERE task_id=? AND status='accepted'`, now.Format(time.RFC3339), p.TaskID)
	if err != nil {
		return models.Task{}, models.TaskAssignment{}, err
	}
	if n, rowsErr := res.RowsAffected(); rowsErr != nil {
		return models.Task{}, models.TaskAssignment{}, rowsErr
	} else if n != 1 {
		return models.Task{}, models.TaskAssignment{}, ErrStatusConflict
	}
	t, err = getTaskTx(ctx, tx, p.TaskID)
	if err != nil {
		return models.Task{}, models.TaskAssignment{}, err
	}
	if err = s.insertTaskTransition(ctx, tx, t, "running", now); err != nil {
		return models.Task{}, models.TaskAssignment{}, err
	}
	if err = tx.Commit(); err != nil {
		return models.Task{}, models.TaskAssignment{}, err
	}
	return t, a, nil
}

func (s *executionQueueStore) CommitExecutionResult(ctx context.Context, p AssignmentResult) (models.Task, models.TaskAssignment, error) {
	if p.Status != "completed" && p.Status != "failed" && p.Status != "outcome_unknown" {
		return models.Task{}, models.TaskAssignment{}, ErrAssignmentConflict
	}
	if p.ConsumedBudget.TokenCount < 0 || p.ConsumedBudget.PaymentAmount < 0 {
		return models.Task{}, models.TaskAssignment{}, ErrBudgetExceeded
	}
	if p.Status == "outcome_unknown" {
		p.ConsumedBudget = models.DelegationBudget{}
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
	current, err := getAssignment(ctx, tx, `assignment_id=?`, p.AssignmentID)
	if err != nil {
		return models.Task{}, current, err
	}
	if current.State == models.AssignmentStateSettled {
		if current.Attempt != p.Attempt || current.ExecutionFence != p.Fence {
			return models.Task{}, current, ErrAssignmentClaimLost
		}
		var owner, token string
		if err = tx.QueryRowContext(ctx, `SELECT lease_owner,claim_token FROM execution_attempts WHERE assignment_id=? AND attempt=? AND execution_fence=?`, p.AssignmentID, p.Attempt, p.Fence).Scan(&owner, &token); err != nil || owner != p.Owner || token != p.ClaimToken {
			return models.Task{}, current, ErrAssignmentClaimLost
		}
		if !sameResult(current, p) {
			return models.Task{}, current, ErrAssignmentResultConflict
		}
		t, loadErr := getTaskTx(ctx, tx, current.TaskID)
		return t, current, loadErr
	}
	res, err := tx.ExecContext(ctx, `UPDATE task_assignments SET state='settled',revision=revision+1,failure_class=?,result_status=?,outcome_json=?,error_code=?,consumed_token_count=?,consumed_payment_amount=?,consumed_currency=?,execution_receipt_json=?,lease_owner='',claim_token='',lease_expires_at=0,updated_at=? WHERE assignment_id=? AND revision=? AND attempt=? AND execution_fence=? AND lease_owner=? AND claim_token=? AND lease_expires_at>? AND state IN ('claimed','dispatched','executing')`, p.FailureClass, p.Status, rawNull(p.Outcome), p.ErrorCode, p.ConsumedBudget.TokenCount, p.ConsumedBudget.PaymentAmount, p.ConsumedBudget.Currency, rawNull(p.ExecutionReceipt), formatTime(now), p.AssignmentID, p.Revision, p.Attempt, p.Fence, p.Owner, p.ClaimToken, now.UnixNano())
	if err != nil {
		return models.Task{}, current, err
	}
	if n, _ := res.RowsAffected(); n != 1 {
		return models.Task{}, current, classifyGuard(ctx, tx, p.AssignmentTransition, models.AssignmentStateExecuting, now)
	}
	attemptRes, err := tx.ExecContext(ctx, `UPDATE execution_attempts SET state='settled',failure_class=?,result_status=?,outcome_json=?,error_code=?,consumed_token_count=?,consumed_payment_amount=?,consumed_currency=?,execution_receipt_json=?,finished_at=? WHERE assignment_id=? AND attempt=? AND execution_fence=?`, p.FailureClass, p.Status, rawNull(p.Outcome), p.ErrorCode, p.ConsumedBudget.TokenCount, p.ConsumedBudget.PaymentAmount, p.ConsumedBudget.Currency, rawNull(p.ExecutionReceipt), formatTime(now), p.AssignmentID, p.Attempt, p.Fence)
	if err != nil || requireOneRow(attemptRes) != nil {
		if err != nil {
			return models.Task{}, current, err
		}
		return models.Task{}, current, errAssignmentAttemptUpdate
	}
	completed := any(nil)
	if isFinalStatus(p.Status) {
		completed = now.Format(time.RFC3339)
	}
	res, err = tx.ExecContext(ctx, `UPDATE tasks SET status=?,updated_at=?,completed_at=COALESCE(?,completed_at),outcome=COALESCE(?,outcome),error_code=CASE WHEN ?='' THEN error_code ELSE ? END WHERE task_id=? AND status='running'`, p.Status, now.Format(time.RFC3339), completed, rawNull(p.Outcome), p.ErrorCode, p.ErrorCode, current.TaskID)
	if err != nil {
		return models.Task{}, current, err
	}
	if n, _ := res.RowsAffected(); n != 1 {
		return models.Task{}, current, ErrStatusConflict
	}
	if err = settleTaskBudgetTx(ctx, tx, current.TaskID, p.Status, p.ConsumedBudget, now); err != nil {
		return models.Task{}, current, err
	}
	reservationState := "released"
	releaseReason := "execution_" + p.Status
	releasedAt := any(formatTime(now))
	if p.Status == "outcome_unknown" {
		reservationState, releaseReason, releasedAt = "uncertain", "outcome_unknown", nil
	}
	reservationResult, err := tx.ExecContext(ctx, `UPDATE agent_capacity_reservations SET state=?,revision=revision+1,release_reason=?,released_at=? WHERE assignment_id=? AND state='held'`, reservationState, releaseReason, releasedAt, current.AssignmentID)
	if err != nil {
		return models.Task{}, current, err
	}
	if n, rowsErr := reservationResult.RowsAffected(); rowsErr != nil {
		return models.Task{}, current, rowsErr
	} else if n > 1 {
		return models.Task{}, current, ErrReservationConflict
	}
	t, err := getTaskTx(ctx, tx, current.TaskID)
	if err != nil {
		return models.Task{}, current, err
	}
	if err = s.insertTaskTransition(ctx, tx, t, p.Status, now); err != nil {
		return models.Task{}, current, err
	}
	current, err = getAssignment(ctx, tx, `assignment_id=?`, p.AssignmentID)
	if err != nil {
		return models.Task{}, current, err
	}
	if err = tx.Commit(); err != nil {
		return models.Task{}, current, err
	}
	return t, current, nil
}

func (s *executionQueueStore) CancelAssignmentAndRelease(ctx context.Context, p CancelAssignmentParams) (models.Task, models.TaskAssignment, error) {
	if !validAssignmentKind(p.Kind) {
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
	current, err := getAssignment(ctx, tx, `assignment_id=?`, p.AssignmentID)
	if err != nil {
		return models.Task{}, current, err
	}
	if current.Kind != p.Kind {
		return models.Task{}, current, ErrAssignmentConflict
	}
	if current.State == models.AssignmentStateCancelled {
		if current.Revision != p.ExpectedRevision+1 || current.Attempt != p.ExpectedAttempt || current.ExecutionFence != p.ExpectedFence+1 {
			return models.Task{}, current, ErrAssignmentConflict
		}
		t, loadErr := getTaskTx(ctx, tx, current.TaskID)
		return t, current, loadErr
	}
	if current.Revision != p.ExpectedRevision || current.Attempt != p.ExpectedAttempt || current.ExecutionFence != p.ExpectedFence {
		return models.Task{}, current, ErrAssignmentConflict
	}
	uncertain := current.State == models.AssignmentStateDispatched || current.State == models.AssignmentStateExecuting || (current.State == models.AssignmentStateSettled && current.Kind == models.AssignmentKindOutboundDelegation && current.ResultStatus == "delivered")
	taskStatus, reservationState, reason := "cancelled", "released", "cancelled_before_dispatch"
	if uncertain {
		taskStatus, reservationState, reason = "outcome_unknown", "uncertain", "cancel_unconfirmed"
	}
	states := "('queued','claimed','dispatched','executing','retry_wait')"
	if current.State == models.AssignmentStateSettled && uncertain {
		states = "('settled')"
	}
	res, err := tx.ExecContext(ctx, `UPDATE task_assignments SET state=CASE WHEN state='settled' THEN state ELSE 'cancelled' END,revision=revision+1,execution_fence=execution_fence+1,lease_owner='',claim_token='',lease_expires_at=0,failure_class=CASE WHEN state='settled' THEN failure_class ELSE ? END,result_status=CASE WHEN state='settled' THEN result_status ELSE ? END,updated_at=? WHERE assignment_id=? AND assignment_kind=? AND revision=? AND attempt=? AND execution_fence=? AND state IN `+states, models.FailureClassCancelled, taskStatus, formatTime(now), current.AssignmentID, p.Kind, p.ExpectedRevision, p.ExpectedAttempt, p.ExpectedFence)
	if err != nil {
		return models.Task{}, current, err
	}
	if err = requireOneRow(res); err != nil {
		return models.Task{}, current, ErrAssignmentConflict
	}
	if current.Attempt > 0 && current.State != models.AssignmentStateSettled {
		attempt, updateErr := tx.ExecContext(ctx, `UPDATE execution_attempts SET state='cancelled',failure_class=?,result_status=?,finished_at=? WHERE assignment_id=? AND attempt=? AND execution_fence=? AND state IN ('claimed','dispatched','executing')`, models.FailureClassCancelled, taskStatus, formatTime(now), current.AssignmentID, current.Attempt, current.ExecutionFence)
		if updateErr != nil {
			return models.Task{}, current, updateErr
		}
		if err = requireOneRow(attempt); err != nil {
			return models.Task{}, current, err
		}
	}
	releasedAt := any(formatTime(now))
	if uncertain {
		releasedAt = nil
	}
	res, err = tx.ExecContext(ctx, `UPDATE agent_capacity_reservations SET state=?,revision=revision+1,release_reason=?,released_at=? WHERE assignment_id=? AND state='held'`, reservationState, reason, releasedAt, current.AssignmentID)
	if err != nil {
		return models.Task{}, current, err
	}
	if n, rowsErr := res.RowsAffected(); rowsErr != nil {
		return models.Task{}, current, rowsErr
	} else if n > 1 {
		return models.Task{}, current, ErrReservationConflict
	}
	res, err = tx.ExecContext(ctx, `UPDATE tasks SET status=?,updated_at=?,completed_at=CASE WHEN ?='cancelled' THEN ? ELSE completed_at END WHERE task_id=? AND status IN ('pending','accepted','running')`, taskStatus, formatTime(now), taskStatus, formatTime(now), current.TaskID)
	if err != nil {
		return models.Task{}, current, err
	}
	if err = requireOneRow(res); err != nil {
		return models.Task{}, current, ErrStatusConflict
	}
	if err = settleTaskBudgetTx(ctx, tx, current.TaskID, taskStatus, models.DelegationBudget{}, now); err != nil {
		return models.Task{}, current, err
	}
	t, err := getTaskTx(ctx, tx, current.TaskID)
	if err != nil {
		return models.Task{}, current, err
	}
	if err = s.insertTaskTransition(ctx, tx, t, taskStatus, now); err != nil {
		return models.Task{}, current, err
	}
	current, err = getAssignment(ctx, tx, `assignment_id=?`, p.AssignmentID)
	if err != nil {
		return models.Task{}, current, err
	}
	if err = tx.Commit(); err != nil {
		return models.Task{}, current, err
	}
	return t, current, nil
}

func settleParentBudget(ctx context.Context, tx *sql.Tx, taskID string, consumed models.DelegationBudget) error {
	parentID, err := childParentID(ctx, tx, taskID)
	if err != nil || parentID == "" {
		return err
	}
	res, err := tx.ExecContext(ctx, `UPDATE tasks SET reserved_token_count=reserved_token_count-(SELECT budget_token_count FROM tasks WHERE task_id=?),reserved_payment_amount=reserved_payment_amount-(SELECT budget_payment_amount FROM tasks WHERE task_id=?),consumed_token_count=consumed_token_count+?,consumed_payment_amount=consumed_payment_amount+? WHERE task_id=? AND reserved_token_count>=(SELECT budget_token_count FROM tasks WHERE task_id=?) AND reserved_payment_amount>=(SELECT budget_payment_amount FROM tasks WHERE task_id=?) AND ?<=(SELECT budget_token_count FROM tasks WHERE task_id=?) AND ?<=(SELECT budget_payment_amount FROM tasks WHERE task_id=?)`, taskID, taskID, consumed.TokenCount, consumed.PaymentAmount, parentID, taskID, taskID, consumed.TokenCount, taskID, consumed.PaymentAmount, taskID)
	if err != nil {
		return err
	}
	if n, _ := res.RowsAffected(); n != 1 {
		return ErrBudgetExceeded
	}
	return nil
}

func (s *executionQueueStore) insertTaskTransition(ctx context.Context, tx *sql.Tx, t models.Task, status string, now time.Time) error {
	payload, err := json.Marshal(t)
	if err != nil {
		return err
	}
	event := models.TaskEvent{
		EventID: newEventID(s.owner, t.TaskID, now), TaskID: t.TaskID,
		EventType: eventTypeForStatus(status), Payload: payload, PublishedAt: now,
	}
	if err = appendEvent(ctx, tx, &event); err != nil {
		return err
	}
	return insertLifecycleOutbox(ctx, tx, t, status, now)
}

func getTaskTx(ctx context.Context, tx *sql.Tx, taskID string) (models.Task, error) {
	return scanTask(tx.QueryRowContext(ctx, `SELECT task_id,session_id,interaction_id,decision_id,root_interaction_id,parent_interaction_id,root_task_id,parent_task_id,delegation_depth,deadline,budget_token_count,budget_payment_amount,budget_currency,reserved_token_count,reserved_payment_amount,consumed_token_count,consumed_payment_amount,allowed_tools_json,allowed_capabilities_json,allow_redelegation,initiator_agent_id,target_agent_id,status,created_at,updated_at,completed_at,outcome,error_code,delegation_token,tenant_id,request_id,target_workload_id,target_instance_id FROM tasks WHERE task_id=?`, taskID))
}
