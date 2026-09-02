package api

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/loop-controller/go/internal/models"
)

var testSecret = []byte("test-secret")

func TestRegisterAgentAndGet(t *testing.T) {
	srv := NewServer(testSecret)
	server := httptest.NewServer(muxFor(srv))
	defer server.Close()

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
	srv := NewServer(testSecret)
	server := httptest.NewServer(muxFor(srv))
	defer server.Close()

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
	srv := NewServer(testSecret)
	server := httptest.NewServer(muxFor(srv))
	defer server.Close()

	req := map[string]string{
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
	srv := NewServer(testSecret)
	server := httptest.NewServer(muxFor(srv))
	defer server.Close()

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
	srv := NewServer(testSecret)
	server := httptest.NewServer(muxFor(srv))
	defer server.Close()

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
	srv := NewServer(testSecret)
	server := httptest.NewServer(muxFor(srv))
	defer server.Close()

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
	srv := NewServer(testSecret)
	server := httptest.NewServer(muxFor(srv))
	defer server.Close()

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
	srv := NewServer(testSecret)
	server := httptest.NewServer(muxFor(srv))
	defer server.Close()
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
	srv := NewServer(testSecret)
	server := httptest.NewServer(muxFor(srv))
	defer server.Close()

	// Create task first.
	req := map[string]string{
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

func muxFor(srv *Server) *http.ServeMux {
	mux := http.NewServeMux()
	srv.RegisterRoutes(mux)
	return mux
}
