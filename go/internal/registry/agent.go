// Package registry maintains the set of registered Agent Cards.
package registry

import (
	"errors"
	"sync"

	"github.com/loop-controller/go/internal/models"
)

var (
	ErrAgentNotFound = errors.New("agent not found")
	ErrAgentExists   = errors.New("agent already exists")
)

// Registry stores AgentCards in memory.
type Registry struct {
	mu     sync.RWMutex
	agents map[string]models.AgentCard
}

// New creates an empty Registry.
func New() *Registry {
	return &Registry{
		agents: make(map[string]models.AgentCard),
	}
}

// Register adds or replaces an AgentCard.
func (r *Registry) Register(card models.AgentCard) error {
	if card.AgentID == "" {
		return errors.New("agent_id is required")
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	r.agents[card.AgentID] = card
	return nil
}

// Get returns the AgentCard for the given id.
func (r *Registry) Get(agentID string) (models.AgentCard, error) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	card, ok := r.agents[agentID]
	if !ok {
		return models.AgentCard{}, ErrAgentNotFound
	}
	return card, nil
}

// List returns all registered agents.
func (r *Registry) List() []models.AgentCard {
	r.mu.RLock()
	defer r.mu.RUnlock()
	out := make([]models.AgentCard, 0, len(r.agents))
	for _, card := range r.agents {
		out = append(out, card)
	}
	return out
}

// Delete removes an agent from the registry.
func (r *Registry) Delete(agentID string) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	if _, ok := r.agents[agentID]; !ok {
		return ErrAgentNotFound
	}
	delete(r.agents, agentID)
	return nil
}
