package store

import (
	"context"
	"crypto/rand"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/loop-controller/go/internal/models"
)

var (
	ErrDAGNotFound       = errors.New("task graph not found")
	ErrDAGConflict       = errors.New("task graph conflict")
	ErrDAGTaskGovernance = errors.New("graph task must be an accepted governed task")
)

type DAGStore struct {
	db          *sql.DB
	path, owner string
}
type DAGWakeup struct {
	DAGID, NodeID string
	Revision      int64
	ClaimToken    string
}

func (db *DB) DAGStore() *DAGStore { return &DAGStore{db: db.DB, path: db.path, owner: db.instanceID} }

// CreateGraph only orchestrates Tasks that were independently governed and accepted.
// Graph admission never grants Task permission and does not create Tasks.
func (s *DAGStore) CreateGraph(ctx context.Context, input models.TaskGraphCreate, key string, now time.Time) (models.TaskGraph, error) {
	in, edges, hash, err := models.ValidateTaskGraph(input)
	if err != nil {
		return models.TaskGraph{}, err
	}
	if key == "" {
		return models.TaskGraph{}, ErrDAGConflict
	}
	if now.IsZero() {
		now = time.Now().UTC()
	}
	db, err := sql.Open("sqlite", s.path+"?_busy_timeout=5000&_journal_mode=WAL&_foreign_keys=ON&_txlock=immediate")
	if err != nil {
		return models.TaskGraph{}, err
	}
	defer db.Close()
	db.SetMaxOpenConns(1)
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return models.TaskGraph{}, err
	}
	defer tx.Rollback()
	var existingID, existingHash string
	if e := tx.QueryRowContext(ctx, `SELECT dag_id,request_hash FROM task_graphs WHERE tenant_id=? AND idempotency_key=?`, in.TenantID, key).Scan(&existingID, &existingHash); e == nil {
		if existingHash != hash {
			return models.TaskGraph{}, ErrDAGConflict
		}
		return getGraphTx(ctx, tx, in.TenantID, existingID)
	} else if !errors.Is(e, sql.ErrNoRows) {
		return models.TaskGraph{}, e
	}
	root, e := getTaskTx(ctx, tx, in.RootTaskID)
	if e != nil || root.TenantID != in.TenantID || root.Status != "accepted" || root.InitiatorAgentID == "" || root.RootTaskID != "" || root.ParentTaskID != "" {
		return models.TaskGraph{}, ErrDAGTaskGovernance
	}
	if in.Deadline != nil && (root.Deadline == nil || in.Deadline.After(*root.Deadline)) {
		return models.TaskGraph{}, models.ErrDAGDeadline
	}
	if !sameCurrency(root.Budget, in.BudgetEnvelope) || root.Budget.TokenCount-root.ReservedBudget.TokenCount-root.ConsumedBudget.TokenCount < in.BudgetEnvelope.TokenCount || root.Budget.PaymentAmount-root.ReservedBudget.PaymentAmount-root.ConsumedBudget.PaymentAmount < in.BudgetEnvelope.PaymentAmount {
		return models.TaskGraph{}, models.ErrDAGBudget
	}
	for i := range in.Nodes {
		n := &in.Nodes[i]
		t, e := getTaskTx(ctx, tx, n.TaskID)
		if e != nil || t.TenantID != in.TenantID || t.Status != "accepted" || t.InitiatorAgentID != root.InitiatorAgentID || t.RootTaskID != in.RootTaskID || t.ParentTaskID == "" {
			return models.TaskGraph{}, ErrDAGTaskGovernance
		}
		parent, e := getTaskTx(ctx, tx, t.ParentTaskID)
		if e != nil || parent.TenantID != in.TenantID || parent.InitiatorAgentID != root.InitiatorAgentID || (parent.TaskID != in.RootTaskID && parent.RootTaskID != in.RootTaskID) {
			return models.TaskGraph{}, ErrDAGTaskGovernance
		}
		if !scopeSubset(n.SchedulingRequest.RequiredTools, t.AllowedTools) || !scopeSubset(n.SchedulingRequest.RequiredAgentCapabilities, t.AllowedCapabilities) || len(n.SchedulingRequest.RequiredSecurityCapabilities) != 0 || n.SchedulingRequest.TrustDomain != "" || len(n.SchedulingRequest.Affinity) != 0 || len(n.SchedulingRequest.AntiAffinity) != 0 {
			return models.TaskGraph{}, ErrDAGTaskGovernance
		}
		n.SchedulingRequest.RequiredTools = append([]string(nil), t.AllowedTools...)
		n.SchedulingRequest.RequiredAgentCapabilities = append([]string(nil), t.AllowedCapabilities...)
		n.SchedulingRequest.Deadline = t.Deadline
		n.SchedulingRequest.BudgetEnvelope = n.BudgetLimit
		if in.Deadline != nil && (t.Deadline == nil || in.Deadline.After(*t.Deadline)) {
			return models.TaskGraph{}, models.ErrDAGDeadline
		}
		if !sameCurrency(t.Budget, n.BudgetLimit) || t.Budget.TokenCount < n.BudgetLimit.TokenCount || t.Budget.PaymentAmount < n.BudgetLimit.PaymentAmount {
			return models.TaskGraph{}, models.ErrDAGBudget
		}
		effectiveDeadline := in.Deadline
		if n.SchedulingRequest.Deadline != nil && (effectiveDeadline == nil || n.SchedulingRequest.Deadline.Before(*effectiveDeadline)) {
			effectiveDeadline = n.SchedulingRequest.Deadline
		}
		if t.Deadline != nil && (effectiveDeadline == nil || t.Deadline.Before(*effectiveDeadline)) {
			effectiveDeadline = t.Deadline
		}
		if n.RetryPolicy.Deadline != nil && (effectiveDeadline == nil || n.RetryPolicy.Deadline.After(*effectiveDeadline)) {
			return models.TaskGraph{}, models.ErrDAGDeadline
		}
		zeroPolicy := n.RetryPolicy.MaxAttempts == 0 && n.RetryPolicy.InitialBackoff == 0 && n.RetryPolicy.MaxBackoff == 0 && n.RetryPolicy.BackoffMultiplier == 0 && n.RetryPolicy.Jitter == 0 && len(n.RetryPolicy.RetryableFailureClasses) == 0 && n.RetryPolicy.AttemptBudgetLimit == (models.DelegationBudget{}) && n.RetryPolicy.Deadline == nil
		if zeroPolicy {
			n.RetryPolicy = normalizedRetryPolicy(nil, effectiveDeadline)
		} else {
			n.RetryPolicy = normalizedRetryPolicy(&n.RetryPolicy, effectiveDeadline)
		}
	}
	stamp := formatTime(now)
	rootUpdate, err := tx.ExecContext(ctx, `UPDATE tasks SET reserved_token_count=reserved_token_count+?,reserved_payment_amount=reserved_payment_amount+?,status='running',updated_at=? WHERE task_id=? AND status='accepted' AND budget_currency=? AND budget_token_count-reserved_token_count-consumed_token_count>=? AND budget_payment_amount-reserved_payment_amount-consumed_payment_amount>=?`, in.BudgetEnvelope.TokenCount, in.BudgetEnvelope.PaymentAmount, stamp, in.RootTaskID, in.BudgetEnvelope.Currency, in.BudgetEnvelope.TokenCount, in.BudgetEnvelope.PaymentAmount)
	if err != nil {
		return models.TaskGraph{}, err
	}
	if err = requireOneRow(rootUpdate); err != nil {
		return models.TaskGraph{}, ErrDAGConflict
	}
	_, err = tx.ExecContext(ctx, `INSERT INTO task_graphs(dag_id,tenant_id,root_task_id,status,max_parallelism,deadline,budget_token_count,budget_payment_amount,budget_currency,idempotency_key,request_hash,created_at,updated_at) VALUES(?,?,?,'running',?,?,?,?,?,?,?,?,?)`, in.DAGID, in.TenantID, in.RootTaskID, in.MaxParallelism, timeText(in.Deadline), in.BudgetEnvelope.TokenCount, in.BudgetEnvelope.PaymentAmount, in.BudgetEnvelope.Currency, key, hash, stamp, stamp)
	if err != nil {
		return models.TaskGraph{}, err
	}
	for _, n := range in.Nodes {
		retry, _ := json.Marshal(n.RetryPolicy)
		request, _ := json.Marshal(n.SchedulingRequest)
		status := models.TaskNodeStatusPending
		if len(n.Dependencies) == 0 {
			status = models.TaskNodeStatusReady
		}
		_, err = tx.ExecContext(ctx, `INSERT INTO task_graph_nodes(dag_id,node_id,task_id,tenant_id,status,failure_policy,retry_policy_json,scheduling_request_json,budget_token_count,budget_payment_amount,budget_currency,depth,topo_order,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`, in.DAGID, n.NodeID, n.TaskID, in.TenantID, status, n.FailurePolicy, string(retry), string(request), n.BudgetLimit.TokenCount, n.BudgetLimit.PaymentAmount, n.BudgetLimit.Currency, n.Depth, n.TopoOrder, stamp, stamp)
		if err != nil {
			return models.TaskGraph{}, err
		}
		if status == models.TaskNodeStatusReady {
			_, err = tx.ExecContext(ctx, `INSERT INTO dag_wakeup_outbox(dag_id,node_id,created_at,updated_at) VALUES(?,?,?,?)`, in.DAGID, n.NodeID, stamp, stamp)
			if err != nil {
				return models.TaskGraph{}, err
			}
		}
	}
	for _, e := range edges {
		if _, err = tx.ExecContext(ctx, `INSERT INTO task_graph_edges(dag_id,from_node_id,to_node_id) VALUES(?,?,?)`, in.DAGID, e.FromNodeID, e.ToNodeID); err != nil {
			return models.TaskGraph{}, err
		}
	}
	_, err = tx.ExecContext(ctx, `INSERT INTO task_graph_events(dag_id,event_type,created_at) VALUES(?,'graph_created',?)`, in.DAGID, stamp)
	if err != nil {
		return models.TaskGraph{}, err
	}
	if err = tx.Commit(); err != nil {
		return models.TaskGraph{}, err
	}
	return s.GetGraph(ctx, in.TenantID, in.DAGID)
}

func sameCurrency(a, b models.DelegationBudget) bool { return a.Currency == b.Currency }

func scopeSubset(requested, governed []string) bool {
	allowed := make(map[string]struct{}, len(governed))
	for _, value := range governed {
		allowed[value] = struct{}{}
	}
	for _, value := range requested {
		if _, ok := allowed[value]; !ok {
			return false
		}
	}
	return true
}
func (s *DAGStore) GetGraph(ctx context.Context, tenant, id string) (models.TaskGraph, error) {
	return getGraph(ctx, s.db, tenant, id)
}
func (s *DAGStore) ListGraphs(ctx context.Context, tenant string) ([]models.TaskGraph, error) {
	rows, e := s.db.QueryContext(ctx, `SELECT dag_id FROM task_graphs WHERE tenant_id=? ORDER BY created_at,dag_id`, tenant)
	if e != nil {
		return nil, e
	}
	defer rows.Close()
	var out []models.TaskGraph
	for rows.Next() {
		var id string
		if e = rows.Scan(&id); e != nil {
			return nil, e
		}
		g, e := s.GetGraph(ctx, tenant, id)
		if e != nil {
			return nil, e
		}
		out = append(out, g)
	}
	return out, rows.Err()
}

type graphQuerier interface {
	QueryRowContext(context.Context, string, ...any) *sql.Row
	QueryContext(context.Context, string, ...any) (*sql.Rows, error)
}

func getGraph(ctx context.Context, q graphQuerier, tenant, id string) (models.TaskGraph, error) {
	var g models.TaskGraph
	var dl, terminal sql.NullString
	var created, updated string
	e := q.QueryRowContext(ctx, `SELECT dag_id,tenant_id,root_task_id,status,max_parallelism,deadline,budget_token_count,budget_payment_amount,budget_currency,reserved_token_count,reserved_payment_amount,consumed_token_count,consumed_payment_amount,revision,idempotency_key,request_hash,cancel_reason,created_at,updated_at,terminal_at FROM task_graphs WHERE tenant_id=? AND dag_id=?`, tenant, id).Scan(&g.DAGID, &g.TenantID, &g.RootTaskID, &g.Status, &g.MaxParallelism, &dl, &g.BudgetEnvelope.TokenCount, &g.BudgetEnvelope.PaymentAmount, &g.BudgetEnvelope.Currency, &g.ReservedBudget.TokenCount, &g.ReservedBudget.PaymentAmount, &g.ConsumedBudget.TokenCount, &g.ConsumedBudget.PaymentAmount, &g.Revision, &g.IdempotencyKey, &g.RequestHash, &g.CancelReason, &created, &updated, &terminal)
	if errors.Is(e, sql.ErrNoRows) {
		return g, ErrDAGNotFound
	}
	if e != nil {
		return g, e
	}
	g.ProtocolVersion = models.DAGProtocolVersion
	g.Deadline = parseTimePtr(dl)
	g.TerminalAt = parseTimePtr(terminal)
	g.CreatedAt, _ = time.Parse(time.RFC3339Nano, created)
	g.UpdatedAt, _ = time.Parse(time.RFC3339Nano, updated)
	rows, e := q.QueryContext(ctx, `SELECT node_id,task_id,tenant_id,status,failure_policy,retry_policy_json,scheduling_request_json,budget_token_count,budget_payment_amount,budget_currency,depth,topo_order,revision,created_at,updated_at,terminal_at FROM task_graph_nodes WHERE dag_id=? ORDER BY topo_order,node_id`, id)
	if e != nil {
		return g, e
	}
	defer rows.Close()
	for rows.Next() {
		var n models.TaskNode
		var retry, request, ca, ua string
		var ta sql.NullString
		if e = rows.Scan(&n.NodeID, &n.TaskID, &n.TenantID, &n.Status, &n.FailurePolicy, &retry, &request, &n.BudgetLimit.TokenCount, &n.BudgetLimit.PaymentAmount, &n.BudgetLimit.Currency, &n.Depth, &n.TopoOrder, &n.Revision, &ca, &ua, &ta); e != nil {
			return g, e
		}
		n.DAGID = id
		if e = json.Unmarshal([]byte(retry), &n.RetryPolicy); e != nil {
			return g, e
		}
		if e = json.Unmarshal([]byte(request), &n.SchedulingRequest); e != nil {
			return g, e
		}
		n.CreatedAt, _ = time.Parse(time.RFC3339Nano, ca)
		n.UpdatedAt, _ = time.Parse(time.RFC3339Nano, ua)
		n.TerminalAt = parseTimePtr(ta)
		deps, e := q.QueryContext(ctx, `SELECT from_node_id FROM task_graph_edges WHERE dag_id=? AND to_node_id=? ORDER BY from_node_id`, id, n.NodeID)
		if e != nil {
			return g, e
		}
		for deps.Next() {
			var d string
			if e = deps.Scan(&d); e != nil {
				deps.Close()
				return g, e
			}
			n.Dependencies = append(n.Dependencies, d)
		}
		if e = deps.Err(); e != nil {
			deps.Close()
			return g, e
		}
		deps.Close()
		g.Nodes = append(g.Nodes, n)
	}
	return g, rows.Err()
}
func getGraphTx(ctx context.Context, tx *sql.Tx, tenant, id string) (models.TaskGraph, error) {
	return getGraph(ctx, tx, tenant, id)
}

func (s *DAGStore) AdmitReadyNode(ctx context.Context, tenant, dag, node string, expected int64, now time.Time) (ScheduleAndReserveResult, error) {
	if now.IsZero() {
		now = time.Now().UTC()
	}
	db, e := sql.Open("sqlite", s.path+"?_busy_timeout=5000&_journal_mode=WAL&_foreign_keys=ON&_txlock=immediate")
	if e != nil {
		return ScheduleAndReserveResult{}, e
	}
	defer db.Close()
	db.SetMaxOpenConns(1)
	tx, e := db.BeginTx(ctx, nil)
	if e != nil {
		return ScheduleAndReserveResult{}, e
	}
	defer tx.Rollback()
	var reqJSON, retryJSON string
	var req models.SchedulingRequest
	var retry models.RetryPolicy
	var limit models.DelegationBudget
	var graphStatus string
	var maxp int
	var graphRevision int64
	var reserved, consumed models.DelegationBudget
	e = tx.QueryRowContext(ctx, `SELECT n.scheduling_request_json,n.retry_policy_json,n.budget_token_count,n.budget_payment_amount,n.budget_currency,g.status,g.max_parallelism,g.revision,g.reserved_token_count,g.reserved_payment_amount,g.consumed_token_count,g.consumed_payment_amount,g.budget_currency FROM task_graph_nodes n JOIN task_graphs g ON g.dag_id=n.dag_id WHERE n.dag_id=? AND n.node_id=? AND n.tenant_id=? AND n.status='ready' AND n.revision=?`, dag, node, tenant, expected).Scan(&reqJSON, &retryJSON, &limit.TokenCount, &limit.PaymentAmount, &limit.Currency, &graphStatus, &maxp, &graphRevision, &reserved.TokenCount, &reserved.PaymentAmount, &consumed.TokenCount, &consumed.PaymentAmount, &reserved.Currency)
	if e != nil {
		return ScheduleAndReserveResult{}, ErrDAGConflict
	}
	if graphStatus != "running" {
		return ScheduleAndReserveResult{}, ErrDAGConflict
	}
	var active int
	if e = tx.QueryRowContext(ctx, `SELECT COUNT(*) FROM task_graph_nodes WHERE dag_id=? AND status IN ('running','outcome_unknown')`, dag).Scan(&active); e != nil {
		return ScheduleAndReserveResult{}, e
	}
	if active >= maxp {
		return ScheduleAndReserveResult{}, ErrDAGConflict
	}
	var unmet int
	if e = tx.QueryRowContext(ctx, `SELECT COUNT(*) FROM task_graph_edges e JOIN task_graph_nodes d ON d.dag_id=e.dag_id AND d.node_id=e.from_node_id WHERE e.dag_id=? AND e.to_node_id=? AND d.status<>'completed'`, dag, node).Scan(&unmet); e != nil {
		return ScheduleAndReserveResult{}, e
	}
	if unmet != 0 {
		return ScheduleAndReserveResult{}, ErrDAGConflict
	}
	var envelope models.DelegationBudget
	if e = tx.QueryRowContext(ctx, `SELECT budget_token_count,budget_payment_amount,budget_currency FROM task_graphs WHERE dag_id=?`, dag).Scan(&envelope.TokenCount, &envelope.PaymentAmount, &envelope.Currency); e != nil {
		return ScheduleAndReserveResult{}, e
	}
	if !sameCurrency(envelope, limit) || envelope.TokenCount-reserved.TokenCount-consumed.TokenCount < limit.TokenCount || envelope.PaymentAmount-reserved.PaymentAmount-consumed.PaymentAmount < limit.PaymentAmount {
		return ScheduleAndReserveResult{}, models.ErrDAGBudget
	}
	if e = json.Unmarshal([]byte(reqJSON), &req); e != nil {
		return ScheduleAndReserveResult{}, ErrDAGConflict
	}
	if e = json.Unmarshal([]byte(retryJSON), &retry); e != nil {
		return ScheduleAndReserveResult{}, ErrDAGConflict
	}
	req.BudgetEnvelope = limit
	p := ScheduleAndReserveParams{Request: req, ProtocolVersion: models.CurrentProtocolVersion, AssignmentID: "dag-assignment-" + dag + "-" + node, DeliveryID: "dag-delivery-" + dag + "-" + node, CapacityUnits: 1, ReservationTTL: time.Hour, Now: now}
	hash, e := schedulingRequestHash(p)
	if e != nil {
		return ScheduleAndReserveResult{}, e
	}
	result, e := scheduleAndReserveTx(ctx, tx, s.owner, p, hash)
	if e != nil {
		return result, e
	}
	policy := normalizedRetryPolicy(&retry, req.Deadline)
	policyJSON, _ := json.Marshal(policy)
	if _, e = tx.ExecContext(ctx, `UPDATE task_assignments SET retry_policy_json=? WHERE assignment_id=? AND state='queued'`, string(policyJSON), result.Assignment.AssignmentID); e != nil {
		return result, e
	}
	result.Assignment.RetryPolicy = &policy
	stamp := formatTime(now)
	r, e := tx.ExecContext(ctx, `UPDATE task_graph_nodes SET status='running',revision=revision+1,updated_at=? WHERE dag_id=? AND node_id=? AND status='ready' AND revision=?`, stamp, dag, node, expected)
	if e != nil {
		return result, e
	}
	if n, _ := r.RowsAffected(); n != 1 {
		return result, ErrDAGConflict
	}
	graphUpdate, e := tx.ExecContext(ctx, `UPDATE task_graphs SET reserved_token_count=reserved_token_count+?,reserved_payment_amount=reserved_payment_amount+?,revision=revision+1,updated_at=? WHERE dag_id=? AND status='running' AND budget_currency=? AND revision=? AND budget_token_count-reserved_token_count-consumed_token_count>=? AND budget_payment_amount-reserved_payment_amount-consumed_payment_amount>=?`, limit.TokenCount, limit.PaymentAmount, stamp, dag, limit.Currency, graphRevision, limit.TokenCount, limit.PaymentAmount)
	if e != nil {
		return result, e
	}
	if e = requireOneRow(graphUpdate); e != nil {
		return result, ErrDAGConflict
	}
	_, e = tx.ExecContext(ctx, `INSERT INTO task_graph_events(dag_id,node_id,event_type,created_at) VALUES(?,?,'node_admitted',?)`, dag, node, stamp)
	if e != nil {
		return result, e
	}
	if e = tx.Commit(); e != nil {
		return result, e
	}
	return result, nil
}

func (s *DAGStore) ClaimWakeups(ctx context.Context, owner string, now time.Time, lease time.Duration, limit int) ([]DAGWakeup, error) {
	if limit <= 0 {
		limit = 32
	}
	if lease <= 0 {
		lease = time.Minute
	}
	db, e := sql.Open("sqlite", s.path+"?_busy_timeout=5000&_journal_mode=WAL&_foreign_keys=ON&_txlock=immediate")
	if e != nil {
		return nil, e
	}
	defer db.Close()
	db.SetMaxOpenConns(1)
	tx, e := db.BeginTx(ctx, nil)
	if e != nil {
		return nil, e
	}
	defer tx.Rollback()
	rows, e := tx.QueryContext(ctx, `SELECT dag_id,node_id,revision FROM dag_wakeup_outbox WHERE state='pending' OR (state='claimed' AND claim_expires_at<=?) ORDER BY dag_id,node_id LIMIT ?`, now.UnixNano(), limit)
	if e != nil {
		return nil, e
	}
	var out []DAGWakeup
	for rows.Next() {
		var w DAGWakeup
		if e = rows.Scan(&w.DAGID, &w.NodeID, &w.Revision); e != nil {
			rows.Close()
			return nil, e
		}
		out = append(out, w)
	}
	if e = rows.Err(); e != nil {
		rows.Close()
		return nil, e
	}
	rows.Close()
	for i := range out {
		var tokenBytes [16]byte
		if _, e = rand.Read(tokenBytes[:]); e != nil {
			return nil, e
		}
		token := hex.EncodeToString(tokenBytes[:])
		r, e := tx.ExecContext(ctx, `UPDATE dag_wakeup_outbox SET state='claimed',revision=revision+1,claimed_by=?,claim_token=?,claim_expires_at=?,updated_at=? WHERE dag_id=? AND node_id=? AND revision=? AND (state='pending' OR claim_expires_at<=?)`, owner, token, now.Add(lease).UnixNano(), formatTime(now), out[i].DAGID, out[i].NodeID, out[i].Revision, now.UnixNano())
		if e != nil {
			return nil, e
		}
		n, _ := r.RowsAffected()
		if n == 1 {
			out[i].ClaimToken = token
			out[i].Revision++
		} else {
			out[i].ClaimToken = ""
		}
	}
	if e = tx.Commit(); e != nil {
		return nil, e
	}
	kept := out[:0]
	for _, w := range out {
		if w.ClaimToken != "" {
			kept = append(kept, w)
		}
	}
	return kept, nil
}
func (s *DAGStore) AckWakeup(ctx context.Context, w DAGWakeup) error {
	r, e := s.db.ExecContext(ctx, `UPDATE dag_wakeup_outbox SET state='acked',revision=revision+1,updated_at=? WHERE dag_id=? AND node_id=? AND state='claimed' AND revision=? AND claim_token=?`, formatTime(time.Now()), w.DAGID, w.NodeID, w.Revision, w.ClaimToken)
	if e != nil {
		return e
	}
	if n, _ := r.RowsAffected(); n != 1 {
		return ErrDAGConflict
	}
	return nil
}
func (s *DAGStore) GetNode(ctx context.Context, dag, node string) (models.TaskNode, error) {
	g, e := s.GetGraph(ctx, "", dag)
	if e == ErrDAGNotFound {
		var tenant string
		if e = s.db.QueryRowContext(ctx, `SELECT tenant_id FROM task_graphs WHERE dag_id=?`, dag).Scan(&tenant); e != nil {
			return models.TaskNode{}, e
		}
		g, e = s.GetGraph(ctx, tenant, dag)
	}
	if e != nil {
		return models.TaskNode{}, e
	}
	for _, n := range g.Nodes {
		if n.NodeID == node {
			return n, nil
		}
	}
	return models.TaskNode{}, ErrDAGNotFound
}

func isDAGNodeTx(ctx context.Context, tx *sql.Tx, taskID string) (bool, error) {
	var one int
	err := tx.QueryRowContext(ctx, `SELECT 1 FROM task_graph_nodes WHERE task_id=?`, taskID).Scan(&one)
	if errors.Is(err, sql.ErrNoRows) {
		return false, nil
	}
	return err == nil, err
}

func settleTaskBudgetTx(ctx context.Context, tx *sql.Tx, taskID, status string, consumed models.DelegationBudget, now time.Time) error {
	dagNode, err := isDAGNodeTx(ctx, tx, taskID)
	if err != nil {
		return err
	}
	if dagNode {
		return settleDAGNodeTx(ctx, tx, taskID, status, consumed, now)
	}
	if status == "outcome_unknown" || !isFinalStatus(status) {
		return nil
	}
	return settleParentBudget(ctx, tx, taskID, consumed)
}

func settleDAGNodeTx(ctx context.Context, tx *sql.Tx, taskID, status string, consumed models.DelegationBudget, now time.Time) error {
	var dag, node, current, policy string
	var limit models.DelegationBudget
	if err := tx.QueryRowContext(ctx, `SELECT dag_id,node_id,status,failure_policy,budget_token_count,budget_payment_amount,budget_currency FROM task_graph_nodes WHERE task_id=?`, taskID).Scan(&dag, &node, &current, &policy, &limit.TokenCount, &limit.PaymentAmount, &limit.Currency); err != nil {
		return err
	}
	if current == status || current == "completed" || current == "failed" || current == "cancelled" || current == "skipped" {
		return nil
	}
	if status == "outcome_unknown" {
		result, err := tx.ExecContext(ctx, `UPDATE task_graph_nodes SET status='outcome_unknown',revision=revision+1,updated_at=? WHERE task_id=? AND status='running'`, formatTime(now), taskID)
		if err != nil {
			return err
		}
		if err = requireOneRow(result); err != nil {
			return ErrDAGConflict
		}
		result, err = tx.ExecContext(ctx, `UPDATE task_graphs SET status='outcome_unknown',revision=revision+1,updated_at=? WHERE dag_id=? AND status IN ('running','cancelling','outcome_unknown')`, formatTime(now), dag)
		if err != nil {
			return err
		}
		if err = requireOneRow(result); err != nil {
			return ErrDAGConflict
		}
		return aggregateDAGTx(ctx, tx, dag, now)
	}
	if status != "completed" && status != "failed" && status != "cancelled" {
		return nil
	}
	if consumed.TokenCount < 0 || consumed.PaymentAmount < 0 || consumed.TokenCount > limit.TokenCount || consumed.PaymentAmount > limit.PaymentAmount || ((consumed.TokenCount != 0 || consumed.PaymentAmount != 0) && consumed.Currency != limit.Currency) {
		return models.ErrDAGBudget
	}
	result, err := tx.ExecContext(ctx, `UPDATE task_graph_nodes SET status=?,revision=revision+1,updated_at=?,terminal_at=? WHERE task_id=? AND status IN ('running','outcome_unknown')`, status, formatTime(now), formatTime(now), taskID)
	if err != nil {
		return err
	}
	if err = requireOneRow(result); err != nil {
		return ErrDAGConflict
	}
	result, err = tx.ExecContext(ctx, `UPDATE task_graphs SET reserved_token_count=reserved_token_count-?,reserved_payment_amount=reserved_payment_amount-?,consumed_token_count=consumed_token_count+?,consumed_payment_amount=consumed_payment_amount+?,status=CASE WHEN status='outcome_unknown' THEN 'running' ELSE status END,revision=revision+1,updated_at=? WHERE dag_id=? AND budget_currency=? AND reserved_token_count>=? AND reserved_payment_amount>=? AND consumed_token_count+?<=budget_token_count AND consumed_payment_amount+?<=budget_payment_amount`, limit.TokenCount, limit.PaymentAmount, consumed.TokenCount, consumed.PaymentAmount, formatTime(now), dag, limit.Currency, limit.TokenCount, limit.PaymentAmount, consumed.TokenCount, consumed.PaymentAmount)
	if err != nil {
		return err
	}
	if err = requireOneRow(result); err != nil {
		return models.ErrDAGBudget
	}
	if status == "completed" {
		rows, err := tx.QueryContext(ctx, `SELECT n.node_id FROM task_graph_nodes n WHERE n.dag_id=? AND n.status='pending' AND NOT EXISTS(SELECT 1 FROM task_graph_edges e JOIN task_graph_nodes d ON d.dag_id=e.dag_id AND d.node_id=e.from_node_id WHERE e.dag_id=n.dag_id AND e.to_node_id=n.node_id AND d.status<>'completed') ORDER BY n.node_id`, dag)
		if err != nil {
			return err
		}
		var ids []string
		for rows.Next() {
			var id string
			if err = rows.Scan(&id); err != nil {
				rows.Close()
				return err
			}
			ids = append(ids, id)
		}
		if err = rows.Err(); err != nil {
			rows.Close()
			return err
		}
		rows.Close()
		for _, id := range ids {
			ready, err := tx.ExecContext(ctx, `UPDATE task_graph_nodes SET status='ready',revision=revision+1,updated_at=? WHERE dag_id=? AND node_id=? AND status='pending'`, formatTime(now), dag, id)
			if err != nil {
				return err
			}
			if n, rowsErr := ready.RowsAffected(); rowsErr != nil {
				return rowsErr
			} else if n == 1 {
				if _, err = tx.ExecContext(ctx, `INSERT OR IGNORE INTO dag_wakeup_outbox(dag_id,node_id,created_at,updated_at) VALUES(?,?,?,?)`, dag, id, formatTime(now), formatTime(now)); err != nil {
					return err
				}
			}
		}
	} else {
		var skip sql.Result
		if policy == string(models.TaskFailurePolicyFailFast) {
			skip, err = tx.ExecContext(ctx, `UPDATE task_graph_nodes SET status='skipped',revision=revision+1,updated_at=?,terminal_at=? WHERE dag_id=? AND status IN ('pending','ready')`, formatTime(now), formatTime(now), dag)
		} else {
			skip, err = tx.ExecContext(ctx, `WITH RECURSIVE descendants(id) AS (SELECT to_node_id FROM task_graph_edges WHERE dag_id=? AND from_node_id=? UNION SELECT e.to_node_id FROM task_graph_edges e JOIN descendants d ON e.from_node_id=d.id WHERE e.dag_id=?) UPDATE task_graph_nodes SET status='skipped',revision=revision+1,updated_at=?,terminal_at=? WHERE dag_id=? AND node_id IN descendants AND status IN ('pending','ready')`, dag, node, dag, formatTime(now), formatTime(now), dag)
		}
		if err != nil {
			return err
		}
		if skipped, rowsErr := skip.RowsAffected(); rowsErr != nil {
			return rowsErr
		} else if skipped > 0 {
			tasks, updateErr := tx.ExecContext(ctx, `UPDATE tasks SET status='cancelled',updated_at=?,completed_at=? WHERE task_id IN(SELECT task_id FROM task_graph_nodes WHERE dag_id=? AND status='skipped' AND updated_at=?) AND status='accepted'`, formatTime(now), formatTime(now), dag, formatTime(now))
			if updateErr != nil {
				return updateErr
			}
			if n, rowsErr := tasks.RowsAffected(); rowsErr != nil || n != skipped {
				if rowsErr != nil {
					return rowsErr
				}
				return ErrDAGConflict
			}
			if _, err = tx.ExecContext(ctx, `INSERT INTO task_graph_events(dag_id,node_id,event_type,payload_json,created_at) VALUES(?,?,'nodes_skipped',?,?)`, dag, node, fmt.Sprintf(`{"count":%d}`, skipped), formatTime(now)); err != nil {
				return err
			}
		}
	}
	return aggregateDAGTx(ctx, tx, dag, now)
}

func aggregateDAGTx(ctx context.Context, tx *sql.Tx, dag string, now time.Time) error {
	counts := map[string]int{}
	rows, e := tx.QueryContext(ctx, `SELECT status,COUNT(*) FROM task_graph_nodes WHERE dag_id=? GROUP BY status`, dag)
	if e != nil {
		return e
	}
	for rows.Next() {
		var st string
		var n int
		if e = rows.Scan(&st, &n); e != nil {
			rows.Close()
			return e
		}
		counts[st] = n
	}
	if e = rows.Err(); e != nil {
		rows.Close()
		return e
	}
	rows.Close()
	total := 0
	for _, n := range counts {
		total += n
	}
	active := counts["pending"] + counts["ready"] + counts["running"]
	terminal := ""
	if counts["completed"] == total {
		terminal = "completed"
	} else if counts["outcome_unknown"] > 0 && active == 0 {
		terminal = "outcome_unknown"
	} else if active == 0 && counts["outcome_unknown"] == 0 {
		var gs string
		if e = tx.QueryRowContext(ctx, `SELECT status FROM task_graphs WHERE dag_id=?`, dag).Scan(&gs); e != nil {
			return e
		}
		if gs == "cancelling" {
			terminal = "cancelled"
		} else {
			terminal = "failed"
		}
	}
	if terminal == "" {
		return nil
	}
	var root string
	var env, used models.DelegationBudget
	var current string
	e = tx.QueryRowContext(ctx, `SELECT root_task_id,budget_token_count,budget_payment_amount,budget_currency,consumed_token_count,consumed_payment_amount,status FROM task_graphs WHERE dag_id=?`, dag).Scan(&root, &env.TokenCount, &env.PaymentAmount, &env.Currency, &used.TokenCount, &used.PaymentAmount, &current)
	if e != nil {
		return e
	}
	if current == "completed" || current == "failed" || current == "cancelled" {
		return nil
	}
	graphUpdate, e := tx.ExecContext(ctx, `UPDATE task_graphs SET status=?,revision=revision+1,updated_at=?,terminal_at=CASE WHEN ?='outcome_unknown' THEN NULL ELSE ? END WHERE dag_id=? AND status NOT IN ('completed','failed','cancelled')`, terminal, formatTime(now), terminal, formatTime(now), dag)
	if e != nil {
		return e
	}
	if e = requireOneRow(graphUpdate); e != nil {
		return ErrDAGConflict
	}
	var rootUpdate sql.Result
	if terminal == "outcome_unknown" {
		rootUpdate, e = tx.ExecContext(ctx, `UPDATE tasks SET status='outcome_unknown',updated_at=? WHERE task_id=? AND status IN ('running','outcome_unknown')`, formatTime(now), root)
	} else {
		rootUpdate, e = tx.ExecContext(ctx, `UPDATE tasks SET status=?,reserved_token_count=reserved_token_count-?,reserved_payment_amount=reserved_payment_amount-?,consumed_token_count=consumed_token_count+?,consumed_payment_amount=consumed_payment_amount+?,updated_at=?,completed_at=? WHERE task_id=? AND status IN ('running','outcome_unknown') AND reserved_token_count>=? AND reserved_payment_amount>=?`, terminal, env.TokenCount, env.PaymentAmount, used.TokenCount, used.PaymentAmount, formatTime(now), formatTime(now), root, env.TokenCount, env.PaymentAmount)
	}
	if e != nil {
		return e
	}
	if e = requireOneRow(rootUpdate); e != nil {
		return ErrDAGConflict
	}
	return nil
}

func (s *DAGStore) CancelGraph(ctx context.Context, tenant, dag, reason string, now time.Time) (models.TaskGraph, error) {
	if now.IsZero() {
		now = time.Now().UTC()
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return models.TaskGraph{}, err
	}
	defer tx.Rollback()
	result, err := tx.ExecContext(ctx, `UPDATE task_graphs SET status='cancelling',cancel_reason=?,revision=revision+1,updated_at=? WHERE tenant_id=? AND dag_id=? AND status IN ('running','outcome_unknown')`, reason, formatTime(now), tenant, dag)
	if err != nil {
		return models.TaskGraph{}, err
	}
	if err = requireOneRow(result); err != nil {
		return models.TaskGraph{}, ErrDAGConflict
	}
	rows, err := tx.QueryContext(ctx, `SELECT task_id,status,budget_token_count,budget_payment_amount,budget_currency FROM task_graph_nodes WHERE dag_id=? AND status IN ('pending','ready','running','outcome_unknown') ORDER BY node_id`, dag)
	if err != nil {
		return models.TaskGraph{}, err
	}
	type cancelNode struct {
		taskID, status string
		limit          models.DelegationBudget
	}
	var nodes []cancelNode
	for rows.Next() {
		var n cancelNode
		if err = rows.Scan(&n.taskID, &n.status, &n.limit.TokenCount, &n.limit.PaymentAmount, &n.limit.Currency); err != nil {
			rows.Close()
			return models.TaskGraph{}, err
		}
		nodes = append(nodes, n)
	}
	if err = rows.Err(); err != nil {
		rows.Close()
		return models.TaskGraph{}, err
	}
	rows.Close()
	for _, n := range nodes {
		if n.status == "pending" || n.status == "ready" {
			taskUpdate, updateErr := tx.ExecContext(ctx, `UPDATE tasks SET status='cancelled',updated_at=?,completed_at=? WHERE task_id=? AND status='accepted'`, formatTime(now), formatTime(now), n.taskID)
			if updateErr != nil || requireOneRow(taskUpdate) != nil {
				if updateErr != nil {
					return models.TaskGraph{}, updateErr
				}
				return models.TaskGraph{}, ErrDAGConflict
			}
			nodeUpdate, updateErr := tx.ExecContext(ctx, `UPDATE task_graph_nodes SET status='cancelled',revision=revision+1,updated_at=?,terminal_at=? WHERE task_id=? AND status=?`, formatTime(now), formatTime(now), n.taskID, n.status)
			if updateErr != nil || requireOneRow(nodeUpdate) != nil {
				if updateErr != nil {
					return models.TaskGraph{}, updateErr
				}
				return models.TaskGraph{}, ErrDAGConflict
			}
			continue
		}
		var assignmentID, assignmentState string
		scanErr := tx.QueryRowContext(ctx, `SELECT assignment_id,state FROM task_assignments WHERE task_id=?`, n.taskID).Scan(&assignmentID, &assignmentState)
		uncertain := n.status == "outcome_unknown"
		if errors.Is(scanErr, sql.ErrNoRows) {
			uncertain = true
		} else if scanErr != nil {
			return models.TaskGraph{}, scanErr
		} else {
			uncertain = uncertain || assignmentState == "dispatched" || assignmentState == "executing" || assignmentState == "settled"
			assignmentUpdate, updateErr := tx.ExecContext(ctx, `UPDATE task_assignments SET state=CASE WHEN ? THEN state ELSE 'cancelled' END,revision=revision+1,execution_fence=execution_fence+1,lease_owner='',claim_token='',lease_expires_at=0 WHERE assignment_id=? AND state=?`, uncertain, assignmentID, assignmentState)
			if updateErr != nil || requireOneRow(assignmentUpdate) != nil {
				if updateErr != nil {
					return models.TaskGraph{}, updateErr
				}
				return models.TaskGraph{}, ErrDAGConflict
			}
			reservationState := "released"
			if uncertain {
				reservationState = "uncertain"
			}
			reservationUpdate, updateErr := tx.ExecContext(ctx, `UPDATE agent_capacity_reservations SET state=?,revision=revision+1 WHERE assignment_id=? AND state='held'`, reservationState, assignmentID)
			if updateErr != nil || requireOneRow(reservationUpdate) != nil {
				if updateErr != nil {
					return models.TaskGraph{}, updateErr
				}
				return models.TaskGraph{}, ErrDAGConflict
			}
		}
		nodeStatus := "cancelled"
		if uncertain {
			nodeStatus = "outcome_unknown"
		}
		nodeUpdate, updateErr := tx.ExecContext(ctx, `UPDATE task_graph_nodes SET status=?,revision=revision+1,updated_at=?,terminal_at=CASE WHEN ?='cancelled' THEN ? ELSE NULL END WHERE task_id=? AND status IN ('running','outcome_unknown')`, nodeStatus, formatTime(now), nodeStatus, formatTime(now), n.taskID)
		if updateErr != nil || requireOneRow(nodeUpdate) != nil {
			if updateErr != nil {
				return models.TaskGraph{}, updateErr
			}
			return models.TaskGraph{}, ErrDAGConflict
		}
		taskUpdate, updateErr := tx.ExecContext(ctx, `UPDATE tasks SET status=?,updated_at=?,completed_at=CASE WHEN ?='cancelled' THEN ? ELSE completed_at END WHERE task_id=? AND status IN ('running','outcome_unknown')`, nodeStatus, formatTime(now), nodeStatus, formatTime(now), n.taskID)
		if updateErr != nil || requireOneRow(taskUpdate) != nil {
			if updateErr != nil {
				return models.TaskGraph{}, updateErr
			}
			return models.TaskGraph{}, ErrDAGConflict
		}
		if !uncertain {
			graphBudget, updateErr := tx.ExecContext(ctx, `UPDATE task_graphs SET reserved_token_count=reserved_token_count-?,reserved_payment_amount=reserved_payment_amount-?,revision=revision+1,updated_at=? WHERE dag_id=? AND reserved_token_count>=? AND reserved_payment_amount>=?`, n.limit.TokenCount, n.limit.PaymentAmount, formatTime(now), dag, n.limit.TokenCount, n.limit.PaymentAmount)
			if updateErr != nil || requireOneRow(graphBudget) != nil {
				if updateErr != nil {
					return models.TaskGraph{}, updateErr
				}
				return models.TaskGraph{}, models.ErrDAGBudget
			}
		}
	}
	if _, err = tx.ExecContext(ctx, `INSERT INTO task_graph_events(dag_id,event_type,payload_json,created_at) VALUES(?,'graph_cancel_requested',?,?)`, dag, fmt.Sprintf(`{"reason":%q}`, reason), formatTime(now)); err != nil {
		return models.TaskGraph{}, err
	}
	if err = aggregateDAGTx(ctx, tx, dag, now); err != nil {
		return models.TaskGraph{}, err
	}
	if err = tx.Commit(); err != nil {
		return models.TaskGraph{}, err
	}
	return s.GetGraph(ctx, tenant, dag)
}
