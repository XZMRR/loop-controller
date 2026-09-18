package assignmentworker

import (
	"context"
	"errors"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"github.com/loop-controller/go/internal/delegation"
	"github.com/loop-controller/go/internal/models"
	"github.com/loop-controller/go/internal/store"
	"github.com/loop-controller/go/internal/token"
)

type blockingEntrypointClient struct {
	started chan string
	release chan struct{}
	mu      sync.Mutex
	seen    map[string]models.EntrypointTaskRequest
}

func (c *blockingEntrypointClient) Dispatch(ctx context.Context, _ models.AgentEntrypoint, req models.EntrypointTaskRequest) error {
	c.mu.Lock()
	c.seen[req.TaskID] = req
	c.mu.Unlock()
	c.started <- req.TaskID
	select {
	case <-c.release:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

func (*blockingEntrypointClient) Cancel(context.Context, models.AgentEntrypoint, string, string) (bool, error) {
	return false, nil
}

func enqueueOutbound(t *testing.T, db *store.DB, id string) {
	t.Helper()
	now := time.Now().UTC()
	task := models.Task{TaskID: id, SessionID: "s", InitiatorAgentID: "a", TargetAgentID: "b", TenantID: "tenant", Status: "running", CreatedAt: now, UpdatedAt: now}
	_, _, err := db.DelegationDispatchOutboxStore().EnqueueOutboundDelegation(context.Background(), store.OutboundDelegationEnqueue{Task: task, Entrypoint: models.AgentEntrypoint{Type: "http", URL: "http://target"}, Request: models.EntrypointTaskRequest{TaskID: id}, Now: now})
	if err != nil {
		t.Fatal(err)
	}
}

func TestOutboundWorkerDispatchesConcurrentlyRenewsAndCloses(t *testing.T) {
	db, err := store.Open(context.Background(), filepath.Join(t.TempDir(), "outbound.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	enqueueOutbound(t, db, "one")
	enqueueOutbound(t, db, "two")
	client := &blockingEntrypointClient{started: make(chan string, 2), release: make(chan struct{}), seen: make(map[string]models.EntrypointTaskRequest)}
	worker, err := NewOutbound(OutboundConfig{Owner: "worker", Lease: 300 * time.Millisecond, RenewInterval: 50 * time.Millisecond}, db.AssignmentStore(), db.DelegationDispatchOutboxStore(), client)
	if err != nil {
		t.Fatal(err)
	}
	if err = worker.RunOnce(context.Background()); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 2; i++ {
		select {
		case <-client.started:
		case <-time.After(time.Second):
			t.Fatal("dispatches did not start concurrently")
		}
	}
	firstBefore, _ := db.AssignmentStore().Get(context.Background(), "outbound-assignment-one")
	timer := time.NewTimer(125 * time.Millisecond)
	<-timer.C
	firstAfter, _ := db.AssignmentStore().Get(context.Background(), "outbound-assignment-one")
	if firstAfter.Revision <= firstBefore.Revision {
		t.Fatalf("lease was not renewed: before=%d after=%d", firstBefore.Revision, firstAfter.Revision)
	}
	close(client.release)
	wait, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	if err = worker.Wait(wait); err != nil {
		t.Fatal(err)
	}
	for _, id := range []string{"one", "two"} {
		a, _ := db.AssignmentStore().Get(context.Background(), "outbound-assignment-"+id)
		if a.State != models.AssignmentStateSettled || a.ResultStatus != "delivered" {
			t.Fatalf("assignment %s not delivered: %+v", id, a)
		}
	}

	enqueueOutbound(t, db, "cancelled")
	client.release = make(chan struct{})
	if err = worker.RunOnce(context.Background()); err != nil {
		t.Fatal(err)
	}
	select {
	case <-client.started:
	case <-time.After(time.Second):
		t.Fatal("cancellable dispatch did not start")
	}
	worker.Close()
	if err = worker.Wait(wait); err != nil {
		t.Fatal(err)
	}
}

type failoverEntrypointClient struct {
	mu       sync.Mutex
	requests []models.EntrypointTaskRequest
}

func (c *failoverEntrypointClient) Dispatch(_ context.Context, _ models.AgentEntrypoint, req models.EntrypointTaskRequest) error {
	c.mu.Lock()
	c.requests = append(c.requests, req)
	call := len(c.requests)
	c.mu.Unlock()
	if call == 1 {
		return &delegation.DispatchError{Err: errors.New("not sent"), FailureClass: models.FailureClassPreDispatchTransient, Disposition: models.DispatchDispositionConfirmedNotSent}
	}
	return nil
}
func (*failoverEntrypointClient) Cancel(context.Context, models.AgentEntrypoint, string, string) (bool, error) {
	return false, nil
}

func TestOutboundWorkerFailoverUsesNewDeliveryFenceAndRetiresOldOutbox(t *testing.T) {
	ctx := context.Background()
	db, err := store.Open(ctx, filepath.Join(t.TempDir(), "outbound-failover.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	now := time.Now().UTC()
	for _, id := range []string{"agent-a", "agent-b"} {
		workload := ""
		if id == "agent-b" {
			workload = "workload-b"
		}
		spec, e := db.AgentStore().CreateSpec(ctx, models.AgentSpec{TenantID: "tenant", AgentID: id, Name: id, Entrypoint: models.AgentEntrypoint{Type: "http", URL: "http://" + id}, SupportedTools: []string{"echo"}, SupportedProtocolVersions: []string{models.CurrentProtocolVersion}, ExpectedWorkloadID: workload, MaxConcurrency: 1, Schedulable: true, SourceType: "static", SourceID: "test"})
		if e != nil {
			t.Fatal(e)
		}
		_, e = db.AgentStore().HeartbeatStatus(ctx, "tenant", id, 0, models.AgentHeartbeatPatch{ObservedGeneration: spec.Generation, Health: models.AgentHealthHealthy, AvailableCapacity: 1, ExpiresAt: now.Add(time.Hour)}, "test")
		if e != nil {
			t.Fatal(e)
		}
	}
	issuer := token.NewHMACIssuer([]byte("secret"))
	task := models.Task{TaskID: "task", RequestID: "request", SessionID: "s", InitiatorAgentID: "a", TargetAgentID: "agent-a", TenantID: "tenant", Status: "running", AllowedTools: []string{"echo"}, Budget: models.DelegationBudget{TokenCount: 5}, CreatedAt: now, UpdatedAt: now}
	oldToken, err := issuer.Issue(token.DelegationClaims{RequestID: task.RequestID, SessionID: task.SessionID, InitiatorAgentID: task.InitiatorAgentID, TargetAgentID: task.TargetAgentID, ToolName: "echo", TaskID: task.TaskID, AllowedTools: task.AllowedTools, BudgetTokenCount: 5, TenantID: task.TenantID}, time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	task.DelegationToken = oldToken
	_, _, err = db.DelegationDispatchOutboxStore().EnqueueOutboundDelegation(ctx, store.OutboundDelegationEnqueue{Task: task, Entrypoint: models.AgentEntrypoint{Type: "http", URL: "http://agent-a"}, Request: models.EntrypointTaskRequest{RequestID: task.RequestID, SessionID: task.SessionID, InitiatorAgentID: task.InitiatorAgentID, TargetAgentID: task.TargetAgentID, ToolName: "echo", TaskID: "task", AllowedTools: task.AllowedTools, Budget: task.Budget, TenantID: task.TenantID, DelegationToken: oldToken}, Now: now})
	if err != nil {
		t.Fatal(err)
	}
	if _, err = db.ExecContext(ctx, `INSERT INTO agent_capacity_reservations(reservation_id,tenant_id,agent_id,task_id,units,expires_at,created_at,assignment_id,state,revision,release_reason) VALUES(?,?,?,?,?,?,?,?,'held',1,'')`, "initial-reservation", "tenant", "agent-a", "task", 1, now.Add(time.Hour).Format(time.RFC3339Nano), now.Format(time.RFC3339Nano), "outbound-assignment-task"); err != nil {
		t.Fatal(err)
	}
	client := &failoverEntrypointClient{}
	worker, err := NewOutbound(OutboundConfig{Owner: "worker", Lease: time.Second, RenewInterval: 100 * time.Millisecond}, db.AssignmentStore(), db.DelegationDispatchOutboxStore(), client, db.SchedulingStore().WithDelegationTokenIssuer(issuer, time.Hour))
	if err != nil {
		t.Fatal(err)
	}
	if err = worker.RunOnce(ctx); err != nil {
		t.Fatal(err)
	}
	wait, cancel := context.WithTimeout(ctx, time.Second)
	defer cancel()
	if err = worker.Wait(wait); err != nil {
		t.Fatal(err)
	}
	if err = worker.RunOnce(ctx); err != nil {
		t.Fatal(err)
	}
	if err = worker.Wait(wait); err != nil {
		t.Fatal(err)
	}
	client.mu.Lock()
	requests := append([]models.EntrypointTaskRequest(nil), client.requests...)
	client.mu.Unlock()
	if len(requests) != 2 || requests[0].AssignmentID == requests[1].AssignmentID || requests[0].DeliveryID == requests[1].DeliveryID || requests[1].AssignmentFence <= requests[0].AssignmentFence {
		t.Fatalf("requests=%+v", requests)
	}
	claims, err := issuer.Validate(requests[1].DelegationToken)
	storedTask, getErr := db.TaskStore().Get(ctx, "task")
	if err != nil || getErr != nil || requests[1].DelegationToken == oldToken || claims.TargetAgentID != "agent-b" || requests[1].TargetAgentID != claims.TargetAgentID || storedTask.TargetAgentID != claims.TargetAgentID || storedTask.DelegationToken != requests[1].DelegationToken {
		t.Fatalf("claims=%+v request=%+v task=%+v tokenErr=%v taskErr=%v", claims, requests[1], storedTask, err, getErr)
	}
	var oldTerminal string
	if err = db.QueryRow(`SELECT terminal_at FROM delegation_dispatch_outbox WHERE assignment_id=?`, requests[0].AssignmentID).Scan(&oldTerminal); err != nil || oldTerminal == "" {
		t.Fatalf("old terminal=%q err=%v", oldTerminal, err)
	}
	items, err := db.DelegationDispatchOutboxStore().ClaimDue(ctx, time.Now().UTC().Add(time.Minute), time.Minute, 10)
	if err != nil || len(items) != 0 {
		t.Fatalf("legacy claim items=%+v err=%v", items, err)
	}
}

func TestOutboundWorkerMaxConcurrentAcrossRunOnce(t *testing.T) {
	db, err := store.Open(context.Background(), filepath.Join(t.TempDir(), "outbound-limit.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	for _, id := range []string{"one", "two", "three"} {
		enqueueOutbound(t, db, id)
	}
	client := &blockingEntrypointClient{started: make(chan string, 3), release: make(chan struct{}), seen: make(map[string]models.EntrypointTaskRequest)}
	worker, err := NewOutbound(OutboundConfig{Owner: "worker", Lease: time.Second, RenewInterval: 100 * time.Millisecond, ClaimLimit: 3, MaxConcurrent: 1}, db.AssignmentStore(), db.DelegationDispatchOutboxStore(), client)
	if err != nil {
		t.Fatal(err)
	}
	if err = worker.RunOnce(context.Background()); err != nil {
		t.Fatal(err)
	}
	<-client.started
	for i := 0; i < 3; i++ {
		if err = worker.RunOnce(context.Background()); err != nil {
			t.Fatal(err)
		}
	}
	select {
	case id := <-client.started:
		t.Fatalf("started over limit: %s", id)
	case <-time.After(50 * time.Millisecond):
	}
	close(client.release)
	wait, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	if err = worker.Wait(wait); err != nil {
		t.Fatal(err)
	}
	if err = worker.RunOnce(context.Background()); err != nil {
		t.Fatal(err)
	}
	select {
	case <-client.started:
	case <-time.After(time.Second):
		t.Fatal("slot was not released")
	}
	worker.Close()
	_ = worker.Wait(wait)
}

func TestOutboundWorkerRejectsUnsafeRenewInterval(t *testing.T) {
	client := &blockingEntrypointClient{}
	if _, err := NewOutbound(OutboundConfig{Owner: "worker", Lease: time.Second, RenewInterval: 500 * time.Millisecond}, store.AssignmentStore(nil), store.DelegationDispatchOutboxStore(nil), client); err == nil {
		t.Fatal("expected invalid dependencies or renew interval")
	}
}
