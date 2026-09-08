package delegation

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"net/http/httptrace"
	"testing"

	"github.com/loop-controller/go/internal/models"
)

func TestHTTPEntrypointClientDispatch(t *testing.T) {
	var got models.EntrypointTaskRequest
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if got := r.Header.Get("Authorization"); got != "Bearer token" {
			t.Errorf("authorization = %q", got)
		}
		if r.URL.Path != "/a2a/v1/entrypoint/tasks" {
			t.Errorf("path = %q", r.URL.Path)
		}
		if err := json.NewDecoder(r.Body).Decode(&got); err != nil {
			t.Errorf("decode: %v", err)
		}
		w.WriteHeader(http.StatusCreated)
	}))
	defer server.Close()

	client := &HTTPEntrypointClient{Client: server.Client()}
	req := models.EntrypointTaskRequest{
		ProtocolVersion:  interactionProtocolVersion,
		TaskID:           "task-1",
		SessionID:        "session-1",
		InitiatorAgentID: "agent-a",
		TargetAgentID:    "agent-b",
		ToolName:         "analyze",
		Arguments:        json.RawMessage(`{"region":"APAC"}`),
		DelegationToken:  "token",
	}
	if err := client.Dispatch(context.Background(), models.AgentEntrypoint{Type: "http", URL: server.URL}, req); err != nil {
		t.Fatalf("dispatch: %v", err)
	}
	if got.TaskID != req.TaskID || got.ToolName != req.ToolName {
		t.Fatalf("unexpected request: %+v", got)
	}
}

type entrypointRoundTripFunc func(*http.Request) (*http.Response, error)

func (f entrypointRoundTripFunc) RoundTrip(req *http.Request) (*http.Response, error) { return f(req) }

func TestHTTPEntrypointClientDispatchErrorTracksWhetherRequestWasSent(t *testing.T) {
	for _, tc := range []struct {
		name       string
		wrote      bool
		mayBeSent bool
	}{
		{name: "connection failure", wrote: false, mayBeSent: false},
		{name: "disconnect after send", wrote: true, mayBeSent: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			client := &HTTPEntrypointClient{Client: &http.Client{Transport: entrypointRoundTripFunc(func(req *http.Request) (*http.Response, error) {
				if tc.wrote {
					httptrace.ContextClientTrace(req.Context()).WroteRequest(httptrace.WroteRequestInfo{})
				}
				return nil, errors.New("connection lost")
			})}}
			err := client.Dispatch(context.Background(), models.AgentEntrypoint{Type: "http", URL: "http://executor.test"}, models.EntrypointTaskRequest{})
			var dispatchErr *DispatchError
			if !errors.As(err, &dispatchErr) || dispatchErr.MayBeSent != tc.mayBeSent {
				t.Fatalf("dispatch error = %v, want MayBeSent=%v", err, tc.mayBeSent)
			}
		})
	}
}

func TestHTTPEntrypointClientCancel(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if got := r.Header.Get("Authorization"); got != "Bearer token" {
			t.Errorf("authorization = %q", got)
		}
		if r.Method != http.MethodPost {
			t.Errorf("method = %q", r.Method)
		}
		if r.URL.Path != "/a2a/v1/entrypoint/tasks/task-1/cancel" {
			t.Errorf("path = %q", r.URL.Path)
		}
		var got struct {
			ProtocolVersion string `json:"protocol_version"`
		}
		if err := json.NewDecoder(r.Body).Decode(&got); err != nil {
			t.Errorf("decode: %v", err)
		}
		if got.ProtocolVersion != interactionProtocolVersion {
			t.Errorf("protocol_version = %q", got.ProtocolVersion)
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(models.Task{TaskID: "task-1", Status: "cancelled"})
	}))
	defer server.Close()

	client := &HTTPEntrypointClient{Client: server.Client()}
	confirmed, err := client.Cancel(context.Background(), models.AgentEntrypoint{Type: "http", URL: server.URL}, "task-1", "token")
	if err != nil {
		t.Fatalf("cancel: %v", err)
	}
	if !confirmed {
		t.Error("expected confirmed cancellation")
	}
}

func TestHTTPEntrypointClientRejectsInvalidURL(t *testing.T) {
	client := &HTTPEntrypointClient{}
	err := client.Dispatch(context.Background(), models.AgentEntrypoint{Type: "http", URL: "file:///tmp/agent"}, models.EntrypointTaskRequest{})
	if err == nil {
		t.Fatal("expected invalid URL error")
	}
}
