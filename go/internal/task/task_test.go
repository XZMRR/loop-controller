package task

import (
	"testing"
)

func TestCreateAndGet(t *testing.T) {
	m := New()
	task := m.Create("session-1", "agent-a", "agent-b")
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
	m := New()
	_, err := m.Get("missing")
	if err != ErrTaskNotFound {
		t.Fatalf("expected ErrTaskNotFound, got %v", err)
	}
}

func TestUpdateStatus(t *testing.T) {
	m := New()
	task := m.Create("session-1", "agent-a", "agent-b")
	updated, err := m.UpdateStatus(task.TaskID, "active")
	if err != nil {
		t.Fatalf("update failed: %v", err)
	}
	if updated.Status != "active" {
		t.Errorf("expected status active, got %q", updated.Status)
	}
}

func TestUpdateStatusInvalid(t *testing.T) {
	m := New()
	task := m.Create("session-1", "agent-a", "agent-b")
	_, err := m.UpdateStatus(task.TaskID, "bogus")
	if err != ErrInvalidStatus {
		t.Fatalf("expected ErrInvalidStatus, got %v", err)
	}
}
