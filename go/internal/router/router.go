// Package router routes messages between registered agents.
package router

import (
	"errors"
	"sync"

	"github.com/loop-controller/go/internal/models"
	"github.com/loop-controller/go/internal/registry"
)

var ErrTargetAgentNotRegistered = errors.New("target agent not registered")

// Router validates and routes messages.
type Router struct {
	registry *registry.Registry
	mu       sync.RWMutex
	messages []models.Message
}

// New creates a Router backed by the given Registry.
func New(reg *registry.Registry) *Router {
	return &Router{
		registry: reg,
		messages: make([]models.Message, 0),
	}
}

// Route checks that the target agent is registered and stores the message.
// In a real implementation this would forward to the target entrypoint.
func (r *Router) Route(msg models.Message) (models.SendMessageResponse, error) {
	if msg.ToAgentID == "" {
		return models.SendMessageResponse{Accepted: false, Reason: "to_agent_id is required"}, nil
	}
	if _, err := r.registry.Get(msg.ToAgentID); err != nil {
		return models.SendMessageResponse{Accepted: false, Reason: err.Error()}, ErrTargetAgentNotRegistered
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	r.messages = append(r.messages, msg)
	return models.SendMessageResponse{Accepted: true, Reason: "routed"}, nil
}

// MessagesFor returns all messages routed to or from an agent.
func (r *Router) MessagesFor(agentID string) []models.Message {
	r.mu.RLock()
	defer r.mu.RUnlock()
	var out []models.Message
	for _, msg := range r.messages {
		if msg.FromAgentID == agentID || msg.ToAgentID == agentID {
			out = append(out, msg)
		}
	}
	return out
}
