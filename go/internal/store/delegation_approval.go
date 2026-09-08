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

type delegationApprovalStore struct{ db *sql.DB }

func (db *DB) DelegationApprovalStore() DelegationApprovalStore {
	return &delegationApprovalStore{db: db.DB}
}

func (s *delegationApprovalStore) Create(ctx context.Context, a models.DelegationApproval) (models.DelegationApproval, bool, error) {
	tools, _ := json.Marshal(a.AllowedTools)
	caps, _ := json.Marshal(a.AllowedCapabilities)
	_, err := s.db.ExecContext(ctx, `INSERT INTO delegation_approvals (
		approval_id,request_id,decision_id,request_hash,initiator_agent_id,target_agent_id,session_id,
		root_task_id,parent_task_id,delegation_depth,effective_args_json,allowed_tools_json,allowed_capabilities_json,
		allow_redelegation,budget_token_count,budget_payment_amount,budget_currency,task_deadline,expires_at,status,
		approver_id,reason,created_at,updated_at,decided_at,task_id,version)
		VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
		a.ApprovalID, a.RequestID, a.DecisionID, a.RequestHash, a.InitiatorAgentID, a.TargetAgentID, a.SessionID,
		a.RootTaskID, a.ParentTaskID, a.DelegationDepth, string(a.EffectiveArgs), string(tools), string(caps), boolInt(a.AllowRedelegation),
		a.Budget.TokenCount, a.Budget.PaymentAmount, a.Budget.Currency, formatOptionalTime(a.TaskDeadline), a.ExpiresAt.UTC().Format(time.RFC3339Nano),
		a.Status, a.ApproverID, a.Reason, a.CreatedAt.UTC().Format(time.RFC3339Nano), a.UpdatedAt.UTC().Format(time.RFC3339Nano), formatOptionalTime(a.DecidedAt), a.TaskID, a.Version)
	if err == nil {
		if auditErr := insertApprovalAuditOutbox(ctx, s.db, a, "created", a.CreatedAt); auditErr != nil {
			_, _ = s.db.ExecContext(ctx, `DELETE FROM delegation_approvals WHERE approval_id=?`, a.ApprovalID)
			return models.DelegationApproval{}, false, auditErr
		}
		return a, true, nil
	}
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

const approvalColumns = `approval_id,request_id,decision_id,request_hash,initiator_agent_id,target_agent_id,session_id,
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
		&a.InitiatorAgentID, &a.TargetAgentID, &a.SessionID, &a.RootTaskID, &a.ParentTaskID, &a.DelegationDepth,
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
	a, err := s.Get(ctx, id)
	if err != nil {
		return a, err
	}
	if actionKey == "" {
		return a, ErrApprovalConflict
	}
	now := time.Now().UTC()
	if (a.Status == "pending" || a.Status == "approved") && !a.ExpiresAt.After(now) {
		_, _ = s.db.ExecContext(ctx, `UPDATE delegation_approvals SET status='expired',updated_at=?,decided_at=?,version=version+1 WHERE approval_id=? AND version=? AND status IN ('pending','approved')`, now.Format(time.RFC3339Nano), now.Format(time.RFC3339Nano), id, a.Version)
		return s.Get(ctx, id)
	}
	actionHash := from + "\x00" + to + "\x00" + principal + "\x00" + reason
	decided := any(now.Format(time.RFC3339Nano))
	if to == "approved" {
		decided = nil
	}
	res, err := s.db.ExecContext(ctx, `UPDATE delegation_approvals SET status=?,approver_id=?,reason=?,updated_at=?,decided_at=?,version=version+1,action_request_id=?,action_hash=? WHERE approval_id=? AND version=? AND status=?`,
		to, principal, reason, now.Format(time.RFC3339Nano), decided, actionKey, actionHash, id, version, from)
	if err != nil {
		return models.DelegationApproval{}, err
	}
	n, _ := res.RowsAffected()
	if n == 0 {
		current, getErr := s.Get(ctx, id)
		if getErr != nil {
			return current, getErr
		}
		var storedKey, storedHash string
		_ = s.db.QueryRowContext(ctx, `SELECT action_request_id,action_hash FROM delegation_approvals WHERE approval_id=?`, id).Scan(&storedKey, &storedHash)
		if actionKey != "" && storedKey == actionKey && storedHash == actionHash {
			return current, nil
		}
		return current, ErrApprovalConflict
	}
	updated, err := s.Get(ctx, id)
	if err != nil {
		return updated, err
	}
	if err := insertApprovalAuditOutbox(ctx, s.db, updated, to, now); err != nil {
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
	eventID := "ev-approval-" + id
	if _, err := tx.ExecContext(ctx, `INSERT INTO events(event_id,task_id,event_type,payload_json,published_at,published) VALUES(?,?,?,?,?,0)`, eventID, task.TaskID, "task_created", string(payload), now.Format(time.RFC3339Nano)); err != nil {
		return a, err
	}
	entrypointJSON, _ := json.Marshal(entrypoint)
	dispatchJSON, err := json.Marshal(dispatch)
	if err != nil {
		return a, err
	}
	deliveryID := "delegation-dispatch:" + id
	if _, err := tx.ExecContext(ctx, `INSERT INTO delegation_dispatch_outbox(delivery_id,approval_id,task_id,entrypoint_json,payload_json,next_attempt_at) VALUES(?,?,?,?,?,?)`, deliveryID, id, task.TaskID, string(entrypointJSON), string(dispatchJSON), now.Format(time.RFC3339Nano)); err != nil {
		return a, err
	}
	if err := insertLifecycleOutbox(ctx, tx, task, "approval_consumed", now); err != nil {
		return a, err
	}
	auditApproval := a
	auditApproval.Status = "consumed"
	auditApproval.TaskID = task.TaskID
	auditApproval.EffectiveArgs = nil
	if err := insertApprovalAuditOutbox(ctx, tx, auditApproval, "consumed", now); err != nil {
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
