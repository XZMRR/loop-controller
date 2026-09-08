// Package router routes messages between registered agents.
package router

import (
	"context"
	"errors"

	"github.com/loop-controller/go/internal/models"
	"github.com/loop-controller/go/internal/registry"
)

var ErrTargetAgentNotRegistered = errors.New("target agent not registered")

// RoutedMessageStore is the narrow persistence contract consumed by Router. It
// keeps the same method set as store.RoutedMessageStore without importing the
// store package.
type RoutedMessageStore interface {
	Save(ctx context.Context, msg models.Message) error
	ListByAgent(ctx context.Context, agentID string) ([]models.Message, error)
}

// Router validates and routes messages.
type Router struct {
	registry *registry.Registry
	messages RoutedMessageStore
}

// New creates a Router backed by the given Registry and routed message store.
func New(reg *registry.Registry, store RoutedMessageStore) *Router {
	return &Router{
		registry: reg,
		messages: store,
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
	if err := r.messages.Save(context.Background(), msg); err != nil {
		return models.SendMessageResponse{Accepted: false, Reason: err.Error()}, err
	}
	return models.SendMessageResponse{Accepted: true, Reason: "routed"}, nil
}

// MessagesFor returns all messages routed to or from an agent.
func (r *Router) MessagesFor(agentID string) []models.Message {
	msgs, err := r.messages.ListByAgent(context.Background(), agentID)
	if err != nil {
		return nil
	}
	return msgs
}
