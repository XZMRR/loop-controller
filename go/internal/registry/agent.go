// Package registry maintains the set of registered Agent Cards.
package registry

import (
	"context"
	"database/sql"
	"errors"
	"sync"

	"github.com/loop-controller/go/internal/models"
)

var (
	ErrAgentNotFound = errors.New("agent not found")
	ErrAgentExists   = errors.New("agent already exists")
)

// AgentStore is the narrow persistence contract consumed by Registry. It keeps
// the same method set as store.AgentStore without importing the store package.
type AgentStore interface {
	Upsert(ctx context.Context, card models.AgentCard) error
	Get(ctx context.Context, agentID string) (models.AgentCard, error)
	Delete(ctx context.Context, agentID string) error
	List(ctx context.Context) ([]models.AgentCard, error)
}

// Registry stores AgentCards in memory or in a durable store.
type Registry struct {
	mu     sync.RWMutex
	agents map[string]models.AgentCard
	store  AgentStore
}

// New creates an empty in-memory Registry.
func New() *Registry {
	return &Registry{
		agents: make(map[string]models.AgentCard),
	}
}

// NewStore creates a Registry backed by a durable AgentStore.
func NewStore(s AgentStore) *Registry {
	return &Registry{
		agents: make(map[string]models.AgentCard),
		store:  s,
	}
}

// Register adds or replaces an AgentCard.
func (r *Registry) Register(card models.AgentCard) error {
	if card.AgentID == "" {
		return errors.New("agent_id is required")
	}
	if r.store != nil {
		return r.store.Upsert(context.Background(), card)
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	r.agents[card.AgentID] = card
	return nil
}

// Get returns the AgentCard for the given id.
func (r *Registry) Get(agentID string) (models.AgentCard, error) {
	if r.store != nil {
		card, err := r.store.Get(context.Background(), agentID)
		if err == sql.ErrNoRows {
			return models.AgentCard{}, ErrAgentNotFound
		}
		return card, err
	}
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
	if r.store != nil {
		cards, err := r.store.List(context.Background())
		if err != nil {
			return nil
		}
		return cards
	}
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
	if r.store != nil {
		err := r.store.Delete(context.Background(), agentID)
		if err == sql.ErrNoRows {
			return ErrAgentNotFound
		}
		return err
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if _, ok := r.agents[agentID]; !ok {
		return ErrAgentNotFound
	}
	delete(r.agents, agentID)
	return nil
}
