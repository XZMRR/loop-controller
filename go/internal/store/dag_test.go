package store

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/loop-controller/go/internal/models"
)

func acceptedDAGTask(t *testing.T, db *DB, id, tenant, root string, budget int64) {
	t.Helper()
	now := time.Now().UTC()
	task := models.Task{TaskID: id, SessionID: "s", TenantID: tenant, RootTaskID: root, ParentTaskID: root, Budget: models.DelegationBudget{TokenCount: budget, Currency: "USD"}, InitiatorAgentID: "owner", TargetAgentID: "agent", Status: "accepted", CreatedAt: now, UpdatedAt: now}
	if err := db.TaskStore().Create(context.Background(), task); err != nil {
		t.Fatal(err)
	}
}
func dagCreateFixture() models.TaskGraphCreate {
	return models.TaskGraphCreate{ProtocolVersion: models.DAGProtocolVersion, DAGID: "dag", TenantID: "tenant", RootTaskID: "root", MaxParallelism: 1, BudgetEnvelope: models.DelegationBudget{TokenCount: 8, Currency: "USD"}, Nodes: []models.TaskNode{{NodeID: "a", TaskID: "ta", FailurePolicy: models.TaskFailurePolicyFailFast, BudgetLimit: models.DelegationBudget{TokenCount: 3, Currency: "USD"}, SchedulingRequest: models.SchedulingRequest{RequestID: "ra", TenantID: "tenant", TaskID: "ta", DAGID: "dag", NodeID: "a"}}, {NodeID: "b", TaskID: "tb", Dependencies: []string{"a"}, FailurePolicy: models.TaskFailurePolicyFailFast, BudgetLimit: models.DelegationBudget{TokenCount: 3, Currency: "USD"}, SchedulingRequest: models.SchedulingRequest{RequestID: "rb", TenantID: "tenant", TaskID: "tb", DAGID: "dag", NodeID: "b"}}}}
}
func TestDAGCreateGovernanceIdempotencyAndRollback(t *testing.T) {
	db := openTestDB(t)
	acceptedDAGTask(t, db, "root", "tenant", "", 10)
	acceptedDAGTask(t, db, "ta", "tenant", "root", 3)
	acceptedDAGTask(t, db, "tb", "tenant", "root", 3)
	s := db.DAGStore()
	g, err := s.CreateGraph(context.Background(), dagCreateFixture(), "key", time.Now())
	if err != nil || g.Status != models.GraphStatusRunning || g.Nodes[0].Status != models.TaskNodeStatusReady {
		t.Fatalf("graph=%+v err=%v", g, err)
	}
	replay, err := s.CreateGraph(context.Background(), dagCreateFixture(), "key", time.Now())
	if err != nil || replay.DAGID != "dag" {
		t.Fatal(err)
	}
	bad := dagCreateFixture()
	bad.MaxParallelism = 2
	if _, err = s.CreateGraph(context.Background(), bad, "key", time.Now()); !errors.Is(err, ErrDAGConflict) {
		t.Fatalf("conflict=%v", err)
	}
	root, _ := db.TaskStore().Get(context.Background(), "root")
	if root.ReservedBudget.TokenCount != 8 || root.Status != "running" {
		t.Fatalf("root=%+v", root)
	}
}
func prepareRunningDAG(t *testing.T, db *DB) {
	t.Helper()
	ctx := context.Background()
	acceptedDAGTask(t, db, "root", "tenant", "", 10)
	acceptedDAGTask(t, db, "ta", "tenant", "root", 3)
	acceptedDAGTask(t, db, "tb", "tenant", "root", 3)
	if _, err := db.DAGStore().CreateGraph(ctx, dagCreateFixture(), "key", time.Now()); err != nil {
		t.Fatal(err)
	}
	if _, err := db.ExecContext(ctx, `UPDATE task_graph_nodes SET status='running' WHERE dag_id='dag'`); err != nil {
		t.Fatal(err)
	}
	if _, err := db.ExecContext(ctx, `UPDATE task_graphs SET reserved_token_count=6 WHERE dag_id='dag'`); err != nil {
		t.Fatal(err)
	}
	if _, err := db.ExecContext(ctx, `UPDATE tasks SET status='running' WHERE task_id IN ('ta','tb')`); err != nil {
		t.Fatal(err)
	}
}

func TestDAGFailoverPreservesNodeBudgetAndSettlesNewRouteOnce(t *testing.T) {
	db := openTestDB(t)
	ctx := context.Background()
	now := time.Now().UTC()
	acceptedDAGTask(t, db, "root", "tenant", "", 10)
	acceptedDAGTask(t, db, "ta", "tenant", "root", 3)
	acceptedDAGTask(t, db, "tb", "tenant", "root", 3)
	createSchedulingAgent(t, db, "agent-a", 1, now)
	createSchedulingAgent(t, db, "agent-b", 1, now)
	if _, err := db.ExecContext(ctx, `UPDATE tasks SET request_id=CASE task_id WHEN 'ta' THEN 'ra' ELSE 'rb' END WHERE task_id IN ('ta','tb')`); err != nil {
		t.Fatal(err)
	}
	if _, err := db.ExecContext(ctx, `UPDATE agents SET protocol_capabilities_json='["0.54.0"]' WHERE agent_id IN ('agent-a','agent-b')`); err != nil {
		t.Fatal(err)
	}
	fixture := dagCreateFixture()
	fixture.Nodes[0].RetryPolicy = models.RetryPolicy{MaxAttempts: 3, InitialBackoff: time.Second, MaxBackoff: time.Minute, BackoffMultiplier: 2, RetryableFailureClasses: []models.FailureClass{models.FailureClassPreDispatchTransient}}
	graph, err := db.DAGStore().CreateGraph(ctx, fixture, "key", now)
	if err != nil {
		t.Fatal(err)
	}
	admitted, err := db.DAGStore().AdmitReadyNode(ctx, "tenant", "dag", "a", graph.Nodes[0].Revision, now.Add(time.Millisecond))
	if err != nil {
		t.Fatal(err)
	}
	claims, err := db.AssignmentStore().ClaimDue(ctx, ClaimDueParams{Kind: models.AssignmentKindTargetExecution, Now: now.Add(2 * time.Millisecond), Lease: time.Minute, Limit: 1, Owner: "worker"})
	if err != nil || len(claims) != 1 {
		t.Fatalf("claims=%+v err=%v", claims, err)
	}
	claim, err := db.AssignmentStore().MarkDispatched(ctx, AssignmentTransition{Kind: claims[0].Kind, AssignmentID: claims[0].AssignmentID, Revision: claims[0].Revision, Attempt: claims[0].Attempt, Fence: claims[0].ExecutionFence, Owner: "worker", ClaimToken: claims[0].ClaimToken, Now: now.Add(3 * time.Millisecond)})
	if err != nil {
		t.Fatal(err)
	}
	claim, err = db.AssignmentStore().MarkExecuting(ctx, AssignmentTransition{Kind: claim.Kind, AssignmentID: claim.AssignmentID, Revision: claim.Revision, Attempt: claim.Attempt, Fence: claim.ExecutionFence, Owner: "worker", ClaimToken: claim.ClaimToken, Now: now.Add(4 * time.Millisecond)})
	if err != nil {
		t.Fatal(err)
	}
	before, _ := db.DAGStore().GetGraph(ctx, "tenant", "dag")
	result, err := db.SchedulingStore().FailoverAndReserve(ctx, FailoverAndReserveParams{AssignmentTransition: AssignmentTransition{Kind: claim.Kind, AssignmentID: claim.AssignmentID, Revision: claim.Revision, Attempt: claim.Attempt, Fence: claim.ExecutionFence, Owner: "worker", ClaimToken: claim.ClaimToken}, TenantID: "tenant", Request: models.SchedulingRequest{RequestID: "failover-dag", TenantID: "tenant", TaskID: "ta"}, FailureClass: models.FailureClassPreDispatchTransient, DispatchDisposition: string(models.DispatchDispositionConfirmedNotSent), CapacityUnits: 1, Now: now.Add(5 * time.Millisecond)})
	if err != nil {
		t.Fatal(err)
	}
	after, _ := db.DAGStore().GetGraph(ctx, "tenant", "dag")
	if after.Status != models.GraphStatusRunning || after.Revision != before.Revision || after.ReservedBudget != before.ReservedBudget || after.ConsumedBudget != before.ConsumedBudget || after.Nodes[0].Status != models.TaskNodeStatusRunning || after.Nodes[0].Revision != before.Nodes[0].Revision {
		t.Fatalf("before=%+v after=%+v", before, after)
	}
	if result.Assignment.AssignmentID == admitted.Assignment.AssignmentID || result.Assignment.ExecutionFence <= claim.ExecutionFence {
		t.Fatalf("old=%+v new=%+v", admitted.Assignment, result.Assignment)
	}
	claims, err = db.AssignmentStore().ClaimDue(ctx, ClaimDueParams{Kind: models.AssignmentKindTargetExecution, Now: now.Add(6 * time.Millisecond), Lease: time.Minute, Limit: 1, Owner: "worker"})
	if err != nil || len(claims) != 1 || claims[0].AssignmentID != result.Assignment.AssignmentID {
		t.Fatalf("new claims=%+v err=%v", claims, err)
	}
	newClaim, _ := db.AssignmentStore().MarkDispatched(ctx, AssignmentTransition{Kind: claims[0].Kind, AssignmentID: claims[0].AssignmentID, Revision: claims[0].Revision, Attempt: claims[0].Attempt, Fence: claims[0].ExecutionFence, Owner: "worker", ClaimToken: claims[0].ClaimToken, Now: now.Add(7 * time.Millisecond)})
	newClaim, _ = db.AssignmentStore().MarkExecuting(ctx, AssignmentTransition{Kind: newClaim.Kind, AssignmentID: newClaim.AssignmentID, Revision: newClaim.Revision, Attempt: newClaim.Attempt, Fence: newClaim.ExecutionFence, Owner: "worker", ClaimToken: newClaim.ClaimToken, Now: now.Add(8 * time.Millisecond)})
	settlement := AssignmentResult{AssignmentTransition: AssignmentTransition{Kind: newClaim.Kind, AssignmentID: newClaim.AssignmentID, Revision: newClaim.Revision, Attempt: newClaim.Attempt, Fence: newClaim.ExecutionFence, Owner: "worker", ClaimToken: newClaim.ClaimToken, Now: now.Add(9 * time.Millisecond)}, Status: "completed", ConsumedBudget: models.DelegationBudget{TokenCount: 2, Currency: "USD"}}
	if _, _, err = db.ExecutionQueueStore().CommitExecutionResult(ctx, settlement); err != nil {
		t.Fatal(err)
	}
	settled, _ := db.AssignmentStore().Get(ctx, newClaim.AssignmentID)
	settlement.Revision = settled.Revision
	if _, _, err = db.ExecutionQueueStore().CommitExecutionResult(ctx, settlement); err != nil {
		t.Fatal(err)
	}
	final, _ := db.DAGStore().GetGraph(ctx, "tenant", "dag")
	if final.ReservedBudget.TokenCount != 0 || final.ConsumedBudget.TokenCount != 2 || final.Nodes[0].Status != models.TaskNodeStatusCompleted || final.Nodes[1].Status != models.TaskNodeStatusReady {
		t.Fatalf("final=%+v", final)
	}
}

func TestDAGNodeSettlementDoesNotDoubleSettleRoot(t *testing.T) {
	db := openTestDB(t)
	prepareRunningDAG(t, db)
	ctx := context.Background()
	if _, _, err := db.TaskStore().UpdateStatusWithConsumption(ctx, "ta", "running", "completed", nil, "", models.DelegationBudget{TokenCount: 2, Currency: "USD"}); err != nil {
		t.Fatal(err)
	}
	root, _ := db.TaskStore().Get(ctx, "root")
	graph, _ := db.DAGStore().GetGraph(ctx, "tenant", "dag")
	if root.ReservedBudget.TokenCount != 8 || root.ConsumedBudget.TokenCount != 0 || graph.ReservedBudget.TokenCount != 3 || graph.ConsumedBudget.TokenCount != 2 {
		t.Fatalf("after first node root=%+v graph=%+v", root, graph)
	}
	if _, _, err := db.TaskStore().UpdateStatusWithConsumption(ctx, "tb", "running", "completed", nil, "", models.DelegationBudget{TokenCount: 1, Currency: "USD"}); err != nil {
		t.Fatal(err)
	}
	root, _ = db.TaskStore().Get(ctx, "root")
	graph, _ = db.DAGStore().GetGraph(ctx, "tenant", "dag")
	if root.ReservedBudget.TokenCount != 0 || root.ConsumedBudget.TokenCount != 3 || graph.Status != models.GraphStatusCompleted || graph.ConsumedBudget.TokenCount != 3 {
		t.Fatalf("final root=%+v graph=%+v", root, graph)
	}
}

func TestDAGOutcomeUnknownRequiresRunningNodeAndLateResultRecovers(t *testing.T) {
	db := openTestDB(t)
	prepareRunningDAG(t, db)
	ctx := context.Background()
	if _, err := db.ExecContext(ctx, `UPDATE task_graph_nodes SET status='cancelled',terminal_at=? WHERE task_id='tb'`, formatTime(time.Now())); err != nil {
		t.Fatal(err)
	}
	if _, err := db.ExecContext(ctx, `UPDATE task_graphs SET reserved_token_count=3 WHERE dag_id='dag'`); err != nil {
		t.Fatal(err)
	}
	if _, _, err := db.TaskStore().UpdateStatusWithConsumption(ctx, "ta", "running", "outcome_unknown", nil, "", models.DelegationBudget{}); err != nil {
		t.Fatal(err)
	}
	graph, _ := db.DAGStore().GetGraph(ctx, "tenant", "dag")
	root, _ := db.TaskStore().Get(ctx, "root")
	if graph.Status != models.GraphStatusOutcomeUnknown || root.Status != "outcome_unknown" || root.ReservedBudget.TokenCount != 8 {
		t.Fatalf("unknown graph=%+v root=%+v", graph, root)
	}
	if _, _, err := db.TaskStore().UpdateStatusWithConsumption(ctx, "ta", "outcome_unknown", "completed", nil, "", models.DelegationBudget{TokenCount: 2, Currency: "USD"}); err != nil {
		t.Fatal(err)
	}
	graph, _ = db.DAGStore().GetGraph(ctx, "tenant", "dag")
	root, _ = db.TaskStore().Get(ctx, "root")
	if graph.Status != models.GraphStatusFailed || root.Status != "failed" || root.ReservedBudget.TokenCount != 0 || root.ConsumedBudget.TokenCount != 2 {
		t.Fatalf("late graph=%+v root=%+v", graph, root)
	}
}

func TestDAGOutcomeUnknownZeroRowDoesNotMutateGraph(t *testing.T) {
	db := openTestDB(t)
	prepareRunningDAG(t, db)
	ctx := context.Background()
	if _, err := db.ExecContext(ctx, `UPDATE task_graph_nodes SET status='ready' WHERE task_id='ta'`); err != nil {
		t.Fatal(err)
	}
	if _, _, err := db.TaskStore().UpdateStatusWithConsumption(ctx, "ta", "running", "outcome_unknown", nil, "", models.DelegationBudget{}); !errors.Is(err, ErrDAGConflict) {
		t.Fatalf("err=%v", err)
	}
	graph, _ := db.DAGStore().GetGraph(ctx, "tenant", "dag")
	task, _ := db.TaskStore().Get(ctx, "ta")
	if graph.Status != models.GraphStatusRunning || task.Status != "running" {
		t.Fatalf("graph=%+v task=%+v", graph, task)
	}
}

func TestCancelGraphReleasesAdmittedQueuedNodeReservation(t *testing.T) {
	db := openTestDB(t)
	ctx := context.Background()
	now := time.Now().UTC()
	acceptedDAGTask(t, db, "root", "tenant", "", 10)
	acceptedDAGTask(t, db, "ta", "tenant", "root", 3)
	acceptedDAGTask(t, db, "tb", "tenant", "root", 3)
	createSchedulingAgent(t, db, "agent", 2, now)
	if _, err := db.ExecContext(ctx, `UPDATE tasks SET request_id=CASE task_id WHEN 'ta' THEN 'ra' ELSE 'rb' END WHERE task_id IN ('ta','tb')`); err != nil {
		t.Fatal(err)
	}
	if _, err := db.ExecContext(ctx, `UPDATE agents SET protocol_capabilities_json='["0.54.0"]' WHERE agent_id='agent'`); err != nil {
		t.Fatal(err)
	}
	graph, err := db.DAGStore().CreateGraph(ctx, dagCreateFixture(), "key", now)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = db.DAGStore().AdmitReadyNode(ctx, "tenant", "dag", "a", graph.Nodes[0].Revision, now.Add(time.Millisecond)); err != nil {
		t.Fatal(err)
	}
	graph, err = db.DAGStore().CancelGraph(ctx, "tenant", "dag", "stop", now.Add(2*time.Millisecond))
	if err != nil {
		t.Fatal(err)
	}
	root, _ := db.TaskStore().Get(ctx, "root")
	nodeTask, _ := db.TaskStore().Get(ctx, "ta")
	if graph.Status != models.GraphStatusCancelled || graph.ReservedBudget.TokenCount != 0 || root.ReservedBudget.TokenCount != 0 || nodeTask.Status != "cancelled" {
		t.Fatalf("graph=%+v root=%+v node=%+v", graph, root, nodeTask)
	}
}

func TestClaimWakeupsUsesUniqueTokensPerItem(t *testing.T) {
	db := openTestDB(t)
	ctx := context.Background()
	acceptedDAGTask(t, db, "root", "tenant", "", 10)
	acceptedDAGTask(t, db, "ta", "tenant", "root", 3)
	acceptedDAGTask(t, db, "tb", "tenant", "root", 3)
	g := dagCreateFixture()
	g.Nodes[1].Dependencies = nil
	if _, err := db.DAGStore().CreateGraph(ctx, g, "key", time.Now()); err != nil {
		t.Fatal(err)
	}
	items, err := db.DAGStore().ClaimWakeups(ctx, "owner", time.Now(), time.Minute, 2)
	if err != nil || len(items) != 2 {
		t.Fatalf("items=%+v err=%v", items, err)
	}
	if items[0].ClaimToken == "" || items[0].ClaimToken == items[1].ClaimToken {
		t.Fatalf("tokens not unique: %+v", items)
	}
}

func TestDAGAdmissionRejectsForgedLineageAndScopeAtomically(t *testing.T) {
	cases := []struct {
		name   string
		mutate func(*DB, *models.TaskGraphCreate)
	}{
		{"cross tenant node", func(db *DB, _ *models.TaskGraphCreate) {
			_, _ = db.Exec(`UPDATE tasks SET tenant_id='other' WHERE task_id='ta'`)
		}},
		{"cross initiator node", func(db *DB, _ *models.TaskGraphCreate) {
			_, _ = db.Exec(`UPDATE tasks SET initiator_agent_id='attacker' WHERE task_id='ta'`)
		}},
		{"wrong root linkage", func(db *DB, _ *models.TaskGraphCreate) {
			_, _ = db.Exec(`UPDATE tasks SET root_task_id='other' WHERE task_id='ta'`)
		}},
		{"empty root linkage", func(db *DB, _ *models.TaskGraphCreate) {
			_, _ = db.Exec(`UPDATE tasks SET root_task_id='' WHERE task_id='ta'`)
		}},
		{"empty parent linkage", func(db *DB, _ *models.TaskGraphCreate) {
			_, _ = db.Exec(`UPDATE tasks SET parent_task_id='' WHERE task_id='ta'`)
		}},
		{"tool expansion", func(_ *DB, g *models.TaskGraphCreate) { g.Nodes[0].SchedulingRequest.RequiredTools = []string{"admin"} }},
		{"capability expansion", func(_ *DB, g *models.TaskGraphCreate) {
			g.Nodes[0].SchedulingRequest.RequiredAgentCapabilities = []string{"admin"}
		}},
		{"security expansion", func(_ *DB, g *models.TaskGraphCreate) {
			g.Nodes[0].SchedulingRequest.RequiredSecurityCapabilities = []string{"privileged"}
		}},
		{"trust expansion", func(_ *DB, g *models.TaskGraphCreate) { g.Nodes[0].SchedulingRequest.TrustDomain = "attacker" }},
		{"affinity expansion", func(_ *DB, g *models.TaskGraphCreate) {
			g.Nodes[0].SchedulingRequest.Affinity = map[string]string{"privileged": "true"}
		}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			db := openTestDB(t)
			acceptedDAGTask(t, db, "root", "tenant", "", 10)
			acceptedDAGTask(t, db, "ta", "tenant", "root", 3)
			acceptedDAGTask(t, db, "tb", "tenant", "root", 3)
			if _, err := db.Exec(`UPDATE tasks SET allowed_tools_json='["echo"]',allowed_capabilities_json='["execute"]' WHERE task_id IN ('ta','tb')`); err != nil {
				t.Fatal(err)
			}
			g := dagCreateFixture()
			tc.mutate(db, &g)
			if _, err := db.DAGStore().CreateGraph(context.Background(), g, "key", time.Now()); !errors.Is(err, ErrDAGTaskGovernance) {
				t.Fatalf("err=%v", err)
			}
			root, _ := db.TaskStore().Get(context.Background(), "root")
			if root.Status != "accepted" || root.ReservedBudget.TokenCount != 0 {
				t.Fatalf("root mutated: %+v", root)
			}
			for _, table := range []string{"task_graphs", "task_graph_nodes", "dag_wakeup_outbox", "task_assignments", "agent_capacity_reservations"} {
				var count int
				if err := db.QueryRow(`SELECT COUNT(*) FROM ` + table).Scan(&count); err != nil {
					t.Fatal(err)
				}
				if count != 0 {
					t.Fatalf("%s count=%d", table, count)
				}
			}
		})
	}
}

func TestDAGRejectsUngovernedNodeAtomically(t *testing.T) {
	db := openTestDB(t)
	acceptedDAGTask(t, db, "root", "tenant", "", 10)
	g := dagCreateFixture()
	if _, err := db.DAGStore().CreateGraph(context.Background(), g, "key", time.Now()); !errors.Is(err, ErrDAGTaskGovernance) {
		t.Fatal(err)
	}
	root, _ := db.TaskStore().Get(context.Background(), "root")
	if root.ReservedBudget.TokenCount != 0 || root.Status != "accepted" {
		t.Fatalf("root mutated: %+v", root)
	}
}
