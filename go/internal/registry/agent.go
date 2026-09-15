// Package registry maintains the set of registered Agent Cards.
package registry

import (
	"context"
	"database/sql"
	"errors"
	"sync"
	"time"

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
	return r.GetContext(context.Background(), "", agentID)
}

func (r *Registry) GetContext(ctx context.Context, tenant, agentID string) (models.AgentCard, error) {
	if r.store != nil {
		if tenant != "" {
			s, ok := r.store.(interface {
				GetForTenant(context.Context, string, string) (models.AgentCard, error)
			})
			if !ok {
				return models.AgentCard{}, ErrAgentNotFound
			}
			card, err := s.GetForTenant(ctx, tenant, agentID)
			if err == sql.ErrNoRows {
				return models.AgentCard{}, ErrAgentNotFound
			}
			return card, err
		}
		card, err := r.store.Get(context.Background(), agentID)
		if err == sql.ErrNoRows {
			return models.AgentCard{}, ErrAgentNotFound
		}
		return card, err
	}
	r.mu.RLock()
	defer r.mu.RUnlock()
	card, ok := r.agents[agentID]
	if !ok || (tenant != "" && card.TenantID != tenant) {
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
// ListContext is the context-aware legacy list and never hides store failures.
func (r *Registry) ListContext(ctx context.Context, tenant string) ([]models.AgentCard, error) {
	if r.store != nil {
		if tenant != "" {
			s, ok := r.store.(interface {
				ListForTenant(context.Context, string) ([]models.AgentCard, error)
			})
			if !ok {
				return nil, ErrAgentNotFound
			}
			return s.ListForTenant(ctx, tenant)
		}
		return r.store.List(ctx)
	}
	cards := r.List()
	if tenant == "" {
		return cards, nil
	}
	out := make([]models.AgentCard, 0, len(cards))
	for _, card := range cards {
		if card.TenantID == tenant {
			out = append(out, card)
		}
	}
	return out, nil
}

// SpecStore is implemented by the v2 durable registry store.
type SpecStore interface {
	CreateSpec(context.Context, models.AgentSpec) (models.AgentSpec, error)
	UpdateSpec(context.Context, models.AgentSpec, int64) (models.AgentSpec, error)
	GetSpec(context.Context, string, string) (models.AgentSpec, error)
	ListSpecs(context.Context, string) ([]models.AgentSpec, error)
	GetStatus(context.Context, string, string) (models.AgentStatus, error)
	HeartbeatStatus(context.Context, string, string, int64, models.AgentHeartbeatPatch, string) (models.AgentStatus, error)
	SetDraining(context.Context, string, string, int64, bool, string) (models.AgentStatus, error)
	SyncSource(context.Context, models.DiscoverySnapshot) error
	MarkSourceStale(context.Context, string, string, string, error) error
	ExpireStatuses(context.Context, time.Time) (int64, error)
}

func (r *Registry) SupportsAgentSpecs() bool { _, ok := r.store.(SpecStore); return ok }
func (r *Registry) specStore() (SpecStore, error) {
	s, ok := r.store.(SpecStore)
	if !ok {
		return nil, errors.New("registry store does not support agent specs")
	}
	return s, nil
}
func (r *Registry) CreateSpec(ctx context.Context, x models.AgentSpec) (models.AgentSpec, error) {
	s, e := r.specStore()
	if e != nil {
		return x, e
	}
	return s.CreateSpec(ctx, x)
}
func (r *Registry) UpdateSpec(ctx context.Context, x models.AgentSpec, rv int64) (models.AgentSpec, error) {
	s, e := r.specStore()
	if e != nil {
		return x, e
	}
	return s.UpdateSpec(ctx, x, rv)
}
func (r *Registry) GetSpec(ctx context.Context, t, id string) (models.AgentSpec, error) {
	s, e := r.specStore()
	if e != nil {
		return models.AgentSpec{}, e
	}
	x, e := s.GetSpec(ctx, t, id)
	if e == sql.ErrNoRows {
		return x, ErrAgentNotFound
	}
	return x, e
}
func (r *Registry) ListSpecs(ctx context.Context, t string) ([]models.AgentSpec, error) {
	s, e := r.specStore()
	if e != nil {
		return nil, e
	}
	return s.ListSpecs(ctx, t)
}
func (r *Registry) GetStatus(ctx context.Context, t, id string) (models.AgentStatus, error) {
	s, e := r.specStore()
	if e != nil {
		return models.AgentStatus{}, e
	}
	return s.GetStatus(ctx, t, id)
}
func (r *Registry) HeartbeatStatus(ctx context.Context, t, id string, rv int64, p models.AgentHeartbeatPatch, by string) (models.AgentStatus, error) {
	s, e := r.specStore()
	if e != nil {
		return models.AgentStatus{}, e
	}
	return s.HeartbeatStatus(ctx, t, id, rv, p, by)
}
func (r *Registry) SetDraining(ctx context.Context, t, id string, rv int64, d bool, by string) (models.AgentStatus, error) {
	s, e := r.specStore()
	if e != nil {
		return models.AgentStatus{}, e
	}
	return s.SetDraining(ctx, t, id, rv, d, by)
}
func (r *Registry) SyncSource(ctx context.Context, x models.DiscoverySnapshot) error {
	s, e := r.specStore()
	if e != nil {
		return e
	}
	return s.SyncSource(ctx, x)
}
func (r *Registry) MarkSourceStale(ctx context.Context, t, typ, id string, cause error) error {
	s, e := r.specStore()
	if e != nil {
		return e
	}
	return s.MarkSourceStale(ctx, t, typ, id, cause)
}
func (r *Registry) ExpireStatuses(ctx context.Context, now time.Time) (int64, error) {
	s, e := r.specStore()
	if e != nil {
		return 0, e
	}
	return s.ExpireStatuses(ctx, now)
}

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
