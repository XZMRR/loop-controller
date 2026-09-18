package store

import (
	"context"
	"path/filepath"
	"testing"
	"time"

	"github.com/loop-controller/go/internal/models"
)

func TestTaskSnapshotUsesOneReadTransaction(t *testing.T) {
	ctx := context.Background()
	path := filepath.Join(t.TempDir(), "snapshot.db")
	reader, err := Open(ctx, path)
	if err != nil {
		t.Fatal(err)
	}
	defer reader.Close()
	writer, err := Open(ctx, path)
	if err != nil {
		t.Fatal(err)
	}
	defer writer.Close()
	now := time.Now().UTC()
	task := models.Task{TaskID: "task", SessionID: "s", TenantID: "tenant-a", InitiatorAgentID: "owner", TargetAgentID: "agent", Status: "accepted", CreatedAt: now, UpdatedAt: now}
	if err = reader.TaskStore().Create(ctx, task); err != nil {
		t.Fatal(err)
	}
	if _, err = reader.AssignmentStore().Create(ctx, models.TaskAssignment{AssignmentID: "old", Kind: models.AssignmentKindTargetExecution, TenantID: task.TenantID, TaskID: task.TaskID, AgentID: "agent", DeliveryID: "old-delivery", CreatedAt: now}); err != nil {
		t.Fatal(err)
	}

	written := make(chan error, 1)
	snapshot, err := reader.SnapshotStore().getTaskSnapshot(ctx, task.TenantID, task.TaskID, func() {
		go func() {
			_, writeErr := writer.ExecContext(ctx, `UPDATE task_assignments SET agent_id='new-agent',revision=revision+1,updated_at=? WHERE assignment_id='old'`, formatTime(now.Add(time.Second)))
			written <- writeErr
		}()
		select {
		case writeErr := <-written:
			if writeErr != nil {
				t.Fatal(writeErr)
			}
		case <-time.After(3 * time.Second):
			t.Fatal("concurrent writer did not commit while snapshot transaction was open")
		}
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(snapshot.Assignments) != 1 || snapshot.Assignments[0].Assignment.AgentID != "agent" {
		t.Fatalf("mixed snapshot: %+v", snapshot.Assignments)
	}
	current, err := writer.AssignmentStore().GetForTenant(ctx, task.TenantID, "old")
	if err != nil || current.AgentID != "new-agent" {
		t.Fatalf("writer update missing: %+v %v", current, err)
	}
}

func TestTaskSnapshotIncludesGraphRoutesAndAttemptsWithTenantJoin(t *testing.T) {
	db := openTestDB(t)
	ctx := context.Background()
	now := time.Now().UTC()
	acceptedDAGTask(t, db, "root", "tenant-a", "", 10)
	acceptedDAGTask(t, db, "node-task", "tenant-a", "root", 3)
	graphInput := models.TaskGraphCreate{ProtocolVersion: models.DAGProtocolVersion, DAGID: "dag", TenantID: "tenant-a", RootTaskID: "root", MaxParallelism: 1, BudgetEnvelope: models.DelegationBudget{TokenCount: 3, Currency: "USD"}, Nodes: []models.TaskNode{{NodeID: "node", TaskID: "node-task", FailurePolicy: models.TaskFailurePolicyFailFast, BudgetLimit: models.DelegationBudget{TokenCount: 3, Currency: "USD"}, SchedulingRequest: models.SchedulingRequest{RequestID: "request", TenantID: "tenant-a", TaskID: "node-task", DAGID: "dag", NodeID: "node"}}}}
	if _, err := db.DAGStore().CreateGraph(ctx, graphInput, "key", now); err != nil {
		t.Fatal(err)
	}
	assignment, err := db.AssignmentStore().Create(ctx, models.TaskAssignment{AssignmentID: "route-1", Kind: models.AssignmentKindTargetExecution, TenantID: "tenant-a", TaskID: "node-task", AgentID: "agent", DeliveryID: "delivery", CreatedAt: now})
	if err != nil {
		t.Fatal(err)
	}
	claims, err := db.AssignmentStore().ClaimDue(ctx, ClaimDueParams{Kind: assignment.Kind, Now: now.Add(time.Second), Lease: time.Minute, Limit: 1, Owner: "worker"})
	if err != nil || len(claims) != 1 {
		t.Fatalf("claims=%+v err=%v", claims, err)
	}
	if _, err = db.ExecContext(ctx, `UPDATE execution_attempts SET tenant_id='tenant-b' WHERE assignment_id='route-1'`); err != nil {
		t.Fatal(err)
	}
	snapshot, err := db.SnapshotStore().GetTaskSnapshot(ctx, "tenant-a", "node-task")
	if err != nil {
		t.Fatal(err)
	}
	if snapshot.SchemaVersion != models.TaskSnapshotSchemaVersion || snapshot.ReadAt.IsZero() || snapshot.Graph == nil || snapshot.Graph.DAGID != "dag" || len(snapshot.Graph.Nodes) != 1 || len(snapshot.Assignments) != 1 {
		t.Fatalf("snapshot missing related state: %+v", snapshot)
	}
	if len(snapshot.Assignments[0].Attempts) != 0 {
		t.Fatalf("cross-tenant attempt leaked: %+v", snapshot.Assignments[0].Attempts)
	}
	if _, err = db.SnapshotStore().GetTaskSnapshot(ctx, "tenant-b", "node-task"); err == nil {
		t.Fatal("cross-tenant snapshot unexpectedly succeeded")
	}
}
