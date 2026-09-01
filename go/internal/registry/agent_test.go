package registry

import (
	"testing"

	"github.com/loop-controller/go/internal/models"
)

func TestRegisterAndGet(t *testing.T) {
	r := New()
	card := models.AgentCard{
		AgentID:     "agent-1",
		Name:        "Test Agent",
		Entrypoint:  models.AgentEntrypoint{Type: "http", URL: "http://localhost:8080"},
		Capabilities: []string{"delegate_execution"},
	}
	if err := r.Register(card); err != nil {
		t.Fatalf("register failed: %v", err)
	}

	got, err := r.Get("agent-1")
	if err != nil {
		t.Fatalf("get failed: %v", err)
	}
	if got.Name != "Test Agent" {
		t.Errorf("expected name %q, got %q", "Test Agent", got.Name)
	}
}

func TestGetNotFound(t *testing.T) {
	r := New()
	_, err := r.Get("missing")
	if err != ErrAgentNotFound {
		t.Fatalf("expected ErrAgentNotFound, got %v", err)
	}
}

func TestRegisterEmptyID(t *testing.T) {
	r := New()
	card := models.AgentCard{AgentID: ""}
	if err := r.Register(card); err == nil {
		t.Fatal("expected error for empty agent_id")
	}
}

func TestList(t *testing.T) {
	r := New()
	r.Register(models.AgentCard{AgentID: "a", Name: "A"})
	r.Register(models.AgentCard{AgentID: "b", Name: "B"})
	list := r.List()
	if len(list) != 2 {
		t.Fatalf("expected 2 agents, got %d", len(list))
	}
}

func TestDelete(t *testing.T) {
	r := New()
	r.Register(models.AgentCard{AgentID: "a", Name: "A"})
	if err := r.Delete("a"); err != nil {
		t.Fatalf("delete failed: %v", err)
	}
	if _, err := r.Get("a"); err != ErrAgentNotFound {
		t.Fatalf("expected agent to be deleted")
	}
}
