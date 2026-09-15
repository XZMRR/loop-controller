// Package discovery loads source-scoped Agent snapshots from files or remote URLs.
package discovery

import (
	"bytes"
	"context"
	"crypto/tls"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/loop-controller/go/internal/entrypointpolicy"
	"github.com/loop-controller/go/internal/models"
	"github.com/loop-controller/go/internal/observability"
	"gopkg.in/yaml.v3"
)

type DiscoveryEventType string

const (
	DiscoveryAdd    DiscoveryEventType = "add"
	DiscoveryUpdate DiscoveryEventType = "update"
	DiscoveryRemove DiscoveryEventType = "remove"
)

type DiscoveryEvent struct {
	Type DiscoveryEventType
	Card models.AgentCard
}
type AgentDiscoveryProvider interface {
	Name() string
	Discover(context.Context) ([]models.AgentCard, error)
	Watch(context.Context) (<-chan DiscoveryEvent, error)
}
type SourceProvider interface {
	AgentDiscoveryProvider
	SourceMetadata() (tenantID, sourceType, sourceID string)
}
type RegistryStore interface {
	Register(models.AgentCard) error
	Get(string) (models.AgentCard, error)
	Delete(string) error
}
type SnapshotRegistry interface {
	SupportsAgentSpecs() bool
	SyncSource(context.Context, models.DiscoverySnapshot) error
	MarkSourceStale(context.Context, string, string, string, error) error
	ExpireStatuses(context.Context, time.Time) (int64, error)
}

type Manager struct {
	registry  RegistryStore
	providers []AgentDiscoveryProvider
	mu        sync.Mutex
	known     map[string]models.AgentCard
	state     observability.State
	policy    entrypointpolicy.Policy
}

func NewManager(r RegistryStore, p ...AgentDiscoveryProvider) *Manager {
	return &Manager{registry: r, providers: p, known: map[string]models.AgentCard{}, policy: entrypointpolicy.Strict()}
}
func (m *Manager) WithEntrypointPolicy(policy entrypointpolicy.Policy) *Manager {
	m.policy = policy
	return m
}
func (m *Manager) Snapshot() observability.StateSnapshot { return m.state.Snapshot() }
func (m *Manager) Required() bool                        { return len(m.providers) > 0 }
func (m *Manager) Close()                                { m.state.Stop() }
func (m *Manager) Sync(ctx context.Context) (err error) {
	m.state.Start()
	defer func() {
		now := time.Now().UTC()
		if err != nil {
			m.state.Error(now)
			observability.Default.Add("discovery_refresh_errors_total", 1)
		} else {
			m.state.Success(now)
			observability.Default.Set("discovery_last_success_unixtime", float64(now.Unix()))
		}
	}()
	m.mu.Lock()
	defer m.mu.Unlock()
	if sr, ok := m.registry.(SnapshotRegistry); ok && sr.SupportsAgentSpecs() {
		return m.syncSnapshots(ctx, sr)
	}
	newKnown := map[string]models.AgentCard{}
	for _, p := range m.providers {
		cards, e := p.Discover(ctx)
		if e != nil {
			return fmt.Errorf("provider %s discover failed: %w", p.Name(), e)
		}
		for _, c := range cards {
			if e = validateCard(c); e == nil {
				e = m.policy.ValidateRegistration(ctx, c.Entrypoint.URL)
			}
			if e != nil {
				return fmt.Errorf("provider %s returned invalid card: %w", p.Name(), e)
			}
			if _, exists := newKnown[c.AgentID]; exists {
				return fmt.Errorf("agent %s supplied by multiple sources", c.AgentID)
			}
			newKnown[c.AgentID] = c
		}
	}
	for id := range m.known {
		if _, ok := newKnown[id]; !ok {
			if e := m.registry.Delete(id); e != nil {
				return e
			}
		}
	}
	for _, c := range newKnown {
		if e := m.registry.Register(c); e != nil {
			return e
		}
	}
	m.known = newKnown
	return nil
}
func (m *Manager) syncSnapshots(ctx context.Context, r SnapshotRegistry) error {
	owners := map[string]string{}
	var errs []error
	for _, p := range m.providers {
		sp, ok := p.(SourceProvider)
		if !ok {
			return fmt.Errorf("provider %s has no source metadata", p.Name())
		}
		tenant, typ, id := sp.SourceMetadata()
		cards, e := p.Discover(ctx)
		if e != nil {
			_ = r.MarkSourceStale(ctx, tenant, typ, id, e)
			errs = append(errs, fmt.Errorf("provider %s discover failed: %w", p.Name(), e))
			continue
		}
		specs := make([]models.AgentSpec, 0, len(cards))
		for _, c := range cards {
			if e = validateCard(c); e == nil {
				e = m.policy.ValidateRegistration(ctx, c.Entrypoint.URL)
			}
			if e != nil {
				break
			}
			key := tenant + "\x00" + c.AgentID
			if owner, exists := owners[key]; exists && owner != typ+"/"+id {
				e = fmt.Errorf("agent %s source ownership conflict", c.AgentID)
				break
			}
			owners[key] = typ + "/" + id
			specs = append(specs, cardSpec(c, tenant, typ, id))
		}
		if e == nil {
			e = r.SyncSource(ctx, models.DiscoverySnapshot{TenantID: tenant, SourceType: typ, SourceID: id, Agents: specs})
		}
		if e != nil {
			_ = r.MarkSourceStale(ctx, tenant, typ, id, e)
			errs = append(errs, e)
		}
	}
	_, e := r.ExpireStatuses(ctx, time.Now())
	if e != nil {
		errs = append(errs, e)
	}
	return errors.Join(errs...)
}
func cardSpec(c models.AgentCard, tenant, typ, id string) models.AgentSpec {
	return models.AgentSpec{TenantID: tenant, AgentID: c.AgentID, Name: c.Name, Description: c.Description, Entrypoint: c.Entrypoint, Capabilities: c.Capabilities, TrustDomain: c.TrustDomain, SupportedProtocolVersions: []string{c.Version}, SourceType: typ, SourceID: id, Schedulable: false, Weight: 1, MaxConcurrency: 1}
}
func validateCard(c models.AgentCard) error {
	if c.AgentID == "" {
		return errors.New("agent_id is required")
	}
	if c.Entrypoint.URL == "" {
		return errors.New("entrypoint.url is required")
	}
	return nil
}

type StaticProvider struct{ path, tenant, sourceID string }

func NewStaticProvider(path string) *StaticProvider {
	return &StaticProvider{path: path, tenant: "legacy", sourceID: path}
}
func NewStaticProviderForSource(path, tenant, sourceID string) *StaticProvider {
	return &StaticProvider{path: path, tenant: tenant, sourceID: sourceID}
}
func (p *StaticProvider) Name() string { return "static:" + p.path }
func (p *StaticProvider) SourceMetadata() (string, string, string) {
	return p.tenant, "static", p.sourceID
}
func (p *StaticProvider) Discover(ctx context.Context) ([]models.AgentCard, error) {
	data, e := os.ReadFile(p.path)
	if e != nil {
		if os.IsNotExist(e) {
			return nil, nil
		}
		return nil, e
	}
	var cards []models.AgentCard
	if ext := strings.ToLower(filepath.Ext(p.path)); ext == ".yaml" || ext == ".yml" {
		e = yaml.Unmarshal(data, &cards)
	} else {
		e = json.Unmarshal(data, &cards)
	}
	if e != nil {
		return nil, e
	}
	return cards, nil
}
func (p *StaticProvider) Watch(context.Context) (<-chan DiscoveryEvent, error) {
	return nil, errors.New("static provider does not support watch")
}

type SignatureVerifier interface {
	Verify(ctx context.Context, body []byte, headers http.Header) error
}
type HTTPProviderConfig struct {
	URL              string
	TenantID         string
	SourceID         string
	CacheFor         time.Duration
	Strict           bool
	MaxResponseBytes int64
	MaxAgents        int
	AllowedHosts     []string
	AllowedIPs       []net.IP
	TLSConfig        *tls.Config
	Client           *http.Client
	RequireSignature bool
	Verifier         SignatureVerifier
}
type HTTPProvider struct {
	client                *http.Client
	url, tenant, sourceID string
	cacheFor              time.Duration
	strict                bool
	maxBytes              int64
	maxAgents             int
	allowedHosts          map[string]bool
	allowedIPs            []net.IP
	requireSignature      bool
	verifier              SignatureVerifier
	mu                    sync.RWMutex
	cachedAt              time.Time
	cached                []models.AgentCard
}

func NewHTTPProvider(raw string, cache time.Duration) *HTTPProvider {
	p, _ := NewHTTPProviderWithConfig(HTTPProviderConfig{URL: raw, CacheFor: cache, MaxResponseBytes: 1 << 20, MaxAgents: 1000, AllowedHosts: []string{"127.0.0.1", "localhost"}})
	return p
}
func NewHTTPProviderWithConfig(c HTTPProviderConfig) (*HTTPProvider, error) {
	u, e := url.Parse(c.URL)
	if e != nil || u.Hostname() == "" {
		return nil, errors.New("invalid provider URL")
	}
	if c.Strict && u.Scheme != "https" {
		return nil, errors.New("strict HTTP provider requires HTTPS")
	}
	if c.RequireSignature && c.Verifier == nil {
		return nil, errors.New("signature required but no verifier configured")
	}
	if c.MaxResponseBytes <= 0 {
		c.MaxResponseBytes = 1 << 20
	}
	if c.MaxAgents <= 0 {
		c.MaxAgents = 1000
	}
	hosts := map[string]bool{}
	for _, h := range c.AllowedHosts {
		hosts[strings.ToLower(h)] = true
	}
	client := c.Client
	if client == nil {
		tr := http.DefaultTransport.(*http.Transport).Clone()
		if c.TLSConfig != nil {
			tr.TLSClientConfig = c.TLSConfig.Clone()
		}
		client = &http.Client{Timeout: 10 * time.Second, Transport: tr}
	}
	if client.CheckRedirect == nil {
		clone := *client
		clone.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
		client = &clone
	}
	return &HTTPProvider{client: client, url: c.URL, tenant: c.TenantID, sourceID: c.SourceID, cacheFor: c.CacheFor, strict: c.Strict, maxBytes: c.MaxResponseBytes, maxAgents: c.MaxAgents, allowedHosts: hosts, allowedIPs: c.AllowedIPs, requireSignature: c.RequireSignature, verifier: c.Verifier}, nil
}
func (p *HTTPProvider) Name() string                             { return "http:" + p.url }
func (p *HTTPProvider) SourceMetadata() (string, string, string) { return p.tenant, "http", p.sourceID }
func (p *HTTPProvider) allowed(ctx context.Context) error {
	u, _ := url.Parse(p.url)
	host := strings.ToLower(u.Hostname())
	if p.allowedHosts[host] {
		return nil
	}
	ips, e := net.DefaultResolver.LookupIP(ctx, "ip", host)
	if e != nil {
		return e
	}
	for _, ip := range ips {
		ok := false
		for _, a := range p.allowedIPs {
			if a.Equal(ip) {
				ok = true
				break
			}
		}
		if ok {
			continue
		}
		if ip.IsLoopback() || ip.IsPrivate() || ip.IsLinkLocalUnicast() || ip.IsLinkLocalMulticast() || ip.IsMulticast() || ip.IsUnspecified() {
			return fmt.Errorf("provider address %s is not allowed", ip)
		}
	}
	return nil
}
func (p *HTTPProvider) Discover(ctx context.Context) ([]models.AgentCard, error) {
	p.mu.RLock()
	if time.Since(p.cachedAt) < p.cacheFor && p.cached != nil {
		out := append([]models.AgentCard(nil), p.cached...)
		p.mu.RUnlock()
		return out, nil
	}
	p.mu.RUnlock()
	if e := p.allowed(ctx); e != nil {
		return nil, e
	}
	req, e := http.NewRequestWithContext(ctx, http.MethodGet, p.url, nil)
	if e != nil {
		return nil, e
	}
	resp, e := p.client.Do(req)
	if e != nil {
		return nil, e
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("unexpected status %d", resp.StatusCode)
	}
	media := strings.ToLower(strings.TrimSpace(strings.Split(resp.Header.Get("Content-Type"), ";")[0]))
	if media != "application/json" {
		return nil, fmt.Errorf("content-type must be application/json")
	}
	body, e := io.ReadAll(io.LimitReader(resp.Body, p.maxBytes+1))
	if e != nil {
		return nil, e
	}
	if int64(len(body)) > p.maxBytes {
		return nil, errors.New("provider response too large")
	}
	if p.requireSignature {
		if e = p.verifier.Verify(ctx, body, resp.Header); e != nil {
			return nil, fmt.Errorf("verify signature: %w", e)
		}
	}
	dec := json.NewDecoder(bytes.NewReader(body))
	dec.DisallowUnknownFields()
	var cards []models.AgentCard
	if e = dec.Decode(&cards); e != nil {
		return nil, fmt.Errorf("parse response: %w", e)
	}
	var trailing any
	if e = dec.Decode(&trailing); e != io.EOF {
		return nil, errors.New("trailing JSON value")
	}
	if len(cards) > p.maxAgents {
		return nil, errors.New("too many agents")
	}
	seen := map[string]bool{}
	for _, c := range cards {
		if seen[c.AgentID] {
			return nil, fmt.Errorf("duplicate agent_id %q", c.AgentID)
		}
		seen[c.AgentID] = true
	}
	p.mu.Lock()
	p.cached = append([]models.AgentCard(nil), cards...)
	p.cachedAt = time.Now()
	p.mu.Unlock()
	return cards, nil
}
func (p *HTTPProvider) Watch(context.Context) (<-chan DiscoveryEvent, error) {
	return nil, errors.New("http provider does not support watch")
}
