package task

import (
	"context"
	"encoding/json"
	"path/filepath"
	"testing"

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
