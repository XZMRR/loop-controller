package api

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/loop-controller/go/internal/delegation"
	"github.com/loop-controller/go/internal/execution"
	"github.com/loop-controller/go/internal/models"
	"github.com/loop-controller/go/internal/token"
)

func newTestServerWithMockR2(t *testing.T, allowed bool) (*Server, *httptest.Server) {
	srv, server := newTestServer(t)
	decision := models.DelegationResponse{Allowed: allowed, Reason: "mock R2"}
	if !allowed {
		decision.Reason = "mock R2 denied"
	}
	srv.SetR2Authorizer(&delegation.StaticR2Authorizer{Decision: decision})
	return srv, server
}

func registerExecutor(t *testing.T, server *httptest.Server) {
	t.Helper()
	registerExecutorAtURL(t, server, "http://executor:8080")
}

func registerExecutorAtURL(t *testing.T, server *httptest.Server, entrypointURL string) {
	t.Helper()
	card := models.AgentCard{
		AgentID:      "executor",
		Name:         "Executor",
		Entrypoint:   models.AgentEntrypoint{Type: "http", URL: entrypointURL},
		Capabilities: []string{"delegate_execution", "echo_capability"},
	}
	body, _ := json.Marshal(card)
	resp, err := http.Post(server.URL+"/a2a/v1/agents", "application/json", bytes.NewReader(body))
	if err != nil {
		t.Fatalf("register agent: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusCreated {
		t.Fatalf("expected 201, got %d", resp.StatusCode)
	}
}

func createDelegation(t *testing.T, server *httptest.Server) (models.Task, string) {
	return createDelegationWithTenant(t, server, "")
}

func createDelegationWithTenant(t *testing.T, server *httptest.Server, tenant string) (models.Task, string) {
	t.Helper()
	req := models.DelegationRequest{
		RequestID:           "req-entrypoint-1",
		InitiatorAgentID:    "planner",
		TargetAgentID:       "executor",
		ToolName:            "echo",
		Arguments:           json.RawMessage(`{"x":"hello"}`),
		SessionID:           "session-entrypoint-1",
		ProtocolVersion:     currentProtocolVersion,
		AllowedTools:        []string{"echo"},
		AllowedCapabilities: []string{"echo_capability"},
		TenantID:            tenant,
	}
	body, _ := json.Marshal(req)
	resp, err := http.Post(server.URL+"/a2a/v1/delegations", "application/json", bytes.NewReader(body))
	if err != nil {
		t.Fatalf("delegation request: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("expected 200, got %d", resp.StatusCode)
	}
	var d models.DelegationResponse
	if err := json.NewDecoder(resp.Body).Decode(&d); err != nil {
		t.Fatalf("decode delegation response: %v", err)
	}
	if !d.Allowed || d.TaskID == "" || d.DelegationToken == "" {
		t.Fatalf("unexpected delegation response: %+v", d)
	}

	getResp, err := http.Get(server.URL + "/a2a/v1/tasks/" + d.TaskID)
	if err != nil {
		t.Fatalf("get task: %v", err)
	}
	defer getResp.Body.Close()
	var task models.Task
	if err := json.NewDecoder(getResp.Body).Decode(&task); err != nil {
		t.Fatalf("decode task: %v", err)
	}
	return task, d.DelegationToken
}

func cancelRequestBody() []byte {
	return []byte(`{"protocol_version":"` + currentProtocolVersion + `","reason":"test"}`)
}

func postEntrypoint(t *testing.T, url string, body []byte, delegationToken string) (*http.Response, error) {
	t.Helper()
	req, err := http.NewRequest(http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+delegationToken)
	return http.DefaultClient.Do(req)
}

func getEntrypoint(t *testing.T, url, delegationToken string) *http.Response {
	t.Helper()
	req, err := http.NewRequest(http.MethodGet, url, nil)
	if err != nil {
		t.Fatalf("create entrypoint request: %v", err)
	}
	req.Header.Set("Authorization", "Bearer "+delegationToken)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("entrypoint request: %v", err)
	}
	return resp
}

func entrypointRequest(t *testing.T, server *httptest.Server, task models.Task, token string) models.EntrypointTaskRequest {
	t.Helper()
	return models.EntrypointTaskRequest{
		ProtocolVersion:     currentProtocolVersion,
		RequestID:           "req-entrypoint-1",
		InteractionID:       task.InteractionID,
		DecisionID:          task.DecisionID,
		RootInteractionID:   task.RootInteractionID,
		ParentInteractionID: task.ParentInteractionID,
		RootTaskID:          task.RootTaskID,
		ParentTaskID:        task.ParentTaskID,
		DelegationDepth:     task.DelegationDepth,
		Deadline:            task.Deadline,
		Budget:              task.Budget,
		TaskID:              task.TaskID,
		SessionID:           task.SessionID,
		InitiatorAgentID:    task.InitiatorAgentID,
		TargetAgentID:       task.TargetAgentID,
		ToolName:            "echo",
		Arguments:           json.RawMessage(`{"x":"hello"}`),
		DelegationToken:     token,
		AllowedTools:        []string{"echo"},
		AllowedCapabilities: []string{"echo_capability"},
		TenantID:            task.TenantID,
		TargetWorkloadID:    task.TargetWorkloadID,
		TargetInstanceID:    task.TargetInstanceID,
	}
}

func issueEntrypointToken(t *testing.T, srv *Server, req models.EntrypointTaskRequest) string {
	t.Helper()
	claims := token.DelegationClaims{
		RequestID:           req.RequestID,
		SessionID:           req.SessionID,
		InteractionID:       req.InteractionID,
		DecisionID:          req.DecisionID,
		RootInteractionID:   req.RootInteractionID,
		ParentInteractionID: req.ParentInteractionID,
		InitiatorAgentID:    req.InitiatorAgentID,
		TargetAgentID:       req.TargetAgentID,
		ToolName:            req.ToolName,
		TaskID:              req.TaskID,
		ArgumentsSHA256:     token.HashArguments(req.Arguments),
		AllowedTools:        req.AllowedTools,
		AllowedCapabilities: req.AllowedCapabilities,
		AllowRedelegation:   req.AllowRedelegation,
		RootTaskID:          req.RootTaskID,
		ParentTaskID:        req.ParentTaskID,
		DelegationDepth:     req.DelegationDepth,
		BudgetTokenCount:    req.Budget.TokenCount,
		BudgetPaymentAmount: req.Budget.PaymentAmount,
		BudgetCurrency:      req.Budget.Currency,
	}
	if req.Deadline != nil {
		claims.Deadline = req.Deadline.Unix()
	}
	value, err := srv.issuer.Issue(claims, time.Hour)
	if err != nil {
		t.Fatalf("issue entrypoint token: %v", err)
	}
	return value
}

func TestEntrypointCreateTaskAndPublishEvent(t *testing.T) {
	srv, server := newTestServerWithMockR2(t, true)
	registerExecutor(t, server)
	task, token := createDelegation(t, server)

	req := entrypointRequest(t, server, task, token)
	body, _ := json.Marshal(req)
	resp, err := postEntrypoint(t, server.URL+"/a2a/v1/entrypoint/tasks", body, token)
	if err != nil {
		t.Fatalf("entrypoint create: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusCreated {
		t.Fatalf("expected 201, got %d", resp.StatusCode)
	}
	var got models.Task
	if err := json.NewDecoder(resp.Body).Decode(&got); err != nil {
		t.Fatalf("decode task: %v", err)
	}
	if got.Status != "pending" {
		t.Errorf("expected pending, got %q", got.Status)
	}
	if got.TaskID != task.TaskID {
		t.Errorf("task_id = %q, want %q", got.TaskID, task.TaskID)
	}

	// An event was published; SSE store should hold it.
	pending, err := srv.db.EventStore().ListPending(context.Background(), task.TaskID)
	if err != nil {
		t.Fatalf("list pending events: %v", err)
	}
	if len(pending) == 0 {
		t.Errorf("expected at least one pending event")
	}
}

func TestEntrypointCreateNewLocalTaskCommitsCreatedEvent(t *testing.T) {
	srv, server := newTestServerWithMockR2(t, true)
	registerExecutor(t, server)
	arguments := json.RawMessage(`{"x":"hello"}`)
	taskID := "task-remote-only"
	tokenString, err := srv.issuer.Issue(token.DelegationClaims{
		RequestID:        "req-remote-only",
		SessionID:        "session-remote-only",
		InitiatorAgentID: "planner",
		TargetAgentID:    "executor",
		ToolName:         "echo",
		TaskID:           taskID,
		ArgumentsSHA256:  token.HashArguments(arguments),
		AllowedTools:     []string{"echo"},
	}, time.Hour)
	if err != nil {
		t.Fatalf("issue delegation token: %v", err)
	}
	req := models.EntrypointTaskRequest{
		ProtocolVersion:  currentProtocolVersion,
		RequestID:        "req-remote-only",
		TaskID:           taskID,
		SessionID:        "session-remote-only",
		InitiatorAgentID: "planner",
		TargetAgentID:    "executor",
		ToolName:         "echo",
		Arguments:        arguments,
		DelegationToken:  tokenString,
		AllowedTools:     []string{"echo"},
	}
	body, _ := json.Marshal(req)
	resp, err := postEntrypoint(t, server.URL+"/a2a/v1/entrypoint/tasks", body, tokenString)
	if err != nil {
		t.Fatalf("entrypoint create: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusCreated {
		t.Fatalf("expected 201, got %d", resp.StatusCode)
	}
	events, err := srv.db.EventStore().ListPending(context.Background(), taskID)
	if err != nil {
		t.Fatalf("list task events: %v", err)
	}
	if len(events) != 1 || events[0].EventType != "task_created" {
		t.Fatalf("expected one task_created event, got %+v", events)
	}
}

func TestEntrypointCreateRejectsInvalidToken(t *testing.T) {
	_, server := newTestServerWithMockR2(t, true)
	registerExecutor(t, server)
	task, _ := createDelegation(t, server)

	req := entrypointRequest(t, server, task, "bad-token")
	body, _ := json.Marshal(req)
	resp, err := postEntrypoint(t, server.URL+"/a2a/v1/entrypoint/tasks", body, "bad-token")
	if err != nil {
		t.Fatalf("entrypoint create: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("expected 401, got %d", resp.StatusCode)
	}
}

func TestEntrypointCreateRejectsMismatchedTokenScope(t *testing.T) {
	_, server := newTestServerWithMockR2(t, true)
	registerExecutor(t, server)
	task, token := createDelegation(t, server)

	req := entrypointRequest(t, server, task, token)
	req.TargetAgentID = "other-executor"
	body, _ := json.Marshal(req)
	resp, err := postEntrypoint(t, server.URL+"/a2a/v1/entrypoint/tasks", body, token)
	if err != nil {
		t.Fatalf("entrypoint create: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("expected 403, got %d", resp.StatusCode)
	}
}

func TestEntrypointCreateRejectsArgumentsNotBoundToToken(t *testing.T) {
	_, server := newTestServerWithMockR2(t, true)
	registerExecutor(t, server)
	task, token := createDelegation(t, server)

	req := entrypointRequest(t, server, task, token)
	req.Arguments = json.RawMessage(`{"x":"tampered"}`)
	body, _ := json.Marshal(req)
	resp, err := postEntrypoint(t, server.URL+"/a2a/v1/entrypoint/tasks", body, token)
	if err != nil {
		t.Fatalf("entrypoint create: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("expected 403, got %d", resp.StatusCode)
	}
	var got models.ErrorResponse
	if err := json.NewDecoder(resp.Body).Decode(&got); err != nil {
		t.Fatalf("decode error: %v", err)
	}
	if got.Code != "token_scope_mismatch" {
		t.Fatalf("code = %q, want token_scope_mismatch", got.Code)
	}
}

func TestEntrypointCreateRejectsTokenInteractionContextMismatch(t *testing.T) {
	_, server := newTestServerWithMockR2(t, true)
	registerExecutor(t, server)
	task, token := createDelegation(t, server)

	for _, tc := range []struct {
		name   string
		mutate func(*models.EntrypointTaskRequest)
	}{
		{name: "request", mutate: func(req *models.EntrypointTaskRequest) { req.RequestID = "other-request" }},
		{name: "session", mutate: func(req *models.EntrypointTaskRequest) { req.SessionID = "other-session" }},
		{name: "interaction", mutate: func(req *models.EntrypointTaskRequest) { req.InteractionID = "other-interaction" }},
		{name: "decision", mutate: func(req *models.EntrypointTaskRequest) { req.DecisionID = "other-decision" }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			req := entrypointRequest(t, server, task, token)
			tc.mutate(&req)
			body, _ := json.Marshal(req)
			resp, err := postEntrypoint(t, server.URL+"/a2a/v1/entrypoint/tasks", body, token)
			if err != nil {
				t.Fatalf("entrypoint create: %v", err)
			}
			defer resp.Body.Close()
			if resp.StatusCode != http.StatusForbidden {
				t.Fatalf("expected 403, got %d", resp.StatusCode)
			}
		})
	}
}

func TestEntrypointCreateRejectsExistingTaskContextMismatch(t *testing.T) {
	for _, tc := range []struct {
		name   string
		mutate func(*models.EntrypointTaskRequest)
	}{
		{name: "scope", mutate: func(req *models.EntrypointTaskRequest) { req.AllowedCapabilities = []string{"delegate_execution"} }},
		{name: "budget", mutate: func(req *models.EntrypointTaskRequest) { req.Budget.TokenCount++ }},
		{name: "deadline", mutate: func(req *models.EntrypointTaskRequest) {
			deadline := time.Now().UTC().Add(time.Hour).Truncate(time.Second)
			req.Deadline = &deadline
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			srv, server := newTestServerWithMockR2(t, true)
			registerExecutor(t, server)
			task, originalToken := createDelegation(t, server)
			req := entrypointRequest(t, server, task, originalToken)
			tc.mutate(&req)
			tokenString := issueEntrypointToken(t, srv, req)
			req.DelegationToken = tokenString
			body, _ := json.Marshal(req)
			resp, err := postEntrypoint(t, server.URL+"/a2a/v1/entrypoint/tasks", body, tokenString)
			if err != nil {
				t.Fatalf("entrypoint create: %v", err)
			}
			defer resp.Body.Close()
			if resp.StatusCode != http.StatusForbidden {
				t.Fatalf("expected 403, got %d", resp.StatusCode)
			}
			var got models.ErrorResponse
			if err := json.NewDecoder(resp.Body).Decode(&got); err != nil {
				t.Fatalf("decode error: %v", err)
			}
			if got.Code != "entrypoint_task_mismatch" {
				t.Fatalf("code = %q, want entrypoint_task_mismatch", got.Code)
			}
		})
	}
}

func TestEntrypointAcceptTransition(t *testing.T) {
	_, server := newTestServerWithMockR2(t, true)
	registerExecutor(t, server)
	task, token := createDelegation(t, server)

	req := entrypointRequest(t, server, task, token)
	body, _ := json.Marshal(req)
	resp, err := postEntrypoint(t, server.URL+"/a2a/v1/entrypoint/tasks", body, token)
	if err != nil {
		t.Fatalf("entrypoint create: %v", err)
	}
	resp.Body.Close()

	resp, err = postEntrypoint(t, server.URL+"/a2a/v1/entrypoint/tasks/"+task.TaskID+"/accept", nil, token)
	if err != nil {
		t.Fatalf("entrypoint accept: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("expected 200, got %d", resp.StatusCode)
	}
	var got models.Task
	if err := json.NewDecoder(resp.Body).Decode(&got); err != nil {
		t.Fatalf("decode task: %v", err)
	}
	if got.Status != "accepted" {
		t.Errorf("expected accepted, got %q", got.Status)
	}
}

func TestEntrypointStartTransition(t *testing.T) {
	executorServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		time.Sleep(100 * time.Millisecond)
		_ = json.NewEncoder(w).Encode(map[string]any{"status": "allow", "result": "ok"})
	}))
	defer executorServer.Close()
	srv, server := newTestServerWithMockR2(t, true)
	srv.SetTargetExecutor(&execution.HTTPExecutor{BaseURL: executorServer.URL, Client: executorServer.Client()})
	registerExecutor(t, server)
	task, token := createDelegation(t, server)

	req := entrypointRequest(t, server, task, token)
	body, _ := json.Marshal(req)
	resp, err := postEntrypoint(t, server.URL+"/a2a/v1/entrypoint/tasks", body, token)
	if err != nil {
		t.Fatalf("entrypoint create: %v", err)
	}
	resp.Body.Close()

	for _, action := range []string{"accept", "start"} {
		actionBody, _ := json.Marshal(map[string]string{"protocol_version": currentProtocolVersion})
		resp, err = postEntrypoint(t, server.URL+"/a2a/v1/entrypoint/tasks/"+task.TaskID+"/"+action, actionBody, token)
		if err != nil {
			t.Fatalf("entrypoint %s: %v", action, err)
		}
		if resp.StatusCode != http.StatusOK {
			resp.Body.Close()
			t.Fatalf("%s: expected 200, got %d", action, resp.StatusCode)
		}
		resp.Body.Close()
	}

	resp = getEntrypoint(t, server.URL+"/a2a/v1/entrypoint/tasks/"+task.TaskID, token)
	defer resp.Body.Close()
	var got models.Task
	if err := json.NewDecoder(resp.Body).Decode(&got); err != nil {
		t.Fatalf("decode task: %v", err)
	}
	if got.Status != "running" {
		t.Errorf("expected running, got %q", got.Status)
	}
}

type countingExecutor struct{ starts int }

func (e *countingExecutor) Start(context.Context, execution.Request) (execution.Handle, error) {
	e.starts++
	return nil, nil
}

func TestEntrypointDurableStartOnlyEnqueuesAndIsIdempotent(t *testing.T) {
	srv, server := newTestServerWithMockR2(t, true)
	executor := &countingExecutor{}
	srv.SetTargetExecutor(executor)
	srv.EnableDurableAssignments(nil)
	registerExecutor(t, server)
	task, delegationToken := createDelegation(t, server)
	body, _ := json.Marshal(entrypointRequest(t, server, task, delegationToken))
	resp, err := postEntrypoint(t, server.URL+"/a2a/v1/entrypoint/tasks", body, delegationToken)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	acceptBody, _ := json.Marshal(map[string]string{"protocol_version": currentProtocolVersion})
	resp, err = postEntrypoint(t, server.URL+"/a2a/v1/entrypoint/tasks/"+task.TaskID+"/accept", acceptBody, delegationToken)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	for i := 0; i < 2; i++ {
		resp, err = postEntrypoint(t, server.URL+"/a2a/v1/entrypoint/tasks/"+task.TaskID+"/start", acceptBody, delegationToken)
		if err != nil || resp.StatusCode != http.StatusOK {
			t.Fatalf("start %d status=%v err=%v", i, resp.StatusCode, err)
		}
		var got models.Task
		if err = json.NewDecoder(resp.Body).Decode(&got); err != nil {
			t.Fatal(err)
		}
		resp.Body.Close()
		if got.ProtocolVersion != currentProtocolVersion || got.Status != "running" {
			t.Fatalf("task=%+v", got)
		}
	}
	if executor.starts != 0 {
		t.Fatalf("executor starts=%d", executor.starts)
	}
	a, err := srv.db.AssignmentStore().Get(context.Background(), "assignment-"+task.TaskID)
	if err != nil || a.State != models.AssignmentStateQueued {
		t.Fatalf("assignment=%+v err=%v", a, err)
	}
}

func TestEntrypointSchedulerEnabledStartUsesPinnedAdmission(t *testing.T) {
	srv, server := newTestServerWithMockR2(t, true)
	srv.EnableDurableAssignments(nil)
	srv.EnableScheduling()
	registerExecutor(t, server)
	if _, err := srv.db.Exec(`UPDATE agents SET tenant_id=? WHERE agent_id='executor'`, "tenant-entrypoint"); err != nil {
		t.Fatal(err)
	}
	task, delegationToken := createDelegationWithTenant(t, server, "tenant-entrypoint")
	now := time.Now().UTC()
	_, err := srv.db.Exec(`UPDATE agents SET tenant_id=?,tools_json='["echo"]',protocol_capabilities_json='["0.54.0"]',security_capabilities_json='[]',expected_workload_id=?,max_concurrency=1,schedulable=1,generation=1,resource_version=1 WHERE agent_id='executor'`, task.TenantID, task.TargetWorkloadID)
	if err != nil {
		t.Fatal(err)
	}
	_, err = srv.db.Exec(`INSERT OR REPLACE INTO agent_status(tenant_id,agent_id,observed_generation,health,current_load,available_capacity,draining,last_seen_at,expires_at,status_revision,reported_by) VALUES(?,?,?,?,?,?,0,?,?,1,'test')`, task.TenantID, "executor", 1, models.AgentHealthHealthy, 0, 1, now.Format(time.RFC3339Nano), now.Add(time.Hour).Format(time.RFC3339Nano))
	if err != nil {
		t.Fatal(err)
	}
	body, _ := json.Marshal(entrypointRequest(t, server, task, delegationToken))
	resp, err := postEntrypoint(t, server.URL+"/a2a/v1/entrypoint/tasks", body, delegationToken)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	actionBody, _ := json.Marshal(map[string]string{"protocol_version": currentProtocolVersion})
	for _, action := range []string{"accept", "start"} {
		resp, err = postEntrypoint(t, server.URL+"/a2a/v1/entrypoint/tasks/"+task.TaskID+"/"+action, actionBody, delegationToken)
		if err != nil || resp.StatusCode != http.StatusOK {
			t.Fatalf("%s status=%v err=%v", action, resp.StatusCode, err)
		}
		resp.Body.Close()
	}
	assignment, err := srv.db.AssignmentStore().Get(context.Background(), "assignment-"+task.TaskID)
	if err != nil || assignment.AgentID != "executor" {
		t.Fatalf("assignment=%+v err=%v", assignment, err)
	}
	var state string
	if err = srv.db.QueryRow(`SELECT state FROM agent_capacity_reservations WHERE assignment_id=?`, assignment.AssignmentID).Scan(&state); err != nil || state != "held" {
		t.Fatalf("reservation state=%s err=%v", state, err)
	}
}

func TestEntrypointStartExecutesHTTPAndAutomaticallyRecordsResult(t *testing.T) {
	executorServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var req map[string]any
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			t.Errorf("decode execution request: %v", err)
		}
		if req["tool_name"] != "echo" {
			t.Errorf("tool_name = %v", req["tool_name"])
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"status": "allow", "result": "hello"})
	}))
	defer executorServer.Close()

	srv, server := newTestServerWithMockR2(t, true)
	srv.SetTargetExecutor(&execution.HTTPExecutor{BaseURL: executorServer.URL, Client: executorServer.Client()})
	registerExecutor(t, server)
	task, token := createDelegation(t, server)
	req := entrypointRequest(t, server, task, token)
	body, _ := json.Marshal(req)
	resp, err := postEntrypoint(t, server.URL+"/a2a/v1/entrypoint/tasks", body, token)
	if err != nil {
		t.Fatalf("entrypoint create: %v", err)
	}
	resp.Body.Close()
	for _, action := range []string{"accept", "start"} {
		actionBody, _ := json.Marshal(map[string]string{"protocol_version": currentProtocolVersion})
		resp, err = postEntrypoint(t, server.URL+"/a2a/v1/entrypoint/tasks/"+task.TaskID+"/"+action, actionBody, token)
		if err != nil || resp.StatusCode != http.StatusOK {
			t.Fatalf("entrypoint %s: status=%v err=%v", action, resp.StatusCode, err)
		}
		resp.Body.Close()
	}

	deadline := time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		got, err := srv.tasks.Get(task.TaskID)
		if err == nil && got.Status == "completed" {
			if string(got.Outcome) != `{"result":"hello"}` {
				t.Fatalf("outcome = %s", got.Outcome)
			}
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("task did not complete automatically")
}

func TestEntrypointCreateCanAutomaticallyAccept(t *testing.T) {
	srv, server := newTestServerWithMockR2(t, true)
	srv.SetTargetTaskAutomation(true, false)
	registerExecutor(t, server)
	task, token := createDelegation(t, server)
	body, _ := json.Marshal(entrypointRequest(t, server, task, token))
	resp, err := postEntrypoint(t, server.URL+"/a2a/v1/entrypoint/tasks", body, token)
	if err != nil {
		t.Fatalf("entrypoint create: %v", err)
	}
	defer resp.Body.Close()
	var got models.Task
	if err := json.NewDecoder(resp.Body).Decode(&got); err != nil {
		t.Fatalf("decode task: %v", err)
	}
	if resp.StatusCode != http.StatusCreated || got.Status != "accepted" {
		t.Fatalf("status=%d task status=%q", resp.StatusCode, got.Status)
	}
}

func TestEntrypointCreateCanAutomaticallyAcceptAndStart(t *testing.T) {
	release := make(chan struct{})
	executorServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		<-release
		_ = json.NewEncoder(w).Encode(map[string]any{"status": "allow", "result": "ok"})
	}))
	defer executorServer.Close()

	srv, server := newTestServerWithMockR2(t, true)
	srv.SetTargetExecutor(&execution.HTTPExecutor{BaseURL: executorServer.URL, Client: executorServer.Client()})
	srv.SetTargetTaskAutomation(false, true)
	registerExecutor(t, server)
	task, token := createDelegation(t, server)
	body, _ := json.Marshal(entrypointRequest(t, server, task, token))
	resp, err := postEntrypoint(t, server.URL+"/a2a/v1/entrypoint/tasks", body, token)
	if err != nil {
		t.Fatalf("entrypoint create: %v", err)
	}
	var got models.Task
	if err := json.NewDecoder(resp.Body).Decode(&got); err != nil {
		resp.Body.Close()
		t.Fatalf("decode task: %v", err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusCreated || got.Status != "running" {
		close(release)
		t.Fatalf("status=%d task status=%q", resp.StatusCode, got.Status)
	}
	close(release)
}

func TestEntrypointStartRequiresAccepted(t *testing.T) {
	_, server := newTestServerWithMockR2(t, true)
	registerExecutor(t, server)
	task, token := createDelegation(t, server)
	req := entrypointRequest(t, server, task, token)
	body, _ := json.Marshal(req)
	resp, err := postEntrypoint(t, server.URL+"/a2a/v1/entrypoint/tasks", body, token)
	if err != nil {
		t.Fatalf("entrypoint create: %v", err)
	}
	resp.Body.Close()

	startBody, _ := json.Marshal(map[string]string{"protocol_version": currentProtocolVersion})
	resp, err = postEntrypoint(t, server.URL+"/a2a/v1/entrypoint/tasks/"+task.TaskID+"/start", startBody, token)
	if err != nil {
		t.Fatalf("entrypoint start: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusConflict {
		t.Fatalf("expected 409, got %d", resp.StatusCode)
	}
}

func TestEntrypointAcceptWithoutBearerIsRejected(t *testing.T) {
	_, server := newTestServerWithMockR2(t, true)
	resp, err := http.Post(server.URL+"/a2a/v1/entrypoint/tasks/missing/accept", "application/json", nil)
	if err != nil {
		t.Fatalf("entrypoint accept: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("expected 401, got %d", resp.StatusCode)
	}
}

func TestEntrypointCancelTransition(t *testing.T) {
	_, server := newTestServerWithMockR2(t, true)
	registerExecutor(t, server)
	task, token := createDelegation(t, server)

	req := entrypointRequest(t, server, task, token)
	body, _ := json.Marshal(req)
	resp, err := postEntrypoint(t, server.URL+"/a2a/v1/entrypoint/tasks", body, token)
	if err != nil {
		t.Fatalf("entrypoint create: %v", err)
	}
	resp.Body.Close()

	resp, err = postEntrypoint(t, server.URL+"/a2a/v1/entrypoint/tasks/"+task.TaskID+"/cancel", cancelRequestBody(), token)
	if err != nil {
		t.Fatalf("entrypoint cancel: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("expected 200, got %d", resp.StatusCode)
	}
	var got models.Task
	if err := json.NewDecoder(resp.Body).Decode(&got); err != nil {
		t.Fatalf("decode task: %v", err)
	}
	if got.Status != "cancelled" {
		t.Errorf("expected cancelled, got %q", got.Status)
	}
}

type apiRoundTripFunc func(*http.Request) (*http.Response, error)

func (f apiRoundTripFunc) RoundTrip(req *http.Request) (*http.Response, error) { return f(req) }

type barrierExecutor struct {
	startEntered chan struct{}
	releaseStart chan struct{}
	handle       *barrierHandle
}

func (e *barrierExecutor) Start(context.Context, execution.Request) (execution.Handle, error) {
	close(e.startEntered)
	<-e.releaseStart
	return e.handle, nil
}

type barrierHandle struct {
	done      chan execution.Result
	cancelled chan struct{}
	cancelOK  bool
}

func (h *barrierHandle) Done() <-chan execution.Result { return h.done }
func (h *barrierHandle) Cancel() bool {
	select {
	case <-h.cancelled:
	default:
		close(h.cancelled)
	}
	return h.cancelOK
}

func TestEntrypointCancelWhileExecutorStartRegistersHandle(t *testing.T) {
	handle := &barrierHandle{done: make(chan execution.Result), cancelled: make(chan struct{}), cancelOK: true}
	executor := &barrierExecutor{startEntered: make(chan struct{}), releaseStart: make(chan struct{}), handle: handle}
	srv, server := newTestServerWithMockR2(t, true)
	srv.SetTargetExecutor(executor)
	registerExecutor(t, server)
	task, token := createDelegation(t, server)
	body, _ := json.Marshal(entrypointRequest(t, server, task, token))
	resp, err := postEntrypoint(t, server.URL+"/a2a/v1/entrypoint/tasks", body, token)
	if err != nil {
		t.Fatalf("entrypoint create: %v", err)
	}
	resp.Body.Close()
	for _, action := range []string{"accept"} {
		actionBody, _ := json.Marshal(map[string]string{"protocol_version": currentProtocolVersion})
		resp, err = postEntrypoint(t, server.URL+"/a2a/v1/entrypoint/tasks/"+task.TaskID+"/"+action, actionBody, token)
		if err != nil || resp.StatusCode != http.StatusOK {
			t.Fatalf("entrypoint %s: status=%v err=%v", action, resp.StatusCode, err)
		}
		resp.Body.Close()
	}

	startDone := make(chan *http.Response, 1)
	go func() {
		startBody, _ := json.Marshal(map[string]string{"protocol_version": currentProtocolVersion})
		startResp, _ := postEntrypoint(t, server.URL+"/a2a/v1/entrypoint/tasks/"+task.TaskID+"/start", startBody, token)
		startDone <- startResp
	}()
	<-executor.startEntered
	cancelDone := make(chan *http.Response, 1)
	go func() {
		cancelResp, _ := postEntrypoint(t, server.URL+"/a2a/v1/entrypoint/tasks/"+task.TaskID+"/cancel", cancelRequestBody(), token)
		cancelDone <- cancelResp
	}()

	srv.executionsMu.Lock()
	slot := srv.executions[task.TaskID]
	var cancelRequested <-chan struct{}
	if slot != nil {
		cancelRequested = slot.cancelRequestedC
	}
	srv.executionsMu.Unlock()
	if cancelRequested == nil {
		t.Fatal("starting execution slot was not registered")
	}
	<-cancelRequested
	close(executor.releaseStart)
	startResp := <-startDone
	if startResp == nil {
		t.Fatal("start request failed")
	}
	startResp.Body.Close()
	cancelResp := <-cancelDone
	if cancelResp == nil || cancelResp.StatusCode != http.StatusOK {
		t.Fatalf("cancel status = %v", cancelResp)
	}
	cancelResp.Body.Close()
	select {
	case <-handle.cancelled:
	default:
		t.Fatal("returned execution handle was not cancelled")
	}
	got, err := srv.tasks.Get(task.TaskID)
	if err != nil || got.Status != "cancelled" {
		t.Fatalf("task = %+v, err=%v", got, err)
	}
	srv.executionsMu.Lock()
	_, active := srv.executions[task.TaskID]
	srv.executionsMu.Unlock()
	if active {
		t.Fatal("cancelled task retained an active execution slot")
	}
}

func TestAwaitExecutionCancelsHandleAfterLeaseLost(t *testing.T) {
	srv, _ := newTestServerWithMockR2(t, true)
	srv.db.SetExecutionLease(3 * time.Millisecond)
	handle := &barrierHandle{done: make(chan execution.Result), cancelled: make(chan struct{}), cancelOK: true}

	result := make(chan bool, 1)
	go func() {
		_, ok := srv.awaitExecution("missing-task", handle)
		result <- ok
	}()
	<-handle.cancelled
	if ok := <-result; ok {
		t.Fatal("awaitExecution reported a result after lease loss")
	}
}

func TestEntrypointCancelCancelsActualHTTPContext(t *testing.T) {
	started := make(chan struct{})
	cancelled := make(chan struct{})
	executorClient := &http.Client{Transport: apiRoundTripFunc(func(req *http.Request) (*http.Response, error) {
		close(started)
		<-req.Context().Done()
		close(cancelled)
		return nil, req.Context().Err()
	})}

	srv, server := newTestServerWithMockR2(t, true)
	srv.SetTargetExecutor(&execution.HTTPExecutor{BaseURL: "http://executor.test", Client: executorClient})
	registerExecutor(t, server)
	task, token := createDelegation(t, server)
	req := entrypointRequest(t, server, task, token)
	body, _ := json.Marshal(req)
	resp, _ := postEntrypoint(t, server.URL+"/a2a/v1/entrypoint/tasks", body, token)
	resp.Body.Close()
	for _, action := range []string{"accept", "start"} {
		actionBody, _ := json.Marshal(map[string]string{"protocol_version": currentProtocolVersion})
		resp, _ = postEntrypoint(t, server.URL+"/a2a/v1/entrypoint/tasks/"+task.TaskID+"/"+action, actionBody, token)
		resp.Body.Close()
	}
	select {
	case <-started:
	case <-time.After(time.Second):
		t.Fatal("execution did not start")
	}
	resp, err := postEntrypoint(t, server.URL+"/a2a/v1/entrypoint/tasks/"+task.TaskID+"/cancel", cancelRequestBody(), token)
	if err != nil {
		t.Fatalf("cancel: %v", err)
	}
	defer resp.Body.Close()
	select {
	case <-cancelled:
	case <-time.After(time.Second):
		t.Fatal("execution context was not cancelled")
	}
	got, _ := srv.tasks.Get(task.TaskID)
	if got.Status != "cancelled" {
		t.Fatalf("status = %q", got.Status)
	}
}

func TestEntrypointCancelTerminalReturnsCurrentState(t *testing.T) {
	_, server := newTestServerWithMockR2(t, true)
	registerExecutor(t, server)
	task, token := createDelegation(t, server)

	req := entrypointRequest(t, server, task, token)
	body, _ := json.Marshal(req)
	resp, err := postEntrypoint(t, server.URL+"/a2a/v1/entrypoint/tasks", body, token)
	if err != nil {
		t.Fatalf("entrypoint create: %v", err)
	}
	resp.Body.Close()

	resp, err = postEntrypoint(t, server.URL+"/a2a/v1/entrypoint/tasks/"+task.TaskID+"/cancel", cancelRequestBody(), token)
	if err != nil {
		t.Fatalf("entrypoint cancel: %v", err)
	}
	resp.Body.Close()

	resp, err = postEntrypoint(t, server.URL+"/a2a/v1/entrypoint/tasks/"+task.TaskID+"/cancel", cancelRequestBody(), token)
	if err != nil {
		t.Fatalf("entrypoint cancel again: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("expected 200, got %d", resp.StatusCode)
	}
	var got models.Task
	if err := json.NewDecoder(resp.Body).Decode(&got); err != nil {
		t.Fatalf("decode task: %v", err)
	}
	if got.Status != "cancelled" {
		t.Errorf("expected cancelled, got %q", got.Status)
	}
}

func TestEntrypointResultsCompleted(t *testing.T) {
	srv, server := newTestServerWithMockR2(t, true)
	registerExecutor(t, server)
	task, token := createDelegation(t, server)

	req := entrypointRequest(t, server, task, token)
	body, _ := json.Marshal(req)
	resp, err := postEntrypoint(t, server.URL+"/a2a/v1/entrypoint/tasks", body, token)
	if err != nil {
		t.Fatalf("entrypoint create: %v", err)
	}
	resp.Body.Close()

	// Transition pending -> accepted -> running via the task manager.
	if _, err := srv.tasks.UpdateStatus(task.TaskID, "accepted"); err != nil {
		t.Fatalf("accepted transition: %v", err)
	}
	if _, err := srv.tasks.UpdateStatus(task.TaskID, "running"); err != nil {
		t.Fatalf("running transition: %v", err)
	}

	resultReq := models.EntrypointResultRequest{
		ProtocolVersion: currentProtocolVersion,
		Status:          "completed",
		Outcome:         json.RawMessage(`{"result":"ok"}`),
	}
	body, _ = json.Marshal(resultReq)
	resp, err = postEntrypoint(t, server.URL+"/a2a/v1/entrypoint/tasks/"+task.TaskID+"/results", body, token)
	if err != nil {
		t.Fatalf("entrypoint results: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("expected 200, got %d", resp.StatusCode)
	}
	var got models.Task
	if err := json.NewDecoder(resp.Body).Decode(&got); err != nil {
		t.Fatalf("decode task: %v", err)
	}
	if got.Status != "completed" {
		t.Errorf("expected completed, got %q", got.Status)
	}
	if string(got.Outcome) != `{"result":"ok"}` {
		t.Errorf("outcome = %q, want {\"result\":\"ok\"}", string(got.Outcome))
	}
	if got.CompletedAt == nil {
		t.Error("expected completed_at to be set")
	}
}

func TestEntrypointResultsStrictRequiresExecutionReceipt(t *testing.T) {
	srv, server := newTestServerWithMockR2(t, true)
	registerExecutor(t, server)
	task, delegationToken := createDelegation(t, server)
	req := entrypointRequest(t, server, task, delegationToken)
	body, _ := json.Marshal(req)
	resp, _ := postEntrypoint(t, server.URL+"/a2a/v1/entrypoint/tasks", body, delegationToken)
	resp.Body.Close()
	srv.SetTargetExecutor(&execution.HTTPExecutor{Strict: true})
	_, _ = srv.tasks.UpdateStatus(task.TaskID, "accepted")
	_, _ = srv.tasks.UpdateStatus(task.TaskID, "running")
	body, _ = json.Marshal(models.EntrypointResultRequest{ProtocolVersion: currentProtocolVersion, Status: "completed", Outcome: json.RawMessage(`{"result":"ok"}`)})
	resp, err := postEntrypoint(t, server.URL+"/a2a/v1/entrypoint/tasks/"+task.TaskID+"/results", body, delegationToken)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("strict result without receipt status=%d, want 403", resp.StatusCode)
	}
	got, _ := srv.tasks.Get(task.TaskID)
	if got.Status == "completed" {
		t.Fatal("strict result without receipt completed task")
	}
}

func TestEntrypointResultsFailed(t *testing.T) {
	srv, server := newTestServerWithMockR2(t, true)
	registerExecutor(t, server)
	task, token := createDelegation(t, server)

	req := entrypointRequest(t, server, task, token)
	body, _ := json.Marshal(req)
	resp, err := postEntrypoint(t, server.URL+"/a2a/v1/entrypoint/tasks", body, token)
	if err != nil {
		t.Fatalf("entrypoint create: %v", err)
	}
	resp.Body.Close()

	if _, err := srv.tasks.UpdateStatus(task.TaskID, "accepted"); err != nil {
		t.Fatalf("accepted transition: %v", err)
	}
	if _, err := srv.tasks.UpdateStatus(task.TaskID, "running"); err != nil {
		t.Fatalf("running transition: %v", err)
	}

	resultReq := models.EntrypointResultRequest{
		ProtocolVersion: currentProtocolVersion,
		Status:          "failed",
		ErrorCode:       "tool_execution_failed",
	}
	body, _ = json.Marshal(resultReq)
	resp, err = postEntrypoint(t, server.URL+"/a2a/v1/entrypoint/tasks/"+task.TaskID+"/results", body, token)
	if err != nil {
		t.Fatalf("entrypoint results: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("expected 200, got %d", resp.StatusCode)
	}
	var got models.Task
	if err := json.NewDecoder(resp.Body).Decode(&got); err != nil {
		t.Fatalf("decode task: %v", err)
	}
	if got.Status != "failed" {
		t.Errorf("expected failed, got %q", got.Status)
	}
	if got.ErrorCode != "tool_execution_failed" {
		t.Errorf("error_code = %q, want tool_execution_failed", got.ErrorCode)
	}
}

func TestEntrypointResultsRejectsInvalidStatusTransition(t *testing.T) {
	_, server := newTestServerWithMockR2(t, true)
	registerExecutor(t, server)
	task, token := createDelegation(t, server)

	req := entrypointRequest(t, server, task, token)
	body, _ := json.Marshal(req)
	resp, err := postEntrypoint(t, server.URL+"/a2a/v1/entrypoint/tasks", body, token)
	if err != nil {
		t.Fatalf("entrypoint create: %v", err)
	}
	resp.Body.Close()

	resultReq := models.EntrypointResultRequest{
		ProtocolVersion: currentProtocolVersion,
		Status:          "completed",
		Outcome:         json.RawMessage(`{"result":"ok"}`),
	}
	body, _ = json.Marshal(resultReq)
	resp, err = postEntrypoint(t, server.URL+"/a2a/v1/entrypoint/tasks/"+task.TaskID+"/results", body, token)
	if err != nil {
		t.Fatalf("entrypoint results: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusConflict {
		t.Fatalf("expected 409, got %d", resp.StatusCode)
	}
}

func TestEntrypointReplayReturnsOriginalResponse(t *testing.T) {
	_, server := newTestServerWithMockR2(t, true)
	registerExecutor(t, server)
	task, token := createDelegation(t, server)
	req := entrypointRequest(t, server, task, token)
	body, _ := json.Marshal(req)

	for i := 0; i < 2; i++ {
		resp, err := postEntrypoint(t, server.URL+"/a2a/v1/entrypoint/tasks", body, token)
		if err != nil {
			t.Fatalf("entrypoint create %d: %v", i+1, err)
		}
		if resp.StatusCode != http.StatusCreated {
			resp.Body.Close()
			t.Fatalf("request %d: expected 201, got %d", i+1, resp.StatusCode)
		}
		resp.Body.Close()
	}
}

func TestEntrypointReplayWithEquivalentJSONReturnsOriginalResponse(t *testing.T) {
	_, server := newTestServerWithMockR2(t, true)
	registerExecutor(t, server)
	task, token := createDelegation(t, server)
	req := entrypointRequest(t, server, task, token)
	body, _ := json.Marshal(req)
	resp, err := postEntrypoint(t, server.URL+"/a2a/v1/entrypoint/tasks", body, token)
	if err != nil {
		t.Fatalf("entrypoint create: %v", err)
	}
	resp.Body.Close()

	var equivalent map[string]any
	if err := json.Unmarshal(body, &equivalent); err != nil {
		t.Fatalf("decode request: %v", err)
	}
	equivalentBody, _ := json.MarshalIndent(equivalent, "", "  ")
	resp, err = postEntrypoint(t, server.URL+"/a2a/v1/entrypoint/tasks", equivalentBody, token)
	if err != nil {
		t.Fatalf("entrypoint replay: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusCreated {
		t.Fatalf("expected 201, got %d", resp.StatusCode)
	}
}

func TestInitiatorTaskQueryRemainsUnauthenticated(t *testing.T) {
	_, server := newTestServerWithMockR2(t, true)
	registerExecutor(t, server)
	task, _ := createDelegation(t, server)
	resp, err := http.Get(server.URL + "/a2a/v1/tasks/" + task.TaskID)
	if err != nil {
		t.Fatalf("query task: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("expected 200, got %d", resp.StatusCode)
	}
}

func TestEntrypointGetRejectsTokenForAnotherTask(t *testing.T) {
	_, server := newTestServerWithMockR2(t, true)
	registerExecutor(t, server)
	_, token := createDelegation(t, server)
	resp := getEntrypoint(t, server.URL+"/a2a/v1/entrypoint/tasks/other-task", token)
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("expected 403, got %d", resp.StatusCode)
	}
}

func TestDelegationIdempotencyReturnsOriginalResponse(t *testing.T) {
	_, server := newTestServerWithMockR2(t, true)
	registerExecutor(t, server)
	req := models.DelegationRequest{
		RequestID:        "req-idempotent",
		InitiatorAgentID: "planner",
		TargetAgentID:    "executor",
		ToolName:         "echo",
		Arguments:        json.RawMessage(`{"x":"hello"}`),
		ProtocolVersion:  currentProtocolVersion,
	}
	body, _ := json.Marshal(req)

	var responses []models.DelegationResponse
	for i := 0; i < 2; i++ {
		resp, err := http.Post(server.URL+"/a2a/v1/delegations", "application/json", bytes.NewReader(body))
		if err != nil {
			t.Fatalf("delegation request %d: %v", i+1, err)
		}
		if resp.StatusCode != http.StatusOK {
			resp.Body.Close()
			t.Fatalf("request %d: expected 200, got %d", i+1, resp.StatusCode)
		}
		var got models.DelegationResponse
		if err := json.NewDecoder(resp.Body).Decode(&got); err != nil {
			resp.Body.Close()
			t.Fatalf("decode response %d: %v", i+1, err)
		}
		resp.Body.Close()
		responses = append(responses, got)
	}
	if responses[0].TaskID != responses[1].TaskID || responses[0].DelegationToken != responses[1].DelegationToken {
		t.Fatalf("duplicate request did not return original response: %#v != %#v", responses[0], responses[1])
	}
}

func TestDelegationIdempotencyEquivalentJSONReturnsOriginalResponse(t *testing.T) {
	_, server := newTestServerWithMockR2(t, true)
	registerExecutor(t, server)
	first := []byte(`{"request_id":"req-idempotency-equivalent","initiator_agent_id":"planner","target_agent_id":"executor","tool_name":"echo","arguments":{"x":"hello","nested":{"a":1,"b":2}},"session_id":"","task_id":"","risk_level":"","protocol_version":"0.53.0"}`)
	second := []byte(`{
		"protocol_version":"0.53.0", "risk_level":"", "task_id":"", "session_id":"",
		"arguments":{"nested":{"b":2,"a":1},"x":"hello"}, "tool_name":"echo",
		"target_agent_id":"executor", "initiator_agent_id":"planner", "request_id":"req-idempotency-equivalent"
	}`)

	for i, body := range [][]byte{first, second} {
		resp, err := http.Post(server.URL+"/a2a/v1/delegations", "application/json", bytes.NewReader(body))
		if err != nil {
			t.Fatalf("delegation request %d: %v", i+1, err)
		}
		if resp.StatusCode != http.StatusOK {
			resp.Body.Close()
			t.Fatalf("request %d: expected 200, got %d", i+1, resp.StatusCode)
		}
		resp.Body.Close()
	}
}

func TestDelegationIdempotencyRejectsChangedRequest(t *testing.T) {
	_, server := newTestServerWithMockR2(t, true)
	registerExecutor(t, server)
	req := models.DelegationRequest{
		RequestID:        "req-idempotency-conflict",
		InitiatorAgentID: "planner",
		TargetAgentID:    "executor",
		ToolName:         "echo",
		Arguments:        json.RawMessage(`{"x":"hello"}`),
		ProtocolVersion:  currentProtocolVersion,
	}
	body, _ := json.Marshal(req)
	resp, err := http.Post(server.URL+"/a2a/v1/delegations", "application/json", bytes.NewReader(body))
	if err != nil {
		t.Fatalf("first delegation request: %v", err)
	}
	resp.Body.Close()

	req.Arguments = json.RawMessage(`{"x":"changed"}`)
	body, _ = json.Marshal(req)
	resp, err = http.Post(server.URL+"/a2a/v1/delegations", "application/json", bytes.NewReader(body))
	if err != nil {
		t.Fatalf("changed delegation request: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusConflict {
		t.Fatalf("expected 409, got %d", resp.StatusCode)
	}
}

func TestDelegationR2DeniedFailClosed(t *testing.T) {
	_, server := newTestServerWithMockR2(t, false)
	registerExecutor(t, server)

	req := models.DelegationRequest{
		RequestID:        "req-denied-1",
		InitiatorAgentID: "planner",
		TargetAgentID:    "executor",
		ToolName:         "echo",
		ProtocolVersion:  currentProtocolVersion,
	}
	body, _ := json.Marshal(req)
	resp, err := http.Post(server.URL+"/a2a/v1/delegations", "application/json", bytes.NewReader(body))
	if err != nil {
		t.Fatalf("delegation request: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("expected 403, got %d", resp.StatusCode)
	}
	var got models.DelegationResponse
	if err := json.NewDecoder(resp.Body).Decode(&got); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	if got.Allowed {
		t.Error("expected denied response")
	}
}

func TestCancelMainChain(t *testing.T) {
	_, server := newTestServerWithMockR2(t, true)
	registerExecutor(t, server)
	task, _ := createDelegation(t, server)

	resp, err := http.Post(server.URL+"/a2a/v1/tasks/"+task.TaskID+"/cancel", "application/json", bytes.NewReader(cancelRequestBody()))
	if err != nil {
		t.Fatalf("cancel task: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("expected 200, got %d", resp.StatusCode)
	}
	var got models.Task
	if err := json.NewDecoder(resp.Body).Decode(&got); err != nil {
		t.Fatalf("decode task: %v", err)
	}
	if got.Status != "cancelled" {
		t.Errorf("expected cancelled, got %q", got.Status)
	}
}

func TestCancelMainChainPropagatesToTarget(t *testing.T) {
	var cancelPath string
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		cancelPath = r.URL.Path
		parts := strings.Split(strings.Trim(r.URL.Path, "/"), "/")
		taskID := parts[len(parts)-2]
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(models.Task{TaskID: taskID, Status: "cancelled"})
	}))
	defer target.Close()

	srv, server := newTestServerWithMockR2(t, true)
	registerExecutorAtURL(t, server, target.URL)
	task, _ := createDelegation(t, server)
	if _, err := srv.tasks.UpdateStatus(task.TaskID, "accepted"); err != nil {
		t.Fatalf("accept task: %v", err)
	}
	if _, err := srv.tasks.UpdateStatus(task.TaskID, "running"); err != nil {
		t.Fatalf("start task: %v", err)
	}
	srv.SetEntrypointClient(&delegation.HTTPEntrypointClient{Client: target.Client()})

	resp, err := http.Post(server.URL+"/a2a/v1/tasks/"+task.TaskID+"/cancel", "application/json", bytes.NewReader(cancelRequestBody()))
	if err != nil {
		t.Fatalf("cancel task: %v", err)
	}
	defer resp.Body.Close()
	var got models.Task
	if err := json.NewDecoder(resp.Body).Decode(&got); err != nil {
		t.Fatalf("decode task: %v", err)
	}
	if cancelPath != "/a2a/v1/entrypoint/tasks/"+task.TaskID+"/cancel" {
		t.Errorf("cancel path = %q", cancelPath)
	}
	if got.Status != "cancelled" {
		t.Errorf("expected cancelled, got %q", got.Status)
	}
}

func TestCancelMainChainUnknownOutcomeOnPropagationFailure(t *testing.T) {
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer target.Close()

	srv, server := newTestServerWithMockR2(t, true)
	registerExecutorAtURL(t, server, target.URL)
	task, _ := createDelegation(t, server)
	if _, err := srv.tasks.UpdateStatus(task.TaskID, "accepted"); err != nil {
		t.Fatalf("accept task: %v", err)
	}
	if _, err := srv.tasks.UpdateStatus(task.TaskID, "running"); err != nil {
		t.Fatalf("start task: %v", err)
	}
	srv.SetEntrypointClient(&delegation.HTTPEntrypointClient{Client: target.Client()})

	resp, err := http.Post(server.URL+"/a2a/v1/tasks/"+task.TaskID+"/cancel", "application/json", bytes.NewReader(cancelRequestBody()))
	if err != nil {
		t.Fatalf("cancel task: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("expected 200, got %d", resp.StatusCode)
	}
	var got models.Task
	if err := json.NewDecoder(resp.Body).Decode(&got); err != nil {
		t.Fatalf("decode task: %v", err)
	}
	if got.Status != "outcome_unknown" {
		t.Errorf("expected outcome_unknown, got %q", got.Status)
	}
	if got.CompletedAt != nil {
		t.Error("outcome_unknown must not set completed_at")
	}
}

func TestCancelMainChainTerminalReturnsCurrentState(t *testing.T) {
	srv, server := newTestServerWithMockR2(t, true)
	registerExecutor(t, server)
	task, _ := createDelegation(t, server)
	if _, err := srv.tasks.UpdateStatusFrom(task.TaskID, "pending", "cancelled"); err != nil {
		t.Fatalf("cancel task: %v", err)
	}

	resp, err := http.Post(server.URL+"/a2a/v1/tasks/"+task.TaskID+"/cancel", "application/json", bytes.NewReader(cancelRequestBody()))
	if err != nil {
		t.Fatalf("cancel task: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("expected 200, got %d", resp.StatusCode)
	}
	var got models.Task
	if err := json.NewDecoder(resp.Body).Decode(&got); err != nil {
		t.Fatalf("decode task: %v", err)
	}
	if got.Status != "cancelled" {
		t.Errorf("expected cancelled, got %q", got.Status)
	}
}
