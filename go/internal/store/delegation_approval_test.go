package store

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"testing"
	"time"

	"github.com/loop-controller/go/internal/models"
)

func TestApprovalConsumeAtomicallyCreatesTaskBudgetAndDispatch(t *testing.T) {
	db := openTestDB(t)
	ctx := context.Background()
	now := time.Now().UTC()
	parent := models.Task{ProtocolVersion: models.CurrentProtocolVersion, TaskID: "parent-approval", SessionID: "s", InteractionID: "pi", DecisionID: "pd", Budget: models.DelegationBudget{TokenCount: 10, Currency: "USD"}, InitiatorAgentID: "a", TargetAgentID: "b", Status: "running", CreatedAt: now, UpdatedAt: now}
	if err := db.TaskStore().Create(ctx, parent); err != nil {
		t.Fatal(err)
	}
	a := models.DelegationApproval{ProtocolVersion: models.CurrentProtocolVersion, ApprovalID: "approval-1", RequestID: "request-1", DecisionID: "decision-1", RequestHash: "hash", InitiatorAgentID: "b", TargetAgentID: "c", SessionID: "s", ParentTaskID: parent.TaskID, EffectiveArgs: json.RawMessage(`{"x":1}`), AllowedTools: []string{"echo"}, Budget: models.DelegationBudget{TokenCount: 4, Currency: "USD"}, ExpiresAt: now.Add(time.Hour), Status: "approved", CreatedAt: now, UpdatedAt: now, Version: 1}
	if _, _, err := db.DelegationApprovalStore().Create(ctx, a); err != nil {
		t.Fatal(err)
	}
	child := models.Task{ProtocolVersion: models.CurrentProtocolVersion, TaskID: "child-approval", SessionID: "s", InteractionID: "i", DecisionID: "d2", ParentTaskID: parent.TaskID, Budget: a.Budget, InitiatorAgentID: "b", TargetAgentID: "c", Status: "pending", CreatedAt: now, UpdatedAt: now, DelegationToken: "token"}
	dispatch := models.EntrypointTaskRequest{DeliveryID: "delegation-dispatch:approval-1", RequestID: a.RequestID, TaskID: child.TaskID}
	consumed, err := db.DelegationApprovalStore().Consume(ctx, a.ApprovalID, a.Version, child, models.AgentEntrypoint{Type: "http", URL: "http://target"}, dispatch)
	if err != nil {
		t.Fatal(err)
	}
	if consumed.Status != "consumed" || consumed.TaskID != child.TaskID {
		t.Fatalf("unexpected approval: %+v", consumed)
	}
	storedParent, _ := db.TaskStore().Get(ctx, parent.TaskID)
	if storedParent.ReservedBudget.TokenCount != 4 {
		t.Fatalf("reserved=%d", storedParent.ReservedBudget.TokenCount)
	}
	items, err := db.DelegationDispatchOutboxStore().ClaimDue(ctx, now.Add(time.Second), time.Minute, 10)
	if err != nil || len(items) != 1 || items[0].Request.DeliveryID != dispatch.DeliveryID {
		t.Fatalf("outbox=%+v err=%v", items, err)
	}
	again, err := db.DelegationApprovalStore().Consume(ctx, a.ApprovalID, a.Version, child, models.AgentEntrypoint{}, dispatch)
	if err != nil || again.TaskID != child.TaskID {
		t.Fatalf("idempotent consume: %+v %v", again, err)
	}
}

func TestApprovalAuditOutboxExcludesEffectiveArguments(t *testing.T) {
	db := openTestDB(t)
	ctx := context.Background()
	now := time.Now().UTC()
	a := models.DelegationApproval{ApprovalID: "approval-audit", RequestID: "request-audit", DecisionID: "decision-audit", RequestHash: "digest", InitiatorAgentID: "a", TargetAgentID: "b", EffectiveArgs: json.RawMessage(`{"password":"secret"}`), ExpiresAt: now.Add(time.Hour), Status: "pending", CreatedAt: now, UpdatedAt: now, Version: 1}
	if _, _, err := db.DelegationApprovalStore().Create(ctx, a); err != nil {
		t.Fatal(err)
	}
	items, err := db.ApprovalAuditOutboxStore().ClaimDue(ctx, now.Add(time.Second), time.Minute, 10)
	if err != nil || len(items) != 1 {
		t.Fatalf("audit items=%+v err=%v", items, err)
	}
	encoded, _ := json.Marshal(items[0].Approval)
	if string(encoded) == "" || json.Valid(encoded) == false {
		t.Fatalf("invalid audit payload: %s", encoded)
	}
	if bytes.Contains(encoded, []byte("secret")) || bytes.Contains(encoded, []byte("password")) || len(items[0].Approval.EffectiveArgs) != 0 {
		t.Fatalf("approval audit leaked effective arguments: %s", encoded)
	}
}

func TestApprovalTransitionCASExpiryAndReplay(t *testing.T) {
	db := openTestDB(t)
	ctx := context.Background()
	now := time.Now().UTC()
	store := db.DelegationApprovalStore()
	seed := func(id string, expires time.Time) models.DelegationApproval {
		a := models.DelegationApproval{ApprovalID: id, RequestID: "request-" + id, DecisionID: "decision-" + id, RequestHash: "hash-" + id, InitiatorAgentID: "a", TargetAgentID: "b", EffectiveArgs: json.RawMessage(`{}`), ExpiresAt: expires, Status: "pending", CreatedAt: now, UpdatedAt: now, Version: 1}
		if _, _, err := store.Create(ctx, a); err != nil {
			t.Fatal(err)
		}
		return a
	}
	a := seed("cas", now.Add(time.Hour))
	approved, err := store.Transition(ctx, a.ApprovalID, a.Version, "pending", "approved", "reviewer", "ok", "action-1")
	if err != nil || approved.ApproverID != "reviewer" {
		t.Fatalf("approve=%+v err=%v", approved, err)
	}
	replayed, err := store.Transition(ctx, a.ApprovalID, a.Version, "pending", "approved", "reviewer", "ok", "action-1")
	if err != nil || replayed.Status != "approved" {
		t.Fatalf("replay=%+v err=%v", replayed, err)
	}
	if _, err := store.Transition(ctx, a.ApprovalID, a.Version, "pending", "approved", "reviewer", "changed", "action-1"); !errors.Is(err, ErrApprovalConflict) {
		t.Fatalf("changed replay err=%v", err)
	}
	if _, err := store.Transition(ctx, a.ApprovalID, approved.Version, "approved", "cancelled", "a", "", ""); !errors.Is(err, ErrApprovalConflict) {
		t.Fatalf("missing replay key err=%v", err)
	}
	expired := seed("expired", now.Add(-time.Second))
	count, err := store.ExpireDue(ctx, now, 10)
	if err != nil || count != 1 {
		t.Fatalf("expire count=%d err=%v", count, err)
	}
	got, _ := store.Get(ctx, expired.ApprovalID)
	if got.Status != "expired" {
		t.Fatalf("status=%s", got.Status)
	}
}

func TestApprovalConsumeRejectsDifferentTaskReplay(t *testing.T) {
	db := openTestDB(t)
	ctx := context.Background()
	now := time.Now().UTC()
	a := models.DelegationApproval{ApprovalID: "approval-task-replay", RequestID: "request-task-replay", DecisionID: "decision-task-replay", RequestHash: "h", InitiatorAgentID: "a", TargetAgentID: "b", EffectiveArgs: json.RawMessage(`{}`), ExpiresAt: now.Add(time.Hour), Status: "approved", CreatedAt: now, UpdatedAt: now, Version: 1}
	store := db.DelegationApprovalStore()
	if _, _, err := store.Create(ctx, a); err != nil {
		t.Fatal(err)
	}
	first := models.Task{ProtocolVersion: models.CurrentProtocolVersion, TaskID: "task-first", InteractionID: "i", DecisionID: "d", InitiatorAgentID: "a", TargetAgentID: "b", Status: "pending", CreatedAt: now, UpdatedAt: now}
	dispatch := models.EntrypointTaskRequest{DeliveryID: "delegation-dispatch:" + a.ApprovalID, TaskID: first.TaskID}
	if _, err := store.Consume(ctx, a.ApprovalID, 1, first, models.AgentEntrypoint{}, dispatch); err != nil {
		t.Fatal(err)
	}
	second := first
	second.TaskID = "task-second"
	if _, err := store.Consume(ctx, a.ApprovalID, 1, second, models.AgentEntrypoint{}, dispatch); !errors.Is(err, ErrApprovalConflict) {
		t.Fatalf("different task replay err=%v", err)
	}
}

func TestDispatchOutboxClaimTokenFencesStaleAck(t *testing.T) {
	db := openTestDB(t)
	ctx := context.Background()
	now := time.Now().UTC()
	// Seed through the atomic consumer to preserve foreign-key and invariant coverage.
	a := models.DelegationApproval{ApprovalID: "approval-fence", RequestID: "request-fence", DecisionID: "decision-fence", RequestHash: "h", InitiatorAgentID: "a", TargetAgentID: "b", EffectiveArgs: json.RawMessage(`{}`), ExpiresAt: now.Add(time.Hour), Status: "approved", CreatedAt: now, UpdatedAt: now, Version: 1}
	if _, _, err := db.DelegationApprovalStore().Create(ctx, a); err != nil {
		t.Fatal(err)
	}
	task := models.Task{ProtocolVersion: models.CurrentProtocolVersion, TaskID: "task-fence", InteractionID: "i", DecisionID: "d", InitiatorAgentID: "a", TargetAgentID: "b", Status: "pending", CreatedAt: now, UpdatedAt: now}
	if _, err := db.DelegationApprovalStore().Consume(ctx, a.ApprovalID, 1, task, models.AgentEntrypoint{Type: "http", URL: "http://target"}, models.EntrypointTaskRequest{DeliveryID: "delegation-dispatch:approval-fence", TaskID: task.TaskID}); err != nil {
		t.Fatal(err)
	}
	outbox := db.DelegationDispatchOutboxStore()
	first, _ := outbox.ClaimDue(ctx, now.Add(time.Second), time.Millisecond, 1)
	second, _ := outbox.ClaimDue(ctx, now.Add(2*time.Second), time.Minute, 1)
	if len(first) != 1 || len(second) != 1 || first[0].ClaimToken == second[0].ClaimToken {
		t.Fatalf("claims: %+v %+v", first, second)
	}
	if err := outbox.MarkDelivered(ctx, first[0].DeliveryID, first[0].ClaimToken, now); !errors.Is(err, ErrDispatchClaimLost) {
		t.Fatalf("stale ack=%v", err)
	}
	if err := outbox.MarkDelivered(ctx, second[0].DeliveryID, second[0].ClaimToken, now); err != nil {
		t.Fatal(err)
	}
}
