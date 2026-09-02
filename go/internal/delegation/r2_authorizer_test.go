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
		Reason:          "R2 authorized delegation",
		DelegationToken: "token-123",
		TargetEntrypoint: models.AgentEntrypoint{
			Type: "http",
			URL:  "http://agent-b:8080",
		},
	}
	body, _ := json.Marshal(want)

	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/r2/v1/delegations/authorize" {
			t.Errorf("unexpected path %q", r.URL.Path)
		}
		if ct := r.Header.Get("Content-Type"); ct != "application/json" {
			t.Errorf("unexpected content-type %q", ct)
		}
		w.WriteHeader(http.StatusOK)
		w.Write(body)
	}))
	defer ts.Close()

	auth := &HTTPR2Authorizer{BaseURL: ts.URL, Client: &http.Client{Timeout: 2 * time.Second}}
	got, err := auth.Authorize(context.Background(), models.DelegationRequest{
		RequestID:        "req-1",
		InitiatorAgentID: "agent-a",
		TargetAgentID:    "agent-b",
		ToolName:         "echo",
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !got.Allowed {
		t.Fatalf("expected allowed, got %+v", got)
	}
	if got.TaskID != want.TaskID {
		t.Errorf("task_id = %q, want %q", got.TaskID, want.TaskID)
	}
	if got.DelegationToken != want.DelegationToken {
		t.Errorf("delegation_token = %q, want %q", got.DelegationToken, want.DelegationToken)
	}
}

func TestHTTPR2Authorizer_Denied(t *testing.T) {
	resp := models.DelegationResponse{Allowed: false, Reason: "target not trusted"}
	body, _ := json.Marshal(resp)

	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		w.Write(body)
	}))
	defer ts.Close()

	auth := &HTTPR2Authorizer{BaseURL: ts.URL, Client: &http.Client{Timeout: 2 * time.Second}}
	got, err := auth.Authorize(context.Background(), models.DelegationRequest{})
	if err == nil {
		t.Fatal("expected error for denied delegation")
	}
	if got.Allowed {
		t.Errorf("expected denied, got allowed")
	}
	if got.Reason != resp.Reason {
		t.Errorf("reason = %q, want %q", got.Reason, resp.Reason)
	}
}

func TestHTTPR2Authorizer_FailClosedOnError(t *testing.T) {
	auth := &HTTPR2Authorizer{BaseURL: "http://127.0.0.1:1", Client: &http.Client{Timeout: 100 * time.Millisecond}}
	got, err := auth.Authorize(context.Background(), models.DelegationRequest{})
	if err == nil {
		t.Fatal("expected network error")
	}
	if got.Allowed {
		t.Errorf("expected fail-closed")
	}
	if got.Reason == "" {
		t.Errorf("expected non-empty denial reason")
	}
}

func TestHTTPR2Authorizer_FailClosedOnNon2xx(t *testing.T) {
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
		w.Write([]byte(`{"error":"internal"}`))
	}))
	defer ts.Close()

	auth := &HTTPR2Authorizer{BaseURL: ts.URL, Client: &http.Client{Timeout: 2 * time.Second}}
	got, err := auth.Authorize(context.Background(), models.DelegationRequest{})
	if err == nil {
		t.Fatal("expected error for non-2xx")
	}
	if got.Allowed {
		t.Errorf("expected fail-closed")
	}
}
