package router

import (
	"testing"
	"time"

	"github.com/loop-controller/go/internal/models"
	"github.com/loop-controller/go/internal/registry"
)

func TestRouteToRegisteredAgent(t *testing.T) {
	reg := registry.New()
	reg.Register(models.AgentCard{AgentID: "agent-b", Name: "B"})
	r := New(reg)
	msg := models.Message{
		MessageID:   "msg-1",
		FromAgentID: "agent-a",
		ToAgentID:   "agent-b",
		Role:        "user",
		Parts:       []models.Part{{Type: "text", Text: "hello"}},
		Timestamp:   time.Now().UTC(),
	}
	resp, err := r.Route(msg)
	if err != nil {
		t.Fatalf("route failed: %v", err)
	}
	if !resp.Accepted {
		t.Fatalf("expected accepted, got %v", resp)
	}
	if len(r.MessagesFor("agent-b")) != 1 {
		t.Errorf("expected 1 message for agent-b, got %d", len(r.MessagesFor("agent-b")))
	}
}

func TestRouteToUnknownAgent(t *testing.T) {
	reg := registry.New()
	r := New(reg)
	msg := models.Message{
		MessageID:   "msg-1",
		FromAgentID: "agent-a",
		ToAgentID:   "agent-b",
		Role:        "user",
		Timestamp:   time.Now().UTC(),
	}
	_, err := r.Route(msg)
	if err != ErrTargetAgentNotRegistered {
		t.Fatalf("expected ErrTargetAgentNotRegistered, got %v", err)
	}
}
