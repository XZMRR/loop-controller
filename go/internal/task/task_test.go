package task

import (
	"context"
	"encoding/json"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/loop-controller/go/internal/models"
	"github.com/loop-controller/go/internal/store"
)

func openTestStore(t *testing.T) store.TaskStore {
	t.Helper()
	db, err := store.Open(context.Background(), filepath.Join(t.TempDir(), "tasks.db"))
	if err != nil {
		t.Fatalf("open store: %v", err)
	}
	t.Cleanup(func() { db.Close() })
	return db.TaskStore()
}

func TestCreateAndGet(t *testing.T) {
	m := New(openTestStore(t))
	task, err := m.CreateReliable("session-1", "agent-a", "agent-b")
	if err != nil {
		t.Fatalf("create failed: %v", err)
	}
	if task.Status != "pending" {
		t.Errorf("expected status pending, got %q", task.Status)
	}
	if task.SessionID != "session-1" {
		t.Errorf("expected session session-1, got %q", task.SessionID)
	}

	got, err := m.Get(task.TaskID)
	if err != nil {
		t.Fatalf("get failed: %v", err)
	}
	if got.TaskID != task.TaskID {
		t.Errorf("expected task id %q, got %q", task.TaskID, got.TaskID)
	}
}

func TestCreateWithDelegationPersistsSecurityBindings(t *testing.T) {
	m := New(openTestStore(t))
	created, err := m.CreateWithDelegation("task-bound", "session-1", "agent-a", "agent-b", "interaction-1", "decision-1", "interaction-1", "", "", "", 0, nil,
		[]string{"echo"}, []string{"read"}, false, models.DelegationBudget{}, "request-1", "tenant-1", "spiffe://example/executor", "instance-1")
	if err != nil {
		t.Fatal(err)
	}
	got, err := m.Get(created.TaskID)
	if err != nil {
		t.Fatal(err)
	}
	if got.RequestID != "request-1" || got.TenantID != "tenant-1" || got.TargetWorkloadID != "spiffe://example/executor" || got.TargetInstanceID != "instance-1" {
		t.Fatalf("delegation security bindings not persisted: %+v", got)
	}
}

func TestGetNotFound(t *testing.T) {
	m := New(openTestStore(t))
	_, err := m.Get("missing")
	if err != ErrTaskNotFound {
		t.Fatalf("expected ErrTaskNotFound, got %v", err)
	}
}

func TestUpdateStatus(t *testing.T) {
	m := New(openTestStore(t))
	task := m.Create("session-1", "agent-a", "agent-b")
	updated, err := m.UpdateStatus(task.TaskID, "accepted")
	if err != nil {
		t.Fatalf("update failed: %v", err)
	}
	if updated.Status != "accepted" {
		t.Errorf("expected status accepted, got %q", updated.Status)
	}
}

func TestUpdateStatusInvalid(t *testing.T) {
	m := New(openTestStore(t))
	task := m.Create("session-1", "agent-a", "agent-b")
	_, err := m.UpdateStatus(task.TaskID, "bogus")
	if err != ErrInvalidStatus {
		t.Fatalf("expected ErrInvalidStatus, got %v", err)
	}
}

func TestUpdateStatusTransition(t *testing.T) {
	m := New(openTestStore(t))
	task := m.Create("session-1", "agent-a", "agent-b")

	invalid := []string{"running", "completed"}
	for _, status := range invalid {
		if _, err := m.UpdateStatus(task.TaskID, status); err != ErrInvalidStatus {
			t.Errorf("expected ErrInvalidStatus for pending->%s, got %v", status, err)
		}
	}

	if _, err := m.UpdateStatus(task.TaskID, "accepted"); err != nil {
		t.Fatalf("pending->accepted failed: %v", err)
	}
	if _, err := m.MarkOutcomeUnknown(task.TaskID); err != ErrInvalidStatus {
		t.Fatalf("expected ErrInvalidStatus for accepted->outcome_unknown, got %v", err)
	}
	if _, err := m.UpdateStatus(task.TaskID, "running"); err != nil {
		t.Fatalf("accepted->running failed: %v", err)
	}
	if _, err := m.Complete(task.TaskID, "completed", nil, ""); err != nil {
		t.Fatalf("running->completed failed: %v", err)
	}
	if _, err := m.Complete(task.TaskID, "failed", nil, ""); err != ErrInvalidStatus {
		t.Fatalf("expected ErrInvalidStatus for completed->failed, got %v", err)
	}
}

func TestPersistenceAcrossManagers(t *testing.T) {
	db, err := store.Open(context.Background(), filepath.Join(t.TempDir(), "tasks.db"))
	if err != nil {
		t.Fatalf("open store: %v", err)
	}
	defer db.Close()

	task := New(db.TaskStore()).Create("session-1", "agent-a", "agent-b")
	got, err := New(db.TaskStore()).Get(task.TaskID)
	if err != nil {
		t.Fatalf("get from fresh manager failed: %v", err)
	}
	if got.TaskID != task.TaskID {
		t.Errorf("expected task id %q, got %q", task.TaskID, got.TaskID)
	}
}

func TestCreateWithID(t *testing.T) {
	m := New(openTestStore(t))
	_, err := m.CreateWithID("task-explicit", "session-1", "agent-a", "agent-b")
	if err != nil {
		t.Fatalf("create with id: %v", err)
	}
	got, err := m.Get("task-explicit")
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	if got.TaskID != "task-explicit" || got.Status != "pending" {
		t.Errorf("unexpected task: %+v", got)
	}
}

func TestConcurrentTransitionUsesCompareAndSet(t *testing.T) {
	m := New(openTestStore(t))
	created := m.Create("session-1", "agent-a", "agent-b")

	firstDone := make(chan struct{})
	results := make(chan error, 2)
	go func() {
		_, err := m.UpdateStatusFrom(created.TaskID, "pending", "accepted")
		results <- err
		close(firstDone)
	}()
	go func() {
		<-firstDone
		_, err := m.UpdateStatusFrom(created.TaskID, "pending", "cancelled")
		results <- err
	}()

	successes := 0
	conflicts := 0
	for range 2 {
		err := <-results
		switch err {
		case nil:
			successes++
		case ErrInvalidStatus:
			conflicts++
		default:
			t.Fatalf("unexpected transition error: %v", err)
		}
	}
	if successes != 1 || conflicts != 1 {
		t.Fatalf("expected one success and one conflict, got successes=%d conflicts=%d", successes, conflicts)
	}
}

func TestComplete(t *testing.T) {
	m := New(openTestStore(t))
	task := m.Create("session-1", "agent-a", "agent-b")
	if _, err := m.UpdateStatus(task.TaskID, "accepted"); err != nil {
		t.Fatalf("accepted: %v", err)
	}
	if _, err := m.UpdateStatus(task.TaskID, "running"); err != nil {
		t.Fatalf("running: %v", err)
	}

	completed, err := m.Complete(task.TaskID, "completed", json.RawMessage(`{"result":"ok"}`), "")
	if err != nil {
		t.Fatalf("complete: %v", err)
	}
	if completed.Status != "completed" || string(completed.Outcome) != `{"result":"ok"}` {
		t.Errorf("unexpected completed task: %+v", completed)
	}
	if completed.CompletedAt == nil {
		t.Error("expected completed_at")
	}

	// Cannot transition completed -> failed.
	if _, err := m.Complete(task.TaskID, "failed", nil, "err"); err != ErrInvalidStatus {
		t.Fatalf("expected ErrInvalidStatus, got %v", err)
	}
}

func TestCompleteFailed(t *testing.T) {
	m := New(openTestStore(t))
	task := m.Create("session-1", "agent-a", "agent-b")
	if _, err := m.UpdateStatus(task.TaskID, "accepted"); err != nil {
		t.Fatalf("accepted: %v", err)
	}
	if _, err := m.UpdateStatus(task.TaskID, "running"); err != nil {
		t.Fatalf("running: %v", err)
	}

	failed, err := m.Complete(task.TaskID, "failed", nil, "tool_execution_failed")
	if err != nil {
		t.Fatalf("complete failed: %v", err)
	}
	if failed.Status != "failed" || failed.ErrorCode != "tool_execution_failed" {
		t.Errorf("unexpected failed task: %+v", failed)
	}
}

func TestCompleteInvalidStatus(t *testing.T) {
	m := New(openTestStore(t))
	task := m.Create("session-1", "agent-a", "agent-b")
	if _, err := m.Complete(task.TaskID, "bogus", nil, ""); err != ErrInvalidStatus {
		t.Fatalf("expected ErrInvalidStatus, got %v", err)
	}
}

func TestCompleteFromOutcomeUnknown(t *testing.T) {
	m := New(openTestStore(t))
	task := m.Create("session-1", "agent-a", "agent-b")
	if _, err := m.UpdateStatus(task.TaskID, "accepted"); err != nil {
		t.Fatalf("accepted: %v", err)
	}
	if _, err := m.UpdateStatus(task.TaskID, "running"); err != nil {
		t.Fatalf("running: %v", err)
	}
	unknown, err := m.MarkOutcomeUnknown(task.TaskID)
	if err != nil {
		t.Fatalf("outcome_unknown: %v", err)
	}
	if unknown.CompletedAt != nil {
		t.Fatal("outcome_unknown must not set completed_at")
	}
	if unknown.IsTerminal() {
		t.Fatal("outcome_unknown must remain recoverable")
	}

	completed, err := m.Complete(task.TaskID, "completed", json.RawMessage(`{"result":"ok"}`), "")
	if err != nil {
		t.Fatalf("complete from outcome_unknown: %v", err)
	}
	if completed.Status != "completed" {
		t.Errorf("expected completed, got %q", completed.Status)
	}
}

type recordingFanout struct {
	mu     sync.Mutex
	events []models.TaskEvent
}

func (f *recordingFanout) PublishCommitted(ev models.TaskEvent) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.events = append(f.events, ev)
}

func (f *recordingFanout) snapshot() []models.TaskEvent {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make([]models.TaskEvent, len(f.events))
	copy(out, f.events)
	return out
}

func TestGeneratedIDIncludesInstancePrefix(t *testing.T) {
	m := New(openTestStore(t)).WithInstanceID("inst-test")
	task, err := m.CreateReliable("session-1", "agent-a", "agent-b")
	if err != nil {
		t.Fatalf("create failed: %v", err)
	}
	if !strings.HasPrefix(task.TaskID, "task-inst-test-") {
		t.Fatalf("task id %q does not include instance prefix", task.TaskID)
	}
}

func TestRecoverExpiredRunningPublishes(t *testing.T) {
	db, err := store.Open(context.Background(), filepath.Join(t.TempDir(), "tasks.db"))
	if err != nil {
		t.Fatalf("open store: %v", err)
	}
	t.Cleanup(func() { db.Close() })

	fanout := &recordingFanout{}
	m := New(db.TaskStore()).WithEventFanout(fanout).WithInstanceID("inst-a")
	task, err := m.CreateReliable("session-1", "agent-a", "agent-b")
	if err != nil {
		t.Fatalf("create: %v", err)
	}
	if _, err := m.UpdateStatus(task.TaskID, "accepted"); err != nil {
		t.Fatalf("accept: %v", err)
	}
	if _, err := m.UpdateStatus(task.TaskID, "running"); err != nil {
		t.Fatalf("run: %v", err)
	}

	expired := time.Now().Add(-time.Minute).UnixNano()
	if _, err := db.ExecContext(context.Background(), `UPDATE tasks SET exec_lease_expires_at = ? WHERE task_id = ?`, expired, task.TaskID); err != nil {
		t.Fatalf("expire lease: %v", err)
	}

	fanout.mu.Lock()
	fanout.events = nil
	fanout.mu.Unlock()

	recovered, err := m.RecoverExpiredRunning(context.Background(), time.Now().UTC(), 10)
	if err != nil {
		t.Fatalf("recover: %v", err)
	}
	if len(recovered) != 1 {
		t.Fatalf("recovered = %d, want 1", len(recovered))
	}

	events := fanout.snapshot()
	if len(events) == 0 {
		t.Fatal("expected recovery fanout event")
	}
	if events[0].EventType != "task_outcome_unknown" || events[0].TaskID != task.TaskID {
		t.Fatalf("unexpected fanout event: %+v", events[0])
	}
}
