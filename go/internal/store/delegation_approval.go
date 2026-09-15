package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"sync/atomic"
	"time"

	"github.com/loop-controller/go/internal/models"
)

var (
	ErrApprovalNotFound = errors.New("delegation approval not found")
	ErrApprovalConflict = errors.New("delegation approval conflict")
	ErrApprovalExpired  = errors.New("delegation approval expired")
)

type DelegationApprovalStore interface {
	Create(context.Context, models.DelegationApproval) (models.DelegationApproval, bool, error)
	Get(context.Context, string) (models.DelegationApproval, error)
	Transition(context.Context, string, int64, string, string, string, string, string) (models.DelegationApproval, error)
	Consume(context.Context, string, int64, models.Task, models.AgentEntrypoint, models.EntrypointTaskRequest) (models.DelegationApproval, error)
	ExpireDue(context.Context, time.Time, int) (int64, error)
}

type delegationApprovalStore struct {
	db                      *sql.DB
	notificationsEnabled    *atomic.Bool
	notificationDestination *atomic.Value
}

func (db *DB) DelegationApprovalStore() DelegationApprovalStore {
	return &delegationApprovalStore{db: db.DB, notificationsEnabled: &db.approvalNotifications, notificationDestination: &db.notificationDestination}
}

func (s *delegationApprovalStore) Create(ctx context.Context, a models.DelegationApproval) (models.DelegationApproval, bool, error) {
	tools, _ := json.Marshal(a.AllowedTools)
	caps, _ := json.Marshal(a.AllowedCapabilities)
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return models.DelegationApproval{}, false, err
	}
	defer tx.Rollback()
	_, err = tx.ExecContext(ctx, `INSERT INTO delegation_approvals (
		approval_id,request_id,decision_id,request_hash,initiator_agent_id,target_agent_id,session_id,tenant_id,target_workload_id,target_instance_id,
		root_task_id,parent_task_id,delegation_depth,effective_args_json,allowed_tools_json,allowed_capabilities_json,
		allow_redelegation,budget_token_count,budget_payment_amount,budget_currency,task_deadline,expires_at,status,
		approver_id,reason,created_at,updated_at,decided_at,task_id,version)
		VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
		a.ApprovalID, a.RequestID, a.DecisionID, a.RequestHash, a.InitiatorAgentID, a.TargetAgentID, a.SessionID, a.TenantID, a.TargetWorkloadID, a.TargetInstanceID,
		a.RootTaskID, a.ParentTaskID, a.DelegationDepth, string(a.EffectiveArgs), string(tools), string(caps), boolInt(a.AllowRedelegation),
		a.Budget.TokenCount, a.Budget.PaymentAmount, a.Budget.Currency, formatOptionalTime(a.TaskDeadline), a.ExpiresAt.UTC().Format(time.RFC3339Nano),
		a.Status, a.ApproverID, a.Reason, a.CreatedAt.UTC().Format(time.RFC3339Nano), a.UpdatedAt.UTC().Format(time.RFC3339Nano), formatOptionalTime(a.DecidedAt), a.TaskID, a.Version)
	if err == nil {
		if err := s.enqueueApprovalEvents(ctx, tx, a, "created", a.CreatedAt); err != nil {
			return models.DelegationApproval{}, false, err
		}
		if err := tx.Commit(); err != nil {
			return models.DelegationApproval{}, false, err
		}
		return a, true, nil
	}
	_ = tx.Rollback()
	existing, getErr := s.getByRequestID(ctx, a.RequestID)
	if getErr != nil {
		return models.DelegationApproval{}, false, fmt.Errorf("create delegation approval: %w", err)
	}
	if existing.RequestHash != a.RequestHash || existing.DecisionID != a.DecisionID {
		return models.DelegationApproval{}, false, ErrApprovalConflict
	}
	return existing, false, nil
}

func (s *delegationApprovalStore) Get(ctx context.Context, id string) (models.DelegationApproval, error) {
	return queryApproval(ctx, s.db.QueryRowContext(ctx, `SELECT `+approvalColumns+` FROM delegation_approvals WHERE approval_id=?`, id))
}

func (s *delegationApprovalStore) getByRequestID(ctx context.Context, id string) (models.DelegationApproval, error) {
	return queryApproval(ctx, s.db.QueryRowContext(ctx, `SELECT `+approvalColumns+` FROM delegation_approvals WHERE request_id=?`, id))
}

const approvalColumns = `approval_id,request_id,decision_id,request_hash,initiator_agent_id,target_agent_id,session_id,tenant_id,target_workload_id,target_instance_id,
root_task_id,parent_task_id,delegation_depth,effective_args_json,allowed_tools_json,allowed_capabilities_json,
allow_redelegation,budget_token_count,budget_payment_amount,budget_currency,task_deadline,expires_at,status,
approver_id,reason,created_at,updated_at,decided_at,task_id,version`

type rowScanner interface{ Scan(...any) error }

func queryApproval(_ context.Context, row rowScanner) (models.DelegationApproval, error) {
	var a models.DelegationApproval
	var args, tools, caps string
	var redelegate int
	var deadline, decided sql.NullString
	var expires, created, updated string
	err := row.Scan(&a.ApprovalID, &a.RequestID, &a.DecisionID, &a.RequestHash,
		&a.InitiatorAgentID, &a.TargetAgentID, &a.SessionID, &a.TenantID, &a.TargetWorkloadID, &a.TargetInstanceID, &a.RootTaskID, &a.ParentTaskID, &a.DelegationDepth,
		&args, &tools, &caps, &redelegate, &a.Budget.TokenCount, &a.Budget.PaymentAmount, &a.Budget.Currency,
		&deadline, &expires, &a.Status, &a.ApproverID, &a.Reason, &created, &updated, &decided, &a.TaskID, &a.Version)
	if errors.Is(err, sql.ErrNoRows) {
		return a, ErrApprovalNotFound
	}
	if err != nil {
		return a, err
	}
	a.ProtocolVersion = models.CurrentProtocolVersion
	a.EffectiveArgs = json.RawMessage(args)
	if err := json.Unmarshal([]byte(tools), &a.AllowedTools); err != nil {
		return a, err
	}
	if err := json.Unmarshal([]byte(caps), &a.AllowedCapabilities); err != nil {
		return a, err
	}
	a.AllowRedelegation = redelegate != 0
	if a.ExpiresAt, err = time.Parse(time.RFC3339Nano, expires); err != nil {
		return a, err
	}
	if a.CreatedAt, err = time.Parse(time.RFC3339Nano, created); err != nil {
		return a, err
	}
	if a.UpdatedAt, err = time.Parse(time.RFC3339Nano, updated); err != nil {
		return a, err
	}
	a.TaskDeadline = parseOptionalTime(deadline)
	a.DecidedAt = parseOptionalTime(decided)
	return a, nil
}

func (s *delegationApprovalStore) Transition(ctx context.Context, id string, version int64, from, to, principal, reason, actionKey string) (models.DelegationApproval, error) {
	if actionKey == "" {
		return models.DelegationApproval{}, ErrApprovalConflict
	}
	now := time.Now().UTC()
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return models.DelegationApproval{}, err
	}
	defer tx.Rollback()
	a, err := queryApproval(ctx, tx.QueryRowContext(ctx, `SELECT `+approvalColumns+` FROM delegation_approvals WHERE approval_id=?`, id))
	if err != nil {
		return a, err
	}
	actualTo, actualPrincipal, actualReason, actualActionKey := to, principal, reason, actionKey
	if (a.Status == "pending" || a.Status == "approved") && !a.ExpiresAt.After(now) {
		actualTo, actualPrincipal, actualReason, actualActionKey = "expired", "go-kernel", "approval expired", "expire:"+id
		from, version = a.Status, a.Version
	}
	actionHash := from + "\x00" + actualTo + "\x00" + actualPrincipal + "\x00" + actualReason
	decided := any(now.Format(time.RFC3339Nano))
	if actualTo == "approved" {
		decided = nil
	}
	res, err := tx.ExecContext(ctx, `UPDATE delegation_approvals SET status=?,approver_id=?,reason=?,updated_at=?,decided_at=?,version=version+1,action_request_id=?,action_hash=? WHERE approval_id=? AND version=? AND status=?`,
		actualTo, actualPrincipal, actualReason, now.Format(time.RFC3339Nano), decided, actualActionKey, actionHash, id, version, from)
	if err != nil {
		return models.DelegationApproval{}, err
	}
	if n, _ := res.RowsAffected(); n == 0 {
		var storedKey, storedHash string
		_ = tx.QueryRowContext(ctx, `SELECT action_request_id,action_hash FROM delegation_approvals WHERE approval_id=?`, id).Scan(&storedKey, &storedHash)
		if storedKey == actionKey && storedHash == actionHash {
			return a, nil
		}
		return a, ErrApprovalConflict
	}
	updated, err := queryApproval(ctx, tx.QueryRowContext(ctx, `SELECT `+approvalColumns+` FROM delegation_approvals WHERE approval_id=?`, id))
	if err != nil {
		return updated, err
	}
	if err := s.enqueueApprovalEvents(ctx, tx, updated, actualTo, now); err != nil {
		return models.DelegationApproval{}, err
	}
	if err := tx.Commit(); err != nil {
		return models.DelegationApproval{}, err
	}
	return updated, nil
}

func (s *delegationApprovalStore) Consume(ctx context.Context, id string, version int64, task models.Task, entrypoint models.AgentEntrypoint, dispatch models.EntrypointTaskRequest) (models.DelegationApproval, error) {
	now := time.Now().UTC()
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return models.DelegationApproval{}, err
	}
	defer tx.Rollback()
	a, err := queryApproval(ctx, tx.QueryRowContext(ctx, `SELECT `+approvalColumns+` FROM delegation_approvals WHERE approval_id=?`, id))
	if err != nil {
		return a, err
	}
	if a.Status == "consumed" {
		if a.TaskID == task.TaskID {
			return a, nil
		}
		return a, ErrApprovalConflict
	}
	if a.Status != "approved" || a.Version != version {
		return a, ErrApprovalConflict
	}
	if !a.ExpiresAt.After(now) || (task.Deadline != nil && !task.Deadline.After(now)) {
		res, updateErr := tx.ExecContext(ctx, `UPDATE delegation_approvals SET status='expired',approver_id='go-kernel',reason='approval expired',updated_at=?,decided_at=?,version=version+1,action_request_id=?,action_hash=? WHERE approval_id=? AND version=? AND status='approved'`, now.Format(time.RFC3339Nano), now.Format(time.RFC3339Nano), "expire:"+id, "approved\x00expired\x00go-kernel\x00approval expired", id, version)
		if updateErr != nil {
			return a, updateErr
		}
		if n, _ := res.RowsAffected(); n != 1 {
			return a, ErrApprovalConflict
		}
		a.Status = "expired"
		a.ApproverID = "go-kernel"
		a.Reason = "approval expired"
		a.UpdatedAt = now
		a.DecidedAt = &now
		a.Version++
		if err := s.enqueueApprovalEvents(ctx, tx, a, "expired", now); err != nil {
			return a, err
		}
		if err := tx.Commit(); err != nil {
			return a, err
		}
		return a, ErrApprovalExpired
	}
	if task.ParentTaskID != "" {
		res, err := tx.ExecContext(ctx, `UPDATE tasks SET reserved_token_count=reserved_token_count+?,reserved_payment_amount=reserved_payment_amount+? WHERE task_id=? AND target_agent_id=? AND status IN ('pending','accepted','running') AND budget_currency=? AND budget_token_count-reserved_token_count-consumed_token_count>=? AND budget_payment_amount-reserved_payment_amount-consumed_payment_amount>=?`, task.Budget.TokenCount, task.Budget.PaymentAmount, task.ParentTaskID, task.InitiatorAgentID, task.Budget.Currency, task.Budget.TokenCount, task.Budget.PaymentAmount)
		if err != nil {
			return a, err
		}
		if n, _ := res.RowsAffected(); n != 1 {
			return a, ErrBudgetExceeded
		}
	}
	if _, err := insertTask(ctx, tx, task); err != nil {
		return a, err
	}
	payload, err := json.Marshal(task)
	if err != nil {
		return a, err
	}
	event := models.TaskEvent{EventID: "ev-approval-" + id, TaskID: task.TaskID, EventType: "task_created", Payload: payload, PublishedAt: now}
	if err := appendEvent(ctx, tx, &event); err != nil {
		return a, err
	}
	entrypointJSON, err := json.Marshal(entrypoint)
	if err != nil {
		return a, err
	}
	assignmentID := "outbound-assignment-" + task.TaskID
	deliveryID := "delegation-dispatch-" + task.TaskID
	dispatch.TaskID = task.TaskID
	dispatch.DeliveryID = deliveryID
	dispatchJSON, err := json.Marshal(dispatch)
	if err != nil {
		return a, err
	}
	if _, err := tx.ExecContext(ctx, `INSERT INTO task_assignments(assignment_id,assignment_kind,tenant_id,task_id,agent_id,state,revision,attempt,execution_fence,lease_owner,claim_token,lease_expires_at,deadline,delivery_id,idempotency_key,created_at,updated_at) VALUES(?,?,?,?,?,'queued',1,0,0,'','',0,?,?,?,?,?)`, assignmentID, models.AssignmentKindOutboundDelegation, task.TenantID, task.TaskID, task.TargetAgentID, timeText(task.Deadline), deliveryID, deliveryID, formatTime(now), formatTime(now)); err != nil {
		return a, err
	}
	if _, err := tx.ExecContext(ctx, `INSERT INTO delegation_dispatch_outbox(delivery_id,approval_id,task_id,assignment_id,entrypoint_json,payload_json,next_attempt_at) VALUES(?,?,?,?,?,?,?)`, deliveryID, id, task.TaskID, assignmentID, string(entrypointJSON), string(dispatchJSON), formatTime(now)); err != nil {
		return a, err
	}
	if err := insertLifecycleOutbox(ctx, tx, task, "approval_consumed", now); err != nil {
		return a, err
	}
	auditApproval := a
	auditApproval.Status = "consumed"
	auditApproval.TaskID = task.TaskID
	auditApproval.EffectiveArgs = nil
	if err := s.enqueueApprovalEvents(ctx, tx, auditApproval, "consumed", now); err != nil {
		return a, err
	}
	res, err := tx.ExecContext(ctx, `UPDATE delegation_approvals SET status='consumed',task_id=?,updated_at=?,decided_at=?,version=version+1 WHERE approval_id=? AND version=? AND status='approved'`, task.TaskID, now.Format(time.RFC3339Nano), now.Format(time.RFC3339Nano), id, version)
	if err != nil {
		return a, err
	}
	if n, _ := res.RowsAffected(); n != 1 {
		return a, ErrApprovalConflict
	}
	if err := tx.Commit(); err != nil {
		return a, err
	}
	return s.Get(ctx, id)
}

func (s *delegationApprovalStore) ExpireDue(ctx context.Context, now time.Time, limit int) (int64, error) {
	if limit <= 0 {
		limit = 100
	}
	rows, err := s.db.QueryContext(ctx, `SELECT approval_id,version,status FROM delegation_approvals WHERE status IN ('pending','approved') AND expires_at<=? ORDER BY approval_id LIMIT ?`, now.UTC().Format(time.RFC3339Nano), limit)
	if err != nil {
		return 0, err
	}
	type dueApproval struct {
		id, status string
		version    int64
	}
	var due []dueApproval
	for rows.Next() {
		var item dueApproval
		if err := rows.Scan(&item.id, &item.version, &item.status); err != nil {
			rows.Close()
			return 0, err
		}
		due = append(due, item)
	}
	if err := rows.Close(); err != nil {
		return 0, err
	}
	var expired int64
	for _, item := range due {
		if _, err := s.Transition(ctx, item.id, item.version, item.status, "expired", "go-kernel", "approval expired", "expire:"+item.id); err == nil {
			expired++
		} else if !errors.Is(err, ErrApprovalConflict) {
			return expired, err
		}
	}
	return expired, nil
}

func (s *delegationApprovalStore) enqueueApprovalEvents(ctx context.Context, tx *sql.Tx, approval models.DelegationApproval, event string, at time.Time) error {
	if err := insertApprovalAuditOutbox(ctx, tx, approval, event, at); err != nil {
		return err
	}
	if s.notificationsEnabled != nil && s.notificationsEnabled.Load() {
		destination, _ := s.notificationDestination.Load().(string)
		return insertApprovalNotificationOutbox(ctx, tx, approval, event, at, destination)
	}
	return nil
}

func boolInt(v bool) int {
	if v {
		return 1
	}
	return 0
}
func formatOptionalTime(v *time.Time) any {
	if v == nil {
		return nil
	}
	return v.UTC().Format(time.RFC3339Nano)
}
func parseOptionalTime(v sql.NullString) *time.Time {
	if !v.Valid {
		return nil
	}
	t, err := time.Parse(time.RFC3339Nano, v.String)
	if err != nil {
		return nil
	}
	return &t
}
