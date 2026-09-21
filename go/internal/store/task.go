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

var (
	ErrStatusConflict     = errors.New("task status conflict")
	ErrInvalidTransition  = errors.New("invalid task status transition")
	ErrBudgetExceeded     = errors.New("parent task budget exceeded")
	ErrInvalidParent      = errors.New("invalid parent task")
	ErrExecutionLeaseLost = errors.New("execution lease lost")
)

var validStatusTransitions = map[string]map[string]bool{
	"pending":         {"accepted": true, "failed": true, "cancelled": true, "outcome_unknown": true},
	"accepted":        {"running": true, "cancelled": true},
	"running":         {"completed": true, "failed": true, "cancelled": true, "outcome_unknown": true},
	"outcome_unknown": {"completed": true, "failed": true, "cancelled": true},
	"completed":       {},
	"failed":          {},
	"cancelled":       {},
}

// TaskStore persists and queries tasks.
type TaskStore interface {
	Create(ctx context.Context, t models.Task) error
	CreateWithEvent(ctx context.Context, t models.Task) (models.TaskEvent, error)
	Get(ctx context.Context, taskID string) (models.Task, error)
	GetForTenant(ctx context.Context, tenantID, taskID string) (models.Task, error)
	ListForTenantInitiator(ctx context.Context, tenantID, initiatorAgentID string, rootOnly bool) ([]models.Task, error)
	UpdateStatus(ctx context.Context, taskID, expectedStatus, status string, outcome []byte, errorCode string) (models.Task, models.TaskEvent, error)
	UpdateStatusWithConsumption(ctx context.Context, taskID, expectedStatus, status string, outcome []byte, errorCode string, consumed models.DelegationBudget) (models.Task, models.TaskEvent, error)
	RecordLifecycle(ctx context.Context, task models.Task, event string) error
	SetDelegationToken(ctx context.Context, taskID, delegationToken string) error
	ListBySession(ctx context.Context, sessionID string) ([]models.Task, error)
	ListByTarget(ctx context.Context, targetAgentID string) ([]models.Task, error)
	ListDescendants(ctx context.Context, taskID string) ([]models.Task, error)
	CancelDescendants(ctx context.Context, taskID string) ([]models.Task, error)
	// RenewExecutionLease extends the lease of a running task owned by this
	// instance. It returns ErrExecutionLeaseLost if the task is missing, is no
	// longer running, or is owned by another instance.
	RenewExecutionLease(ctx context.Context, taskID string) error
	// RecoverExpiredRunning transitions running tasks whose lease expired to
	// outcome_unknown. Each transition is an atomic compare-and-set guarded by
	// the lease expiry, so concurrent instances cannot double-recover a task.
	RecoverExpiredRunning(ctx context.Context, now time.Time, limit int) ([]models.Task, error)
	CreateChildWithBudget(ctx context.Context, child models.Task) (models.TaskEvent, error)
}

type taskStore struct {
	db    *sql.DB
	owner string
	lease *time.Duration
}

func (s *taskStore) DelegationApprovalStore() DelegationApprovalStore {
	return &delegationApprovalStore{db: s.db}
}

func (s *taskStore) InstanceID() string { return s.owner }

func (s *taskStore) leaseDuration() time.Duration {
	if s.lease == nil || *s.lease <= 0 {
		return defaultExecutionLease
	}
	return *s.lease
}

func (s *taskStore) Create(ctx context.Context, t models.Task) error {
	_, err := insertTask(ctx, s.db, t)
	return err
}

// CreateWithEvent inserts a task and its task_created event atomically.
func (s *taskStore) CreateWithEvent(ctx context.Context, t models.Task) (models.TaskEvent, error) {
	if t.TaskID == "" {
		return models.TaskEvent{}, fmt.Errorf("task_id is required")
	}
	payload, err := json.Marshal(t)
	if err != nil {
		return models.TaskEvent{}, fmt.Errorf("marshal task event payload: %w", err)
	}
	event := models.TaskEvent{
		ProtocolVersion: models.CurrentProtocolVersion,
		EventID:         newEventID(s.owner, t.TaskID, t.CreatedAt),
		TaskID:          t.TaskID,
		EventType:       "task_created",
		Payload:         payload,
		PublishedAt:     t.CreatedAt,
	}

	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return models.TaskEvent{}, fmt.Errorf("begin task creation: %w", err)
	}
	defer tx.Rollback()
	if _, err := insertTask(ctx, tx, t); err != nil {
		return models.TaskEvent{}, err
	}
	if err := appendEvent(ctx, tx, &event); err != nil {
		return models.TaskEvent{}, fmt.Errorf("insert task event: %w", err)
	}
	if err := tx.Commit(); err != nil {
		return models.TaskEvent{}, fmt.Errorf("commit task creation: %w", err)
	}
	return event, nil
}

// CreateChildWithBudget atomically reserves the requested envelope from the
// parent and persists the child task plus its creation event.
func (s *taskStore) CreateChildWithBudget(ctx context.Context, child models.Task) (models.TaskEvent, error) {
	if child.ParentTaskID == "" {
		return models.TaskEvent{}, ErrInvalidParent
	}
	if child.Budget.TokenCount < 0 || child.Budget.PaymentAmount < 0 {
		return models.TaskEvent{}, ErrBudgetExceeded
	}
	payload, err := json.Marshal(child)
	if err != nil {
		return models.TaskEvent{}, fmt.Errorf("marshal task event payload: %w", err)
	}
	event := models.TaskEvent{
		ProtocolVersion: models.CurrentProtocolVersion,
		EventID:         newEventID(s.owner, child.TaskID, child.CreatedAt),
		TaskID:          child.TaskID, EventType: "task_created", Payload: payload, PublishedAt: child.CreatedAt,
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return models.TaskEvent{}, fmt.Errorf("begin child task creation: %w", err)
	}
	defer tx.Rollback()
	res, err := tx.ExecContext(ctx, `
		UPDATE tasks SET reserved_token_count = reserved_token_count + ?, reserved_payment_amount = reserved_payment_amount + ?
		WHERE task_id = ? AND target_agent_id = ? AND status IN ('pending','accepted','running')
		  AND budget_currency = ?
		  AND budget_token_count - reserved_token_count - consumed_token_count >= ?
		  AND budget_payment_amount - reserved_payment_amount - consumed_payment_amount >= ?
	`, child.Budget.TokenCount, child.Budget.PaymentAmount, child.ParentTaskID, child.InitiatorAgentID,
		child.Budget.Currency, child.Budget.TokenCount, child.Budget.PaymentAmount)
	if err != nil {
		return models.TaskEvent{}, fmt.Errorf("reserve parent budget: %w", err)
	}
	n, err := res.RowsAffected()
	if err != nil {
		return models.TaskEvent{}, fmt.Errorf("reserve parent budget rows affected: %w", err)
	}
	if n == 0 {
		return models.TaskEvent{}, ErrBudgetExceeded
	}
	if _, err := insertTask(ctx, tx, child); err != nil {
		return models.TaskEvent{}, err
	}
	if err := appendEvent(ctx, tx, &event); err != nil {
		return models.TaskEvent{}, fmt.Errorf("insert task event: %w", err)
	}
	if err := tx.Commit(); err != nil {
		return models.TaskEvent{}, fmt.Errorf("commit child task creation: %w", err)
	}
	return event, nil
}

type taskExecer interface {
	ExecContext(context.Context, string, ...any) (sql.Result, error)
}

func insertTask(ctx context.Context, execer taskExecer, t models.Task) (sql.Result, error) {
	if t.TaskID == "" {
		return nil, fmt.Errorf("task_id is required")
	}
	completedAt := sql.NullString{}
	if t.CompletedAt != nil {
		completedAt = sql.NullString{String: t.CompletedAt.Format(time.RFC3339), Valid: true}
	}
	outcome := sql.NullString{}
	if len(t.Outcome) > 0 {
		outcome = sql.NullString{String: string(t.Outcome), Valid: true}
	}
	deadline := sql.NullString{}
	if t.Deadline != nil {
		deadline = sql.NullString{String: t.Deadline.UTC().Format(time.RFC3339Nano), Valid: true}
	}
	allowedTools, _ := json.Marshal(t.AllowedTools)
	allowedCapabilities, _ := json.Marshal(t.AllowedCapabilities)
	res, err := execer.ExecContext(ctx, `
		INSERT INTO tasks (task_id, session_id, interaction_id, decision_id, root_interaction_id, parent_interaction_id, root_task_id, parent_task_id, delegation_depth, deadline, budget_token_count, budget_payment_amount, budget_currency, reserved_token_count, reserved_payment_amount, consumed_token_count, consumed_payment_amount, allowed_tools_json, allowed_capabilities_json, allow_redelegation, initiator_agent_id, target_agent_id, status, created_at, updated_at, completed_at, outcome, error_code, delegation_token, tenant_id, request_id, target_workload_id, target_instance_id)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
	`, t.TaskID, t.SessionID, t.InteractionID, t.DecisionID, t.RootInteractionID, t.ParentInteractionID,
		t.RootTaskID, t.ParentTaskID, t.DelegationDepth, deadline,
		t.Budget.TokenCount, t.Budget.PaymentAmount, t.Budget.Currency,
		t.ReservedBudget.TokenCount, t.ReservedBudget.PaymentAmount,
		t.ConsumedBudget.TokenCount, t.ConsumedBudget.PaymentAmount,
		string(allowedTools), string(allowedCapabilities), t.AllowRedelegation,
		t.InitiatorAgentID, t.TargetAgentID, t.Status, t.CreatedAt.Format(time.RFC3339), t.UpdatedAt.Format(time.RFC3339),
		completedAt, outcome, t.ErrorCode, t.DelegationToken, t.TenantID, t.RequestID, t.TargetWorkloadID, t.TargetInstanceID)
	if err != nil {
		return nil, fmt.Errorf("insert task: %w", err)
	}
	return res, nil
}

func (s *taskStore) Get(ctx context.Context, taskID string) (models.Task, error) {
	return getTask(ctx, s.db, "task_id = ?", taskID)
}

func (s *taskStore) GetForTenant(ctx context.Context, tenantID, taskID string) (models.Task, error) {
	return getTask(ctx, s.db, "tenant_id = ? AND task_id = ?", tenantID, taskID)
}

func (s *taskStore) ListForTenantInitiator(ctx context.Context, tenantID, initiatorAgentID string, rootOnly bool) ([]models.Task, error) {
	rootClause := ""
	if rootOnly {
		rootClause = " AND parent_task_id = ''"
	}
	rows, err := s.db.QueryContext(ctx, `
		SELECT task_id, session_id, interaction_id, decision_id, root_interaction_id, parent_interaction_id, root_task_id, parent_task_id, delegation_depth, deadline, budget_token_count, budget_payment_amount, budget_currency, reserved_token_count, reserved_payment_amount, consumed_token_count, consumed_payment_amount, allowed_tools_json, allowed_capabilities_json, allow_redelegation, initiator_agent_id, target_agent_id, status, created_at, updated_at, completed_at, outcome, error_code, delegation_token, tenant_id, request_id, target_workload_id, target_instance_id
		FROM tasks
		WHERE tenant_id = ? AND initiator_agent_id = ?`+rootClause+`
		ORDER BY created_at DESC
	`, tenantID, initiatorAgentID)
	if err != nil {
		return nil, fmt.Errorf("list tasks for tenant initiator: %w", err)
	}
	defer rows.Close()
	tasks, err := scanTasks(rows)
	if err != nil {
		return nil, err
	}
	if tasks == nil {
		tasks = []models.Task{}
	}
	return tasks, nil
}

func getTask(ctx context.Context, q interface {
	QueryRowContext(context.Context, string, ...any) *sql.Row
}, where string, args ...any) (models.Task, error) {
	row := q.QueryRowContext(ctx, `
		SELECT task_id, session_id, interaction_id, decision_id, root_interaction_id, parent_interaction_id, root_task_id, parent_task_id, delegation_depth, deadline, budget_token_count, budget_payment_amount, budget_currency, reserved_token_count, reserved_payment_amount, consumed_token_count, consumed_payment_amount, allowed_tools_json, allowed_capabilities_json, allow_redelegation, initiator_agent_id, target_agent_id, status, created_at, updated_at, completed_at, outcome, error_code, delegation_token, tenant_id, request_id, target_workload_id, target_instance_id
		FROM tasks WHERE `+where, args...)
	return scanTask(row)
}

func (s *taskStore) UpdateStatus(ctx context.Context, taskID, expectedStatus, status string, outcome []byte, errorCode string) (models.Task, models.TaskEvent, error) {
	return s.UpdateStatusWithConsumption(ctx, taskID, expectedStatus, status, outcome, errorCode, models.DelegationBudget{})
}

func (s *taskStore) UpdateStatusWithConsumption(ctx context.Context, taskID, expectedStatus, status string, outcome []byte, errorCode string, consumed models.DelegationBudget) (models.Task, models.TaskEvent, error) {
	if !validStatusTransitions[expectedStatus][status] {
		return models.Task{}, models.TaskEvent{}, ErrInvalidTransition
	}
	if consumed.TokenCount < 0 || consumed.PaymentAmount < 0 {
		return models.Task{}, models.TaskEvent{}, ErrBudgetExceeded
	}
	now := time.Now().UTC()
	completedAt := sql.NullString{}
	if isFinalStatus(status) {
		completedAt = sql.NullString{String: now.Format(time.RFC3339), Valid: true}
	}
	outcomeStr := sql.NullString{}
	if len(outcome) > 0 {
		outcomeStr = sql.NullString{String: string(outcome), Valid: true}
	}

	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return models.Task{}, models.TaskEvent{}, fmt.Errorf("begin task transition: %w", err)
	}
	defer tx.Rollback()
	leaseExpiresAt := now.Add(s.leaseDuration()).UnixNano()
	res, err := tx.ExecContext(ctx, `
		UPDATE tasks
		SET status = ?, updated_at = ?, completed_at = COALESCE(?, completed_at), outcome = COALESCE(?, outcome), error_code = CASE WHEN ? = '' THEN error_code ELSE ? END,
		    exec_owner = CASE WHEN ? = 'running' THEN ? ELSE exec_owner END,
		    exec_lease_expires_at = CASE WHEN ? = 'running' THEN ? ELSE exec_lease_expires_at END
		WHERE task_id = ? AND status = ?
	`, status, now.Format(time.RFC3339), completedAt, outcomeStr, errorCode, errorCode,
		status, s.owner, status, leaseExpiresAt, taskID, expectedStatus)
	if err != nil {
		return models.Task{}, models.TaskEvent{}, fmt.Errorf("update task status: %w", err)
	}
	n, err := res.RowsAffected()
	if err != nil {
		return models.Task{}, models.TaskEvent{}, fmt.Errorf("rows affected: %w", err)
	}
	if n == 0 {
		return models.Task{}, models.TaskEvent{}, ErrStatusConflict
	}
	if isFinalStatus(status) {
		if _, err := tx.ExecContext(ctx, `
			UPDATE tasks
			SET exec_owner = '', exec_lease_expires_at = 0
			WHERE task_id = ?
		`, taskID); err != nil {
			return models.Task{}, models.TaskEvent{}, fmt.Errorf("clear execution lease: %w", err)
		}
	}
	if err := settleTaskBudgetTx(ctx, tx, taskID, status, consumed, now); err != nil {
		return models.Task{}, models.TaskEvent{}, err
	}
	updated, err := scanTask(tx.QueryRowContext(ctx, `
		SELECT task_id, session_id, interaction_id, decision_id, root_interaction_id, parent_interaction_id, root_task_id, parent_task_id, delegation_depth, deadline, budget_token_count, budget_payment_amount, budget_currency, reserved_token_count, reserved_payment_amount, consumed_token_count, consumed_payment_amount, allowed_tools_json, allowed_capabilities_json, allow_redelegation, initiator_agent_id, target_agent_id, status, created_at, updated_at, completed_at, outcome, error_code, delegation_token, tenant_id, request_id, target_workload_id, target_instance_id
		FROM tasks WHERE task_id = ?
	`, taskID))
	if err != nil {
		return models.Task{}, models.TaskEvent{}, err
	}
	payload, err := json.Marshal(updated)
	if err != nil {
		return models.Task{}, models.TaskEvent{}, fmt.Errorf("marshal task event payload: %w", err)
	}
	event := models.TaskEvent{
		ProtocolVersion: models.CurrentProtocolVersion,
		EventID:         newEventID(s.owner, taskID, now),
		TaskID:          taskID,
		EventType:       eventTypeForStatus(status),
		Payload:         payload,
		PublishedAt:     now,
	}
	if err := appendEvent(ctx, tx, &event); err != nil {
		return models.Task{}, models.TaskEvent{}, fmt.Errorf("insert task event: %w", err)
	}
	if err := insertLifecycleOutbox(ctx, tx, updated, status, now); err != nil {
		return models.Task{}, models.TaskEvent{}, err
	}
	if err := tx.Commit(); err != nil {
		return models.Task{}, models.TaskEvent{}, fmt.Errorf("commit task transition: %w", err)
	}
	return updated, event, nil
}

func childParentID(ctx context.Context, tx *sql.Tx, taskID string) (string, error) {
	var parentID string
	if err := tx.QueryRowContext(ctx, `SELECT parent_task_id FROM tasks WHERE task_id = ?`, taskID).Scan(&parentID); err != nil {
		return "", fmt.Errorf("load child parent for budget settlement: %w", err)
	}
	return parentID, nil
}

func (s *taskStore) RecordLifecycle(ctx context.Context, task models.Task, event string) error {
	now := time.Now().UTC()
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("begin lifecycle record: %w", err)
	}
	defer tx.Rollback()
	if err := insertLifecycleOutbox(ctx, tx, task, event, now); err != nil {
		return err
	}
	if err := tx.Commit(); err != nil {
		return fmt.Errorf("commit lifecycle record: %w", err)
	}
	return nil
}

func (s *taskStore) SetDelegationToken(ctx context.Context, taskID, delegationToken string) error {
	res, err := s.db.ExecContext(ctx, `UPDATE tasks SET delegation_token = ? WHERE task_id = ?`, delegationToken, taskID)
	if err != nil {
		return fmt.Errorf("persist delegation token: %w", err)
	}
	n, err := res.RowsAffected()
	if err != nil {
		return fmt.Errorf("delegation token rows affected: %w", err)
	}
	if n == 0 {
		return sql.ErrNoRows
	}
	return nil
}

// RenewExecutionLease extends this instance's lease on a running task. It
// returns ErrExecutionLeaseLost when the guarded update no longer matches.
func (s *taskStore) RenewExecutionLease(ctx context.Context, taskID string) error {
	now := time.Now().UTC()
	leaseExpiresAt := now.Add(s.leaseDuration()).UnixNano()
	res, err := s.db.ExecContext(ctx, `
		UPDATE tasks
		SET exec_lease_expires_at = ?, updated_at = ?
		WHERE task_id = ? AND status = 'running' AND exec_owner = ?
	`, leaseExpiresAt, now.Format(time.RFC3339), taskID, s.owner)
	if err != nil {
		return fmt.Errorf("renew execution lease: %w", err)
	}
	n, err := res.RowsAffected()
	if err != nil {
		return fmt.Errorf("renew execution lease rows affected: %w", err)
	}
	if n == 0 {
		return fmt.Errorf("%w: task %s", ErrExecutionLeaseLost, taskID)
	}
	return nil
}

// RecoverExpiredRunning transitions running tasks whose lease has expired to
// outcome_unknown. Only the lease-expiry guard can win a transition, so
// concurrent recovery by multiple instances cannot double-recover a task.
func (s *taskStore) RecoverExpiredRunning(ctx context.Context, now time.Time, limit int) ([]models.Task, error) {
	if limit <= 0 {
		limit = 100
	}
	rows, err := s.db.QueryContext(ctx, `
		SELECT task_id FROM tasks
		WHERE status = 'running'
		  AND exec_owner != ''
		  AND exec_lease_expires_at > 0
		  AND exec_lease_expires_at < ?
		ORDER BY exec_lease_expires_at ASC
		LIMIT ?
	`, now.UnixNano(), limit)
	if err != nil {
		return nil, fmt.Errorf("list expired running tasks: %w", err)
	}
	var taskIDs []string
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			rows.Close()
			return nil, fmt.Errorf("scan expired task id: %w", err)
		}
		taskIDs = append(taskIDs, id)
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return nil, fmt.Errorf("iterate expired task ids: %w", err)
	}
	rows.Close()

	recovered := make([]models.Task, 0, len(taskIDs))
	for _, taskID := range taskIDs {
		t, err := s.recoverOne(ctx, taskID, now)
		if err != nil {
			return recovered, err
		}
		if t != nil {
			recovered = append(recovered, *t)
		}
	}
	return recovered, nil
}

func (s *taskStore) recoverOne(ctx context.Context, taskID string, now time.Time) (*models.Task, error) {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return nil, fmt.Errorf("begin recovery: %w", err)
	}
	defer tx.Rollback()

	res, err := tx.ExecContext(ctx, `
		UPDATE tasks
		SET status = 'outcome_unknown', updated_at = ?
		WHERE task_id = ? AND status = 'running'
		  AND exec_owner != ''
		  AND exec_lease_expires_at > 0
		  AND exec_lease_expires_at < ?
	`, now.Format(time.RFC3339), taskID, now.UnixNano())
	if err != nil {
		return nil, fmt.Errorf("recover task status: %w", err)
	}
	n, err := res.RowsAffected()
	if err != nil {
		return nil, fmt.Errorf("recovery rows affected: %w", err)
	}
	if n == 0 {
		// The lease was renewed, or another instance already recovered it.
		return nil, nil
	}

	updated, err := scanTask(tx.QueryRowContext(ctx, `
		SELECT task_id, session_id, interaction_id, decision_id, root_interaction_id, parent_interaction_id, root_task_id, parent_task_id, delegation_depth, deadline, budget_token_count, budget_payment_amount, budget_currency, reserved_token_count, reserved_payment_amount, consumed_token_count, consumed_payment_amount, allowed_tools_json, allowed_capabilities_json, allow_redelegation, initiator_agent_id, target_agent_id, status, created_at, updated_at, completed_at, outcome, error_code, delegation_token, tenant_id, request_id, target_workload_id, target_instance_id
		FROM tasks WHERE task_id = ?
	`, taskID))
	if err != nil {
		return nil, err
	}
	payload, err := json.Marshal(updated)
	if err != nil {
		return nil, fmt.Errorf("marshal recovered task event payload: %w", err)
	}
	event := models.TaskEvent{
		ProtocolVersion: models.CurrentProtocolVersion,
		EventID:         newEventID(s.owner, taskID, now),
		TaskID:          taskID,
		EventType:       eventTypeForStatus("outcome_unknown"),
		Payload:         payload,
		PublishedAt:     now,
	}
	if err := appendEvent(ctx, tx, &event); err != nil {
		return nil, fmt.Errorf("insert recovery event: %w", err)
	}
	if err := insertLifecycleOutbox(ctx, tx, updated, "outcome_unknown", now); err != nil {
		return nil, err
	}
	if err := tx.Commit(); err != nil {
		return nil, fmt.Errorf("commit recovery: %w", err)
	}
	return &updated, nil
}

func (s *taskStore) ListBySession(ctx context.Context, sessionID string) ([]models.Task, error) {
	rows, err := s.db.QueryContext(ctx, `
		SELECT task_id, session_id, interaction_id, decision_id, root_interaction_id, parent_interaction_id, root_task_id, parent_task_id, delegation_depth, deadline, budget_token_count, budget_payment_amount, budget_currency, reserved_token_count, reserved_payment_amount, consumed_token_count, consumed_payment_amount, allowed_tools_json, allowed_capabilities_json, allow_redelegation, initiator_agent_id, target_agent_id, status, created_at, updated_at, completed_at, outcome, error_code, delegation_token, tenant_id, request_id, target_workload_id, target_instance_id
		FROM tasks
		WHERE session_id = ?
		ORDER BY created_at DESC
	`, sessionID)
	if err != nil {
		return nil, fmt.Errorf("list tasks by session: %w", err)
	}
	defer rows.Close()
	return scanTasks(rows)
}

func (s *taskStore) ListByTarget(ctx context.Context, targetAgentID string) ([]models.Task, error) {
	rows, err := s.db.QueryContext(ctx, `
		SELECT task_id, session_id, interaction_id, decision_id, root_interaction_id, parent_interaction_id, root_task_id, parent_task_id, delegation_depth, deadline, budget_token_count, budget_payment_amount, budget_currency, reserved_token_count, reserved_payment_amount, consumed_token_count, consumed_payment_amount, allowed_tools_json, allowed_capabilities_json, allow_redelegation, initiator_agent_id, target_agent_id, status, created_at, updated_at, completed_at, outcome, error_code, delegation_token, tenant_id, request_id, target_workload_id, target_instance_id
		FROM tasks
		WHERE target_agent_id = ?
		ORDER BY created_at DESC
	`, targetAgentID)
	if err != nil {
		return nil, fmt.Errorf("list tasks by target: %w", err)
	}
	defer rows.Close()
	return scanTasks(rows)
}

func (s *taskStore) ListDescendants(ctx context.Context, taskID string) ([]models.Task, error) {
	rows, err := s.db.QueryContext(ctx, `
		WITH RECURSIVE descendants(task_id, depth) AS (
			SELECT task_id, 1 FROM tasks WHERE parent_task_id = ?
			UNION ALL
			SELECT t.task_id, d.depth + 1 FROM tasks t JOIN descendants d ON t.parent_task_id = d.task_id
		)
		SELECT t.task_id, t.session_id, t.interaction_id, t.decision_id, t.root_interaction_id, t.parent_interaction_id, t.root_task_id, t.parent_task_id, t.delegation_depth, t.deadline, t.budget_token_count, t.budget_payment_amount, t.budget_currency, t.reserved_token_count, t.reserved_payment_amount, t.consumed_token_count, t.consumed_payment_amount, t.allowed_tools_json, t.allowed_capabilities_json, t.allow_redelegation, t.initiator_agent_id, t.target_agent_id, t.status, t.created_at, t.updated_at, t.completed_at, t.outcome, t.error_code, t.delegation_token, t.tenant_id, t.request_id, t.target_workload_id, t.target_instance_id
		FROM tasks t JOIN descendants d ON t.task_id = d.task_id
		ORDER BY d.depth DESC, t.created_at DESC
	`, taskID)
	if err != nil {
		return nil, fmt.Errorf("list descendants: %w", err)
	}
	defer rows.Close()
	return scanTasks(rows)
}

func (s *taskStore) CancelDescendants(ctx context.Context, taskID string) ([]models.Task, error) {
	now := time.Now().UTC()
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return nil, fmt.Errorf("begin descendant cancellation: %w", err)
	}
	defer tx.Rollback()
	rows, err := tx.QueryContext(ctx, `
		WITH RECURSIVE descendants(task_id) AS (
			SELECT task_id FROM tasks WHERE parent_task_id = ?
			UNION ALL
			SELECT t.task_id FROM tasks t JOIN descendants d ON t.parent_task_id = d.task_id
		)
		SELECT task_id FROM descendants
	`, taskID)
	if err != nil {
		return nil, fmt.Errorf("list descendants: %w", err)
	}
	var ids []string
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			rows.Close()
			return nil, err
		}
		ids = append(ids, id)
	}
	rows.Close()
	cancelled := make([]models.Task, 0, len(ids))
	for _, id := range ids {
		res, err := tx.ExecContext(ctx, `UPDATE tasks SET status = 'cancelled', updated_at = ?, completed_at = ? WHERE task_id = ? AND status IN ('pending','accepted')`, now.Format(time.RFC3339), now.Format(time.RFC3339), id)
		if err != nil {
			return nil, fmt.Errorf("cancel descendant: %w", err)
		}
		n, _ := res.RowsAffected()
		if n == 0 {
			continue
		}
		if _, err := tx.ExecContext(ctx, `
			UPDATE tasks
			SET reserved_token_count = reserved_token_count - (SELECT budget_token_count FROM tasks WHERE task_id = ?),
			    reserved_payment_amount = reserved_payment_amount - (SELECT budget_payment_amount FROM tasks WHERE task_id = ?)
			WHERE task_id = (SELECT parent_task_id FROM tasks WHERE task_id = ?)
			  AND reserved_token_count >= (SELECT budget_token_count FROM tasks WHERE task_id = ?)
			  AND reserved_payment_amount >= (SELECT budget_payment_amount FROM tasks WHERE task_id = ?)
		`, id, id, id, id, id); err != nil {
			return nil, fmt.Errorf("refund cancelled descendant budget: %w", err)
		}
		t, err := scanTask(tx.QueryRowContext(ctx, `
			SELECT task_id, session_id, interaction_id, decision_id, root_interaction_id, parent_interaction_id, root_task_id, parent_task_id, delegation_depth, deadline, budget_token_count, budget_payment_amount, budget_currency, reserved_token_count, reserved_payment_amount, consumed_token_count, consumed_payment_amount, allowed_tools_json, allowed_capabilities_json, allow_redelegation, initiator_agent_id, target_agent_id, status, created_at, updated_at, completed_at, outcome, error_code, delegation_token, tenant_id, request_id, target_workload_id, target_instance_id FROM tasks WHERE task_id = ?`, id))
		if err != nil {
			return nil, err
		}
		payload, err := json.Marshal(t)
		if err != nil {
			return nil, err
		}
		event := models.TaskEvent{ProtocolVersion: models.CurrentProtocolVersion, EventID: newEventID(s.owner, id, now), TaskID: id, EventType: "task_cancelled", Payload: payload, PublishedAt: now}
		if err := appendEvent(ctx, tx, &event); err != nil {
			return nil, err
		}
		if err := insertLifecycleOutbox(ctx, tx, t, "cancelled", now); err != nil {
			return nil, err
		}
		cancelled = append(cancelled, t)
	}
	if err := tx.Commit(); err != nil {
		return nil, fmt.Errorf("commit descendant cancellation: %w", err)
	}
	return cancelled, nil
}

func scanTask(row *sql.Row) (models.Task, error) {
	var t models.Task
	var createdAt, updatedAt, completedAt, deadline sql.NullString
	var outcome sql.NullString
	var allowedTools, allowedCapabilities string
	err := row.Scan(&t.TaskID, &t.SessionID, &t.InteractionID, &t.DecisionID, &t.RootInteractionID, &t.ParentInteractionID,
		&t.RootTaskID, &t.ParentTaskID, &t.DelegationDepth, &deadline, &t.Budget.TokenCount, &t.Budget.PaymentAmount, &t.Budget.Currency, &t.ReservedBudget.TokenCount, &t.ReservedBudget.PaymentAmount,
		&t.ConsumedBudget.TokenCount, &t.ConsumedBudget.PaymentAmount, &allowedTools, &allowedCapabilities, &t.AllowRedelegation,
		&t.InitiatorAgentID, &t.TargetAgentID, &t.Status, &createdAt, &updatedAt, &completedAt, &outcome, &t.ErrorCode, &t.DelegationToken, &t.TenantID, &t.RequestID, &t.TargetWorkloadID, &t.TargetInstanceID)
	if err != nil {
		if err == sql.ErrNoRows {
			return models.Task{}, err
		}
		return models.Task{}, fmt.Errorf("scan task: %w", err)
	}
	ca, err := time.Parse(time.RFC3339, createdAt.String)
	if err != nil {
		return models.Task{}, fmt.Errorf("parse created_at: %w", err)
	}
	ua, err := time.Parse(time.RFC3339, updatedAt.String)
	if err != nil {
		return models.Task{}, fmt.Errorf("parse updated_at: %w", err)
	}
	t.ProtocolVersion = models.CurrentProtocolVersion
	if err := json.Unmarshal([]byte(allowedTools), &t.AllowedTools); err != nil {
		return models.Task{}, fmt.Errorf("parse allowed_tools: %w", err)
	}
	if err := json.Unmarshal([]byte(allowedCapabilities), &t.AllowedCapabilities); err != nil {
		return models.Task{}, fmt.Errorf("parse allowed_capabilities: %w", err)
	}
	t.CreatedAt = ca
	t.UpdatedAt = ua
	if deadline.Valid {
		d, err := time.Parse(time.RFC3339Nano, deadline.String)
		if err != nil {
			return models.Task{}, fmt.Errorf("parse deadline: %w", err)
		}
		t.Deadline = &d
	}
	if completedAt.Valid {
		c, err := time.Parse(time.RFC3339, completedAt.String)
		if err != nil {
			return models.Task{}, fmt.Errorf("parse completed_at: %w", err)
		}
		t.CompletedAt = &c
	}
	if outcome.Valid {
		t.Outcome = json.RawMessage(outcome.String)
	}
	return t, nil
}

func scanTasks(rows *sql.Rows) ([]models.Task, error) {
	var out []models.Task
	for rows.Next() {
		t, err := scanTaskFromRows(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, t)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate tasks: %w", err)
	}
	return out, nil
}

func scanTaskFromRows(rows *sql.Rows) (models.Task, error) {
	var t models.Task
	var createdAt, updatedAt, completedAt, deadline sql.NullString
	var outcome sql.NullString
	var allowedTools, allowedCapabilities string
	err := rows.Scan(&t.TaskID, &t.SessionID, &t.InteractionID, &t.DecisionID, &t.RootInteractionID, &t.ParentInteractionID,
		&t.RootTaskID, &t.ParentTaskID, &t.DelegationDepth, &deadline, &t.Budget.TokenCount, &t.Budget.PaymentAmount, &t.Budget.Currency, &t.ReservedBudget.TokenCount, &t.ReservedBudget.PaymentAmount,
		&t.ConsumedBudget.TokenCount, &t.ConsumedBudget.PaymentAmount, &allowedTools, &allowedCapabilities, &t.AllowRedelegation,
		&t.InitiatorAgentID, &t.TargetAgentID, &t.Status, &createdAt, &updatedAt, &completedAt, &outcome, &t.ErrorCode, &t.DelegationToken, &t.TenantID, &t.RequestID, &t.TargetWorkloadID, &t.TargetInstanceID)
	if err != nil {
		return models.Task{}, fmt.Errorf("scan task row: %w", err)
	}
	ca, err := time.Parse(time.RFC3339, createdAt.String)
	if err != nil {
		return models.Task{}, fmt.Errorf("parse created_at: %w", err)
	}
	ua, err := time.Parse(time.RFC3339, updatedAt.String)
	if err != nil {
		return models.Task{}, fmt.Errorf("parse updated_at: %w", err)
	}
	t.ProtocolVersion = models.CurrentProtocolVersion
	if err := json.Unmarshal([]byte(allowedTools), &t.AllowedTools); err != nil {
		return models.Task{}, fmt.Errorf("parse allowed_tools: %w", err)
	}
	if err := json.Unmarshal([]byte(allowedCapabilities), &t.AllowedCapabilities); err != nil {
		return models.Task{}, fmt.Errorf("parse allowed_capabilities: %w", err)
	}
	t.CreatedAt = ca
	t.UpdatedAt = ua
	if deadline.Valid {
		d, err := time.Parse(time.RFC3339Nano, deadline.String)
		if err != nil {
			return models.Task{}, fmt.Errorf("parse deadline: %w", err)
		}
		t.Deadline = &d
	}
	if completedAt.Valid {
		c, err := time.Parse(time.RFC3339, completedAt.String)
		if err != nil {
			return models.Task{}, fmt.Errorf("parse completed_at: %w", err)
		}
		t.CompletedAt = &c
	}
	if outcome.Valid {
		t.Outcome = json.RawMessage(outcome.String)
	}
	return t, nil
}

func eventTypeForStatus(status string) string {
	switch status {
	case "accepted":
		return "task_accepted"
	case "running":
		return "task_running"
	case "completed":
		return "task_completed"
	case "failed":
		return "task_failed"
	case "cancelled":
		return "task_cancelled"
	case "outcome_unknown":
		return "task_outcome_unknown"
	}
	return "task_updated"
}

func isFinalStatus(status string) bool {
	switch status {
	case "completed", "failed", "cancelled":
		return true
	}
	return false
}
