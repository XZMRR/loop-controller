package api

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/loop-controller/go/internal/delegation"
	"github.com/loop-controller/go/internal/models"
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
	card := models.AgentCard{
		AgentID:      "executor",
		Name:         "Executor",
		Entrypoint:   models.AgentEntrypoint{Type: "http", URL: "http://executor:8080"},
		Capabilities: []string{"delegate_execution"},
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
	t.Helper()
	req := models.DelegationRequest{
		RequestID:        "req-entrypoint-1",
		InitiatorAgentID: "planner",
		TargetAgentID:    "executor",
		ToolName:         "echo",
		SessionID:        "session-entrypoint-1",
		ProtocolVersion:  currentProtocolVersion,
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

func entrypointRequest(t *testing.T, server *httptest.Server, task models.Task, token string) models.EntrypointTaskRequest {
	t.Helper()
	return models.EntrypointTaskRequest{
		ProtocolVersion:  currentProtocolVersion,
		TaskID:           task.TaskID,
		SessionID:        task.SessionID,
		InitiatorAgentID: task.InitiatorAgentID,
		TargetAgentID:    task.TargetAgentID,
		ToolName:         "echo",
		Arguments:        json.RawMessage(`{"x":"hello"}`),
		DelegationToken:  token,
	}
}

func TestEntrypointCreateTaskAndPublishEvent(t *testing.T) {
	srv, server := newTestServerWithMockR2(t, true)
	registerExecutor(t, server)
	task, token := createDelegation(t, server)

	req := entrypointRequest(t, server, task, token)
	body, _ := json.Marshal(req)
	resp, err := http.Post(server.URL+"/a2a/v1/entrypoint/tasks", "application/json", bytes.NewReader(body))
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

func TestEntrypointCreateRejectsInvalidToken(t *testing.T) {
	_, server := newTestServerWithMockR2(t, true)
	registerExecutor(t, server)
	task, _ := createDelegation(t, server)

	req := entrypointRequest(t, server, task, "bad-token")
	body, _ := json.Marshal(req)
	resp, err := http.Post(server.URL+"/a2a/v1/entrypoint/tasks", "application/json", bytes.NewReader(body))
	if err != nil {
		t.Fatalf("entrypoint create: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("expected 403, got %d", resp.StatusCode)
	}
}

func TestEntrypointCreateRejectsMismatchedTokenScope(t *testing.T) {
	_, server := newTestServerWithMockR2(t, true)
	registerExecutor(t, server)
	task, token := createDelegation(t, server)

	req := entrypointRequest(t, server, task, token)
	req.TargetAgentID = "other-executor"
	body, _ := json.Marshal(req)
	resp, err := http.Post(server.URL+"/a2a/v1/entrypoint/tasks", "application/json", bytes.NewReader(body))
	if err != nil {
		t.Fatalf("entrypoint create: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("expected 403, got %d", resp.StatusCode)
	}
}

func TestEntrypointAcceptTransition(t *testing.T) {
	_, server := newTestServerWithMockR2(t, true)
	registerExecutor(t, server)
	task, token := createDelegation(t, server)

	req := entrypointRequest(t, server, task, token)
	body, _ := json.Marshal(req)
	resp, err := http.Post(server.URL+"/a2a/v1/entrypoint/tasks", "application/json", bytes.NewReader(body))
	if err != nil {
		t.Fatalf("entrypoint create: %v", err)
	}
	resp.Body.Close()

	resp, err = http.Post(server.URL+"/a2a/v1/entrypoint/tasks/"+task.TaskID+"/accept", "application/json", nil)
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

func TestEntrypointAcceptNotFound(t *testing.T) {
	_, server := newTestServerWithMockR2(t, true)
	resp, err := http.Post(server.URL+"/a2a/v1/entrypoint/tasks/missing/accept", "application/json", nil)
	if err != nil {
		t.Fatalf("entrypoint accept: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusNotFound {
		t.Fatalf("expected 404, got %d", resp.StatusCode)
	}
}

func TestEntrypointCancelTransition(t *testing.T) {
	_, server := newTestServerWithMockR2(t, true)
	registerExecutor(t, server)
	task, token := createDelegation(t, server)

	req := entrypointRequest(t, server, task, token)
	body, _ := json.Marshal(req)
	resp, err := http.Post(server.URL+"/a2a/v1/entrypoint/tasks", "application/json", bytes.NewReader(body))
	if err != nil {
		t.Fatalf("entrypoint create: %v", err)
	}
	resp.Body.Close()

	resp, err = http.Post(server.URL+"/a2a/v1/entrypoint/tasks/"+task.TaskID+"/cancel", "application/json", nil)
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

func TestEntrypointCancelTerminalReturnsCurrentState(t *testing.T) {
	_, server := newTestServerWithMockR2(t, true)
	registerExecutor(t, server)
	task, token := createDelegation(t, server)

	req := entrypointRequest(t, server, task, token)
	body, _ := json.Marshal(req)
	resp, err := http.Post(server.URL+"/a2a/v1/entrypoint/tasks", "application/json", bytes.NewReader(body))
	if err != nil {
		t.Fatalf("entrypoint create: %v", err)
	}
	resp.Body.Close()

	resp, err = http.Post(server.URL+"/a2a/v1/entrypoint/tasks/"+task.TaskID+"/cancel", "application/json", nil)
	if err != nil {
		t.Fatalf("entrypoint cancel: %v", err)
	}
	resp.Body.Close()

	resp, err = http.Post(server.URL+"/a2a/v1/entrypoint/tasks/"+task.TaskID+"/cancel", "application/json", nil)
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
	resp, err := http.Post(server.URL+"/a2a/v1/entrypoint/tasks", "application/json", bytes.NewReader(body))
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
	resp, err = http.Post(server.URL+"/a2a/v1/entrypoint/tasks/"+task.TaskID+"/results", "application/json", bytes.NewReader(body))
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

func TestEntrypointResultsFailed(t *testing.T) {
	srv, server := newTestServerWithMockR2(t, true)
	registerExecutor(t, server)
	task, token := createDelegation(t, server)

	req := entrypointRequest(t, server, task, token)
	body, _ := json.Marshal(req)
	resp, err := http.Post(server.URL+"/a2a/v1/entrypoint/tasks", "application/json", bytes.NewReader(body))
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
	resp, err = http.Post(server.URL+"/a2a/v1/entrypoint/tasks/"+task.TaskID+"/results", "application/json", bytes.NewReader(body))
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
	resp, err := http.Post(server.URL+"/a2a/v1/entrypoint/tasks", "application/json", bytes.NewReader(body))
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
	resp, err = http.Post(server.URL+"/a2a/v1/entrypoint/tasks/"+task.TaskID+"/results", "application/json", bytes.NewReader(body))
	if err != nil {
		t.Fatalf("entrypoint results: %v", err)
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

	resp, err := http.Post(server.URL+"/a2a/v1/tasks/"+task.TaskID+"/cancel", "application/json", nil)
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

func TestCancelMainChainTerminalReturnsCurrentState(t *testing.T) {
	srv, server := newTestServerWithMockR2(t, true)
	registerExecutor(t, server)
	task, _ := createDelegation(t, server)
	if _, err := srv.tasks.UpdateStatus(task.TaskID, "cancelled"); err != nil {
		t.Fatalf("cancel task: %v", err)
	}

	resp, err := http.Post(server.URL+"/a2a/v1/tasks/"+task.TaskID+"/cancel", "application/json", nil)
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
