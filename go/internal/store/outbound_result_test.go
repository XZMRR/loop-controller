package store

import (
	"context"
	"encoding/json"
	"testing"
	"time"

	"github.com/loop-controller/go/internal/models"
)

func TestCommitOutboundTaskResultSettlesBudgetCapacityAndReplays(t *testing.T) {
	db := openTestDB(t)
	ctx := context.Background()
	now := time.Now().UTC()
	parent := models.Task{TaskID: "outbound-parent", SessionID: "s", InitiatorAgentID: "a", TargetAgentID: "b", TenantID: "tenant", Status: "running", Budget: models.DelegationBudget{TokenCount: 20, Currency: "USD"}, CreatedAt: now, UpdatedAt: now}
	if err := db.TaskStore().Create(ctx, parent); err != nil {
		t.Fatal(err)
	}
	child := models.Task{TaskID: "outbound-child", ParentTaskID: parent.TaskID, SessionID: "s", InitiatorAgentID: "b", TargetAgentID: "c", TenantID: "tenant", Status: "running", Budget: models.DelegationBudget{TokenCount: 10, Currency: "USD"}, CreatedAt: now, UpdatedAt: now}
	_, assignment, err := db.DelegationDispatchOutboxStore().EnqueueOutboundDelegation(ctx, OutboundDelegationEnqueue{Task: child, Entrypoint: models.AgentEntrypoint{Type: "http", URL: "http://target"}, Request: models.EntrypointTaskRequest{TaskID: child.TaskID}, Now: now})
	if err != nil {
		t.Fatal(err)
	}
	createSchedulingAgent(t, db, "c", 1, now)
	if _, err = db.ExecContext(ctx, `INSERT INTO agent_capacity_reservations(reservation_id,tenant_id,agent_id,task_id,units,expires_at,created_at,assignment_id,state,revision,release_reason) VALUES('r','tenant','c',?,1,?,?,?,'held',1,'')`, child.TaskID, formatTime(now.Add(time.Hour)), formatTime(now), assignment.AssignmentID); err != nil {
		t.Fatal(err)
	}
	claims, err := db.AssignmentStore().ClaimDue(ctx, ClaimDueParams{Kind: models.AssignmentKindOutboundDelegation, Now: now.Add(time.Millisecond), Lease: time.Minute, Limit: 1, Owner: "worker"})
	if err != nil || len(claims) != 1 {
		t.Fatalf("claims=%+v err=%v", claims, err)
	}
	dispatched, err := db.AssignmentStore().MarkDispatched(ctx, AssignmentTransition{Kind: claims[0].Kind, AssignmentID: claims[0].AssignmentID, Revision: claims[0].Revision, Attempt: claims[0].Attempt, Fence: claims[0].ExecutionFence, Owner: "worker", ClaimToken: claims[0].ClaimToken, Now: now.Add(2 * time.Millisecond)})
	if err != nil {
		t.Fatal(err)
	}
	_, _, err = db.DelegationDispatchOutboxStore().CommitOutboundDispatchResult(ctx, AssignmentResult{AssignmentTransition: AssignmentTransition{Kind: dispatched.Kind, AssignmentID: dispatched.AssignmentID, Revision: dispatched.Revision, Attempt: dispatched.Attempt, Fence: dispatched.ExecutionFence, Owner: "worker", ClaimToken: dispatched.ClaimToken, Now: now.Add(3 * time.Millisecond)}, Status: "delivered"})
	if err != nil {
		t.Fatal(err)
	}
	outcome := json.RawMessage(`{"ok":true}`)
	consumed := models.DelegationBudget{TokenCount: 3, Currency: "USD"}
	result, err := db.DelegationDispatchOutboxStore().CommitOutboundTaskResult(ctx, child.TaskID, "completed", outcome, "", consumed, now.Add(4*time.Millisecond))
	if err != nil || result.Status != "completed" {
		t.Fatalf("result=%+v err=%v", result, err)
	}
	parent, _ = db.TaskStore().Get(ctx, parent.TaskID)
	if parent.ReservedBudget.TokenCount != 0 || parent.ConsumedBudget.TokenCount != 3 {
		t.Fatalf("parent budget=%+v reserved=%+v", parent.ConsumedBudget, parent.ReservedBudget)
	}
	var reservationState string
	if err = db.QueryRowContext(ctx, `SELECT state FROM agent_capacity_reservations WHERE reservation_id='r'`).Scan(&reservationState); err != nil || reservationState != "released" {
		t.Fatalf("reservation=%q err=%v", reservationState, err)
	}
	var eventCount, lifecycleCount int
	if err = db.QueryRowContext(ctx, `SELECT COUNT(*) FROM events WHERE task_id=?`, child.TaskID).Scan(&eventCount); err != nil {
		t.Fatal(err)
	}
	if err = db.QueryRowContext(ctx, `SELECT COUNT(*) FROM lifecycle_outbox WHERE task_id=?`, child.TaskID).Scan(&lifecycleCount); err != nil {
		t.Fatal(err)
	}
	if _, err = db.DelegationDispatchOutboxStore().CommitOutboundTaskResult(ctx, child.TaskID, "completed", outcome, "", consumed, now.Add(5*time.Millisecond)); err != nil {
		t.Fatalf("idempotent replay: %v", err)
	}
	var replayEvents, replayLifecycle int
	_ = db.QueryRowContext(ctx, `SELECT COUNT(*) FROM events WHERE task_id=?`, child.TaskID).Scan(&replayEvents)
	_ = db.QueryRowContext(ctx, `SELECT COUNT(*) FROM lifecycle_outbox WHERE task_id=?`, child.TaskID).Scan(&replayLifecycle)
	parent, _ = db.TaskStore().Get(ctx, parent.TaskID)
	if replayEvents != eventCount || replayLifecycle != lifecycleCount || parent.ConsumedBudget.TokenCount != 3 || parent.ReservedBudget.TokenCount != 0 {
		t.Fatalf("replay events=%d/%d lifecycle=%d/%d parent=%+v", replayEvents, eventCount, replayLifecycle, lifecycleCount, parent)
	}
	if _, err = db.DelegationDispatchOutboxStore().CommitOutboundTaskResult(ctx, child.TaskID, "failed", outcome, "changed", consumed, now.Add(6*time.Millisecond)); err != ErrAssignmentResultConflict {
		t.Fatalf("different replay=%v", err)
	}
}
