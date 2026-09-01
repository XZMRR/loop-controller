package discovery

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/loop-controller/go/internal/models"
	"github.com/loop-controller/go/internal/registry"
)

func TestStaticProviderLoadsCards(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "agents.json")
	cards := []models.AgentCard{
		{
			AgentID:    "agent-1",
			Name:       "Agent One",
			Entrypoint: models.AgentEntrypoint{Type: "http", URL: "http://a1:8080"},
		},
	}
	data, _ := json.Marshal(cards)
	os.WriteFile(path, data, 0644)

	reg := registry.New()
	mgr := NewManager(reg, NewStaticProvider(path))
	if err := mgr.Sync(context.Background()); err != nil {
		t.Fatalf("sync failed: %v", err)
	}

	card, err := reg.Get("agent-1")
	if err != nil {
		t.Fatalf("get failed: %v", err)
	}
	if card.Name != "Agent One" {
		t.Errorf("unexpected name: %q", card.Name)
	}
}

func TestStaticProviderMissingFileIsEmpty(t *testing.T) {
	reg := registry.New()
	mgr := NewManager(reg, NewStaticProvider("/no/such/file.json"))
	if err := mgr.Sync(context.Background()); err != nil {
		t.Fatalf("sync failed: %v", err)
	}
	if len(reg.List()) != 0 {
		t.Errorf("expected empty registry, got %d", len(reg.List()))
	}
}

func TestSyncRemovesStaleCards(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "agents.json")
	cards := []models.AgentCard{
		{AgentID: "agent-1", Name: "A", Entrypoint: models.AgentEntrypoint{URL: "http://a"}},
		{AgentID: "agent-2", Name: "B", Entrypoint: models.AgentEntrypoint{URL: "http://b"}},
	}
	data, _ := json.Marshal(cards)
	os.WriteFile(path, data, 0644)

	reg := registry.New()
	mgr := NewManager(reg, NewStaticProvider(path))
	mgr.Sync(context.Background())
	if len(reg.List()) != 2 {
		t.Fatalf("expected 2 agents, got %d", len(reg.List()))
	}

	cards = []models.AgentCard{
		{AgentID: "agent-1", Name: "A", Entrypoint: models.AgentEntrypoint{URL: "http://a"}},
	}
	data, _ = json.Marshal(cards)
	os.WriteFile(path, data, 0644)

	mgr.Sync(context.Background())
	if len(reg.List()) != 1 {
		t.Fatalf("expected 1 agent, got %d", len(reg.List()))
	}
	if _, err := reg.Get("agent-2"); err == nil {
		t.Fatal("expected agent-2 to be removed")
	}
}

func TestHTTPProviderCaches(t *testing.T) {
	server := newDiscoveryServer(t, []models.AgentCard{
		{AgentID: "remote-1", Name: "Remote", Entrypoint: models.AgentEntrypoint{URL: "http://r:8080"}},
	})
	defer server.Close()

	reg := registry.New()
	prov := NewHTTPProvider(server.URL, time.Minute)
	mgr := NewManager(reg, prov)
	if err := mgr.Sync(context.Background()); err != nil {
		t.Fatalf("sync failed: %v", err)
	}

	// second call should hit cache; handler count stays 1
	if err := mgr.Sync(context.Background()); err != nil {
		t.Fatalf("second sync failed: %v", err)
	}
	if prov.cached == nil {
		t.Fatal("expected cache to be populated")
	}
}

func TestValidateCardRequiresEntrypointURL(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "agents.json")
	cards := []models.AgentCard{
		{AgentID: "x", Name: "Invalid", Entrypoint: models.AgentEntrypoint{URL: ""}},
	}
	data, _ := json.Marshal(cards)
	os.WriteFile(path, data, 0644)

	reg := registry.New()
	mgr := NewManager(reg, NewStaticProvider(path))
	if err := mgr.Sync(context.Background()); err == nil {
		t.Fatal("expected sync to fail for invalid card")
	}
	if len(reg.List()) != 0 {
		t.Errorf("expected no agents registered, got %d", len(reg.List()))
	}
}

func newDiscoveryServer(t *testing.T, cards []models.AgentCard) *httptest.Server {
	t.Helper()
	data, _ := json.Marshal(cards)
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.Write(data)
	}))
}
