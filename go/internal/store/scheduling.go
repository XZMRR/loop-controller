package store

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"time"

	"github.com/loop-controller/go/internal/models"
	"github.com/loop-controller/go/internal/observability"
	"github.com/loop-controller/go/internal/router"
	"github.com/loop-controller/go/internal/token"
	_ "modernc.org/sqlite"
)

var (
	ErrSchedulingNoCandidate = errors.New("no scheduling candidate")
	ErrSchedulingCapacity    = errors.New("scheduling capacity unavailable")
	ErrSchedulingIdempotency = errors.New("scheduling idempotency conflict")
	ErrSchedulingInvalid     = errors.New("invalid scheduling request")
	ErrReservationConflict   = errors.New("reservation revision conflict")
)

type ScheduleAndReserveParams struct {
	Request         models.SchedulingRequest
	PinnedAgentID   string
	ProtocolVersion string
	WorkloadID      string
	AssignmentID    string
	DeliveryID      string
	CapacityUnits   int
	ReservationTTL  time.Duration
	Now             time.Time
}

type ScheduleAndReserveResult struct {
	Decision   router.SchedulingDecision
	Assignment models.TaskAssignment
}

type FailoverAndReserveParams struct {
	AssignmentTransition
	TenantID            string
	Request             models.SchedulingRequest
	ProtocolVersion     string
	WorkloadID          string
	FailureClass        models.FailureClass
	DispatchDisposition string
	CapacityUnits       int
	ReservationTTL      time.Duration
	Now                 time.Time
}

type DelegationTokenIssuer interface {
	Issue(token.DelegationClaims, time.Duration) (string, error)
}

type SchedulingStore struct {
	path, dsn, owner string
	issuer           DelegationTokenIssuer
	tokenTTL         time.Duration
}

func (db *DB) SchedulingStore() *SchedulingStore {
	return &SchedulingStore{path: db.path, dsn: db.path + "?_busy_timeout=5000&_journal_mode=WAL&_foreign_keys=ON&_txlock=immediate", owner: db.instanceID, issuer: db.delegationTokenIssuer, tokenTTL: db.delegationTokenTTL}
}

func (db *DB) SetDelegationTokenIssuer(issuer DelegationTokenIssuer, ttl time.Duration) {
	db.delegationTokenIssuer = issuer
	if ttl <= 0 {
		ttl = 5 * time.Minute
	}
	db.delegationTokenTTL = ttl
}

func (s *SchedulingStore) WithDelegationTokenIssuer(issuer DelegationTokenIssuer, ttl time.Duration) *SchedulingStore {
	s.issuer = issuer
	if ttl <= 0 {
		ttl = 5 * time.Minute
	}
	s.tokenTTL = ttl
	return s
}

func (s *SchedulingStore) ScheduleAndReserve(ctx context.Context, p ScheduleAndReserveParams) (ScheduleAndReserveResult, error) {
	if p.AssignmentID == "" || p.DeliveryID == "" || p.Request.RequestID == "" || p.Request.TenantID == "" || p.Request.TaskID == "" || p.CapacityUnits <= 0 {
		return ScheduleAndReserveResult{}, ErrSchedulingInvalid
	}
	if p.Now.IsZero() {
		p.Now = time.Now().UTC()
	}
	if p.ReservationTTL <= 0 {
		p.ReservationTTL = time.Hour
	}
	hash, err := schedulingRequestHash(p)
	if err != nil {
		return ScheduleAndReserveResult{}, fmt.Errorf("hash scheduling request: %w", err)
	}
	db, err := sql.Open("sqlite", s.dsn)
	if err != nil {
		return ScheduleAndReserveResult{}, err
	}
	defer db.Close()
	db.SetMaxOpenConns(1)
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return ScheduleAndReserveResult{}, err
	}
	defer tx.Rollback()
	result, err := scheduleAndReserveTx(ctx, tx, s.owner, p, hash)
	if err != nil {
		return ScheduleAndReserveResult{}, err
	}
	if err = tx.Commit(); err != nil {
		return ScheduleAndReserveResult{}, err
	}
	return result, nil
}

func scheduleAndReserveTx(ctx context.Context, tx *sql.Tx, owner string, p ScheduleAndReserveParams, hash string) (ScheduleAndReserveResult, error) {
	if replay, found, e := loadSchedulingReplay(ctx, tx, p, hash); found || e != nil {
		return replay, e
	}

	task, err := getTaskTx(ctx, tx, p.Request.TaskID)
	if errors.Is(err, sql.ErrNoRows) {
		return ScheduleAndReserveResult{}, ErrAssignmentNotFound
	}
	if err != nil {
		return ScheduleAndReserveResult{}, err
	}
	if task.TenantID != p.Request.TenantID || task.Status != "accepted" {
		return ScheduleAndReserveResult{}, ErrSchedulingInvalid
	}

	agents, err := loadSchedulingAgents(ctx, tx, p.Request.TenantID, p.PinnedAgentID)
	if err != nil {
		return ScheduleAndReserveResult{}, err
	}
	decision, err := router.Rank(router.SchedulingInput{Request: p.Request, ProtocolVersion: p.ProtocolVersion, WorkloadID: p.WorkloadID, CapacityUnits: p.CapacityUnits, BudgetAvailable: budgetAdmits(p.Request.BudgetEnvelope), Now: p.Now}, agents)
	if err != nil {
		return ScheduleAndReserveResult{}, ErrSchedulingInvalid
	}
	eligible := 0
	for _, candidate := range decision.Candidates {
		if candidate.Eligible {
			eligible++
		}
	}
	observability.Default.Add("scheduler_candidates_total", float64(eligible), "eligible")
	observability.Default.Add("scheduler_candidates_total", float64(len(decision.Candidates)-eligible), "rejected")
	if decision.SelectedAgentID == "" {
		for _, c := range decision.Candidates {
			for _, reason := range c.ReasonCodes {
				if reason == models.SchedulingReasonCapacityUnavailable {
					observability.Default.Add("scheduler_no_candidate_total", 1, "capacity")
					return ScheduleAndReserveResult{}, ErrSchedulingCapacity
				}
			}
		}
		observability.Default.Add("scheduler_no_candidate_total", 1, "ineligible")
		return ScheduleAndReserveResult{}, ErrSchedulingNoCandidate
	}
	decisionJSON, err := json.Marshal(decision.Candidates)
	if err != nil {
		return ScheduleAndReserveResult{}, err
	}
	decisionID := "decision-" + hash[:32]
	if _, err = tx.ExecContext(ctx, `INSERT INTO scheduling_decisions(decision_id,request_id,tenant_id,task_id,selected_agent_id,algorithm_version,candidates_json,created_at,request_hash) VALUES(?,?,?,?,?,?,?,?,?)`, decisionID, p.Request.RequestID, p.Request.TenantID, p.Request.TaskID, decision.SelectedAgentID, decision.AlgorithmVersion, string(decisionJSON), formatTime(p.Now), hash); err != nil {
		return ScheduleAndReserveResult{}, err
	}
	policy := normalizedRetryPolicy(nil, p.Request.Deadline)
	policyJSON, _ := json.Marshal(policy)
	a := models.TaskAssignment{Kind: models.AssignmentKindTargetExecution, AssignmentID: p.AssignmentID, TenantID: p.Request.TenantID, TaskID: p.Request.TaskID, AgentID: decision.SelectedAgentID, State: models.AssignmentStateQueued, Revision: 1, RouteAttempt: 1, DeliveryID: p.DeliveryID, IdempotencyKey: p.Request.RequestID, Deadline: p.Request.Deadline, RetryPolicy: &policy, CreatedAt: p.Now, UpdatedAt: p.Now}
	res, err := tx.ExecContext(ctx, `INSERT INTO task_assignments (assignment_id,assignment_kind,tenant_id,task_id,agent_id,state,revision,attempt,execution_fence,lease_owner,claim_token,lease_expires_at,not_before,deadline,delivery_id,idempotency_key,retry_policy_json,failure_class,result_status,outcome_json,error_code,consumed_token_count,consumed_payment_amount,consumed_currency,execution_receipt_json,created_at,updated_at) VALUES (?,?,?,?,?,?,1,0,0,'','',0,NULL,?,?,?,?,'','',NULL,'',0,0,'',NULL,?,?)`, a.AssignmentID, a.Kind, a.TenantID, a.TaskID, a.AgentID, a.State, timeText(a.Deadline), a.DeliveryID, a.IdempotencyKey, string(policyJSON), formatTime(p.Now), formatTime(p.Now))
	if err != nil {
		return ScheduleAndReserveResult{}, err
	}
	if err = requireOneRow(res); err != nil {
		return ScheduleAndReserveResult{}, err
	}
	res, err = tx.ExecContext(ctx, `INSERT INTO agent_capacity_reservations(reservation_id,tenant_id,agent_id,task_id,units,expires_at,created_at,assignment_id,state,revision,release_reason,released_at) VALUES(?,?,?,?,?,?,?,?, 'held',1,'',NULL)`, "reservation-"+hash[:32], p.Request.TenantID, a.AgentID, a.TaskID, p.CapacityUnits, formatTime(p.Now.Add(p.ReservationTTL)), formatTime(p.Now), a.AssignmentID)
	if err != nil {
		return ScheduleAndReserveResult{}, err
	}
	if err = requireOneRow(res); err != nil {
		return ScheduleAndReserveResult{}, err
	}
	res, err = tx.ExecContext(ctx, `UPDATE tasks SET status='running',target_agent_id=?,updated_at=? WHERE task_id=? AND status='accepted' AND tenant_id=?`, a.AgentID, formatTime(p.Now), a.TaskID, a.TenantID)
	if err != nil {
		return ScheduleAndReserveResult{}, err
	}
	if err = requireOneRow(res); err != nil {
		return ScheduleAndReserveResult{}, ErrStatusConflict
	}
	task, err = getTaskTx(ctx, tx, a.TaskID)
	if err != nil {
		return ScheduleAndReserveResult{}, err
	}
	queue := executionQueueStore{owner: owner}
	if err = queue.insertTaskTransition(ctx, tx, task, "running", p.Now); err != nil {
		return ScheduleAndReserveResult{}, err
	}
	return ScheduleAndReserveResult{Decision: decision, Assignment: a}, nil
}

func loadSchedulingAgents(ctx context.Context, tx *sql.Tx, tenant, pinned string) ([]router.SchedulingAgent, error) {
	query := `SELECT ` + specColumns + ` FROM agents WHERE deleted_at IS NULL AND tenant_id=?`
	args := []any{tenant}
	if pinned != "" {
		query += ` AND agent_id=?`
		args = append(args, pinned)
	}
	query += ` ORDER BY agent_id`
	rows, err := tx.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var specs []models.AgentSpec
	for rows.Next() {
		spec, e := scanSpec(rows)
		if e != nil {
			return nil, e
		}
		specs = append(specs, spec)
	}
	if err = rows.Err(); err != nil {
		return nil, err
	}
	out := make([]router.SchedulingAgent, 0, len(specs))
	for _, spec := range specs {
		var status *models.AgentStatus
		x, e := scanStatus(tx.QueryRowContext(ctx, `SELECT tenant_id,agent_id,observed_generation,health,current_load,available_capacity,draining,last_seen_at,expires_at,status_revision,reported_by FROM agent_status WHERE tenant_id=? AND agent_id=?`, spec.TenantID, spec.AgentID))
		if e == nil {
			status = &x
		} else if !errors.Is(e, sql.ErrNoRows) {
			return nil, e
		}
		var reserved int
		if e = tx.QueryRowContext(ctx, `SELECT COALESCE(SUM(units),0) FROM agent_capacity_reservations WHERE tenant_id=? AND agent_id=? AND state IN ('held','uncertain')`, tenant, spec.AgentID).Scan(&reserved); e != nil {
			return nil, e
		}
		out = append(out, router.SchedulingAgent{Record: models.AgentRecord{Spec: spec, Status: status}, ReservedUnits: reserved})
	}
	return out, nil
}

func loadSchedulingReplay(ctx context.Context, tx *sql.Tx, p ScheduleAndReserveParams, hash string) (ScheduleAndReserveResult, bool, error) {
	var storedHash, selected, algorithm, candidates string
	err := tx.QueryRowContext(ctx, `SELECT request_hash,selected_agent_id,algorithm_version,candidates_json FROM scheduling_decisions WHERE tenant_id=? AND request_id=?`, p.Request.TenantID, p.Request.RequestID).Scan(&storedHash, &selected, &algorithm, &candidates)
	if errors.Is(err, sql.ErrNoRows) {
		return ScheduleAndReserveResult{}, false, nil
	}
	if err != nil {
		return ScheduleAndReserveResult{}, true, err
	}
	if storedHash != hash {
		return ScheduleAndReserveResult{}, true, ErrSchedulingIdempotency
	}
	var decoded []router.SchedulingCandidate
	if err = json.Unmarshal([]byte(candidates), &decoded); err != nil {
		return ScheduleAndReserveResult{}, true, err
	}
	a, err := getAssignment(ctx, tx, `task_id=?`, p.Request.TaskID)
	return ScheduleAndReserveResult{Decision: router.SchedulingDecision{SelectedAgentID: selected, AlgorithmVersion: algorithm, Candidates: decoded}, Assignment: a}, true, err
}

func schedulingRequestHash(p ScheduleAndReserveParams) (string, error) {
	p.Request.RequiredAgentCapabilities = sorted(p.Request.RequiredAgentCapabilities)
	p.Request.RequiredSecurityCapabilities = sorted(p.Request.RequiredSecurityCapabilities)
	p.Request.RequiredTools = sorted(p.Request.RequiredTools)
	p.Request.ExcludedAgentIDs = sorted(p.Request.ExcludedAgentIDs)
	p.Request.RetryContext.PreviousAgentIDs = sorted(p.Request.RetryContext.PreviousAgentIDs)
	payload := struct {
		Request                                                              models.SchedulingRequest `json:"request"`
		PinnedAgentID, ProtocolVersion, WorkloadID, AssignmentID, DeliveryID string
		CapacityUnits                                                        int
	}{p.Request, p.PinnedAgentID, p.ProtocolVersion, p.WorkloadID, p.AssignmentID, p.DeliveryID, p.CapacityUnits}
	b, e := json.Marshal(payload)
	if e != nil {
		return "", e
	}
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:]), nil
}
func sorted(in []string) []string                 { out := append([]string(nil), in...); sort.Strings(out); return out }
func budgetAdmits(b models.DelegationBudget) bool { return b.TokenCount >= 0 && b.PaymentAmount >= 0 }

func (s *SchedulingStore) FailoverAndReserve(ctx context.Context, p FailoverAndReserveParams) (ScheduleAndReserveResult, error) {
	if p.TenantID == "" || p.AssignmentID == "" || p.Request.TaskID == "" || p.Request.RequestID == "" || p.CapacityUnits <= 0 || p.FailureClass != models.FailureClassPreDispatchTransient || p.DispatchDisposition != "confirmed_not_sent" {
		return ScheduleAndReserveResult{}, ErrSchedulingInvalid
	}
	if p.Now.IsZero() {
		p.Now = time.Now().UTC()
	}
	if p.ReservationTTL <= 0 {
		p.ReservationTTL = time.Hour
	}
	db, err := sql.Open("sqlite", s.dsn)
	if err != nil {
		return ScheduleAndReserveResult{}, err
	}
	defer db.Close()
	db.SetMaxOpenConns(1)
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return ScheduleAndReserveResult{}, err
	}
	defer tx.Rollback()
	old, err := getAssignment(ctx, tx, `tenant_id=? AND assignment_id=?`, p.TenantID, p.AssignmentID)
	if err != nil {
		return ScheduleAndReserveResult{}, err
	}
	if old.Kind != p.Kind || old.TaskID != p.Request.TaskID || old.Revision != p.Revision || old.Attempt != p.Attempt || old.ExecutionFence != p.Fence || old.LeaseOwner != p.Owner || old.ClaimToken != p.ClaimToken || old.LeaseExpiresAt == nil || !old.LeaseExpiresAt.After(p.Now) || (old.State != models.AssignmentStateClaimed && old.State != models.AssignmentStateDispatched && old.State != models.AssignmentStateExecuting) {
		return ScheduleAndReserveResult{}, ErrAssignmentClaimLost
	}
	policy := normalizedRetryPolicy(old.RetryPolicy, old.Deadline)
	deadline := old.Deadline
	if policy.Deadline != nil && (deadline == nil || policy.Deadline.Before(*deadline)) {
		deadline = policy.Deadline
	}
	if old.RouteAttempt >= int64(policy.MaxAttempts) || deadline != nil && !deadline.After(p.Now) || !retryClassAllowed(policy, p.FailureClass) || !retryBudgetAdmits(policy, old.ConsumedBudget) {
		return ScheduleAndReserveResult{}, ErrAssignmentRetryForbidden
	}
	if admitted, e := retryTaskBudgetAdmitsTx(ctx, tx, old.TaskID, policy.AttemptBudgetLimit); e != nil || !admitted {
		if e != nil {
			return ScheduleAndReserveResult{}, e
		}
		return ScheduleAndReserveResult{}, ErrAssignmentRetryForbidden
	}
	task, err := getTaskTx(ctx, tx, old.TaskID)
	if err != nil {
		return ScheduleAndReserveResult{}, err
	}
	trustedRequest, protocolVersion, workloadID, err := trustedFailoverRequest(ctx, tx, task)
	if err != nil {
		return ScheduleAndReserveResult{}, err
	}
	trustedRequest.RequestID = p.Request.RequestID
	trustedRequest.ExcludedAgentIDs = append(trustedRequest.ExcludedAgentIDs, old.AgentID)
	trustedRequest.RetryContext.Attempt = int(old.RouteAttempt + 1)
	trustedRequest.RetryContext.PreviousAgentIDs = append(trustedRequest.RetryContext.PreviousAgentIDs, old.AgentID)
	trustedRequest.RetryContext.PreviousFailureClass = p.FailureClass
	p.Request, p.ProtocolVersion, p.WorkloadID = trustedRequest, protocolVersion, workloadID
	if deadline != nil && (p.Request.Deadline == nil || deadline.Before(*p.Request.Deadline)) {
		p.Request.Deadline = deadline
	}
	agents, err := loadSchedulingAgents(ctx, tx, old.TenantID, "")
	if err != nil {
		return ScheduleAndReserveResult{}, err
	}
	decision, err := router.Rank(router.SchedulingInput{Request: p.Request, ProtocolVersion: p.ProtocolVersion, WorkloadID: p.WorkloadID, CapacityUnits: p.CapacityUnits, BudgetAvailable: budgetAdmits(p.Request.BudgetEnvelope), Now: p.Now}, agents)
	if err != nil {
		return ScheduleAndReserveResult{}, ErrSchedulingInvalid
	}
	if decision.SelectedAgentID == "" {
		return ScheduleAndReserveResult{}, ErrSchedulingNoCandidate
	}
	transitionID := fmt.Sprintf("failover-%s-%d", old.AssignmentID, old.RouteAttempt+1)
	newID, deliveryID := transitionID+"-assignment", transitionID+"-delivery"
	candidates, _ := json.Marshal(decision.Candidates)
	decisionID := transitionID + "-decision"
	if _, err = tx.ExecContext(ctx, `INSERT INTO scheduling_decisions(decision_id,request_id,tenant_id,task_id,selected_agent_id,algorithm_version,candidates_json,created_at,request_hash) VALUES(?,?,?,?,?,?,?,?,?)`, decisionID, p.Request.RequestID, old.TenantID, old.TaskID, decision.SelectedAgentID, decision.AlgorithmVersion, string(candidates), formatTime(p.Now), transitionID); err != nil {
		return ScheduleAndReserveResult{}, err
	}
	res, err := tx.ExecContext(ctx, `UPDATE task_assignments SET state='superseded',revision=revision+1,execution_fence=execution_fence+1,lease_owner='',claim_token='',lease_expires_at=0,failure_class=?,dispatch_disposition=?,failover_transition_id=?,updated_at=? WHERE assignment_id=? AND revision=? AND attempt=? AND execution_fence=? AND lease_owner=? AND claim_token=? AND state IN ('claimed','dispatched','executing')`, p.FailureClass, p.DispatchDisposition, "", formatTime(p.Now), old.AssignmentID, p.Revision, p.Attempt, p.Fence, p.Owner, p.ClaimToken)
	if err != nil || requireOneRow(res) != nil {
		return ScheduleAndReserveResult{}, ErrAssignmentClaimLost
	}
	res, err = tx.ExecContext(ctx, `UPDATE execution_attempts SET state='superseded',failure_class=?,error_code='failed_over',finished_at=? WHERE assignment_id=? AND attempt=? AND execution_fence=? AND state IN ('claimed','dispatched','executing')`, p.FailureClass, formatTime(p.Now), old.AssignmentID, p.Attempt, p.Fence)
	if err != nil || requireOneRow(res) != nil {
		return ScheduleAndReserveResult{}, errAssignmentAttemptUpdate
	}
	res, err = tx.ExecContext(ctx, `UPDATE agent_capacity_reservations SET state='released',revision=revision+1,release_reason='failed_over',released_at=? WHERE assignment_id=? AND state='held'`, formatTime(p.Now), old.AssignmentID)
	if err != nil || requireOneRow(res) != nil {
		return ScheduleAndReserveResult{}, ErrReservationConflict
	}
	policyJSON, _ := json.Marshal(policy)
	a := models.TaskAssignment{AssignmentID: newID, Kind: old.Kind, TenantID: old.TenantID, TaskID: old.TaskID, AgentID: decision.SelectedAgentID, State: models.AssignmentStateQueued, Revision: 1, RouteAttempt: old.RouteAttempt + 1, ExecutionFence: old.ExecutionFence + 1, DeliveryID: deliveryID, IdempotencyKey: transitionID, RetryPolicy: &policy, Deadline: deadline, SupersedesAssignmentID: old.AssignmentID, FailoverTransitionID: transitionID, CreatedAt: p.Now, UpdatedAt: p.Now}
	_, err = tx.ExecContext(ctx, `INSERT INTO task_assignments(assignment_id,assignment_kind,tenant_id,task_id,agent_id,state,revision,route_attempt,attempt,execution_fence,lease_owner,claim_token,lease_expires_at,deadline,delivery_id,idempotency_key,retry_policy_json,supersedes_assignment_id,failover_transition_id,created_at,updated_at) VALUES(?,?,?,?,?,'queued',1,?,0,?,'','',0,?,?,?,?,?,?,?,?)`, newID, old.Kind, old.TenantID, old.TaskID, a.AgentID, a.RouteAttempt, old.ExecutionFence+1, timeText(deadline), deliveryID, transitionID, string(policyJSON), old.AssignmentID, transitionID, formatTime(p.Now), formatTime(p.Now))
	if err != nil {
		return ScheduleAndReserveResult{}, err
	}
	_, err = tx.ExecContext(ctx, `INSERT INTO agent_capacity_reservations(reservation_id,tenant_id,agent_id,task_id,units,expires_at,created_at,assignment_id,state,revision,release_reason) VALUES(?,?,?,?,?,?,?,?,'held',1,'')`, transitionID+"-reservation", old.TenantID, a.AgentID, old.TaskID, p.CapacityUnits, formatTime(p.Now.Add(p.ReservationTTL)), formatTime(p.Now), newID)
	if err != nil {
		return ScheduleAndReserveResult{}, err
	}
	if old.Kind == models.AssignmentKindOutboundDelegation {
		var approval sql.NullString
		var payload string
		if err = tx.QueryRowContext(ctx, `SELECT approval_id,payload_json FROM delegation_dispatch_outbox WHERE assignment_id=? AND delivered_at IS NULL AND terminal_at IS NULL`, old.AssignmentID).Scan(&approval, &payload); err != nil {
			return ScheduleAndReserveResult{}, err
		}
		var entrypoint models.AgentEntrypoint
		var targetWorkloadID string
		if err = tx.QueryRowContext(ctx, `SELECT entrypoint_type,entrypoint_url,expected_workload_id FROM agents WHERE tenant_id=? AND agent_id=? AND deleted_at IS NULL`, old.TenantID, a.AgentID).Scan(&entrypoint.Type, &entrypoint.URL, &targetWorkloadID); err != nil {
			return ScheduleAndReserveResult{}, err
		}
		if s.issuer == nil {
			return ScheduleAndReserveResult{}, errors.New("outbound failover delegation token issuer unavailable")
		}
		epBytes, _ := json.Marshal(entrypoint)
		if res, err = tx.ExecContext(ctx, `UPDATE delegation_dispatch_outbox SET terminal_at=?,failure_class=?,last_error='failed_over',claimed_by='',claim_token='',claim_expires_at=0 WHERE assignment_id=? AND delivered_at IS NULL AND terminal_at IS NULL`, formatTime(p.Now), p.FailureClass, old.AssignmentID); err != nil || requireOneRow(res) != nil {
			return ScheduleAndReserveResult{}, ErrAssignmentClaimLost
		}
		var req models.EntrypointTaskRequest
		if err = json.Unmarshal([]byte(payload), &req); err != nil {
			return ScheduleAndReserveResult{}, err
		}
		req.AssignmentID = newID
		req.DeliveryID = deliveryID
		req.AssignmentAttempt = 0
		req.AssignmentFence = old.ExecutionFence + 1
		req.TargetAgentID = a.AgentID
		req.TargetWorkloadID = targetWorkloadID
		req.TargetInstanceID = ""
		claims := delegationClaimsForRebind(task, req)
		req.DelegationToken, err = s.issuer.Issue(claims, s.tokenTTL)
		if err != nil || req.DelegationToken == "" {
			return ScheduleAndReserveResult{}, fmt.Errorf("issue failover delegation token: %w", err)
		}
		payloadBytes, _ := json.Marshal(req)
		if _, err = tx.ExecContext(ctx, `INSERT INTO delegation_dispatch_outbox(delivery_id,approval_id,task_id,assignment_id,entrypoint_json,payload_json,next_attempt_at) VALUES(?,?,?,?,?,?,?)`, deliveryID, approval, old.TaskID, newID, string(epBytes), string(payloadBytes), formatTime(p.Now)); err != nil {
			return ScheduleAndReserveResult{}, err
		}
	}
	if old.Kind == models.AssignmentKindOutboundDelegation {
		var req models.EntrypointTaskRequest
		var payload string
		if err = tx.QueryRowContext(ctx, `SELECT payload_json FROM delegation_dispatch_outbox WHERE assignment_id=?`, newID).Scan(&payload); err != nil {
			return ScheduleAndReserveResult{}, err
		}
		if err = json.Unmarshal([]byte(payload), &req); err != nil {
			return ScheduleAndReserveResult{}, err
		}
		if _, err = tx.ExecContext(ctx, `UPDATE tasks SET target_agent_id=?,target_workload_id=?,target_instance_id=?,delegation_token=?,updated_at=? WHERE task_id=? AND tenant_id=? AND status='running'`, req.TargetAgentID, req.TargetWorkloadID, req.TargetInstanceID, req.DelegationToken, formatTime(p.Now), old.TaskID, old.TenantID); err != nil {
			return ScheduleAndReserveResult{}, err
		}
	} else if _, err = tx.ExecContext(ctx, `UPDATE tasks SET target_agent_id=?,target_workload_id=?,target_instance_id='',updated_at=? WHERE task_id=? AND tenant_id=? AND status='running'`, a.AgentID, p.WorkloadID, formatTime(p.Now), old.TaskID, old.TenantID); err != nil {
		return ScheduleAndReserveResult{}, err
	}
	if _, err = tx.ExecContext(ctx, `INSERT INTO assignment_retry_events(tenant_id,assignment_id,event_type,failure_class,dispatch_disposition,failover_transition_id,superseded_assignment_id,expected_revision,created_at) VALUES(?,?,'failed_over',?,?,?,?,?,?)`, old.TenantID, newID, p.FailureClass, p.DispatchDisposition, transitionID, old.AssignmentID, old.Revision, formatTime(p.Now)); err != nil {
		return ScheduleAndReserveResult{}, err
	}
	if err = tx.Commit(); err != nil {
		return ScheduleAndReserveResult{}, err
	}
	return ScheduleAndReserveResult{Decision: decision, Assignment: a}, nil
}

func trustedFailoverRequest(ctx context.Context, tx *sql.Tx, task models.Task) (models.SchedulingRequest, string, string, error) {
	var requestJSON string
	err := tx.QueryRowContext(ctx, `SELECT scheduling_request_json FROM task_graph_nodes WHERE task_id=? AND tenant_id=? AND status IN ('running','outcome_unknown')`, task.TaskID, task.TenantID).Scan(&requestJSON)
	if err == nil {
		var request models.SchedulingRequest
		if err = json.Unmarshal([]byte(requestJSON), &request); err != nil {
			return request, "", "", ErrSchedulingInvalid
		}
		request.TenantID = task.TenantID
		request.TaskID = task.TaskID
		return request, models.CurrentProtocolVersion, task.TargetWorkloadID, nil
	}
	if !errors.Is(err, sql.ErrNoRows) {
		return models.SchedulingRequest{}, "", "", err
	}
	return models.SchedulingRequest{
		TenantID:                  task.TenantID,
		TaskID:                    task.TaskID,
		RequiredAgentCapabilities: append([]string(nil), task.AllowedCapabilities...),
		RequiredTools:             append([]string(nil), task.AllowedTools...),
		Deadline:                  task.Deadline,
		BudgetEnvelope:            task.Budget,
	}, models.CurrentProtocolVersion, task.TargetWorkloadID, nil
}

func delegationClaimsForRebind(task models.Task, req models.EntrypointTaskRequest) token.DelegationClaims {
	claims := token.DelegationClaims{
		RequestID: req.RequestID, SessionID: req.SessionID, InteractionID: req.InteractionID, DecisionID: req.DecisionID,
		RootInteractionID: req.RootInteractionID, ParentInteractionID: req.ParentInteractionID,
		InitiatorAgentID: req.InitiatorAgentID, TargetAgentID: req.TargetAgentID, TargetWorkloadID: req.TargetWorkloadID,
		TargetInstanceID: req.TargetInstanceID, ToolName: req.ToolName, TaskID: req.TaskID,
		ArgumentsSHA256: token.HashArguments(req.Arguments), AllowedTools: append([]string(nil), req.AllowedTools...),
		AllowedCapabilities: append([]string(nil), req.AllowedCapabilities...), AllowRedelegation: req.AllowRedelegation,
		RootTaskID: req.RootTaskID, ParentTaskID: req.ParentTaskID, DelegationDepth: req.DelegationDepth,
		BudgetTokenCount: req.Budget.TokenCount, BudgetPaymentAmount: req.Budget.PaymentAmount, BudgetCurrency: req.Budget.Currency,
		TenantID: task.TenantID,
	}
	if req.Deadline != nil {
		claims.Deadline = req.Deadline.Unix()
	}
	return claims
}

func (s *SchedulingStore) ResolveUncertainReservation(ctx context.Context, reservationID string, expectedRevision int64, reason string, now time.Time) error {
	if reservationID == "" || expectedRevision <= 0 || reason == "" {
		return ErrSchedulingInvalid
	}
	if now.IsZero() {
		now = time.Now().UTC()
	}
	res, err := sql.Open("sqlite", s.dsn)
	if err != nil {
		return err
	}
	defer res.Close()
	res.SetMaxOpenConns(1)
	r, e := res.ExecContext(ctx, `UPDATE agent_capacity_reservations SET state='released',revision=revision+1,release_reason=?,released_at=? WHERE reservation_id=? AND revision=? AND state='uncertain'`, reason, formatTime(now), reservationID, expectedRevision)
	if e != nil {
		return e
	}
	n, e := r.RowsAffected()
	if e != nil {
		return e
	}
	if n == 1 {
		return nil
	}
	var state string
	var revision int64
	e = res.QueryRowContext(ctx, `SELECT state,revision FROM agent_capacity_reservations WHERE reservation_id=?`, reservationID).Scan(&state, &revision)
	if e != nil {
		return e
	}
	if state == "released" && revision == expectedRevision+1 {
		return nil
	}
	return ErrReservationConflict
}
