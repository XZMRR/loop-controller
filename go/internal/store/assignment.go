package store

import (
	"context"
	"crypto/rand"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"time"

	"github.com/loop-controller/go/internal/models"
)

var (
	ErrAssignmentNotFound          = errors.New("assignment not found")
	ErrAssignmentExists            = errors.New("assignment exists")
	ErrAssignmentConflict          = errors.New("assignment conflict")
	ErrAssignmentClaimLost         = errors.New("assignment claim lost")
	ErrAssignmentLeaseExpired      = errors.New("assignment lease expired")
	ErrAssignmentTerminal          = errors.New("assignment terminal")
	ErrAssignmentInvalidTransition = errors.New("invalid assignment transition")
	ErrAssignmentResultConflict    = errors.New("assignment result conflict")
	ErrAssignmentRetryForbidden    = errors.New("assignment retry forbidden")
	ErrAssignmentDeadLetterReplay  = errors.New("assignment dead-letter replay conflict")
	errAssignmentAttemptUpdate     = errors.New("assignment attempt update affected unexpected row count")
)

type ClaimDueParams struct {
	Kind  models.AssignmentKind
	Now   time.Time
	Lease time.Duration
	Limit int
	Owner string
}

type AssignmentClaim struct {
	Kind         models.AssignmentKind
	AssignmentID string
	Revision     int64
	Attempt      int64
	Fence        int64
	Owner        string
	ClaimToken   string
	Now          time.Time
	Lease        time.Duration
}

type AssignmentTransition struct {
	AssignmentID string
	Revision     int64
	Attempt      int64
	Fence        int64
	Owner        string
	ClaimToken   string
	Now          time.Time
	Kind         models.AssignmentKind
}

type RetryDecision struct {
	AssignmentTransition
	FailureClass     models.FailureClass
	ErrorCode        string
	ConfirmedNotSent bool
	RetryAfter       time.Duration
	JitterValue      float64
}

type DeadLetterReplayParams struct {
	TenantID         string
	AssignmentID     string
	ExpectedRevision int64
	NotBefore        *time.Time
	Actor            string
	CorrelationID    string
	Now              time.Time
}

type AssignmentResult struct {
	AssignmentTransition
	Status           string
	FailureClass     models.FailureClass
	Outcome          json.RawMessage
	ErrorCode        string
	ConsumedBudget   models.DelegationBudget
	ExecutionReceipt json.RawMessage
}

type CancelAssignmentParams struct {
	Kind             models.AssignmentKind
	AssignmentID     string
	ExpectedRevision int64
	ExpectedAttempt  int64
	ExpectedFence    int64
	Now              time.Time
}

type AssignmentStore interface {
	Create(context.Context, models.TaskAssignment) (models.TaskAssignment, error)
	Get(context.Context, string) (models.TaskAssignment, error)
	GetForTenant(context.Context, string, string) (models.TaskAssignment, error)
	ListTaskAssignments(context.Context, string, string) ([]models.TaskAssignment, error)
	ListAttempts(context.Context, string) ([]models.ExecutionAttempt, error)
	ListAttemptsForTenant(context.Context, string, string) ([]models.ExecutionAttempt, error)
	ListDeadLetters(context.Context, string, int) ([]models.TaskAssignment, error)
	ReplayDeadLetter(context.Context, DeadLetterReplayParams) (models.TaskAssignment, error)
	ClaimDue(context.Context, ClaimDueParams) ([]models.TaskAssignment, error)
	Renew(context.Context, AssignmentClaim) (models.TaskAssignment, error)
	MarkDispatched(context.Context, AssignmentTransition) (models.TaskAssignment, error)
	MarkExecuting(context.Context, AssignmentTransition) (models.TaskAssignment, error)
	CommitResult(context.Context, AssignmentResult) (models.TaskAssignment, error)
	ApplyRetryDecision(context.Context, RetryDecision) (models.TaskAssignment, error)
	ExpireDueDeadLetters(context.Context, models.AssignmentKind, time.Time, int) (int64, error)
	RecoverExpiredActive(context.Context, models.AssignmentKind, time.Time, int) (int64, error)
	RequestCancel(context.Context, CancelAssignmentParams) (models.TaskAssignment, error)
}

type assignmentStore struct{ db *sql.DB }

func validAssignmentKind(kind models.AssignmentKind) bool {
	return kind == models.AssignmentKindTargetExecution || kind == models.AssignmentKindOutboundDelegation
}

func (s *assignmentStore) Create(ctx context.Context, a models.TaskAssignment) (models.TaskAssignment, error) {
	if a.AssignmentID == "" || a.TaskID == "" || a.DeliveryID == "" || !validAssignmentKind(a.Kind) {
		return models.TaskAssignment{}, fmt.Errorf("assignment_id, task_id and delivery_id are required")
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return models.TaskAssignment{}, fmt.Errorf("begin assignment creation: %w", err)
	}
	defer tx.Rollback()
	var taskTenant string
	if err := tx.QueryRowContext(ctx, `SELECT tenant_id FROM tasks WHERE task_id=?`, a.TaskID).Scan(&taskTenant); errors.Is(err, sql.ErrNoRows) {
		return models.TaskAssignment{}, ErrAssignmentNotFound
	} else if err != nil {
		return models.TaskAssignment{}, fmt.Errorf("read assignment task: %w", err)
	}
	if taskTenant != a.TenantID {
		return models.TaskAssignment{}, ErrAssignmentConflict
	}

	a.State, a.Revision, a.Attempt, a.ExecutionFence = models.AssignmentStateQueued, 1, 0, 0
	a.LeaseOwner, a.ClaimToken, a.LeaseExpiresAt = "", "", nil
	a.FailureClass, a.ResultStatus, a.Outcome, a.ErrorCode, a.ExecutionReceipt = "", "", nil, "", nil
	a.ConsumedBudget = models.DelegationBudget{}
	policy := normalizedRetryPolicy(a.RetryPolicy, a.Deadline)
	a.RetryPolicy = &policy
	policyJSON, err := json.Marshal(policy)
	if err != nil {
		return models.TaskAssignment{}, ErrAssignmentConflict
	}
	if a.CreatedAt.IsZero() {
		a.CreatedAt = time.Now().UTC()
	}
	a.CreatedAt = a.CreatedAt.UTC()
	a.UpdatedAt = a.CreatedAt
	_, err = tx.ExecContext(ctx, `INSERT INTO task_assignments
		(assignment_id,assignment_kind,tenant_id,task_id,agent_id,state,revision,attempt,execution_fence,lease_owner,claim_token,lease_expires_at,not_before,deadline,delivery_id,idempotency_key,retry_policy_json,replay_count,failure_class,result_status,outcome_json,error_code,consumed_token_count,consumed_payment_amount,consumed_currency,execution_receipt_json,created_at,updated_at)
		VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
		a.AssignmentID, a.Kind, a.TenantID, a.TaskID, a.AgentID, a.State, a.Revision, a.Attempt, a.ExecutionFence, "", "", 0, timeText(a.NotBefore), timeText(a.Deadline), a.DeliveryID, a.IdempotencyKey, string(policyJSON), 0, "", "", nil, "", 0, 0, "", nil, formatTime(a.CreatedAt), formatTime(a.UpdatedAt))
	if err != nil {
		if a.IdempotencyKey != "" {
			existing, getErr := getAssignment(ctx, tx, `tenant_id=? AND idempotency_key=?`, a.TenantID, a.IdempotencyKey)
			if getErr == nil {
				if sameCreation(existing, a) {
					return existing, nil
				}
				return models.TaskAssignment{}, ErrAssignmentConflict
			}
		}
		if existing, getErr := getAssignment(ctx, tx, `assignment_id=?`, a.AssignmentID); getErr == nil {
			if sameCreation(existing, a) {
				return existing, nil
			}
			return models.TaskAssignment{}, ErrAssignmentExists
		}
		var existingID string
		if getErr := tx.QueryRowContext(ctx, `SELECT assignment_id FROM task_assignments WHERE task_id=? OR delivery_id=? LIMIT 1`, a.TaskID, a.DeliveryID).Scan(&existingID); getErr == nil {
			return models.TaskAssignment{}, ErrAssignmentExists
		}
		return models.TaskAssignment{}, fmt.Errorf("insert assignment: %w", err)
	}
	if err := tx.Commit(); err != nil {
		return models.TaskAssignment{}, fmt.Errorf("commit assignment creation: %w", err)
	}
	return a, nil
}

func (s *assignmentStore) Get(ctx context.Context, id string) (models.TaskAssignment, error) {
	return getAssignment(ctx, s.db, `assignment_id=?`, id)
}

func (s *assignmentStore) GetForTenant(ctx context.Context, tenant, id string) (models.TaskAssignment, error) {
	return getAssignment(ctx, s.db, `tenant_id=? AND assignment_id=?`, tenant, id)
}

func (s *assignmentStore) ListTaskAssignments(ctx context.Context, tenant, taskID string) ([]models.TaskAssignment, error) {
	rows, err := s.db.QueryContext(ctx, `SELECT assignment_id FROM task_assignments WHERE tenant_id=? AND task_id=? ORDER BY route_attempt,created_at`, tenant, taskID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var ids []string
	for rows.Next() {
		var id string
		if err = rows.Scan(&id); err != nil {
			return nil, err
		}
		ids = append(ids, id)
	}
	out := make([]models.TaskAssignment, 0, len(ids))
	for _, id := range ids {
		a, e := getAssignment(ctx, s.db, `assignment_id=?`, id)
		if e != nil {
			return nil, e
		}
		out = append(out, a)
	}
	return out, rows.Err()
}

func (s *assignmentStore) ListDeadLetters(ctx context.Context, tenant string, limit int) ([]models.TaskAssignment, error) {
	if tenant == "" {
		return nil, ErrAssignmentConflict
	}
	if limit <= 0 || limit > 1000 {
		limit = 100
	}
	rows, err := s.db.QueryContext(ctx, `SELECT assignment_id FROM task_assignments WHERE tenant_id=? AND state='dead_letter' ORDER BY updated_at,assignment_id LIMIT ?`, tenant, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var ids []string
	for rows.Next() {
		var id string
		if err = rows.Scan(&id); err != nil {
			return nil, err
		}
		ids = append(ids, id)
	}
	if err = rows.Err(); err != nil {
		return nil, err
	}
	out := make([]models.TaskAssignment, 0, len(ids))
	for _, id := range ids {
		a, loadErr := getAssignment(ctx, s.db, `tenant_id=? AND assignment_id=?`, tenant, id)
		if loadErr != nil {
			return nil, loadErr
		}
		out = append(out, a)
	}
	return out, nil
}

func (s *assignmentStore) ReplayDeadLetter(ctx context.Context, p DeadLetterReplayParams) (models.TaskAssignment, error) {
	if p.TenantID == "" || p.AssignmentID == "" || p.ExpectedRevision <= 0 {
		return models.TaskAssignment{}, ErrAssignmentDeadLetterReplay
	}
	now := p.Now.UTC()
	if now.IsZero() {
		now = time.Now().UTC()
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return models.TaskAssignment{}, err
	}
	defer tx.Rollback()
	current, err := getAssignment(ctx, tx, `tenant_id=? AND assignment_id=?`, p.TenantID, p.AssignmentID)
	if err != nil {
		return current, err
	}
	if current.State != models.AssignmentStateDeadLetter || current.Revision != p.ExpectedRevision || current.ExecutionFence == math.MaxInt64 {
		return current, ErrAssignmentDeadLetterReplay
	}
	policy := normalizedRetryPolicy(current.RetryPolicy, current.Deadline)
	deadline := current.Deadline
	if policy.Deadline != nil && (deadline == nil || policy.Deadline.Before(*deadline)) {
		deadline = policy.Deadline
	}
	if current.Attempt >= int64(policy.MaxAttempts) || deadline != nil && !deadline.After(now) {
		return current, ErrAssignmentDeadLetterReplay
	}
	if p.NotBefore != nil && deadline != nil && !p.NotBefore.Before(*deadline) {
		return current, ErrAssignmentDeadLetterReplay
	}
	if admitted, budgetErr := retryTaskBudgetAdmitsTx(ctx, tx, current.TaskID, policy.AttemptBudgetLimit); budgetErr != nil {
		return current, budgetErr
	} else if !admitted {
		return current, ErrAssignmentDeadLetterReplay
	}
	res, err := tx.ExecContext(ctx, `UPDATE task_assignments SET state='queued',revision=revision+1,execution_fence=execution_fence+1,replay_count=replay_count+1,not_before=?,failure_class='',result_status='',error_code='',lease_owner='',claim_token='',lease_expires_at=0,updated_at=? WHERE tenant_id=? AND assignment_id=? AND state='dead_letter' AND revision=?`, timeText(p.NotBefore), formatTime(now), p.TenantID, p.AssignmentID, p.ExpectedRevision)
	if err != nil || requireOneRow(res) != nil {
		return current, ErrAssignmentDeadLetterReplay
	}
	if _, err = tx.ExecContext(ctx, `INSERT INTO assignment_retry_events(tenant_id,assignment_id,event_type,expected_revision,not_before,actor,action,result,correlation_id,created_at) VALUES(?,?,'replayed',?,?,?,'replay','success',?,?)`, p.TenantID, p.AssignmentID, p.ExpectedRevision, timeText(p.NotBefore), p.Actor, p.CorrelationID, formatTime(now)); err != nil {
		return current, err
	}
	res, err = tx.ExecContext(ctx, `UPDATE tasks SET status='running',error_code='',completed_at=NULL,updated_at=? WHERE task_id=? AND tenant_id=? AND status='failed' AND error_code='dead_lettered'`, formatTime(now), current.TaskID, p.TenantID)
	if err != nil || requireOneRow(res) != nil {
		return current, ErrAssignmentDeadLetterReplay
	}
	var dagID string
	dagErr := tx.QueryRowContext(ctx, `SELECT dag_id FROM task_graph_nodes WHERE task_id=?`, current.TaskID).Scan(&dagID)
	if dagErr == nil {
		res, err = tx.ExecContext(ctx, `UPDATE task_graph_nodes SET status='running',revision=revision+1,updated_at=?,terminal_at=NULL WHERE task_id=? AND status='failed'`, formatTime(now), current.TaskID)
		if err != nil || requireOneRow(res) != nil {
			return current, ErrAssignmentDeadLetterReplay
		}
		res, err = tx.ExecContext(ctx, `UPDATE task_graphs SET status='running',reserved_token_count=reserved_token_count+(SELECT budget_token_count FROM task_graph_nodes WHERE task_id=?),reserved_payment_amount=reserved_payment_amount+(SELECT budget_payment_amount FROM task_graph_nodes WHERE task_id=?),revision=revision+1,updated_at=?,terminal_at=NULL WHERE dag_id=? AND status='failed' AND reserved_token_count+(SELECT budget_token_count FROM task_graph_nodes WHERE task_id=?)<=budget_token_count-consumed_token_count AND reserved_payment_amount+(SELECT budget_payment_amount FROM task_graph_nodes WHERE task_id=?)<=budget_payment_amount-consumed_payment_amount`, current.TaskID, current.TaskID, formatTime(now), dagID, current.TaskID, current.TaskID)
		if err != nil || requireOneRow(res) != nil {
			return current, ErrAssignmentDeadLetterReplay
		}
	} else if !errors.Is(dagErr, sql.ErrNoRows) {
		return current, dagErr
	}
	current, err = getAssignment(ctx, tx, `assignment_id=?`, p.AssignmentID)
	if err == nil {
		err = tx.Commit()
	}
	return current, err
}

func (s *assignmentStore) ListAttempts(ctx context.Context, id string) ([]models.ExecutionAttempt, error) {
	if _, err := s.Get(ctx, id); err != nil {
		return nil, err
	}
	return s.listAttempts(ctx, `WHERE e.assignment_id=?`, id)
}

func (s *assignmentStore) ListAttemptsForTenant(ctx context.Context, tenant, id string) ([]models.ExecutionAttempt, error) {
	if _, err := s.GetForTenant(ctx, tenant, id); err != nil {
		return nil, err
	}
	return s.listAttempts(ctx, `JOIN task_assignments a ON a.assignment_id=e.assignment_id AND a.tenant_id=e.tenant_id AND a.task_id=e.task_id WHERE a.tenant_id=? AND a.assignment_id=?`, tenant, id)
}

func (s *assignmentStore) listAttempts(ctx context.Context, scope string, args ...any) ([]models.ExecutionAttempt, error) {
	rows, err := s.db.QueryContext(ctx, `SELECT e.assignment_id,e.tenant_id,e.task_id,e.agent_id,e.attempt,e.execution_fence,e.state,e.lease_owner,e.claim_token,e.lease_expires_at,e.failure_class,e.result_status,e.outcome_json,e.error_code,e.consumed_token_count,e.consumed_payment_amount,e.consumed_currency,e.execution_receipt_json,e.started_at,e.finished_at FROM execution_attempts e `+scope+` ORDER BY e.attempt`, args...)
	if err != nil {
		return nil, fmt.Errorf("list attempts: %w", err)
	}
	defer rows.Close()
	var out []models.ExecutionAttempt
	for rows.Next() {
		var a models.ExecutionAttempt
		if err := scanAttempt(rows, &a); err != nil {
			return nil, err
		}
		out = append(out, a)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("list attempts: %w", err)
	}
	return out, nil
}

func (s *assignmentStore) ClaimDue(ctx context.Context, p ClaimDueParams) ([]models.TaskAssignment, error) {
	if !validAssignmentKind(p.Kind) {
		return nil, ErrAssignmentConflict
	}
	if p.Limit <= 0 || p.Owner == "" || p.Lease <= 0 {
		return []models.TaskAssignment{}, nil
	}
	now := p.Now.UTC()
	if now.IsZero() {
		now = time.Now().UTC()
	}
	rows, err := s.db.QueryContext(ctx, `SELECT assignment_id,revision,attempt,execution_fence,state FROM task_assignments WHERE assignment_kind=? AND (deadline IS NULL OR deadline>?) AND ((state='queued' AND (not_before IS NULL OR not_before<=?)) OR (state='retry_wait' AND not_before IS NOT NULL AND not_before<=?) OR (state='claimed' AND lease_expires_at<=?)) ORDER BY COALESCE(not_before,created_at),assignment_id LIMIT ?`, p.Kind, formatTime(now), formatTime(now), formatTime(now), now.UnixNano(), p.Limit)
	if err != nil {
		return nil, fmt.Errorf("find due assignments: %w", err)
	}
	type candidate struct {
		id                       string
		revision, attempt, fence int64
		state                    models.AssignmentState
	}
	var candidates []candidate
	for rows.Next() {
		var c candidate
		if err := rows.Scan(&c.id, &c.revision, &c.attempt, &c.fence, &c.state); err != nil {
			rows.Close()
			return nil, err
		}
		candidates = append(candidates, c)
	}
	if err := rows.Close(); err != nil {
		return nil, err
	}
	out := make([]models.TaskAssignment, 0, len(candidates))
	for _, c := range candidates {
		if c.revision == math.MaxInt64 || c.attempt == math.MaxInt64 || c.fence == math.MaxInt64 {
			continue
		}
		token, err := newClaimToken()
		if err != nil {
			return nil, err
		}
		tx, err := s.db.BeginTx(ctx, nil)
		if err != nil {
			return nil, err
		}
		lease := now.Add(p.Lease)
		res, err := tx.ExecContext(ctx, `UPDATE task_assignments SET state='claimed',revision=revision+1,attempt=attempt+1,execution_fence=execution_fence+1,lease_owner=?,claim_token=?,lease_expires_at=?,updated_at=? WHERE assignment_id=? AND assignment_kind=? AND revision=? AND attempt=? AND execution_fence=? AND (deadline IS NULL OR deadline>?) AND ((state='queued' AND (not_before IS NULL OR not_before<=?)) OR (state='retry_wait' AND not_before IS NOT NULL AND not_before<=?) OR (state='claimed' AND lease_expires_at<=?))`, p.Owner, token, lease.UnixNano(), formatTime(now), c.id, p.Kind, c.revision, c.attempt, c.fence, formatTime(now), formatTime(now), formatTime(now), now.UnixNano())
		if err != nil {
			tx.Rollback()
			return nil, fmt.Errorf("claim assignment: %w", err)
		}
		n, rowsErr := res.RowsAffected()
		if rowsErr != nil {
			tx.Rollback()
			return nil, rowsErr
		}
		if n == 0 {
			tx.Rollback()
			continue
		}
		if n != 1 {
			tx.Rollback()
			return nil, errAssignmentAttemptUpdate
		}
		if c.state == models.AssignmentStateClaimed && c.attempt > 0 {
			attemptRes, updateErr := tx.ExecContext(ctx, `UPDATE execution_attempts SET state='superseded',finished_at=? WHERE assignment_id=? AND attempt=? AND state='claimed'`, formatTime(now), c.id, c.attempt)
			if updateErr != nil {
				tx.Rollback()
				return nil, updateErr
			}
			if err = requireOneRow(attemptRes); err != nil {
				tx.Rollback()
				return nil, err
			}
		}
		claimed, err := getAssignment(ctx, tx, `assignment_id=?`, c.id)
		if err != nil {
			tx.Rollback()
			return nil, err
		}
		_, err = tx.ExecContext(ctx, `INSERT INTO execution_attempts (assignment_id,tenant_id,task_id,agent_id,attempt,execution_fence,state,lease_owner,claim_token,lease_expires_at,started_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)`, claimed.AssignmentID, claimed.TenantID, claimed.TaskID, claimed.AgentID, claimed.Attempt, claimed.ExecutionFence, claimed.State, claimed.LeaseOwner, claimed.ClaimToken, lease.UnixNano(), formatTime(now))
		if err != nil {
			tx.Rollback()
			return nil, fmt.Errorf("insert execution attempt: %w", err)
		}
		if err = tx.Commit(); err != nil {
			return nil, fmt.Errorf("commit assignment claim: %w", err)
		}
		out = append(out, claimed)
	}
	return out, nil
}

func (s *assignmentStore) Renew(ctx context.Context, p AssignmentClaim) (models.TaskAssignment, error) {
	if !validAssignmentKind(p.Kind) {
		return models.TaskAssignment{}, ErrAssignmentConflict
	}
	if p.Lease <= 0 || p.Revision == math.MaxInt64 {
		return models.TaskAssignment{}, ErrAssignmentConflict
	}
	now := p.Now.UTC()
	if now.IsZero() {
		now = time.Now().UTC()
	}
	newLease := now.Add(p.Lease).UTC()
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return models.TaskAssignment{}, err
	}
	defer tx.Rollback()
	res, err := tx.ExecContext(ctx, `UPDATE task_assignments SET revision=revision+1,lease_expires_at=?,updated_at=? WHERE assignment_id=? AND assignment_kind=? AND revision=? AND attempt=? AND execution_fence=? AND lease_owner=? AND claim_token=? AND lease_expires_at>? AND lease_expires_at<? AND state IN ('claimed','dispatched','executing')`, newLease.UnixNano(), formatTime(now), p.AssignmentID, p.Kind, p.Revision, p.Attempt, p.Fence, p.Owner, p.ClaimToken, now.UnixNano(), newLease.UnixNano())
	if err != nil {
		return models.TaskAssignment{}, err
	}
	n, rowsErr := res.RowsAffected()
	if rowsErr != nil {
		return models.TaskAssignment{}, rowsErr
	}
	if n == 0 {
		return models.TaskAssignment{}, classifyRenew(ctx, tx, p.AssignmentTransition(), now, newLease)
	}
	if n != 1 {
		return models.TaskAssignment{}, errAssignmentAttemptUpdate
	}
	attemptRes, err := tx.ExecContext(ctx, `UPDATE execution_attempts SET lease_expires_at=? WHERE assignment_id=? AND attempt=? AND execution_fence=?`, newLease.UnixNano(), p.AssignmentID, p.Attempt, p.Fence)
	if err != nil {
		return models.TaskAssignment{}, err
	}
	if err = requireOneRow(attemptRes); err != nil {
		return models.TaskAssignment{}, err
	}
	a, err := getAssignment(ctx, tx, `assignment_id=?`, p.AssignmentID)
	if err != nil {
		return a, err
	}
	if err = tx.Commit(); err != nil {
		return a, err
	}
	return a, nil
}

func (p AssignmentClaim) AssignmentTransition() AssignmentTransition {
	return AssignmentTransition{Kind: p.Kind, AssignmentID: p.AssignmentID, Revision: p.Revision, Attempt: p.Attempt, Fence: p.Fence, Owner: p.Owner, ClaimToken: p.ClaimToken, Now: p.Now}
}
func (s *assignmentStore) MarkDispatched(ctx context.Context, p AssignmentTransition) (models.TaskAssignment, error) {
	return s.guardUpdate(ctx, p, models.AssignmentStateClaimed, models.AssignmentStateDispatched, time.Time{}, false)
}
func (s *assignmentStore) MarkExecuting(ctx context.Context, p AssignmentTransition) (models.TaskAssignment, error) {
	return s.guardUpdate(ctx, p, models.AssignmentStateDispatched, models.AssignmentStateExecuting, time.Time{}, false)
}

func (s *assignmentStore) guardUpdate(ctx context.Context, p AssignmentTransition, from, to models.AssignmentState, newLease time.Time, clear bool) (models.TaskAssignment, error) {
	if !validAssignmentKind(p.Kind) {
		return models.TaskAssignment{}, ErrAssignmentConflict
	}
	now := p.Now.UTC()
	if now.IsZero() {
		now = time.Now().UTC()
	}
	if p.Revision == math.MaxInt64 {
		return models.TaskAssignment{}, ErrAssignmentConflict
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return models.TaskAssignment{}, err
	}
	defer tx.Rollback()
	leaseExpr := "lease_expires_at"
	leaseArg := any(nil)
	if !newLease.IsZero() {
		leaseExpr = "?"
		leaseArg = newLease.UTC().UnixNano()
	}
	if clear {
		leaseExpr = "0"
	}
	query := `UPDATE task_assignments SET state=?,revision=revision+1,updated_at=?,lease_expires_at=` + leaseExpr
	args := []any{to, formatTime(now)}
	if leaseExpr == "?" {
		args = append(args, leaseArg)
	}
	if clear {
		query += `,lease_owner='',claim_token=''`
	}
	query += ` WHERE assignment_id=? AND assignment_kind=? AND revision=? AND attempt=? AND execution_fence=? AND lease_owner=? AND claim_token=? AND lease_expires_at>? AND state=?`
	args = append(args, p.AssignmentID, p.Kind, p.Revision, p.Attempt, p.Fence, p.Owner, p.ClaimToken, now.UnixNano(), from)
	res, err := tx.ExecContext(ctx, query, args...)
	if err != nil {
		return models.TaskAssignment{}, err
	}
	n, rowsErr := res.RowsAffected()
	if rowsErr != nil {
		return models.TaskAssignment{}, rowsErr
	}
	if n == 0 {
		return models.TaskAssignment{}, classifyGuard(ctx, tx, p, from, now)
	}
	if n != 1 {
		return models.TaskAssignment{}, errAssignmentAttemptUpdate
	}
	attemptQuery := `UPDATE execution_attempts SET state=?,lease_expires_at=` + leaseExpr + ` WHERE assignment_id=? AND attempt=? AND execution_fence=?`
	aargs := []any{to}
	if leaseExpr == "?" {
		aargs = append(aargs, leaseArg)
	}
	aargs = append(aargs, p.AssignmentID, p.Attempt, p.Fence)
	attemptRes, err := tx.ExecContext(ctx, attemptQuery, aargs...)
	if err != nil {
		return models.TaskAssignment{}, err
	}
	if err = requireOneRow(attemptRes); err != nil {
		return models.TaskAssignment{}, err
	}
	a, err := getAssignment(ctx, tx, `assignment_id=?`, p.AssignmentID)
	if err != nil {
		return a, err
	}
	if err = tx.Commit(); err != nil {
		return a, err
	}
	return a, nil
}

func (s *assignmentStore) CommitResult(ctx context.Context, p AssignmentResult) (models.TaskAssignment, error) {
	if !validAssignmentKind(p.Kind) {
		return models.TaskAssignment{}, ErrAssignmentConflict
	}
	switch p.Status {
	case "completed", "failed", "outcome_unknown", "cancelled":
	default:
		return models.TaskAssignment{}, ErrAssignmentConflict
	}
	now := p.Now.UTC()
	if now.IsZero() {
		now = time.Now().UTC()
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return models.TaskAssignment{}, err
	}
	defer tx.Rollback()
	current, err := getAssignment(ctx, tx, `assignment_id=?`, p.AssignmentID)
	if err != nil {
		return current, err
	}
	if current.Kind != p.Kind {
		return current, ErrAssignmentClaimLost
	}
	if current.State == models.AssignmentStateSettled {
		if current.Attempt != p.Attempt || current.ExecutionFence != p.Fence {
			return current, ErrAssignmentClaimLost
		}
		var owner, token string
		if err := tx.QueryRowContext(ctx, `SELECT lease_owner,claim_token FROM execution_attempts WHERE assignment_id=? AND attempt=? AND execution_fence=?`, p.AssignmentID, p.Attempt, p.Fence).Scan(&owner, &token); err != nil {
			return current, ErrAssignmentClaimLost
		}
		if owner != p.Owner || token != p.ClaimToken {
			return current, ErrAssignmentClaimLost
		}
		if sameResult(current, p) {
			return current, nil
		}
		return current, ErrAssignmentResultConflict
	}
	if p.Revision == math.MaxInt64 {
		return current, ErrAssignmentConflict
	}
	res, err := tx.ExecContext(ctx, `UPDATE task_assignments SET state='settled',revision=revision+1,failure_class=?,result_status=?,outcome_json=?,error_code=?,consumed_token_count=?,consumed_payment_amount=?,consumed_currency=?,execution_receipt_json=?,lease_owner='',claim_token='',lease_expires_at=0,updated_at=? WHERE assignment_id=? AND assignment_kind=? AND revision=? AND attempt=? AND execution_fence=? AND lease_owner=? AND claim_token=? AND lease_expires_at>? AND state='executing'`, p.FailureClass, p.Status, rawNull(p.Outcome), p.ErrorCode, p.ConsumedBudget.TokenCount, p.ConsumedBudget.PaymentAmount, p.ConsumedBudget.Currency, rawNull(p.ExecutionReceipt), formatTime(now), p.AssignmentID, p.Kind, p.Revision, p.Attempt, p.Fence, p.Owner, p.ClaimToken, now.UnixNano())
	if err != nil {
		return current, err
	}
	n, rowsErr := res.RowsAffected()
	if rowsErr != nil {
		return current, rowsErr
	}
	if n == 0 {
		return current, classifyGuard(ctx, tx, p.AssignmentTransition, models.AssignmentStateExecuting, now)
	}
	if n != 1 {
		return current, errAssignmentAttemptUpdate
	}
	attemptRes, err := tx.ExecContext(ctx, `UPDATE execution_attempts SET state='settled',failure_class=?,result_status=?,outcome_json=?,error_code=?,consumed_token_count=?,consumed_payment_amount=?,consumed_currency=?,execution_receipt_json=?,finished_at=? WHERE assignment_id=? AND attempt=? AND execution_fence=?`, p.FailureClass, p.Status, rawNull(p.Outcome), p.ErrorCode, p.ConsumedBudget.TokenCount, p.ConsumedBudget.PaymentAmount, p.ConsumedBudget.Currency, rawNull(p.ExecutionReceipt), formatTime(now), p.AssignmentID, p.Attempt, p.Fence)
	if err != nil {
		return current, err
	}
	if err = requireOneRow(attemptRes); err != nil {
		return current, err
	}
	current, err = getAssignment(ctx, tx, `assignment_id=?`, p.AssignmentID)
	if err != nil {
		return current, err
	}
	if err = tx.Commit(); err != nil {
		return current, err
	}
	return current, nil
}

func (s *assignmentStore) ApplyRetryDecision(ctx context.Context, p RetryDecision) (models.TaskAssignment, error) {
	if p.FailureClass != models.FailureClassPreDispatchTransient || !p.ConfirmedNotSent {
		return models.TaskAssignment{}, ErrAssignmentRetryForbidden
	}
	now := p.Now.UTC()
	if now.IsZero() {
		now = time.Now().UTC()
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return models.TaskAssignment{}, err
	}
	defer tx.Rollback()
	current, err := getAssignment(ctx, tx, `assignment_id=?`, p.AssignmentID)
	if err != nil {
		return current, err
	}
	if current.Kind != p.Kind || current.Revision != p.Revision || current.Attempt != p.Attempt || current.ExecutionFence != p.Fence || current.LeaseOwner != p.Owner || current.ClaimToken != p.ClaimToken || current.LeaseExpiresAt == nil || !current.LeaseExpiresAt.After(now) || (current.State != models.AssignmentStateClaimed && current.State != models.AssignmentStateDispatched && current.State != models.AssignmentStateExecuting) {
		return current, ErrAssignmentClaimLost
	}
	policy := normalizedRetryPolicy(current.RetryPolicy, current.Deadline)
	deadline := current.Deadline
	if policy.Deadline != nil && (deadline == nil || policy.Deadline.Before(*deadline)) {
		deadline = policy.Deadline
	}
	deadReason := ""
	if current.Attempt >= int64(policy.MaxAttempts) {
		deadReason = "max_attempts_exceeded"
	} else if deadline != nil && !deadline.After(now) {
		deadReason = "deadline_exceeded"
	} else if !retryClassAllowed(policy, p.FailureClass) {
		return current, ErrAssignmentRetryForbidden
	} else if !retryBudgetAdmits(policy, current.ConsumedBudget) {
		deadReason = "retry_budget_exhausted"
	} else if admitted, budgetErr := retryTaskBudgetAdmitsTx(ctx, tx, current.TaskID, policy.AttemptBudgetLimit); budgetErr != nil {
		return current, budgetErr
	} else if !admitted {
		deadReason = "retry_budget_exhausted"
	}
	state := models.AssignmentStateRetryWait
	var notBefore *time.Time
	if deadReason == "" {
		delay := RetryBackoff(policy, current.Attempt, p.JitterValue)
		if p.RetryAfter > delay {
			delay = p.RetryAfter
		}
		due := now.Add(delay)
		if deadline != nil && !due.Before(*deadline) {
			deadReason = "deadline_exceeded"
		} else {
			notBefore = &due
		}
	}
	if deadReason != "" {
		state = models.AssignmentStateDeadLetter
		if p.ErrorCode == "" {
			p.ErrorCode = deadReason
		}
	}
	res, err := tx.ExecContext(ctx, `UPDATE task_assignments SET state=?,revision=revision+1,not_before=?,failure_class=?,error_code=?,lease_owner='',claim_token='',lease_expires_at=0,updated_at=? WHERE assignment_id=? AND revision=? AND attempt=? AND execution_fence=? AND lease_owner=? AND claim_token=? AND state IN ('claimed','dispatched','executing')`, state, timeText(notBefore), p.FailureClass, p.ErrorCode, formatTime(now), p.AssignmentID, p.Revision, p.Attempt, p.Fence, p.Owner, p.ClaimToken)
	if err != nil || requireOneRow(res) != nil {
		return current, ErrAssignmentConflict
	}
	res, err = tx.ExecContext(ctx, `UPDATE execution_attempts SET state='superseded',failure_class=?,error_code=?,finished_at=? WHERE assignment_id=? AND attempt=? AND execution_fence=? AND state IN ('claimed','dispatched','executing')`, p.FailureClass, p.ErrorCode, formatTime(now), p.AssignmentID, p.Attempt, p.Fence)
	if err != nil || requireOneRow(res) != nil {
		return current, errAssignmentAttemptUpdate
	}
	event := "retry_scheduled"
	if state == models.AssignmentStateDeadLetter {
		event = "dead_lettered"
	}
	if _, err = tx.ExecContext(ctx, `INSERT INTO assignment_retry_events(tenant_id,assignment_id,event_type,failure_class,not_before,expected_revision,created_at) VALUES(?,?,?,?,?,?,?)`, current.TenantID, current.AssignmentID, event, p.FailureClass, timeText(notBefore), current.Revision, formatTime(now)); err != nil {
		return current, err
	}
	if state == models.AssignmentStateDeadLetter {
		if err = settleDeadLetterTaskTx(ctx, tx, current, now); err != nil {
			return current, err
		}
	}
	current, err = getAssignment(ctx, tx, `assignment_id=?`, p.AssignmentID)
	if err == nil {
		err = tx.Commit()
	}
	return current, err
}

func (s *assignmentStore) ExpireDueDeadLetters(ctx context.Context, kind models.AssignmentKind, now time.Time, limit int) (int64, error) {
	if !validAssignmentKind(kind) {
		return 0, ErrAssignmentConflict
	}
	if limit <= 0 {
		limit = 100
	}
	now = now.UTC()
	if now.IsZero() {
		now = time.Now().UTC()
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return 0, err
	}
	defer tx.Rollback()
	rows, err := tx.QueryContext(ctx, `SELECT assignment_id FROM task_assignments WHERE assignment_kind=? AND state IN ('queued','retry_wait') AND deadline IS NOT NULL AND deadline<=? ORDER BY assignment_id LIMIT ?`, kind, formatTime(now), limit)
	if err != nil {
		return 0, err
	}
	var ids []string
	for rows.Next() {
		var id string
		if err = rows.Scan(&id); err != nil {
			rows.Close()
			return 0, err
		}
		ids = append(ids, id)
	}
	rows.Close()
	var count int64
	for _, id := range ids {
		current, loadErr := getAssignment(ctx, tx, `assignment_id=?`, id)
		if loadErr != nil {
			return count, loadErr
		}
		res, updateErr := tx.ExecContext(ctx, `UPDATE task_assignments SET state='dead_letter',revision=revision+1,execution_fence=execution_fence+1,not_before=NULL,failure_class=?,error_code='deadline_exceeded',lease_owner='',claim_token='',lease_expires_at=0,updated_at=? WHERE assignment_id=? AND revision=? AND state IN ('queued','retry_wait') AND deadline<=?`, models.FailureClassDeadlineExceeded, formatTime(now), id, current.Revision, formatTime(now))
		if updateErr != nil {
			return count, updateErr
		}
		n, _ := res.RowsAffected()
		if n == 0 {
			continue
		}
		count++
		if _, updateErr = tx.ExecContext(ctx, `INSERT INTO assignment_retry_events(tenant_id,assignment_id,event_type,failure_class,expected_revision,created_at) VALUES(?,?,'dead_lettered',?,?,?)`, current.TenantID, id, models.FailureClassDeadlineExceeded, current.Revision, formatTime(now)); updateErr != nil {
			return count, updateErr
		}
		if updateErr = settleDeadLetterTaskTx(ctx, tx, current, now); updateErr != nil {
			return count, updateErr
		}
	}
	if err = tx.Commit(); err != nil {
		return 0, err
	}
	return count, nil
}

func settleDeadLetterTaskTx(ctx context.Context, tx *sql.Tx, a models.TaskAssignment, now time.Time) error {
	res, err := tx.ExecContext(ctx, `UPDATE tasks SET status='failed',error_code='dead_lettered',updated_at=?,completed_at=? WHERE task_id=? AND status IN ('pending','accepted','running')`, formatTime(now), formatTime(now), a.TaskID)
	if err != nil {
		return err
	}
	if n, _ := res.RowsAffected(); n == 0 {
		return ErrStatusConflict
	}
	if err = settleTaskBudgetTx(ctx, tx, a.TaskID, "failed", models.DelegationBudget{}, now); err != nil {
		return err
	}
	task, err := getTaskTx(ctx, tx, a.TaskID)
	if err != nil {
		return err
	}
	queue := executionQueueStore{}
	if err = queue.insertTaskTransition(ctx, tx, task, "failed", now); err != nil {
		return err
	}
	_, err = tx.ExecContext(ctx, `UPDATE agent_capacity_reservations SET state='released',revision=revision+1,release_reason='dead_lettered',released_at=? WHERE assignment_id=? AND state='held'`, formatTime(now), a.AssignmentID)
	return err
}

func (s *assignmentStore) RecoverExpiredActive(ctx context.Context, kind models.AssignmentKind, now time.Time, limit int) (int64, error) {
	if !validAssignmentKind(kind) {
		return 0, ErrAssignmentConflict
	}
	if limit <= 0 {
		limit = 100
	}
	now = now.UTC()
	if now.IsZero() {
		now = time.Now().UTC()
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return 0, err
	}
	defer tx.Rollback()
	rows, err := tx.QueryContext(ctx, `SELECT assignment_id,task_id,attempt,execution_fence FROM task_assignments WHERE assignment_kind=? AND state IN ('dispatched','executing') AND lease_expires_at<=? ORDER BY assignment_id LIMIT ?`, kind, now.UnixNano(), limit)
	if err != nil {
		return 0, err
	}
	type expired struct {
		id, task       string
		attempt, fence int64
	}
	var items []expired
	for rows.Next() {
		var x expired
		if err = rows.Scan(&x.id, &x.task, &x.attempt, &x.fence); err != nil {
			rows.Close()
			return 0, err
		}
		items = append(items, x)
	}
	rows.Close()
	var recovered int64
	for _, x := range items {
		res, e := tx.ExecContext(ctx, `UPDATE task_assignments SET state='settled',revision=revision+1,execution_fence=execution_fence+1,result_status='outcome_unknown',failure_class=?,error_code='expired_active_lease',lease_owner='',claim_token='',lease_expires_at=0,updated_at=? WHERE assignment_id=? AND assignment_kind=? AND attempt=? AND execution_fence=? AND state IN ('dispatched','executing') AND lease_expires_at<=?`, models.FailureClassSentUnacknowledged, formatTime(now), x.id, kind, x.attempt, x.fence, now.UnixNano())
		if e != nil {
			return 0, e
		}
		n, rowsErr := res.RowsAffected()
		if rowsErr != nil {
			return 0, rowsErr
		}
		if n == 0 {
			continue
		}
		recovered++
		attemptRes, updateErr := tx.ExecContext(ctx, `UPDATE execution_attempts SET state='settled',result_status='outcome_unknown',failure_class=?,error_code='expired_active_lease',finished_at=? WHERE assignment_id=? AND attempt=? AND execution_fence=?`, models.FailureClassSentUnacknowledged, formatTime(now), x.id, x.attempt, x.fence)
		if updateErr != nil {
			return 0, updateErr
		}
		if updateErr = requireOneRow(attemptRes); updateErr != nil {
			return 0, updateErr
		}
		taskRes, updateErr := tx.ExecContext(ctx, `UPDATE tasks SET status='outcome_unknown',updated_at=? WHERE task_id=? AND status IN ('pending','accepted','running')`, formatTime(now), x.task)
		if updateErr != nil {
			return 0, updateErr
		}
		if updateErr = requireOneRow(taskRes); updateErr != nil {
			return 0, ErrStatusConflict
		}
		reservationRes, updateErr := tx.ExecContext(ctx, `UPDATE agent_capacity_reservations SET state='uncertain',revision=revision+1,release_reason='expired_active_lease' WHERE assignment_id=? AND state='held'`, x.id)
		if updateErr != nil {
			return 0, updateErr
		}
		if n, rowsErr := reservationRes.RowsAffected(); rowsErr != nil {
			return 0, rowsErr
		} else if n > 1 {
			return 0, ErrReservationConflict
		}
		task, loadErr := getTaskTx(ctx, tx, x.task)
		if loadErr != nil {
			return 0, loadErr
		}
		queue := executionQueueStore{}
		if updateErr = queue.insertTaskTransition(ctx, tx, task, "outcome_unknown", now); updateErr != nil {
			return 0, updateErr
		}
		if updateErr = settleTaskBudgetTx(ctx, tx, x.task, "outcome_unknown", models.DelegationBudget{}, now); updateErr != nil {
			return 0, updateErr
		}
		if kind == models.AssignmentKindOutboundDelegation {
			outboxRes, outboxErr := tx.ExecContext(ctx, `UPDATE delegation_dispatch_outbox SET terminal_at=?,failure_class=?,last_error='expired_active_lease' WHERE assignment_id=? AND delivered_at IS NULL`, formatTime(now), models.FailureClassSentUnacknowledged, x.id)
			if outboxErr != nil {
				return 0, outboxErr
			}
			if n, rowsErr := outboxRes.RowsAffected(); rowsErr != nil {
				return 0, rowsErr
			} else if n > 1 {
				return 0, ErrAssignmentConflict
			}
		}
	}
	if err = tx.Commit(); err != nil {
		return 0, err
	}
	return recovered, nil
}

func (s *assignmentStore) RequestCancel(ctx context.Context, p CancelAssignmentParams) (models.TaskAssignment, error) {
	if !validAssignmentKind(p.Kind) {
		return models.TaskAssignment{}, ErrAssignmentConflict
	}
	now := p.Now.UTC()
	if now.IsZero() {
		now = time.Now().UTC()
	}
	if p.ExpectedRevision == math.MaxInt64 {
		return models.TaskAssignment{}, ErrAssignmentConflict
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return models.TaskAssignment{}, err
	}
	defer tx.Rollback()
	current, err := getAssignment(ctx, tx, `assignment_id=?`, p.AssignmentID)
	if err != nil {
		return current, err
	}
	if current.Kind != p.Kind {
		return current, ErrAssignmentClaimLost
	}
	if current.State == models.AssignmentStateCancelled {
		if current.Revision != p.ExpectedRevision+1 || current.Attempt != p.ExpectedAttempt || current.ExecutionFence != p.ExpectedFence+1 {
			return current, ErrAssignmentConflict
		}
		return current, nil
	}
	if current.State == models.AssignmentStateSettled || current.State == models.AssignmentStateDeadLetter {
		return current, ErrAssignmentTerminal
	}
	if current.Revision != p.ExpectedRevision || current.Attempt != p.ExpectedAttempt || current.ExecutionFence != p.ExpectedFence {
		return current, ErrAssignmentConflict
	}
	res, err := tx.ExecContext(ctx, `UPDATE task_assignments SET state='cancelled',revision=revision+1,execution_fence=execution_fence+1,lease_owner='',claim_token='',lease_expires_at=0,failure_class=?,updated_at=? WHERE assignment_id=? AND assignment_kind=? AND revision=? AND attempt=? AND execution_fence=? AND state IN ('queued','claimed','dispatched','executing','retry_wait')`, models.FailureClassCancelled, formatTime(now), p.AssignmentID, p.Kind, p.ExpectedRevision, p.ExpectedAttempt, p.ExpectedFence)
	if err != nil {
		return current, err
	}
	n, rowsErr := res.RowsAffected()
	if rowsErr != nil {
		return current, rowsErr
	}
	if n == 0 {
		return current, ErrAssignmentConflict
	}
	if n != 1 {
		return current, errAssignmentAttemptUpdate
	}
	if current.Attempt > 0 {
		attemptRes, updateErr := tx.ExecContext(ctx, `UPDATE execution_attempts SET state='cancelled',failure_class=?,finished_at=? WHERE assignment_id=? AND attempt=? AND execution_fence=? AND state IN ('claimed','dispatched','executing')`, models.FailureClassCancelled, formatTime(now), p.AssignmentID, current.Attempt, current.ExecutionFence)
		if updateErr != nil {
			return current, updateErr
		}
		if err = requireOneRow(attemptRes); err != nil {
			return current, err
		}
	}
	current, err = getAssignment(ctx, tx, `assignment_id=?`, p.AssignmentID)
	if err != nil {
		return current, err
	}
	if err = tx.Commit(); err != nil {
		return current, err
	}
	return current, nil
}

type assignmentQuerier interface {
	QueryRowContext(context.Context, string, ...any) *sql.Row
}

func getAssignment(ctx context.Context, q assignmentQuerier, where string, args ...any) (models.TaskAssignment, error) {
	row := q.QueryRowContext(ctx, `SELECT assignment_id,assignment_kind,tenant_id,task_id,agent_id,state,revision,route_attempt,attempt,execution_fence,lease_owner,claim_token,lease_expires_at,not_before,deadline,delivery_id,idempotency_key,retry_policy_json,replay_count,supersedes_assignment_id,dispatch_disposition,failover_transition_id,failure_class,result_status,outcome_json,error_code,consumed_token_count,consumed_payment_amount,consumed_currency,execution_receipt_json,created_at,updated_at FROM task_assignments WHERE `+where, args...)
	var a models.TaskAssignment
	var lease int64
	var notBefore, deadline, outcome, receipt sql.NullString
	var retryJSON, created, updated string
	err := row.Scan(&a.AssignmentID, &a.Kind, &a.TenantID, &a.TaskID, &a.AgentID, &a.State, &a.Revision, &a.RouteAttempt, &a.Attempt, &a.ExecutionFence, &a.LeaseOwner, &a.ClaimToken, &lease, &notBefore, &deadline, &a.DeliveryID, &a.IdempotencyKey, &retryJSON, &a.ReplayCount, &a.SupersedesAssignmentID, &a.DispatchDisposition, &a.FailoverTransitionID, &a.FailureClass, &a.ResultStatus, &outcome, &a.ErrorCode, &a.ConsumedBudget.TokenCount, &a.ConsumedBudget.PaymentAmount, &a.ConsumedBudget.Currency, &receipt, &created, &updated)
	if errors.Is(err, sql.ErrNoRows) {
		return a, ErrAssignmentNotFound
	}
	if err != nil {
		return a, fmt.Errorf("scan assignment: %w", err)
	}
	var policy models.RetryPolicy
	if err = json.Unmarshal([]byte(retryJSON), &policy); err != nil {
		return a, fmt.Errorf("decode retry policy: %w", err)
	}
	a.RetryPolicy = &policy
	a.LeaseExpiresAt = unixTimePtr(lease)
	a.NotBefore = parseTimePtr(notBefore)
	a.Deadline = parseTimePtr(deadline)
	a.Outcome = rawMessage(outcome)
	a.ExecutionReceipt = rawMessage(receipt)
	a.CreatedAt, _ = time.Parse(time.RFC3339Nano, created)
	a.UpdatedAt, _ = time.Parse(time.RFC3339Nano, updated)
	return a, nil
}

func scanAttempt(row rowScanner, a *models.ExecutionAttempt) error {
	var lease int64
	var outcome, receipt, started, finished sql.NullString
	err := row.Scan(&a.AssignmentID, &a.TenantID, &a.TaskID, &a.AgentID, &a.Attempt, &a.ExecutionFence, &a.State, &a.LeaseOwner, &a.ClaimToken, &lease, &a.FailureClass, &a.ResultStatus, &outcome, &a.ErrorCode, &a.ConsumedBudget.TokenCount, &a.ConsumedBudget.PaymentAmount, &a.ConsumedBudget.Currency, &receipt, &started, &finished)
	if err != nil {
		return fmt.Errorf("scan attempt: %w", err)
	}
	a.LeaseExpiresAt = unixTimePtr(lease)
	a.Outcome = rawMessage(outcome)
	a.ExecutionReceipt = rawMessage(receipt)
	a.StartedAt = parseTimePtr(started)
	a.FinishedAt = parseTimePtr(finished)
	return nil
}

func classifyRenew(ctx context.Context, q assignmentQuerier, p AssignmentTransition, now, newLease time.Time) error {
	a, err := getAssignment(ctx, q, `assignment_id=?`, p.AssignmentID)
	if err != nil {
		return err
	}
	if a.Kind != p.Kind {
		return ErrAssignmentClaimLost
	}
	if a.State == models.AssignmentStateSettled || a.State == models.AssignmentStateCancelled || a.State == models.AssignmentStateDeadLetter {
		return ErrAssignmentTerminal
	}
	if a.Attempt != p.Attempt || a.ExecutionFence != p.Fence || a.LeaseOwner != p.Owner || a.ClaimToken != p.ClaimToken {
		return ErrAssignmentClaimLost
	}
	if a.LeaseExpiresAt == nil || !a.LeaseExpiresAt.After(now) {
		return ErrAssignmentLeaseExpired
	}
	if a.Revision != p.Revision {
		return ErrAssignmentConflict
	}
	if a.State != models.AssignmentStateClaimed && a.State != models.AssignmentStateDispatched && a.State != models.AssignmentStateExecuting {
		return ErrAssignmentInvalidTransition
	}
	if !newLease.After(*a.LeaseExpiresAt) {
		return ErrAssignmentConflict
	}
	return ErrAssignmentConflict
}

func classifyGuard(ctx context.Context, q assignmentQuerier, p AssignmentTransition, expected models.AssignmentState, now time.Time) error {
	a, err := getAssignment(ctx, q, `assignment_id=?`, p.AssignmentID)
	if err != nil {
		return err
	}
	if a.State == models.AssignmentStateSettled || a.State == models.AssignmentStateCancelled || a.State == models.AssignmentStateDeadLetter {
		return ErrAssignmentTerminal
	}
	if a.Attempt != p.Attempt || a.ExecutionFence != p.Fence || a.LeaseOwner != p.Owner || a.ClaimToken != p.ClaimToken {
		return ErrAssignmentClaimLost
	}
	if a.LeaseExpiresAt == nil || !a.LeaseExpiresAt.After(now) {
		return ErrAssignmentLeaseExpired
	}
	if a.Revision != p.Revision {
		return ErrAssignmentConflict
	}
	if a.State != expected {
		return ErrAssignmentInvalidTransition
	}
	return ErrAssignmentConflict
}
func normalizedRetryPolicy(in *models.RetryPolicy, assignmentDeadline *time.Time) models.RetryPolicy {
	p := models.RetryPolicy{MaxAttempts: 5, InitialBackoff: time.Second, MaxBackoff: time.Minute, BackoffMultiplier: 2, RetryableFailureClasses: []models.FailureClass{models.FailureClassPreDispatchTransient}, Deadline: assignmentDeadline}
	if in != nil {
		p = *in
	}
	if p.MaxAttempts < 1 {
		p.MaxAttempts = 1
	}
	if p.InitialBackoff < 0 {
		p.InitialBackoff = 0
	}
	if p.MaxBackoff < p.InitialBackoff {
		p.MaxBackoff = p.InitialBackoff
	}
	if p.BackoffMultiplier < 1 {
		p.BackoffMultiplier = 1
	}
	if p.Jitter < 0 {
		p.Jitter = 0
	}
	if p.Jitter > 1 {
		p.Jitter = 1
	}
	return p
}

func RetryBackoff(policy models.RetryPolicy, attempt int64, jitterValue float64) time.Duration {
	delay := float64(policy.InitialBackoff)
	for i := int64(1); i < attempt; i++ {
		delay *= policy.BackoffMultiplier
		if delay >= float64(policy.MaxBackoff) {
			delay = float64(policy.MaxBackoff)
			break
		}
	}
	if jitterValue < 0 {
		jitterValue = 0
	}
	if jitterValue > 1 {
		jitterValue = 1
	}
	delay *= 1 + policy.Jitter*(2*jitterValue-1)
	if delay < 0 {
		return 0
	}
	if delay > float64(policy.MaxBackoff) {
		delay = float64(policy.MaxBackoff)
	}
	return time.Duration(delay)
}

func retryClassAllowed(policy models.RetryPolicy, failure models.FailureClass) bool {
	for _, allowed := range policy.RetryableFailureClasses {
		if allowed == failure {
			return true
		}
	}
	return false
}

func retryBudgetAdmits(policy models.RetryPolicy, consumed models.DelegationBudget) bool {
	limit := policy.AttemptBudgetLimit
	if limit.TokenCount == 0 && limit.PaymentAmount == 0 {
		return true
	}
	return consumed.TokenCount <= limit.TokenCount && consumed.PaymentAmount <= limit.PaymentAmount && (consumed.TokenCount == 0 && consumed.PaymentAmount == 0 || consumed.Currency == limit.Currency)
}

func retryTaskBudgetAdmitsTx(ctx context.Context, tx *sql.Tx, taskID string, attempt models.DelegationBudget) (bool, error) {
	if attempt.TokenCount == 0 && attempt.PaymentAmount == 0 {
		return true, nil
	}
	var envelope, reserved, consumed models.DelegationBudget
	if err := tx.QueryRowContext(ctx, `SELECT budget_token_count,budget_payment_amount,budget_currency,reserved_token_count,reserved_payment_amount,consumed_token_count,consumed_payment_amount FROM tasks WHERE task_id=?`, taskID).Scan(&envelope.TokenCount, &envelope.PaymentAmount, &envelope.Currency, &reserved.TokenCount, &reserved.PaymentAmount, &consumed.TokenCount, &consumed.PaymentAmount); err != nil {
		return false, err
	}
	if attempt.Currency != "" && envelope.Currency != "" && attempt.Currency != envelope.Currency {
		return false, nil
	}
	return consumed.TokenCount+attempt.TokenCount <= envelope.TokenCount && consumed.PaymentAmount+attempt.PaymentAmount <= envelope.PaymentAmount && reserved.TokenCount <= envelope.TokenCount && reserved.PaymentAmount <= envelope.PaymentAmount, nil
}

func sameCreation(a, b models.TaskAssignment) bool {
	return a.Kind == b.Kind && a.TenantID == b.TenantID && a.TaskID == b.TaskID && a.AgentID == b.AgentID && a.DeliveryID == b.DeliveryID && a.IdempotencyKey == b.IdempotencyKey && equalTimePtr(a.NotBefore, b.NotBefore) && equalTimePtr(a.Deadline, b.Deadline)
}
func sameResult(a models.TaskAssignment, p AssignmentResult) bool {
	return a.ResultStatus == p.Status && a.FailureClass == p.FailureClass && string(a.Outcome) == string(p.Outcome) && a.ErrorCode == p.ErrorCode && a.ConsumedBudget == p.ConsumedBudget && string(a.ExecutionReceipt) == string(p.ExecutionReceipt)
}
func equalTimePtr(a, b *time.Time) bool {
	if a == nil || b == nil {
		return a == nil && b == nil
	}
	return a.Equal(*b)
}
func requireOneRow(result sql.Result) error {
	n, err := result.RowsAffected()
	if err != nil {
		return err
	}
	if n != 1 {
		return errAssignmentAttemptUpdate
	}
	return nil
}
func formatTime(t time.Time) string { return t.UTC().Format(time.RFC3339Nano) }
func timeText(t *time.Time) any {
	if t == nil {
		return nil
	}
	return formatTime(*t)
}
func parseTimePtr(v sql.NullString) *time.Time {
	if !v.Valid {
		return nil
	}
	t, err := time.Parse(time.RFC3339Nano, v.String)
	if err != nil {
		return nil
	}
	return &t
}
func unixTimePtr(v int64) *time.Time {
	if v == 0 {
		return nil
	}
	t := time.Unix(0, v).UTC()
	return &t
}
func rawNull(v json.RawMessage) any {
	if len(v) == 0 {
		return nil
	}
	return string(v)
}
func rawMessage(v sql.NullString) json.RawMessage {
	if !v.Valid {
		return nil
	}
	return json.RawMessage(v.String)
}
func newClaimToken() (string, error) {
	b := make([]byte, 16)
	if _, err := rand.Read(b); err != nil {
		return "", fmt.Errorf("generate claim token: %w", err)
	}
	return hex.EncodeToString(b), nil
}
