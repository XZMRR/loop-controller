package api

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"

	"github.com/loop-controller/go/internal/models"
	"github.com/loop-controller/go/internal/stream"
)

func TestV054HandlerContractRegisterListErrorAndCompatibility(t *testing.T) {
	srv, err := NewServer(testSecret, filepath.Join(t.TempDir(), "contract.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer srv.Close()
	srv.SetControlAuthForTenant("control", "owner", "tenant-a")
	mux := muxFor(srv)

	do := func(method, path, token, body string) *httptest.ResponseRecorder {
		req := httptest.NewRequest(method, path, strings.NewReader(body))
		if token != "" {
			req.Header.Set("Authorization", "Bearer "+token)
		}
		res := httptest.NewRecorder()
		mux.ServeHTTP(res, req)
		return res
	}

	unauthorized := do(http.MethodGet, "/a2a/v1/agents", "", "")
	assertJSONContract(t, unauthorized, http.StatusUnauthorized)
	var apiError models.ErrorResponse
	if err := json.Unmarshal(unauthorized.Body.Bytes(), &apiError); err != nil || apiError.ProtocolVersion != currentProtocolVersion || apiError.Code != "invalid_control_token" {
		t.Fatalf("error contract: %+v err=%v", apiError, err)
	}

	card := `{"protocol_version":"0.54.0","agent_id":"agent-b","name":"B","description":"fixture","entrypoint":{"type":"http","url":"https://agent.example/a2a"},"capabilities":["delegate_execution"],"trust_domain":"example.test","version":"2.0.0","tenant_id":"tenant-a"}`
	created := do(http.MethodPost, "/a2a/v1/agents", "control", card)
	assertJSONContract(t, created, http.StatusCreated)
	var registration models.AgentRegistrationResponse
	if err := json.Unmarshal(created.Body.Bytes(), &registration); err != nil || registration.ProtocolVersion != "0.54.0" || registration.AgentID != "agent-b" {
		t.Fatalf("registration contract: %+v err=%v", registration, err)
	}

	listed := do(http.MethodGet, "/a2a/v1/agents", "control", "")
	assertJSONContract(t, listed, http.StatusOK)
	var envelope models.AgentList
	if err := json.Unmarshal(listed.Body.Bytes(), &envelope); err != nil || envelope.ProtocolVersion != "0.54.0" || len(envelope.Agents) != 1 || envelope.Agents[0].ProtocolVersion != "0.54.0" {
		t.Fatalf("list contract: %+v err=%v", envelope, err)
	}

	legacy := do(http.MethodPost, "/a2a/v1/tasks", "control", `{"protocol_version":"0.53.0","session_id":"s","initiator_agent_id":"owner","target_agent_id":"agent-b"}`)
	assertJSONContract(t, legacy, http.StatusCreated)
	unknown := do(http.MethodPost, "/a2a/v1/tasks", "control", `{"protocol_version":"0.54.0","session_id":"s","initiator_agent_id":"owner","target_agent_id":"agent-b","scheduler":true}`)
	assertJSONContract(t, unknown, http.StatusBadRequest)
}

func TestV054MessageRejectionAndSSEPreflightContract(t *testing.T) {
	srv, err := NewServer(testSecret, filepath.Join(t.TempDir(), "stream-contract.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer srv.Close()
	mux := muxFor(srv)

	message := `{"message_id":"m","task_id":"t","from_agent_id":"a","to_agent_id":"missing","role":"user","parts":[{"type":"text","text":"hello"}],"timestamp":"2026-09-01T12:00:00Z","protocol_version":"0.54.0"}`
	req := httptest.NewRequest(http.MethodPost, "/a2a/v1/messages", strings.NewReader(message))
	res := httptest.NewRecorder()
	mux.ServeHTTP(res, req)
	assertJSONContract(t, res, http.StatusBadRequest)
	var rejected models.SendMessageResponse
	if err := json.Unmarshal(res.Body.Bytes(), &rejected); err != nil || rejected.Accepted || rejected.ProtocolVersion != "0.54.0" || rejected.Reason != "agent not found" {
		t.Fatalf("message rejection: %+v err=%v", rejected, err)
	}

	task, err := srv.tasks.CreateReliable("s", "a", "b")
	if err != nil {
		t.Fatal(err)
	}
	badReq := httptest.NewRequest(http.MethodGet, "/a2a/v1/tasks/"+task.TaskID+"/stream", nil)
	badReq.Header.Set("Last-Event-ID", "not-a-cursor")
	bad := httptest.NewRecorder()
	mux.ServeHTTP(bad, badReq)
	assertJSONContract(t, bad, http.StatusBadRequest)
	if strings.HasPrefix(bad.Header().Get("Content-Type"), "text/event-stream") {
		t.Fatal("SSE headers committed before cursor validation")
	}
	var cursorError models.ErrorResponse
	if err := json.Unmarshal(bad.Body.Bytes(), &cursorError); err != nil || cursorError.ProtocolVersion != "0.54.0" || cursorError.Code != "event_cursor_invalid" {
		t.Fatalf("cursor error: %+v err=%v", cursorError, err)
	}

	closed, err := NewServer(testSecret, filepath.Join(t.TempDir(), "closed.db"))
	if err != nil {
		t.Fatal(err)
	}
	closedTask, _ := closed.tasks.CreateReliable("s", "a", "b")
	publisher := closed.publisher.(*stream.SQLitePublisher)
	_ = publisher.Close()
	closedReq := httptest.NewRequestWithContext(context.Background(), http.MethodGet, "/a2a/v1/tasks/"+closedTask.TaskID+"/stream", nil)
	closedRes := httptest.NewRecorder()
	muxFor(closed).ServeHTTP(closedRes, closedReq)
	assertJSONContract(t, closedRes, http.StatusServiceUnavailable)
	var closedError models.ErrorResponse
	_ = json.Unmarshal(closedRes.Body.Bytes(), &closedError)
	if closedError.Error != "service unavailable" || closedError.Code != "event_stream_closed" || closedError.ProtocolVersion != "0.54.0" {
		t.Fatalf("closed stream error leaked or drifted: %+v", closedError)
	}
	_ = closed.Close()
}

func TestV054ModelsRejectUnknownFields(t *testing.T) {
	for name, tc := range map[string]struct {
		raw string
		dst any
	}{
		"dag":      {`{"protocol_version":"0.54.0","dag_id":"d","tenant_id":"t","root_task_id":"r","max_parallelism":1,"budget_envelope":{},"nodes":[],"unknown":true}`, &models.TaskGraphCreate{}},
		"approval": {`{"request_id":"r","unknown":true}`, &models.ApprovalActionRequest{}},
	} {
		t.Run(name, func(t *testing.T) {
			decoder := json.NewDecoder(bytes.NewBufferString(tc.raw))
			decoder.DisallowUnknownFields()
			if err := decoder.Decode(tc.dst); err == nil {
				t.Fatal("unknown field accepted")
			}
		})
	}
}

func assertJSONContract(t *testing.T, res *httptest.ResponseRecorder, status int) {
	t.Helper()
	if res.Code != status {
		t.Fatalf("status=%d want=%d body=%s", res.Code, status, res.Body.String())
	}
	if got := res.Header().Get("Content-Type"); got != "application/json" {
		t.Fatalf("Content-Type=%q", got)
	}
	if !json.Valid(res.Body.Bytes()) {
		t.Fatalf("invalid JSON: %s", res.Body.String())
	}
}
