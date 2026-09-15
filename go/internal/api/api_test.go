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
	"testing"
	"time"

	"github.com/loop-controller/go/internal/delegation"
	"github.com/loop-controller/go/internal/entrypointpolicy"
	"github.com/loop-controller/go/internal/models"
	"github.com/loop-controller/go/internal/store"
)

var testSecret = []byte("test-secret")

func TestDAGRoutesDefaultOff(t *testing.T) {
	srv, err := NewServer(testSecret, filepath.Join(t.TempDir(), "dag-off.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer srv.Close()
	req := httptest.NewRequest(http.MethodPost, "/a2a/v1/task-graphs", strings.NewReader(`{}`))
	res := httptest.NewRecorder()
	muxFor(srv).ServeHTTP(res, req)
	if res.Code != http.StatusNotFound {
		t.Fatalf("status=%d body=%s", res.Code, res.Body.String())
	}
}

func TestServerCloseWaitsForDispatcher(t *testing.T) {
	srv, err := NewServer(testSecret, filepath.Join(t.TempDir(), "close.db"))
	if err != nil {
		t.Fatal(err)
	}
	notifier := &blockingNotifier{started: make(chan struct{}), stopped: make(chan struct{})}
	srv.SetApprovalNotifier(notifier)
	now := time.Now().UTC()
	a := models.DelegationApproval{ApprovalID: "close-wait", RequestID: "request-close", DecisionID: "decision-close", RequestHash: "h", InitiatorAgentID: "a", TargetAgentID: "b", ExpiresAt: now.Add(time.Hour), Status: "pending", CreatedAt: now, UpdatedAt: now, Version: 1}
	if _, _, err := srv.db.DelegationApprovalStore().Create(context.Background(), a); err != nil {
		t.Fatal(err)
	}
	select {
	case <-notifier.started:
	case <-time.After(2 * time.Second):
		t.Fatal("dispatcher did not start")
	}
	if err := srv.Close(); err != nil {
		t.Fatal(err)
	}
	select {
	case <-notifier.stopped:
	default:
		t.Fatal("Close returned before dispatcher exited")
	}
}

type blockingNotifier struct {
	started chan struct{}
	stopped chan struct{}
}

func (*blockingNotifier) DestinationURL() string { return "http://approval-webhook.invalid/" }
func (n *blockingNotifier) NotifyApproval(ctx context.Context, _ string, _ store.ApprovalNotification) error {
	close(n.started)
	<-ctx.Done()
	close(n.stopped)
	return ctx.Err()
}

func newTestServer(t *testing.T) (*Server, *httptest.Server) {
	t.Helper()
	srv, err := NewServer(testSecret, filepath.Join(t.TempDir(), "a2a-test.db"))
	if err != nil {
		t.Fatalf("new server: %v", err)
	}
	srv.SetEntrypointPolicy(entrypointpolicy.Development())
	srv.SetR2Authorizer(&delegation.StaticR2Authorizer{
		Decision: models.DelegationResponse{Allowed: true},
	})
	srv.EnableDAG()
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

func TestDeadLetterRoutesRequireControlAuthAndReplayAudits(t *testing.T) {
	srv, server := newTestServer(t)
	srv.SetControlAuth("control-secret", "control-agent")
	ctx := context.Background()
	now := time.Now().UTC()
	deadline := now.Add(time.Hour)
	task := models.Task{TaskID: "dead-task", SessionID: "s", InitiatorAgentID: "control-agent", TargetAgentID: "target", TenantID: "tenant", Status: "accepted", Deadline: &deadline, CreatedAt: now, UpdatedAt: now}
	if err := srv.db.TaskStore().Create(ctx, task); err != nil {
		t.Fatal(err)
	}
	_, _, err := srv.db.ExecutionQueueStore().EnqueueAcceptedTask(ctx, store.EnqueueAcceptedTaskParams{Kind: models.AssignmentKindTargetExecution, AssignmentID: "dead-assignment", DeliveryID: "dead-delivery", TaskID: task.TaskID, TenantID: task.TenantID, TargetAgentID: task.TargetAgentID, Deadline: &deadline, RetryPolicy: &models.RetryPolicy{MaxAttempts: 3, InitialBackoff: time.Second, MaxBackoff: time.Minute, BackoffMultiplier: 2, RetryableFailureClasses: []models.FailureClass{models.FailureClassPreDispatchTransient}}})
	if err != nil {
		t.Fatal(err)
	}
	if _, err = srv.db.ExecContext(ctx, `UPDATE task_assignments SET state='dead_letter',revision=revision+1,execution_fence=execution_fence+1,failure_class='pre_dispatch_permanent',error_code='dead_lettered' WHERE assignment_id='dead-assignment'`); err != nil {
		t.Fatal(err)
	}
	if _, err = srv.db.ExecContext(ctx, `UPDATE tasks SET status='failed',error_code='dead_lettered' WHERE task_id='dead-task'`); err != nil {
		t.Fatal(err)
	}
	dead, _ := srv.db.AssignmentStore().Get(ctx, "dead-assignment")

	do := func(method, path, token, correlation string, body []byte) *http.Response {
		req, reqErr := http.NewRequest(method, server.URL+path, bytes.NewReader(body))
		if reqErr != nil {
			t.Fatal(reqErr)
		}
		if token != "" {
			req.Header.Set("Authorization", "Bearer "+token)
		}
		if correlation != "" {
			req.Header.Set("X-Correlation-ID", correlation)
		}
		resp, reqErr := http.DefaultClient.Do(req)
		if reqErr != nil {
			t.Fatal(reqErr)
		}
		return resp
	}
	resp := do(http.MethodGet, "/a2a/v1/dead-letters?tenant_id=tenant", "", "", nil)
	resp.Body.Close()
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("unauthorized list=%d", resp.StatusCode)
	}
	resp = do(http.MethodGet, "/a2a/v1/dead-letters?tenant_id=other", "control-secret", "", nil)
	var listed struct {
		Assignments []models.TaskAssignment `json:"assignments"`
	}
	_ = json.NewDecoder(resp.Body).Decode(&listed)
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK || len(listed.Assignments) != 0 {
		t.Fatalf("cross tenant list=%d %+v", resp.StatusCode, listed)
	}
	body, _ := json.Marshal(map[string]any{"tenant_id": "tenant", "expected_revision": dead.Revision})
	resp = do(http.MethodPost, "/a2a/v1/dead-letters/dead-assignment/replay", "control-secret", "corr-1", body)
	var replay models.TaskAssignment
	_ = json.NewDecoder(resp.Body).Decode(&replay)
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK || replay.State != models.AssignmentStateQueued || replay.ExecutionFence != dead.ExecutionFence+1 || replay.Revision != dead.Revision+1 {
		t.Fatalf("replay status=%d assignment=%+v dead=%+v", resp.StatusCode, replay, dead)
	}
	var actor, action, result, correlation string
	if err = srv.db.QueryRow(`SELECT actor,action,result,correlation_id FROM assignment_retry_events WHERE assignment_id=? AND event_type='replayed'`, replay.AssignmentID).Scan(&actor, &action, &result, &correlation); err != nil || actor != "control-agent" || action != "replay" || result != "success" || correlation != "corr-1" {
		t.Fatalf("audit=%q/%q/%q/%q err=%v", actor, action, result, correlation, err)
	}
	resp = do(http.MethodPost, "/a2a/v1/dead-letters/dead-assignment/replay", "control-secret", "corr-2", body)
	resp.Body.Close()
	if resp.StatusCode != http.StatusConflict {
		t.Fatalf("stale replay=%d", resp.StatusCode)
	}
}

func TestAgentRoutesRequireConfiguredControlAuth(t *testing.T) {
	srv, server := newTestServer(t)
	srv.SetControlAuth("control-secret", "control-agent")
	card := models.AgentCard{AgentID: "agent-auth", Name: "Auth Test"}
	body, _ := json.Marshal(card)

	do := func(method, path, bearer string, body []byte) *http.Response {
		t.Helper()
		req, err := http.NewRequest(method, server.URL+path, bytes.NewReader(body))
		if err != nil {
			t.Fatal(err)
		}
		if bearer != "" {
			req.Header.Set("Authorization", "Bearer "+bearer)
		}
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		return resp
	}

	for _, tc := range []struct {
		method, path string
		body         []byte
	}{
		{http.MethodPost, "/a2a/v1/agents", body},
		{http.MethodGet, "/a2a/v1/agents", nil},
		{http.MethodGet, "/a2a/v1/agents/agent-auth", nil},
	} {
		resp := do(tc.method, tc.path, "", tc.body)
		resp.Body.Close()
		if resp.StatusCode != http.StatusUnauthorized {
			t.Fatalf("%s %s without token=%d", tc.method, tc.path, resp.StatusCode)
		}
	}

	resp := do(http.MethodPost, "/a2a/v1/agents", "control-secret", body)
	resp.Body.Close()
	if resp.StatusCode != http.StatusCreated {
		t.Fatalf("authorized POST=%d", resp.StatusCode)
	}
	for _, path := range []string{"/a2a/v1/agents", "/a2a/v1/agents/agent-auth"} {
		resp = do(http.MethodGet, path, "control-secret", nil)
		resp.Body.Close()
		if resp.StatusCode != http.StatusOK {
			t.Fatalf("authorized GET %s=%d", path, resp.StatusCode)
		}
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

func TestServerCloseDisconnectsTaskStreams(t *testing.T) {
	srv, err := NewServer([]byte("test-secret"), filepath.Join(t.TempDir(), "close.db"))
	if err != nil {
		t.Fatal(err)
	}
	task, err := srv.tasks.CreateReliable("session-close", "agent-a", "agent-b")
	if err != nil {
		t.Fatal(err)
	}
	ch, err := srv.publisher.Subscribe(context.Background(), task.TaskID, "")
	if err != nil {
		t.Fatal(err)
	}
	if err := srv.Close(); err != nil {
		t.Fatal(err)
	}
	if err := srv.Close(); err != nil {
		t.Fatal(err)
	}
	select {
	case _, ok := <-ch:
		if ok {
			t.Fatal("stream remained open after Server.Close")
		}
	case <-time.After(time.Second):
		t.Fatal("stream did not close with server")
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
	req.Header.Set("Last-Event-ID", history[0].Cursor)
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

func TestControlAuthTenantScopePreventsQueryOverrideAndEnumeration(t *testing.T) {
	srv, server := newTestServer(t)
	srv.SetControlAuthForTenant("control-secret", "owner", "tenant-a")
	now := time.Now().UTC()
	for _, task := range []models.Task{
		{TaskID: "task-a", SessionID: "s", TenantID: "tenant-a", InitiatorAgentID: "owner", TargetAgentID: "agent", Status: "accepted", CreatedAt: now, UpdatedAt: now},
		{TaskID: "task-b", SessionID: "s", TenantID: "tenant-b", Budget: models.DelegationBudget{Currency: "USD"}, InitiatorAgentID: "owner", TargetAgentID: "agent", Status: "accepted", CreatedAt: now, UpdatedAt: now},
	} {
		if err := srv.db.TaskStore().Create(context.Background(), task); err != nil {
			t.Fatal(err)
		}
	}
	graph := models.TaskGraphCreate{ProtocolVersion: models.DAGProtocolVersion, DAGID: "dag-b", TenantID: "tenant-b", RootTaskID: "task-b", MaxParallelism: 1, BudgetEnvelope: models.DelegationBudget{Currency: "USD"}, Nodes: []models.TaskNode{{NodeID: "n", TaskID: "node-b", FailurePolicy: models.TaskFailurePolicyFailFast, BudgetLimit: models.DelegationBudget{Currency: "USD"}, SchedulingRequest: models.SchedulingRequest{RequestID: "r", TenantID: "tenant-b", TaskID: "node-b", DAGID: "dag-b", NodeID: "n"}}}}
	node := models.Task{TaskID: "node-b", SessionID: "s", TenantID: "tenant-b", RootTaskID: "task-b", ParentTaskID: "task-b", Budget: models.DelegationBudget{Currency: "USD"}, InitiatorAgentID: "owner", TargetAgentID: "agent", Status: "accepted", CreatedAt: now, UpdatedAt: now}
	if err := srv.db.TaskStore().Create(context.Background(), node); err != nil {
		t.Fatal(err)
	}
	if _, err := srv.db.DAGStore().CreateGraph(context.Background(), graph, "key-b", now); err != nil {
		t.Fatal(err)
	}

	status := func(path string) int {
		req, _ := http.NewRequest(http.MethodGet, server.URL+path, nil)
		req.Header.Set("Authorization", "Bearer control-secret")
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		return resp.StatusCode
	}
	if got := status("/a2a/v1/tasks/task-a?tenant_id=tenant-b"); got != http.StatusOK {
		t.Fatalf("trusted tenant ignored due to query override: %d", got)
	}
	for _, path := range []string{
		"/a2a/v1/tasks/task-b",
		"/a2a/v1/tasks/task-b/stream?cursor=forged",
		"/a2a/v1/tasks/task-b/snapshot?tenant_id=tenant-b",
		"/a2a/v1/task-graphs/dag-b?tenant_id=tenant-b",
	} {
		if got := status(path); got != http.StatusNotFound {
			t.Errorf("%s status=%d want 404", path, got)
		}
	}
	if got := status("/a2a/v1/tasks/task-a/snapshot"); got != http.StatusOK {
		t.Fatalf("own snapshot status=%d", got)
	}
	if got := status("/a2a/v1/dead-letters?tenant_id=tenant-b"); got != http.StatusNotFound {
		t.Fatalf("dead-letter tenant override status=%d", got)
	}
}

func TestControlAuthTenantScopeIsAppliedToAgentSQL(t *testing.T) {
	srv, server := newTestServer(t)
	srv.SetControlAuthForTenant("control-secret", "owner", "tenant-a")
	ctx := context.Background()
	for _, card := range []models.AgentCard{
		{AgentID: "agent-a", TenantID: "tenant-a", Name: "A"},
		{AgentID: "agent-b", TenantID: "tenant-b", Name: "B"},
	} {
		if err := srv.db.AgentStore().Upsert(ctx, card); err != nil {
			t.Fatal(err)
		}
	}
	request := func(path string) *http.Response {
		req, _ := http.NewRequest(http.MethodGet, server.URL+path, nil)
		req.Header.Set("Authorization", "Bearer control-secret")
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		return resp
	}
	resp := request("/a2a/v1/agents")
	defer resp.Body.Close()
	var list models.AgentList
	if err := json.NewDecoder(resp.Body).Decode(&list); err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode != http.StatusOK || len(list.Agents) != 1 || list.Agents[0].AgentID != "agent-a" {
		t.Fatalf("status=%d agents=%+v", resp.StatusCode, list.Agents)
	}
	resp = request("/a2a/v1/agents/agent-b")
	resp.Body.Close()
	if resp.StatusCode != http.StatusNotFound {
		t.Fatalf("cross-tenant agent GET=%d want 404", resp.StatusCode)
	}
	resp = request("/a2a/v1/agents/agent-a")
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("own agent GET=%d", resp.StatusCode)
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
