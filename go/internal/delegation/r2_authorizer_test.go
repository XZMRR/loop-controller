package delegation

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/loop-controller/go/internal/models"
)

func TestHTTPR2Authorizer_Allowed(t *testing.T) {
	want := models.DelegationResponse{
		Allowed:         true,
		TaskID:          "task-001",
		Reason:          "IIGE authorized delegation",
		DelegationToken: "token-123",
		ProtocolVersion: interactionProtocolVersion,
		TargetEntrypoint: models.AgentEntrypoint{
			Type: "http",
			URL:  "http://agent-b:8080",
		},
	}
	body, _ := json.Marshal(want)

	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/interaction/v1/delegations/authorize" {
			t.Errorf("unexpected path %q", r.URL.Path)
		}
		if got := r.Header.Get("Authorization"); got != "Bearer service-token" {
			t.Errorf("unexpected authorization %q", got)
		}
		var payload map[string]any
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Fatalf("decode request: %v", err)
		}
		if payload["source_agent_id"] != "agent-a" {
			t.Errorf("source_agent_id = %v", payload["source_agent_id"])
		}
		if _, exists := payload["arguments_json"]; exists {
			t.Error("arguments_json must not be sent")
		}
		arguments, ok := payload["arguments"].(map[string]any)
		if !ok || arguments["text"] != "你好" {
			t.Errorf("arguments not preserved: %#v", payload["arguments"])
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(body)
	}))
	defer ts.Close()

	auth := &HTTPR2Authorizer{
		BaseURL: ts.URL, BearerToken: "service-token",
		Client: &http.Client{Timeout: 2 * time.Second},
	}
	got, err := auth.Authorize(context.Background(), models.DelegationRequest{
		RequestID:        "req-1",
		InitiatorAgentID: "agent-a",
		TargetAgentID:    "agent-b",
		ToolName:         "echo",
		Arguments:        json.RawMessage(`{"text":"你好"}`),
		ProtocolVersion:  interactionProtocolVersion,
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !got.Allowed || got.TaskID != want.TaskID || got.DelegationToken != want.DelegationToken {
		t.Fatalf("unexpected response: %+v", got)
	}
}

func TestHTTPR2Authorizer_FallsBackOnlyOn404(t *testing.T) {
	paths := make([]string, 0, 2)
	response := models.DelegationResponse{
		Allowed: true, ProtocolVersion: interactionProtocolVersion,
	}
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		paths = append(paths, r.URL.Path)
		if r.URL.Path == "/interaction/v1/delegations/authorize" {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		_ = json.NewEncoder(w).Encode(response)
	}))
	defer ts.Close()

	auth := &HTTPR2Authorizer{BaseURL: ts.URL}
	_, err := auth.Authorize(context.Background(), models.DelegationRequest{
		ProtocolVersion: interactionProtocolVersion,
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(paths) != 2 || paths[1] != "/r2/v1/delegations/authorize" {
		t.Fatalf("unexpected paths: %#v", paths)
	}
}

func TestHTTPR2Authorizer_Denied(t *testing.T) {
	resp := models.DelegationResponse{
		Allowed: false, Reason: "target not trusted", ProtocolVersion: interactionProtocolVersion,
	}
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(resp)
	}))
	defer ts.Close()

	auth := &HTTPR2Authorizer{BaseURL: ts.URL}
	got, err := auth.Authorize(context.Background(), models.DelegationRequest{
		ProtocolVersion: interactionProtocolVersion,
	})
	if err == nil || got.Allowed || got.Reason != resp.Reason {
		t.Fatalf("unexpected denied response: %+v, %v", got, err)
	}
}

func TestHTTPR2Authorizer_FailClosedOnIncompatibleResponse(t *testing.T) {
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(models.DelegationResponse{
			Allowed: true, ProtocolVersion: "0.38.0",
		})
	}))
	defer ts.Close()

	auth := &HTTPR2Authorizer{BaseURL: ts.URL}
	got, err := auth.Authorize(context.Background(), models.DelegationRequest{
		ProtocolVersion: interactionProtocolVersion,
	})
	if err == nil || got.Allowed {
		t.Fatalf("expected fail-closed, got %+v, %v", got, err)
	}
}

func TestHTTPR2Authorizer_FailClosedOnError(t *testing.T) {
	auth := &HTTPR2Authorizer{
		BaseURL: "http://127.0.0.1:1",
		Client:  &http.Client{Timeout: 100 * time.Millisecond},
	}
	got, err := auth.Authorize(context.Background(), models.DelegationRequest{
		ProtocolVersion: interactionProtocolVersion,
	})
	if err == nil || got.Allowed || got.Reason == "" {
		t.Fatalf("expected fail-closed, got %+v, %v", got, err)
	}
}

func TestHTTPR2Authorizer_FailClosedOnNon2xx(t *testing.T) {
	requestCount := 0
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requestCount++
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer ts.Close()

	auth := &HTTPR2Authorizer{BaseURL: ts.URL}
	got, err := auth.Authorize(context.Background(), models.DelegationRequest{
		ProtocolVersion: interactionProtocolVersion,
	})
	if err == nil || got.Allowed || requestCount != 1 {
		t.Fatalf("expected fail-closed without fallback, got %+v, %v, requests=%d", got, err, requestCount)
	}
}
