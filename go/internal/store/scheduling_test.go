package store

import (
	"context"
	"errors"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"github.com/loop-controller/go/internal/models"
)

func TestScheduleAndReserveConcurrentCapacityPinnedAndIdempotency(t *testing.T) {
	ctx := context.Background()
	path := filepath.Join(t.TempDir(), "schedule.db")
	db1, e := Open(ctx, path)
	if e != nil {
		t.Fatal(e)
	}
	defer db1.Close()
	db2, e := Open(ctx, path)
	if e != nil {
		t.Fatal(e)
	}
	defer db2.Close()
	now := time.Now().UTC()
	createSchedulingAgent(t, db1, "agent-a", 1, now)
	createAcceptedSchedulingTask(t, db1, "task-1", "request-1", now)
	createAcceptedSchedulingTask(t, db1, "task-2", "request-2", now)
	params := func(task, request string) ScheduleAndReserveParams {
		return ScheduleAndReserveParams{Request: models.SchedulingRequest{RequestID: request, TenantID: "tenant", TaskID: task}, PinnedAgentID: "agent-a", AssignmentID: "assignment-" + task, DeliveryID: "delivery-" + task, CapacityUnits: 1, Now: now}
	}
	var wg sync.WaitGroup
	errs := make(chan error, 2)
	for i, item := range []struct {
		db            *DB
		task, request string
	}{{db1, "task-1", "request-1"}, {db2, "task-2", "request-2"}} {
		_ = i
		wg.Add(1)
		go func(x struct {
			db            *DB
			task, request string
		}) {
			defer wg.Done()
			_, err := x.db.SchedulingStore().ScheduleAndReserve(ctx, params(x.task, x.request))
			errs <- err
		}(item)
	}
	wg.Wait()
	close(errs)
	success, capacity := 0, 0
	for err := range errs {
		if err == nil {
			success++
		} else if errors.Is(err, ErrSchedulingCapacity) {
			capacity++
		} else {
			t.Fatalf("unexpected error %v", err)
		}
	}
	if success != 1 || capacity != 1 {
		t.Fatalf("success=%d capacity=%d", success, capacity)
	}
	var winner string
	if e = db1.QueryRow(`SELECT task_id FROM agent_capacity_reservations WHERE state='held'`).Scan(&winner); e != nil {
		t.Fatal(e)
	}
	p := params(winner, map[string]string{"task-1": "request-1", "task-2": "request-2"}[winner])
	first, e := db1.SchedulingStore().ScheduleAndReserve(ctx, p)
	if e != nil {
		t.Fatal(e)
	}
	again, e := db2.SchedulingStore().ScheduleAndReserve(ctx, p)
	if e != nil {
		t.Fatal(e)
	}
	if first.Assignment.AssignmentID != again.Assignment.AssignmentID {
		t.Fatal("idempotent replay changed assignment")
	}
	p.DeliveryID = "different"
	if _, e = db1.SchedulingStore().ScheduleAndReserve(ctx, p); !errors.Is(e, ErrSchedulingIdempotency) {
		t.Fatalf("conflict=%v", e)
	}
	loser := "task-1"
	if winner == loser {
		loser = "task-2"
	}
	lp := params(loser, map[string]string{"task-1": "request-1", "task-2": "request-2"}[loser])
	lp.PinnedAgentID = "missing"
	if _, e = db1.SchedulingStore().ScheduleAndReserve(ctx, lp); !errors.Is(e, ErrSchedulingNoCandidate) {
		t.Fatalf("pinned reroute=%v", e)
	}
}

func TestScheduleUsesAuthoritativeReservationsAndTenantCandidates(t *testing.T) {
	ctx := context.Background()
	db, err := Open(ctx, filepath.Join(t.TempDir(), "tenant-schedule.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	now := time.Now().UTC()
	createSchedulingAgent(t, db, "agent-own", 1, now)
	if _, err = db.Exec(`UPDATE agent_status SET current_load=1000,available_capacity=0 WHERE tenant_id='tenant' AND agent_id='agent-own'`); err != nil {
		t.Fatal(err)
	}
	other, err := db.AgentStore().CreateSpec(ctx, models.AgentSpec{TenantID: "other-tenant", AgentID: "agent-other", Name: "other", MaxConcurrency: 1, Schedulable: true, SourceType: "static", SourceID: "test"})
	if err != nil {
		t.Fatal(err)
	}
	if _, err = db.AgentStore().HeartbeatStatus(ctx, "other-tenant", other.AgentID, 0, models.AgentHeartbeatPatch{ObservedGeneration: other.Generation, Health: models.AgentHealthHealthy, AvailableCapacity: 1, ExpiresAt: now.Add(time.Hour)}, "test"); err != nil {
		t.Fatal(err)
	}
	createAcceptedSchedulingTask(t, db, "task-authoritative", "request-authoritative", now)

	result, err := db.SchedulingStore().ScheduleAndReserve(ctx, ScheduleAndReserveParams{
		Request:       models.SchedulingRequest{RequestID: "request-authoritative", TenantID: "tenant", TaskID: "task-authoritative"},
		AssignmentID:  "assignment-authoritative",
		DeliveryID:    "delivery-authoritative",
		CapacityUnits: 1,
		Now:           now,
	})
	if err != nil {
		t.Fatal(err)
	}
	if result.Decision.SelectedAgentID != "agent-own" {
		t.Fatalf("selected=%q", result.Decision.SelectedAgentID)
	}
	if len(result.Decision.Candidates) != 1 || result.Decision.Candidates[0].AgentID != "agent-own" {
		t.Fatalf("cross-tenant candidates leaked into audit: %+v", result.Decision.Candidates)
	}
}

func TestSchedulePinnedAgentIsTenantFiltered(t *testing.T) {
	ctx := context.Background()
	db, err := Open(ctx, filepath.Join(t.TempDir(), "pinned-tenant.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	now := time.Now().UTC()
	other, err := db.AgentStore().CreateSpec(ctx, models.AgentSpec{TenantID: "other-tenant", AgentID: "agent-other", Name: "other", MaxConcurrency: 1, Schedulable: true, SourceType: "static", SourceID: "test"})
	if err != nil {
		t.Fatal(err)
	}
	if _, err = db.AgentStore().HeartbeatStatus(ctx, "other-tenant", other.AgentID, 0, models.AgentHeartbeatPatch{ObservedGeneration: other.Generation, Health: models.AgentHealthHealthy, AvailableCapacity: 1, ExpiresAt: now.Add(time.Hour)}, "test"); err != nil {
		t.Fatal(err)
	}
	createAcceptedSchedulingTask(t, db, "task-pinned-tenant", "request-pinned-tenant", now)
	_, err = db.SchedulingStore().ScheduleAndReserve(ctx, ScheduleAndReserveParams{
		Request:       models.SchedulingRequest{RequestID: "request-pinned-tenant", TenantID: "tenant", TaskID: "task-pinned-tenant"},
		PinnedAgentID: "agent-other",
		AssignmentID:  "assignment-pinned-tenant",
		DeliveryID:    "delivery-pinned-tenant",
		CapacityUnits: 1,
		Now:           now,
	})
	if !errors.Is(err, ErrSchedulingNoCandidate) {
		t.Fatalf("cross-tenant pinned agent err=%v", err)
	}
}

func TestCancelReleasesReservationAndUncertainResolution(t *testing.T) {
	ctx := context.Background()
	db, e := Open(ctx, filepath.Join(t.TempDir(), "cancel.db"))
	if e != nil {
		t.Fatal(e)
	}
	defer db.Close()
	now := time.Now().UTC()
	createSchedulingAgent(t, db, "agent-a", 1, now)
	createAcceptedSchedulingTask(t, db, "task", "request", now)
	result, e := db.SchedulingStore().ScheduleAndReserve(ctx, ScheduleAndReserveParams{Request: models.SchedulingRequest{RequestID: "request", TenantID: "tenant", TaskID: "task"}, PinnedAgentID: "agent-a", AssignmentID: "assignment-task", DeliveryID: "delivery-task", CapacityUnits: 1, Now: now})
	if e != nil {
		t.Fatal(e)
	}
	task, _, e := db.ExecutionQueueStore().CancelAssignmentAndRelease(ctx, CancelAssignmentParams{Kind: models.AssignmentKindTargetExecution, AssignmentID: result.Assignment.AssignmentID, ExpectedRevision: result.Assignment.Revision, ExpectedAttempt: result.Assignment.Attempt, ExpectedFence: result.Assignment.ExecutionFence, Now: now.Add(time.Second)})
	if e != nil {
		t.Fatal(e)
	}
	if task.Status != "cancelled" {
		t.Fatalf("status=%s", task.Status)
	}
	var state string
	if e = db.QueryRow(`SELECT state FROM agent_capacity_reservations WHERE task_id='task'`).Scan(&state); e != nil || state != "released" {
		t.Fatalf("state=%s err=%v", state, e)
	}
}

func TestFailoverAndReserveIsAtomicAndPreservesRouteHistory(t *testing.T) {
	ctx := context.Background()
	path := filepath.Join(t.TempDir(), "failover.db")
	db, err := Open(ctx, path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	now := time.Now().UTC()
	createSchedulingAgent(t, db, "agent-a", 1, now)
	createSchedulingAgent(t, db, "agent-b", 1, now)
	createAcceptedSchedulingTask(t, db, "task-f", "request-f", now)
	initial, err := db.SchedulingStore().ScheduleAndReserve(ctx, ScheduleAndReserveParams{Request: models.SchedulingRequest{RequestID: "request-f", TenantID: "tenant", TaskID: "task-f"}, PinnedAgentID: "agent-a", AssignmentID: "assignment-f", DeliveryID: "delivery-f", CapacityUnits: 1, Now: now})
	if err != nil {
		t.Fatal(err)
	}
	claims, err := db.AssignmentStore().ClaimDue(ctx, ClaimDueParams{Kind: models.AssignmentKindTargetExecution, Now: now.Add(time.Second), Lease: time.Minute, Limit: 1, Owner: "worker"})
	if err != nil || len(claims) != 1 {
		t.Fatalf("claim=%v err=%v", claims, err)
	}
	claim := claims[0]
	p := FailoverAndReserveParams{AssignmentTransition: AssignmentTransition{Kind: claim.Kind, AssignmentID: claim.AssignmentID, Revision: claim.Revision, Attempt: claim.Attempt, Fence: claim.ExecutionFence, Owner: claim.LeaseOwner, ClaimToken: claim.ClaimToken}, TenantID: "tenant", Request: models.SchedulingRequest{RequestID: "failover-request", TenantID: "tenant", TaskID: "task-f"}, FailureClass: models.FailureClassPreDispatchTransient, DispatchDisposition: "confirmed_not_sent", CapacityUnits: 1, Now: now.Add(2 * time.Second)}
	result, err := db.SchedulingStore().FailoverAndReserve(ctx, p)
	if err != nil {
		t.Fatal(err)
	}
	if result.Assignment.AgentID != "agent-b" || result.Assignment.RouteAttempt != 2 || result.Assignment.SupersedesAssignmentID != initial.Assignment.AssignmentID {
		t.Fatalf("result=%+v", result)
	}
	history, err := db.AssignmentStore().ListTaskAssignments(ctx, "tenant", "task-f")
	if err != nil || len(history) != 2 || history[0].State != models.AssignmentStateSuperseded || history[1].State != models.AssignmentStateQueued {
		t.Fatalf("history=%+v err=%v", history, err)
	}
	if _, err = db.SchedulingStore().FailoverAndReserve(ctx, p); !errors.Is(err, ErrAssignmentClaimLost) {
		t.Fatalf("second failover=%v", err)
	}
	var held, released int
	if err = db.QueryRow(`SELECT SUM(state='held'),SUM(state='released') FROM agent_capacity_reservations WHERE task_id='task-f'`).Scan(&held, &released); err != nil || held != 1 || released != 1 {
		t.Fatalf("held=%d released=%d err=%v", held, released, err)
	}
}

func TestFailoverRestoresTrustedTaskConstraints(t *testing.T) {
	ctx := context.Background()
	db, err := Open(ctx, filepath.Join(t.TempDir(), "trusted-failover.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	now := time.Now().UTC()
	createSchedulingAgent(t, db, "agent-a", 1, now)
	createSchedulingAgent(t, db, "agent-b", 1, now)
	if _, err = db.Exec(`UPDATE agents SET capabilities_json='["required"]',tools_json='["tool"]',expected_workload_id='workload' WHERE agent_id IN ('agent-a','agent-b')`); err != nil {
		t.Fatal(err)
	}
	createAcceptedSchedulingTask(t, db, "trusted-task", "trusted-request", now)
	if _, err = db.Exec(`UPDATE tasks SET allowed_capabilities_json='["required"]',allowed_tools_json='["tool"]',budget_token_count=7,budget_currency='USD',target_workload_id='workload' WHERE task_id='trusted-task'`); err != nil {
		t.Fatal(err)
	}
	_, err = db.SchedulingStore().ScheduleAndReserve(ctx, ScheduleAndReserveParams{Request: models.SchedulingRequest{RequestID: "trusted-request", TenantID: "tenant", TaskID: "trusted-task", RequiredAgentCapabilities: []string{"required"}, RequiredTools: []string{"tool"}, BudgetEnvelope: models.DelegationBudget{TokenCount: 7, Currency: "USD"}}, PinnedAgentID: "agent-a", AssignmentID: "trusted-assignment", DeliveryID: "trusted-delivery", CapacityUnits: 1, Now: now})
	if err != nil {
		t.Fatal(err)
	}
	claim, _ := db.AssignmentStore().ClaimDue(ctx, ClaimDueParams{Kind: models.AssignmentKindTargetExecution, Now: now.Add(time.Second), Lease: time.Minute, Limit: 1, Owner: "worker"})
	result, err := db.SchedulingStore().FailoverAndReserve(ctx, FailoverAndReserveParams{AssignmentTransition: AssignmentTransition{Kind: claim[0].Kind, AssignmentID: claim[0].AssignmentID, Revision: claim[0].Revision, Attempt: claim[0].Attempt, Fence: claim[0].ExecutionFence, Owner: claim[0].LeaseOwner, ClaimToken: claim[0].ClaimToken}, TenantID: "tenant", Request: models.SchedulingRequest{RequestID: "trusted-failover", TaskID: "trusted-task"}, FailureClass: models.FailureClassPreDispatchTransient, DispatchDisposition: "confirmed_not_sent", CapacityUnits: 1, Now: now.Add(2 * time.Second)})
	if err != nil || result.Assignment.AgentID != "agent-b" {
		t.Fatalf("result=%+v err=%v", result, err)
	}
}

func TestConcurrentFailoverOnlyOneSucceeds(t *testing.T) {
	ctx := context.Background()
	path := filepath.Join(t.TempDir(), "concurrent-failover.db")
	db1, err := Open(ctx, path)
	if err != nil {
		t.Fatal(err)
	}
	defer db1.Close()
	db2, err := Open(ctx, path)
	if err != nil {
		t.Fatal(err)
	}
	defer db2.Close()
	now := time.Now().UTC()
	createSchedulingAgent(t, db1, "agent-a", 1, now)
	createSchedulingAgent(t, db1, "agent-b", 1, now)
	createAcceptedSchedulingTask(t, db1, "task-cf", "request-cf", now)
	_, err = db1.SchedulingStore().ScheduleAndReserve(ctx, ScheduleAndReserveParams{Request: models.SchedulingRequest{RequestID: "request-cf", TenantID: "tenant", TaskID: "task-cf"}, PinnedAgentID: "agent-a", AssignmentID: "assignment-cf", DeliveryID: "delivery-cf", CapacityUnits: 1, Now: now})
	if err != nil {
		t.Fatal(err)
	}
	claims, _ := db1.AssignmentStore().ClaimDue(ctx, ClaimDueParams{Kind: models.AssignmentKindTargetExecution, Now: now.Add(time.Second), Lease: time.Minute, Limit: 1, Owner: "worker"})
	a := claims[0]
	p := FailoverAndReserveParams{AssignmentTransition: AssignmentTransition{Kind: a.Kind, AssignmentID: a.AssignmentID, Revision: a.Revision, Attempt: a.Attempt, Fence: a.ExecutionFence, Owner: a.LeaseOwner, ClaimToken: a.ClaimToken}, TenantID: "tenant", Request: models.SchedulingRequest{RequestID: "failover-cf", TaskID: "task-cf"}, FailureClass: models.FailureClassPreDispatchTransient, DispatchDisposition: "confirmed_not_sent", CapacityUnits: 1, Now: now.Add(2 * time.Second)}
	start := make(chan struct{})
	errs := make(chan error, 2)
	var wg sync.WaitGroup
	for _, s := range []*SchedulingStore{db1.SchedulingStore(), db2.SchedulingStore()} {
		wg.Add(1)
		go func(s *SchedulingStore) { defer wg.Done(); <-start; _, e := s.FailoverAndReserve(ctx, p); errs <- e }(s)
	}
	close(start)
	wg.Wait()
	close(errs)
	success, lost := 0, 0
	for e := range errs {
		if e == nil {
			success++
		} else if errors.Is(e, ErrAssignmentClaimLost) {
			lost++
		} else {
			t.Fatalf("unexpected=%v", e)
		}
	}
	if success != 1 || lost != 1 {
		t.Fatalf("success=%d lost=%d", success, lost)
	}
}

func TestFailoverNoCandidateRollsBack(t *testing.T) {
	ctx := context.Background()
	db, err := Open(ctx, filepath.Join(t.TempDir(), "rollback-failover.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	now := time.Now().UTC()
	createSchedulingAgent(t, db, "agent-a", 1, now)
	createAcceptedSchedulingTask(t, db, "task-r", "request-r", now)
	_, err = db.SchedulingStore().ScheduleAndReserve(ctx, ScheduleAndReserveParams{Request: models.SchedulingRequest{RequestID: "request-r", TenantID: "tenant", TaskID: "task-r"}, PinnedAgentID: "agent-a", AssignmentID: "assignment-r", DeliveryID: "delivery-r", CapacityUnits: 1, Now: now})
	if err != nil {
		t.Fatal(err)
	}
	claims, _ := db.AssignmentStore().ClaimDue(ctx, ClaimDueParams{Kind: models.AssignmentKindTargetExecution, Now: now.Add(time.Second), Lease: time.Minute, Limit: 1, Owner: "worker"})
	a := claims[0]
	_, err = db.SchedulingStore().FailoverAndReserve(ctx, FailoverAndReserveParams{AssignmentTransition: AssignmentTransition{Kind: a.Kind, AssignmentID: a.AssignmentID, Revision: a.Revision, Attempt: a.Attempt, Fence: a.ExecutionFence, Owner: a.LeaseOwner, ClaimToken: a.ClaimToken}, TenantID: "tenant", Request: models.SchedulingRequest{RequestID: "failover-r", TaskID: "task-r"}, FailureClass: models.FailureClassPreDispatchTransient, DispatchDisposition: "confirmed_not_sent", CapacityUnits: 1, Now: now.Add(2 * time.Second)})
	if !errors.Is(err, ErrSchedulingNoCandidate) {
		t.Fatalf("err=%v", err)
	}
	current, _ := db.AssignmentStore().Get(ctx, a.AssignmentID)
	if current.State != models.AssignmentStateClaimed || current.ClaimToken != a.ClaimToken {
		t.Fatalf("changed=%+v", current)
	}
}

func createSchedulingAgent(t *testing.T, db *DB, id string, capacity int, now time.Time) {
	t.Helper()
	spec, e := db.AgentStore().CreateSpec(context.Background(), models.AgentSpec{TenantID: "tenant", AgentID: id, Name: id, SupportedProtocolVersions: []string{models.CurrentProtocolVersion}, MaxConcurrency: capacity, Schedulable: true, SourceType: "static", SourceID: "test"})
	if e != nil {
		t.Fatal(e)
	}
	_, e = db.AgentStore().HeartbeatStatus(context.Background(), "tenant", id, 0, models.AgentHeartbeatPatch{ObservedGeneration: spec.Generation, Health: models.AgentHealthHealthy, AvailableCapacity: capacity, ExpiresAt: now.Add(time.Hour)}, "test")
	if e != nil {
		t.Fatal(e)
	}
}
func createAcceptedSchedulingTask(t *testing.T, db *DB, id, request string, now time.Time) {
	t.Helper()
	task := models.Task{TaskID: id, SessionID: "session", InitiatorAgentID: "source", TargetAgentID: "agent-a", TenantID: "tenant", RequestID: request, Status: "pending", CreatedAt: now, UpdatedAt: now}
	if e := db.TaskStore().Create(context.Background(), task); e != nil {
		t.Fatal(e)
	}
	if _, _, e := db.TaskStore().UpdateStatus(context.Background(), id, "pending", "accepted", nil, ""); e != nil {
		t.Fatal(e)
	}
}
