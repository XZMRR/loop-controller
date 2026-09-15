package api

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/loop-controller/go/internal/delegation"
	"github.com/loop-controller/go/internal/models"
)

var testSecret = []byte("test-secret")

func newTestServer(t *testing.T) (*Server, *httptest.Server) {
	t.Helper()
	srv, err := NewServer(testSecret, filepath.Join(t.TempDir(), "a2a.db"))
	if err != nil {
		t.Fatalf("new server: %v", err)
	}
	srv.SetR2Authorizer(&delegation.StaticR2Authorizer{
		Decision: models.DelegationResponse{Allowed: true},
	})
	server := httptest.NewServer(muxFor(srv))
	t.Cleanup(func() {
		server.Close()
		_ = srv.Close()
	})
	return srv, server
}

func TestRegisterAgentAndGet(t *testing.T) {
	_, server := newTestServer(t)

	card := models.AgentCard{
		AgentID:      "agent-1",
		Name:         "Test",
		Entrypoint:   models.AgentEntrypoint{Type: "http", URL: "http://localhost:8080"},
		Capabilities: []string{"delegate_execution"},
	}
	body, _ := json.Marshal(card)
	resp, err := http.Post(server.URL+"/a2a/v1/agents", "application/json", bytes.NewReader(body))
	if err != nil {
		t.Fatalf("post failed: %v", err)
	}
	if resp.StatusCode != http.StatusCreated {
		t.Fatalf("expected 201, got %d", resp.StatusCode)
	}

	resp, err = http.Get(server.URL + "/a2a/v1/agents/agent-1")
	if err != nil {
		t.Fatalf("get failed: %v", err)
	}
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("expected 200, got %d", resp.StatusCode)
	}
	var got models.AgentCard
	json.NewDecoder(resp.Body).Decode(&got)
	if got.AgentID != "agent-1" {
		t.Errorf("expected agent-1, got %q", got.AgentID)
	}
}

func TestListAgents(t *testing.T) {
	_, server := newTestServer(t)

	card := models.AgentCard{AgentID: "agent-1", Name: "Test"}
	body, _ := json.Marshal(card)
	http.Post(server.URL+"/a2a/v1/agents", "application/json", bytes.NewReader(body))

	resp, err := http.Get(server.URL + "/a2a/v1/agents")
	if err != nil {
		t.Fatalf("get failed: %v", err)
	}
	var list models.AgentList
	json.NewDecoder(resp.Body).Decode(&list)
	if len(list.Agents) != 1 {
		t.Errorf("expected 1 agent, got %d", len(list.Agents))
	}
}

func TestCreateTaskAndGet(t *testing.T) {
	_, server := newTestServer(t)

	req := map[string]string{
		"protocol_version":   currentProtocolVersion,
		"session_id":         "session-1",
		"initiator_agent_id": "agent-a",
		"target_agent_id":    "agent-b",
	}
	body, _ := json.Marshal(req)
	resp, err := http.Post(server.URL+"/a2a/v1/tasks", "application/json", bytes.NewReader(body))
	if err != nil {
		t.Fatalf("post failed: %v", err)
	}
	if resp.StatusCode != http.StatusCreated {
		t.Fatalf("expected 201, got %d", resp.StatusCode)
	}
	var task models.Task
	json.NewDecoder(resp.Body).Decode(&task)
	if task.Status != "pending" {
		t.Errorf("expected pending, got %q", task.Status)
	}

	resp, err = http.Get(server.URL + "/a2a/v1/tasks/" + task.TaskID)
	if err != nil {
		t.Fatalf("get failed: %v", err)
	}
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("expected 200, got %d", resp.StatusCode)
	}
}

func TestSendMessage(t *testing.T) {
	_, server := newTestServer(t)

	// Register target first.
	card := models.AgentCard{AgentID: "agent-b", Name: "B"}
	body, _ := json.Marshal(card)
	http.Post(server.URL+"/a2a/v1/agents", "application/json", bytes.NewReader(body))

	msg := models.Message{
		MessageID:       "msg-1",
		FromAgentID:     "agent-a",
		ToAgentID:       "agent-b",
		Role:            "user",
		Parts:           []models.Part{{Type: "text", Text: "hello"}},
		Timestamp:       time.Now().UTC(),
		ProtocolVersion: currentProtocolVersion,
	}
	body, _ = json.Marshal(msg)
	resp, err := http.Post(server.URL+"/a2a/v1/messages", "application/json", bytes.NewReader(body))
	if err != nil {
		t.Fatalf("post failed: %v", err)
	}
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("expected 200, got %d", resp.StatusCode)
	}
}

func TestDelegationAllowed(t *testing.T) {
	_, server := newTestServer(t)

	card := models.AgentCard{
		AgentID:      "executor",
		Name:         "Executor",
		Entrypoint:   models.AgentEntrypoint{Type: "http", URL: "http://executor:8080"},
		Capabilities: []string{"delegate_execution"},
	}
	body, _ := json.Marshal(card)
	http.Post(server.URL+"/a2a/v1/agents", "application/json", bytes.NewReader(body))

	req := models.DelegationRequest{
		RequestID:        "req-1",
		InitiatorAgentID: "planner",
		TargetAgentID:    "executor",
		ToolName:         "query_sales",
		ProtocolVersion:  currentProtocolVersion,
	}
	body, _ = json.Marshal(req)
	resp, err := http.Post(server.URL+"/a2a/v1/delegations", "application/json", bytes.NewReader(body))
	if err != nil {
		t.Fatalf("post failed: %v", err)
	}
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("expected 200, got %d", resp.StatusCode)
	}
	var got models.DelegationResponse
	json.NewDecoder(resp.Body).Decode(&got)
	if !got.Allowed {
		t.Fatalf("expected allowed, got %v", got)
	}
}

func TestJSONPostValidation(t *testing.T) {
	_, server := newTestServer(t)

	tests := []struct {
		name   string
		path   string
		body   string
		status int
	}{
		{"unknown field", "/a2a/v1/tasks", `{"session_id":"s","initiator_agent_id":"a","target_agent_id":"b","extra":true}`, http.StatusBadRequest},
		{"multiple values", "/a2a/v1/tasks", `{"session_id":"s"} {"session_id":"s2"}`, http.StatusBadRequest},
		{"oversized", "/a2a/v1/tasks", `{"session_id":"` + strings.Repeat("x", maxJSONBodyBytes) + `"}`, http.StatusRequestEntityTooLarge},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			resp, err := http.Post(server.URL+tc.path, "application/json", strings.NewReader(tc.body))
			if err != nil {
				t.Fatalf("post failed: %v", err)
			}
			defer resp.Body.Close()
			if resp.StatusCode != tc.status {
				t.Fatalf("status = %d, want %d", resp.StatusCode, tc.status)
			}
		})
	}
}

func TestMessagePartFieldPolicy(t *testing.T) {
	_, server := newTestServer(t)

	bodies := []string{
		`{"from_agent_id":"a","to_agent_id":"b","protocol_version":"0.36.1","parts":[{"type":"text","text":"x","data":{}}]}`,
		`{"from_agent_id":"a","to_agent_id":"b","protocol_version":"0.36.1","parts":[{"type":"text","text":null}]}`,
		`{"from_agent_id":"a","to_agent_id":"b","protocol_version":"0.36.1","parts":[{"type":"data","data":null}]}`,
		`{"from_agent_id":"a","to_agent_id":"b","protocol_version":"0.36.1","parts":[{"type":"data"}]}`,
	}
	for _, body := range bodies {
		resp, err := http.Post(server.URL+"/a2a/v1/messages", "application/json", strings.NewReader(body))
		if err != nil {
			t.Fatalf("post failed: %v", err)
		}
		resp.Body.Close()
		if resp.StatusCode != http.StatusBadRequest {
			t.Errorf("body %s: status = %d, want 400", body, resp.StatusCode)
		}
	}
}

func TestMissingProtocolVersionRejected(t *testing.T) {
	_, server := newTestServer(t)
	resp, err := http.Post(server.URL+"/a2a/v1/messages", "application/json", strings.NewReader(`{"from_agent_id":"a","to_agent_id":"b","parts":[{"type":"text","text":"x"}]}`))
	if err != nil {
		t.Fatalf("post failed: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", resp.StatusCode)
	}
}

func TestTaskStreamSSE(t *testing.T) {
	srv, server := newTestServer(t)

	// Create task first.
	req := map[string]string{
		"protocol_version":   currentProtocolVersion,
		"session_id":         "session-1",
		"initiator_agent_id": "agent-a",
		"target_agent_id":    "agent-b",
	}
	body, _ := json.Marshal(req)
	resp, _ := http.Post(server.URL+"/a2a/v1/tasks", "application/json", bytes.NewReader(body))
	var task models.Task
	json.NewDecoder(resp.Body).Decode(&task)

	// Start SSE consumer in background.
	events := make(chan string, 1)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() {
		req, _ := http.NewRequestWithContext(ctx, http.MethodGet, server.URL+"/a2a/v1/tasks/"+task.TaskID+"/stream", nil)
		req.Header.Set("Accept", "text/event-stream")
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			return
		}
		defer resp.Body.Close()
		reader := bufio.NewReader(resp.Body)
		for {
			line, err := reader.ReadString('\n')
			if err != nil {
				return
			}
			if strings.HasPrefix(line, "data: ") {
				events <- line
				return
			}
		}
	}()

	// Trigger a delegation to publish a task event.
	time.Sleep(50 * time.Millisecond)
	srv.registry.Register(models.AgentCard{
		AgentID:      "agent-b",
		Capabilities: []string{"delegate_execution"},
		Entrypoint:   models.AgentEntrypoint{URL: "http://agent-b:8080"},
	})
	dr := models.DelegationRequest{
		RequestID:        "req-1",
		InitiatorAgentID: "agent-a",
		TargetAgentID:    "agent-b",
		ToolName:         "echo",
		TaskID:           task.TaskID,
		ProtocolVersion:  currentProtocolVersion,
	}
	body, _ = json.Marshal(dr)
	http.Post(server.URL+"/a2a/v1/delegations", "application/json", bytes.NewReader(body))

	select {
	case ev := <-events:
		if !strings.Contains(ev, task.TaskID) {
			t.Errorf("expected event to contain task id, got %q", ev)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("timeout waiting for SSE event")
	}
}

func TestTaskStreamLastEventIDReplaysOnlyNewerEvents(t *testing.T) {
	srv, server := newTestServer(t)
	task, err := srv.tasks.CreateReliable("session-replay", "agent-a", "agent-b")
	if err != nil {
		t.Fatalf("create task: %v", err)
	}
	for _, status := range []string{"accepted", "running"} {
		if err := srv.publisher.Publish(context.Background(), models.Task{TaskID: task.TaskID, Status: status}); err != nil {
			t.Fatalf("publish %s: %v", status, err)
		}
	}
	history, err := srv.db.EventStore().ListAfter(context.Background(), task.TaskID, "")
	if err != nil || len(history) != 3 {
		t.Fatalf("history: len=%d err=%v", len(history), err)
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, server.URL+"/a2a/v1/tasks/"+task.TaskID+"/stream", nil)
	req.Header.Set("Accept", "text/event-stream")
	req.Header.Set("Last-Event-ID", history[0].EventID)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("stream request: %v", err)
	}
	defer resp.Body.Close()
	reader := bufio.NewReader(resp.Body)
	var eventTypes []string
	for len(eventTypes) < 2 {
		line, err := reader.ReadString('\n')
		if err != nil {
			t.Fatalf("read stream: %v", err)
		}
		if strings.HasPrefix(line, "event: ") {
			eventTypes = append(eventTypes, strings.TrimSpace(strings.TrimPrefix(line, "event: ")))
		}
	}
	if eventTypes[0] != "task_accepted" || eventTypes[1] != "task_running" {
		t.Fatalf("event types = %#v, want accepted then running", eventTypes)
	}
}

func TestControlAuthBindsInitiatorAndAuthorizesTaskObjects(t *testing.T) {
	srv, server := newTestServer(t)
	srv.SetControlAuth("control-secret", "agent-a")
	own, err := srv.tasks.CreateReliable("session-own", "agent-a", "agent-b")
	if err != nil {
		t.Fatalf("create own task: %v", err)
	}
	other, err := srv.tasks.CreateReliable("session-other", "agent-x", "agent-b")
	if err != nil {
		t.Fatalf("create other task: %v", err)
	}

	do := func(method, path, bearer string, body []byte) *http.Response {
		t.Helper()
		req, err := http.NewRequest(method, server.URL+path, bytes.NewReader(body))
		if err != nil {
			t.Fatalf("new request: %v", err)
		}
		if bearer != "" {
			req.Header.Set("Authorization", "Bearer "+bearer)
		}
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatalf("request: %v", err)
		}
		return resp
	}

	tests := []struct {
		name, method, path, bearer string
		body                       []byte
		want                       int
	}{
		{name: "missing bearer", method: http.MethodGet, path: "/a2a/v1/tasks/" + own.TaskID, want: http.StatusUnauthorized},
		{name: "wrong bearer", method: http.MethodGet, path: "/a2a/v1/tasks/" + own.TaskID, bearer: "wrong", want: http.StatusUnauthorized},
		{name: "query own", method: http.MethodGet, path: "/a2a/v1/tasks/" + own.TaskID, bearer: "control-secret", want: http.StatusOK},
		{name: "query other", method: http.MethodGet, path: "/a2a/v1/tasks/" + other.TaskID, bearer: "control-secret", want: http.StatusForbidden},
		{name: "stream other", method: http.MethodGet, path: "/a2a/v1/tasks/" + other.TaskID + "/stream", bearer: "control-secret", want: http.StatusForbidden},
		{name: "cancel other", method: http.MethodPost, path: "/a2a/v1/tasks/" + other.TaskID + "/cancel", bearer: "control-secret", body: []byte(`{"protocol_version":"` + currentProtocolVersion + `"}`), want: http.StatusForbidden},
		{name: "create spoofed", method: http.MethodPost, path: "/a2a/v1/tasks", bearer: "control-secret", body: []byte(`{"protocol_version":"` + currentProtocolVersion + `","session_id":"s","initiator_agent_id":"agent-x","target_agent_id":"agent-b"}`), want: http.StatusForbidden},
		{name: "create bound", method: http.MethodPost, path: "/a2a/v1/tasks", bearer: "control-secret", body: []byte(`{"protocol_version":"` + currentProtocolVersion + `","session_id":"s","initiator_agent_id":"agent-a","target_agent_id":"agent-b"}`), want: http.StatusCreated},
	}
	for _, tc := range tests {
		resp := do(tc.method, tc.path, tc.bearer, tc.body)
		defer resp.Body.Close()
		if resp.StatusCode != tc.want {
			t.Errorf("%s: status = %d, want %d", tc.name, resp.StatusCode, tc.want)
		}
	}
}

func TestDelegationApprovalAPIAuthTransitionsAndReplay(t *testing.T) {
	srv, server := newTestServer(t)
	srv.SetControlAuth("initiator-token", "planner")
	srv.SetApprovalAuth("approver-token", "reviewer")
	now := time.Now().UTC()
	codes := func(method, path, bearer, body string) int {
		req, _ := http.NewRequest(method, server.URL+path, strings.NewReader(body))
		if bearer != "" {
			req.Header.Set("Authorization", "Bearer "+bearer)
		}
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		return resp.StatusCode
	}
	seed := func(id, status string) models.DelegationApproval {
		a := models.DelegationApproval{ApprovalID: id, RequestID: "req-" + id, DecisionID: "decision-" + id, RequestHash: "hash-" + id, InitiatorAgentID: "planner", TargetAgentID: "executor", EffectiveArgs: json.RawMessage(`{}`), ExpiresAt: now.Add(time.Hour), Status: status, CreatedAt: now, UpdatedAt: now, Version: 1}
		if _, _, err := srv.db.DelegationApprovalStore().Create(context.Background(), a); err != nil {
			t.Fatal(err)
		}
		return a
	}
	pending := seed("approval-reject", "pending")
	base := "/a2a/v1/delegation-approvals/" + pending.ApprovalID
	if got := codes(http.MethodGet, base, "", ""); got != http.StatusUnauthorized {
		t.Fatalf("unauthenticated GET = %d", got)
	}
	if got := codes(http.MethodGet, base, "initiator-token", ""); got != http.StatusOK {
		t.Fatalf("authorized GET = %d", got)
	}
	if got := codes(http.MethodPost, base+"/reject", "initiator-token", `{"request_id":"reject-1","reason":"no"}`); got != http.StatusUnauthorized {
		t.Fatalf("initiator reject = %d", got)
	}
	if got := codes(http.MethodPost, base+"/reject", "approver-token", `{}`); got != http.StatusBadRequest {
		t.Fatalf("missing replay key = %d", got)
	}
	body := `{"request_id":"reject-1","reason":"no"}`
	if got := codes(http.MethodPost, base+"/reject", "approver-token", body); got != http.StatusOK {
		t.Fatalf("reject = %d", got)
	}
	stored, _ := srv.db.DelegationApprovalStore().Get(context.Background(), pending.ApprovalID)
	if stored.Status != "rejected" || stored.ApproverID != "reviewer" {
		t.Fatalf("principal not recorded: %+v", stored)
	}
	if got := codes(http.MethodPost, base+"/reject", "approver-token", body); got != http.StatusOK {
		t.Fatalf("same replay = %d", got)
	}
	if got := codes(http.MethodPost, base+"/reject", "approver-token", `{"request_id":"reject-1","reason":"changed"}`); got != http.StatusConflict {
		t.Fatalf("changed replay = %d", got)
	}
}

func TestDelegationApprovalListScopesToControlInitiator(t *testing.T) {
	srv, server := newTestServer(t)
	srv.SetControlAuth("initiator-token", "planner")
	now := time.Now().UTC()
	seed := func(id, initiator, status string) {
		a := models.DelegationApproval{ApprovalID: id, RequestID: "req-" + id, DecisionID: "decision-" + id, RequestHash: "hash-" + id, InitiatorAgentID: initiator, TargetAgentID: "executor", EffectiveArgs: json.RawMessage(`{}`), ExpiresAt: now.Add(time.Hour), Status: status, CreatedAt: now, UpdatedAt: now, Version: 1}
		if _, _, err := srv.db.DelegationApprovalStore().Create(context.Background(), a); err != nil {
			t.Fatal(err)
		}
	}
	seed("approval-own-pending", "planner", "pending")
	seed("approval-own-consumed", "planner", "consumed")
	seed("approval-other", "other-agent", "pending")

	get := func(path, bearer string) *http.Response {
		req, _ := http.NewRequest(http.MethodGet, server.URL+path, nil)
		if bearer != "" {
			req.Header.Set("Authorization", "Bearer "+bearer)
		}
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		return resp
	}

	resp := get("/a2a/v1/delegation-approvals", "")
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("unauthenticated list = %d", resp.StatusCode)
	}
	resp.Body.Close()

	resp = get("/a2a/v1/delegation-approvals", "initiator-token")
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("authorized list = %d", resp.StatusCode)
	}
	var body struct {
		Approvals []models.DelegationApproval `json:"approvals"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		t.Fatal(err)
	}
	if len(body.Approvals) != 2 {
		t.Fatalf("expected 2 scoped approvals, got %d", len(body.Approvals))
	}
	for _, approval := range body.Approvals {
		if approval.InitiatorAgentID != "planner" {
			t.Errorf("approval leaked from another initiator: %s", approval.ApprovalID)
		}
	}

	resp = get("/a2a/v1/delegation-approvals?status=pending", "initiator-token")
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status-filtered list = %d", resp.StatusCode)
	}
	body = struct {
		Approvals []models.DelegationApproval `json:"approvals"`
	}{}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		t.Fatal(err)
	}
	if len(body.Approvals) != 1 || body.Approvals[0].ApprovalID != "approval-own-pending" {
		t.Fatalf("status filter mismatch: %+v", body.Approvals)
	}
}

func TestDelegationApprovalCancelConsumedDelegatesToTask(t *testing.T) {
	srv, server := newTestServer(t)
	srv.SetControlAuth("initiator-token", "planner")
	now := time.Now().UTC()
	taskValue := models.Task{ProtocolVersion: currentProtocolVersion, TaskID: "approval-task", SessionID: "s", InteractionID: "i", DecisionID: "d", InitiatorAgentID: "planner", TargetAgentID: "executor", Status: "pending", CreatedAt: now, UpdatedAt: now}
	if err := srv.db.TaskStore().Create(context.Background(), taskValue); err != nil {
		t.Fatal(err)
	}
	a := models.DelegationApproval{ApprovalID: "approval-consumed", RequestID: "req-consumed", DecisionID: "decision-consumed", RequestHash: "hash", InitiatorAgentID: "planner", TargetAgentID: "executor", EffectiveArgs: json.RawMessage(`{}`), ExpiresAt: now.Add(time.Hour), Status: "consumed", TaskID: taskValue.TaskID, CreatedAt: now, UpdatedAt: now, Version: 2}
	if _, _, err := srv.db.DelegationApprovalStore().Create(context.Background(), a); err != nil {
		t.Fatal(err)
	}
	req, _ := http.NewRequest(http.MethodPost, server.URL+"/a2a/v1/delegation-approvals/approval-consumed/cancel", strings.NewReader(`{"request_id":"cancel-1"}`))
	req.Header.Set("Authorization", "Bearer initiator-token")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("cancel consumed = %d", resp.StatusCode)
	}
	storedApproval, _ := srv.db.DelegationApprovalStore().Get(context.Background(), a.ApprovalID)
	storedTask, _ := srv.tasks.Get(taskValue.TaskID)
	if storedApproval.Status != "consumed" || storedTask.Status != "cancelled" {
		t.Fatalf("approval=%s task=%s", storedApproval.Status, storedTask.Status)
	}
}

func muxFor(srv *Server) *http.ServeMux {
	mux := http.NewServeMux()
	srv.RegisterRoutes(mux)
	return mux
}

type recordingEntrypointClient struct {
	mu          sync.Mutex
	dispatched  []models.EntrypointTaskRequest
	entrypoints []models.AgentEntrypoint
}

func (c *recordingEntrypointClient) Dispatch(_ context.Context, ep models.AgentEntrypoint, req models.EntrypointTaskRequest) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.dispatched = append(c.dispatched, req)
	c.entrypoints = append(c.entrypoints, ep)
	return nil
}

func (c *recordingEntrypointClient) Cancel(context.Context, models.AgentEntrypoint, string, string) (bool, error) {
	return true, nil
}

func (c *recordingEntrypointClient) waitDispatch(t *testing.T, timeout time.Duration) (models.AgentEntrypoint, models.EntrypointTaskRequest) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		c.mu.Lock()
		if len(c.dispatched) > 0 {
			ep, req := c.entrypoints[0], c.dispatched[0]
			c.mu.Unlock()
			return ep, req
		}
		c.mu.Unlock()
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatal("timed out waiting for entrypoint dispatch")
	return models.AgentEntrypoint{}, models.EntrypointTaskRequest{}
}

type sequenceR2Authorizer struct {
	decisions []models.DelegationResponse
	calls     int
}

func (a *sequenceR2Authorizer) Authorize(context.Context, models.DelegationRequest) (models.DelegationResponse, error) {
	d := a.decisions[a.calls]
	a.calls++
	return d, nil
}

// TestApprovalConsumptionDispatchesViaOutboxDispatcher verifies the full loop:
// require_approval delegation → approve → ResumeApproval consumes the approval
// and enqueues the dispatch outbox → the dispatcher started by
// SetEntrypointClient delivers the task to the target entrypoint.
func TestApprovalConsumptionDispatchesViaOutboxDispatcher(t *testing.T) {
	srv, server := newTestServer(t)
	srv.SetApprovalAuth("approver-token", "reviewer")
	srv.SetR2Authorizer(&sequenceR2Authorizer{decisions: []models.DelegationResponse{
		{Allowed: false, Verdict: "require_approval", DecisionID: "approval-decision", Reason: "review"},
		{Allowed: true, Verdict: "allow", DecisionID: "allow-decision"},
	}})

	card := models.AgentCard{
		AgentID:      "executor",
		Name:         "Executor",
		Entrypoint:   models.AgentEntrypoint{Type: "http", URL: "http://executor.local"},
		Capabilities: []string{"delegate_execution"},
	}
	body, _ := json.Marshal(card)
	resp, err := http.Post(server.URL+"/a2a/v1/agents", "application/json", bytes.NewReader(body))
	if err != nil || resp.StatusCode != http.StatusCreated {
		t.Fatalf("register agent: %v status=%d", err, statusOf(resp))
	}
	resp.Body.Close()

	client := &recordingEntrypointClient{}
	srv.SetEntrypointClient(client)

	delegationBody := `{"protocol_version":"` + currentProtocolVersion + `","request_id":"req-approval","session_id":"s-1","initiator_agent_id":"planner","target_agent_id":"executor","tool_name":"echo","arguments":{"x":1},"allowed_tools":["echo"],"budget":{}}`
	resp, err = http.Post(server.URL+"/a2a/v1/delegations", "application/json", strings.NewReader(delegationBody))
	if err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode != http.StatusAccepted {
		t.Fatalf("delegation status=%d", resp.StatusCode)
	}
	var delegationResp models.DelegationResponse
	if err := json.NewDecoder(resp.Body).Decode(&delegationResp); err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if delegationResp.ApprovalID == "" {
		t.Fatal("expected approval id in require_approval response")
	}

	approveBody := `{"request_id":"approve-1","reason":"ok"}`
	approveReq, _ := http.NewRequest(http.MethodPost, server.URL+"/a2a/v1/delegation-approvals/"+delegationResp.ApprovalID+"/approve", strings.NewReader(approveBody))
	approveReq.Header.Set("Authorization", "Bearer approver-token")
	resp, err = http.DefaultClient.Do(approveReq)
	if err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("approve status=%d", resp.StatusCode)
	}
	var consumed models.DelegationApproval
	if err := json.NewDecoder(resp.Body).Decode(&consumed); err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if consumed.Status != "consumed" || consumed.TaskID == "" {
		t.Fatalf("expected consumed approval with task, got %+v", consumed)
	}

	ep, req := client.waitDispatch(t, 5*time.Second)
	if ep.URL != "http://executor.local" {
		t.Fatalf("dispatched entrypoint = %q", ep.URL)
	}
	if req.TaskID != consumed.TaskID {
		t.Fatalf("dispatched task = %q, want %q", req.TaskID, consumed.TaskID)
	}
	if req.DelegationToken == "" || req.DeliveryID == "" {
		t.Fatalf("dispatch missing token/delivery: %+v", req)
	}
}

func statusOf(resp *http.Response) int {
	if resp == nil {
		return 0
	}
	return resp.StatusCode
}
