package api

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestHealthMetricsAndStrictReadinessContracts(t *testing.T) {
	s, err := NewServer([]byte("01234567890123456789012345678901"), t.TempDir()+"/ready.db")
	if err != nil {
		t.Fatal(err)
	}
	mux := http.NewServeMux()
	s.RegisterRoutes(mux)

	health := httptest.NewRecorder()
	mux.ServeHTTP(health, httptest.NewRequest(http.MethodGet, "/health", nil))
	if health.Code != http.StatusOK || !strings.Contains(health.Body.String(), `"status":"ok"`) {
		t.Fatalf("health=%d %s", health.Code, health.Body.String())
	}

	metrics := httptest.NewRecorder()
	mux.ServeHTTP(metrics, httptest.NewRequest(http.MethodGet, "/metrics", nil))
	if metrics.Code != http.StatusOK || !strings.HasPrefix(metrics.Header().Get("Content-Type"), "text/plain; version=0.0.4") {
		t.Fatalf("metrics contract: %d %q", metrics.Code, metrics.Header().Get("Content-Type"))
	}
	for _, name := range []string{"scheduler_candidates_total", "assignment_claim_total", "assignment_queue_depth", "discovery_stale_agents", "sse_active_subscribers"} {
		if !strings.Contains(metrics.Body.String(), "# TYPE "+name) {
			t.Errorf("missing %s", name)
		}
	}
	for _, forbidden := range []string{"task_id=", "user_id=", "agent_id=", "tenant_id=", "secret"} {
		if strings.Contains(strings.ToLower(metrics.Body.String()), forbidden) {
			t.Errorf("forbidden metric content %q", forbidden)
		}
	}

	ready := httptest.NewRecorder()
	mux.ServeHTTP(ready, httptest.NewRequest(http.MethodGet, "/ready", nil))
	if ready.Code != http.StatusServiceUnavailable {
		t.Fatalf("strict readiness=%d %s", ready.Code, ready.Body.String())
	}
	var response map[string]any
	if err := json.Unmarshal(ready.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if response["schema_version"] != "p54-09.v1" || response["status"] != "not_ready" || response["protocol_version"] != currentProtocolVersion {
		t.Fatalf("response=%v", response)
	}

	s.SetReadinessConfig(ReadinessConfig{Strict: false, MaxQueueDepth: 10000, MaxQueueOldestLag: time.Hour, MaxRecoveryStaleness: time.Minute, MaxDiscoveryStaleness: time.Minute})
	deadline := time.Now().Add(time.Second)
	for {
		ready = httptest.NewRecorder()
		mux.ServeHTTP(ready, httptest.NewRequest(http.MethodGet, "/ready", nil))
		if ready.Code == http.StatusOK || time.Now().After(deadline) {
			break
		}
		time.Sleep(time.Millisecond)
	}
	if ready.Code != http.StatusOK {
		t.Fatalf("development readiness=%d %s", ready.Code, ready.Body.String())
	}
	if err := s.Close(); err != nil {
		t.Fatal(err)
	}
}
