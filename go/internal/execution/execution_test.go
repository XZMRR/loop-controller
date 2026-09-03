package execution

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"net/http/httptrace"
	"testing"
	"time"
)

func TestHTTPExecutorCompletes(t *testing.T) {
	var got map[string]any
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/govern/tool-call" {
			t.Errorf("path = %q", r.URL.Path)
		}
		if err := json.NewDecoder(r.Body).Decode(&got); err != nil {
			t.Errorf("decode request: %v", err)
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"status": "allow", "result": map[string]any{"value": "ok"}})
	}))
	defer server.Close()

	executor := &HTTPExecutor{BaseURL: server.URL, Client: server.Client()}
	handle, err := executor.Start(context.Background(), Request{
		TaskID: "task-1", SessionID: "session-1", InitiatorAgentID: "planner", TargetAgentID: "executor",
		ToolName: "echo", Arguments: json.RawMessage(`{"x":"hello"}`),
	})
	if err != nil {
		t.Fatalf("start: %v", err)
	}
	select {
	case result := <-handle.Done():
		if result.Status != "completed" || string(result.Outcome) != `{"result":{"value":"ok"}}` {
			t.Fatalf("unexpected result: %+v", result)
		}
	case <-time.After(time.Second):
		t.Fatal("execution did not complete")
	}
	if got["agent_id"] != "executor" || got["tool_name"] != "echo" {
		t.Fatalf("unexpected execution request: %+v", got)
	}
}

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(req *http.Request) (*http.Response, error) { return f(req) }

func TestHTTPExecutorTransportFailureTracksWhetherRequestWasSent(t *testing.T) {
	for _, tc := range []struct {
		name       string
		wrote      bool
		mayBeSent bool
	}{
		{name: "connection failure", wrote: false, mayBeSent: false},
		{name: "disconnect after send", wrote: true, mayBeSent: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			client := &http.Client{Transport: roundTripFunc(func(req *http.Request) (*http.Response, error) {
				if tc.wrote {
					httptrace.ContextClientTrace(req.Context()).WroteRequest(httptrace.WroteRequestInfo{})
				}
				return nil, errors.New("connection lost")
			})}
			executor := &HTTPExecutor{BaseURL: "http://executor.test", Client: client}
			handle, err := executor.Start(context.Background(), Request{
				TaskID: "task-1", InitiatorAgentID: "planner", TargetAgentID: "executor",
				ToolName: "echo", Arguments: json.RawMessage(`{}`),
			})
			if err != nil {
				t.Fatalf("start: %v", err)
			}
			result := <-handle.Done()
			if result.Status != "failed" || result.ErrorCode != "executor_unreachable" || result.MayBeSent != tc.mayBeSent {
				t.Fatalf("unexpected result: %+v", result)
			}
		})
	}
}

func TestHTTPExecutorInvalidResponseAfterSendIsUncertain(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`not-json`))
	}))
	defer server.Close()

	executor := &HTTPExecutor{BaseURL: server.URL, Client: server.Client()}
	handle, err := executor.Start(context.Background(), Request{
		TaskID: "task-1", InitiatorAgentID: "planner", TargetAgentID: "executor",
		ToolName: "echo", Arguments: json.RawMessage(`{}`),
	})
	if err != nil {
		t.Fatalf("start: %v", err)
	}
	result := <-handle.Done()
	if result.ErrorCode != "executor_invalid_response" || !result.MayBeSent {
		t.Fatalf("unexpected result: %+v", result)
	}
}

func TestHTTPExecutorCancelCancelsRequestContext(t *testing.T) {
	started := make(chan struct{})
	cancelled := make(chan struct{})
	client := &http.Client{Transport: roundTripFunc(func(req *http.Request) (*http.Response, error) {
		close(started)
		<-req.Context().Done()
		close(cancelled)
		return nil, req.Context().Err()
	})}

	executor := &HTTPExecutor{BaseURL: "http://executor.test", Client: client}
	handle, err := executor.Start(context.Background(), Request{
		TaskID: "task-1", InitiatorAgentID: "planner", TargetAgentID: "executor",
		ToolName: "wait", Arguments: json.RawMessage(`{}`),
	})
	if err != nil {
		t.Fatalf("start: %v", err)
	}
	select {
	case <-started:
	case <-time.After(time.Second):
		t.Fatal("HTTP request did not start")
	}
	handle.Cancel()
	select {
	case <-cancelled:
	case <-time.After(time.Second):
		t.Fatal("HTTP request context was not cancelled")
	}
}
