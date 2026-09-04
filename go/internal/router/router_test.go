package router

import (
	"context"
	"testing"
	"time"

	"github.com/loop-controller/go/internal/models"
	"github.com/loop-controller/go/internal/registry"
)

type fakeRoutedMessageStore struct {
	messages []models.Message
}

func (f *fakeRoutedMessageStore) Save(_ context.Context, msg models.Message) error {
	f.messages = append(f.messages, msg)
	return nil
}

func (f *fakeRoutedMessageStore) ListByAgent(_ context.Context, agentID string) ([]models.Message, error) {
	var out []models.Message
	for _, msg := range f.messages {
		if msg.FromAgentID == agentID || msg.ToAgentID == agentID {
			out = append(out, msg)
		}
	}
	return out, nil
}

func TestRouteToRegisteredAgent(t *testing.T) {
	reg := registry.New()
	reg.Register(models.AgentCard{AgentID: "agent-b", Name: "B"})
	store := &fakeRoutedMessageStore{}
	r := New(reg, store)
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
	store := &fakeRoutedMessageStore{}
	r := New(reg, store)
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
