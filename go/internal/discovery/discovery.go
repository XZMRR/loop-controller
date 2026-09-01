// Package discovery loads Agent Cards from static files or remote URLs.
package discovery

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"sync"
	"time"

	"github.com/loop-controller/go/internal/models"
)

// DiscoveryEventType indicates the kind of discovery change.
type DiscoveryEventType string

const (
	DiscoveryAdd    DiscoveryEventType = "add"
	DiscoveryUpdate DiscoveryEventType = "update"
	DiscoveryRemove DiscoveryEventType = "remove"
)

// DiscoveryEvent is a single change notification from a provider.
type DiscoveryEvent struct {
	Type DiscoveryEventType
	Card models.AgentCard
}

// AgentDiscoveryProvider abstracts a source of Agent Cards.
type AgentDiscoveryProvider interface {
	Name() string
	Discover(ctx context.Context) ([]models.AgentCard, error)
	Watch(ctx context.Context) (<-chan DiscoveryEvent, error)
}

// RegistryStore is the minimal interface the discovery manager needs.
type RegistryStore interface {
	Register(card models.AgentCard) error
	Get(agentID string) (models.AgentCard, error)
	Delete(agentID string) error
}

// Manager coordinates one or more discovery providers.
type Manager struct {
	registry  RegistryStore
	providers []AgentDiscoveryProvider
	mu        sync.Mutex
	known     map[string]models.AgentCard
}

// NewManager creates a discovery manager.
func NewManager(registry RegistryStore, providers ...AgentDiscoveryProvider) *Manager {
	return &Manager{
		registry:  registry,
		providers: providers,
		known:     make(map[string]models.AgentCard),
	}
}

// Sync performs a one-time full sync from all providers.
func (m *Manager) Sync(ctx context.Context) error {
	m.mu.Lock()
	defer m.mu.Unlock()

	newKnown := make(map[string]models.AgentCard)
	for _, provider := range m.providers {
		cards, err := provider.Discover(ctx)
		if err != nil {
			return fmt.Errorf("provider %s discover failed: %w", provider.Name(), err)
		}
		for _, card := range cards {
			if err := validateCard(card); err != nil {
				return fmt.Errorf("provider %s returned invalid card: %w", provider.Name(), err)
			}
			newKnown[card.AgentID] = card
		}
	}

	// Remove cards that disappeared.
	for id := range m.known {
		if _, ok := newKnown[id]; !ok {
			_ = m.registry.Delete(id)
		}
	}

	// Register or update cards.
	for _, card := range newKnown {
		if err := m.registry.Register(card); err != nil {
			return fmt.Errorf("register %s failed: %w", card.AgentID, err)
		}
	}
	m.known = newKnown
	return nil
}

func validateCard(card models.AgentCard) error {
	if card.AgentID == "" {
		return errors.New("agent_id is required")
	}
	if card.Entrypoint.URL == "" {
		return errors.New("entrypoint.url is required")
	}
	return nil
}

// StaticProvider loads Agent Cards from a JSON or YAML array file.
type StaticProvider struct {
	path string
}

// NewStaticProvider creates a provider backed by a file.
func NewStaticProvider(path string) *StaticProvider {
	return &StaticProvider{path: path}
}

// Name returns the provider name.
func (p *StaticProvider) Name() string {
	return "static:" + p.path
}

// Discover reads the file and parses the cards.
func (p *StaticProvider) Discover(ctx context.Context) ([]models.AgentCard, error) {
	data, err := os.ReadFile(p.path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, err
	}
	var cards []models.AgentCard
	if err := json.Unmarshal(data, &cards); err != nil {
		return nil, fmt.Errorf("parse %s: %w", p.path, err)
	}
	return cards, nil
}

// Watch is not supported for static files.
func (p *StaticProvider) Watch(ctx context.Context) (<-chan DiscoveryEvent, error) {
	return nil, errors.New("static provider does not support watch")
}

// HTTPProvider fetches Agent Cards from a remote URL with caching.
type HTTPProvider struct {
	client     *http.Client
	url        string
	cacheFor   time.Duration
	mu         sync.RWMutex
	cachedAt   time.Time
	cached     []models.AgentCard
}

// NewHTTPProvider creates a provider that fetches from url.
func NewHTTPProvider(url string, cacheFor time.Duration) *HTTPProvider {
	return &HTTPProvider{
		client:   &http.Client{Timeout: 10 * time.Second},
		url:      url,
		cacheFor: cacheFor,
	}
}

// Name returns the provider name.
func (p *HTTPProvider) Name() string {
	return "http:" + p.url
}

// Discover fetches cards, using the cache if still valid.
func (p *HTTPProvider) Discover(ctx context.Context) ([]models.AgentCard, error) {
	p.mu.RLock()
	if time.Since(p.cachedAt) < p.cacheFor && p.cached != nil {
		cached := p.cached
		p.mu.RUnlock()
		return cached, nil
	}
	p.mu.RUnlock()

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, p.url, nil)
	if err != nil {
		return nil, err
	}
	resp, err := p.client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("unexpected status %d", resp.StatusCode)
	}
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}
	var cards []models.AgentCard
	if err := json.Unmarshal(body, &cards); err != nil {
		return nil, fmt.Errorf("parse response: %w", err)
	}

	p.mu.Lock()
	p.cached = cards
	p.cachedAt = time.Now()
	p.mu.Unlock()
	return cards, nil
}

// Watch is not supported for simple HTTP provider.
func (p *HTTPProvider) Watch(ctx context.Context) (<-chan DiscoveryEvent, error) {
	return nil, errors.New("http provider does not support watch")
}
