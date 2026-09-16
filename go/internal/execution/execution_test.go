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
		if got := r.Header.Get("Authorization"); got != "Bearer service-token" {
			t.Errorf("Authorization = %q", got)
		}
		if err := json.NewDecoder(r.Body).Decode(&got); err != nil {
			t.Errorf("decode request: %v", err)
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"status": "allow", "result": map[string]any{"value": "ok"}})
	}))
	defer server.Close()

	executor := &HTTPExecutor{BaseURL: server.URL, BearerToken: "service-token", Client: server.Client()}
	handle, err := executor.Start(context.Background(), Request{
		TaskID: "task-1", SessionID: "session-1", InitiatorAgentID: "planner", TargetAgentID: "executor",
		ToolName: "echo", Arguments: json.RawMessage(`{"x":"hello"}`),
		AllowedTools: []string{"echo"}, AllowedCapabilities: []string{"read_data"}, AllowRedelegation: true,
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
	if tools, ok := got["allowed_tools"].([]any); !ok || len(tools) != 1 || tools[0] != "echo" ||
		got["allow_redelegation"] != true {
		t.Fatalf("execution scope was not forwarded: %+v", got)
	}
}

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(req *http.Request) (*http.Response, error) { return f(req) }

func TestHTTPExecutorTransportFailureTracksWhetherRequestWasSent(t *testing.T) {
	for _, tc := range []struct {
		name      string
		wrote     bool
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

func TestHTTPExecutorDeadlineCancelsSentRequestAsUncertain(t *testing.T) {
	client := &http.Client{Transport: roundTripFunc(func(req *http.Request) (*http.Response, error) {
		httptrace.ContextClientTrace(req.Context()).WroteRequest(httptrace.WroteRequestInfo{})
		<-req.Context().Done()
		return nil, req.Context().Err()
	})}
	deadline := time.Now().Add(30 * time.Millisecond)
	handle, err := (&HTTPExecutor{BaseURL: "http://executor.test", Client: client}).Start(context.Background(), Request{
		TaskID: "task-deadline", InitiatorAgentID: "planner", TargetAgentID: "executor",
		ToolName: "wait", Arguments: json.RawMessage(`{}`), Deadline: &deadline,
	})
	if err != nil {
		t.Fatalf("start: %v", err)
	}
	result := <-handle.Done()
	if result.ErrorCode != "execution_deadline_exceeded" || !result.MayBeSent {
		t.Fatalf("unexpected deadline result: %+v", result)
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

func TestPendingResultsHandleCompleteWakesWaiter(t *testing.T) {
	handle := NewPendingResultsHandle(nil)
	if !handle.Complete(Result{Status: "completed", Outcome: json.RawMessage(`{"ok":true}`)}) {
		t.Fatal("first Complete should succeed")
	}
	select {
	case result := <-handle.Done():
		if result.Status != "completed" {
			t.Errorf("status = %q", result.Status)
		}
	case <-time.After(time.Second):
		t.Fatal("Done was not woken by Complete")
	}
	// A duplicate (late or repeated) delivery must be rejected.
	if handle.Complete(Result{Status: "failed"}) {
		t.Error("duplicate Complete should return false")
	}
}

func TestPendingResultsHandleCancel(t *testing.T) {
	handle := NewPendingResultsHandle(nil)
	if !handle.Cancel() {
		t.Fatal("Cancel should succeed")
	}
	select {
	case result := <-handle.Done():
		if result.ErrorCode != "execution_cancelled" {
			t.Errorf("error_code = %q", result.ErrorCode)
		}
	case <-time.After(time.Second):
		t.Fatal("Done was not woken by Cancel")
	}
	// Complete after cancel must be rejected.
	if handle.Complete(Result{Status: "completed"}) {
		t.Error("Complete after cancel should return false")
	}
}

func TestPendingResultsHandleDeadlineTimeout(t *testing.T) {
	deadline := time.Now().Add(50 * time.Millisecond)
	handle := NewPendingResultsHandle(&deadline)
	select {
	case result := <-handle.Done():
		if result.ErrorCode != "execution_deadline_exceeded" {
			t.Errorf("error_code = %q", result.ErrorCode)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("deadline timer did not fire")
	}
	// A racing Complete after timeout must not block or redeliver.
	handle.Complete(Result{Status: "completed"})
}
