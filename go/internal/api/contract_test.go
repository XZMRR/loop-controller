package api

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"github.com/loop-controller/go/internal/models"
)

func loadFixture(t *testing.T) map[string]json.RawMessage {
	t.Helper()
	root, err := os.Getwd()
	if err != nil {
		t.Fatalf("getwd: %v", err)
	}
	// go/internal/api -> project root -> contract
	fixturePath := filepath.Join(root, "..", "..", "..", "contract", "a2a_v0.37.0.json")
	data, err := os.ReadFile(fixturePath)
	if err != nil {
		t.Fatalf("read fixture: %v", err)
	}
	var fixture map[string]json.RawMessage
	if err := json.Unmarshal(data, &fixture); err != nil {
		t.Fatalf("unmarshal fixture: %v", err)
	}
	return fixture
}

func TestCheckProtocolVersion(t *testing.T) {
	cases := []struct {
		version string
		wantErr bool
	}{
		{"0.37.0", false},
		{"0.37.1", false},
		{"0.37.99", false},
		{"", true},
		{"0.37", true},
		{"0.37.0.0", true},
		{"v0.37.0", true},
		{"0.36.1", true},
		{"0.38.0", true},
		{"not-a-version", true},
	}
	for _, c := range cases {
		err := checkProtocolVersion(c.version)
		if c.wantErr && err == nil {
			t.Errorf("checkProtocolVersion(%q) wanted error", c.version)
		}
		if !c.wantErr && err != nil {
			t.Errorf("checkProtocolVersion(%q) unexpected error: %v", c.version, err)
		}
	}
}

func TestContractFixture_DecodeAgentCard(t *testing.T) {
	fixture := loadFixture(t)
	var card models.AgentCard
	if err := json.Unmarshal(fixture["agent_card"], &card); err != nil {
		t.Fatalf("unmarshal agent_card: %v", err)
	}
	if card.AgentID != "agent-b" {
		t.Errorf("agent_id = %q, want agent-b", card.AgentID)
	}
	if card.Entrypoint.Type != "http" {
		t.Errorf("entrypoint.type = %q, want http", card.Entrypoint.Type)
	}
}

func TestContractFixture_DecodeMessage(t *testing.T) {
	fixture := loadFixture(t)
	var msg models.Message
	if err := json.Unmarshal(fixture["message"], &msg); err != nil {
		t.Fatalf("unmarshal message: %v", err)
	}
	if msg.ProtocolVersion != "0.37.0" {
		t.Errorf("protocol_version = %q, want 0.37.0", msg.ProtocolVersion)
	}
	if len(msg.Parts) != 2 {
		t.Fatalf("parts length = %d, want 2", len(msg.Parts))
	}
	if msg.Parts[0].Type != "text" || msg.Parts[0].Text != "invoke-tool" {
		t.Errorf("text part mismatch: %+v", msg.Parts[0])
	}
	if msg.Parts[1].Type != "data" || len(msg.Parts[1].Data) == 0 {
		t.Errorf("data part mismatch: %+v", msg.Parts[1])
	}
	if err := validateMessageParts(&msg); err != nil {
		t.Errorf("validateMessageParts: %v", err)
	}
}

func canonicalJSON(t *testing.T, value any) []byte {
	t.Helper()
	data, err := json.Marshal(value)
	if err != nil {
		t.Fatalf("marshal canonical JSON: %v", err)
	}
	var normalized any
	if err := json.Unmarshal(data, &normalized); err != nil {
		t.Fatalf("normalize canonical JSON: %v", err)
	}
	data, err = json.Marshal(normalized)
	if err != nil {
		t.Fatalf("marshal normalized JSON: %v", err)
	}
	var compact bytes.Buffer
	if err := json.Compact(&compact, data); err != nil {
		t.Fatalf("compact canonical JSON: %v", err)
	}
	return compact.Bytes()
}

func assertCanonicalRoundTrip(t *testing.T, raw json.RawMessage, value any) {
	t.Helper()
	var fixture any
	if err := json.Unmarshal(raw, &fixture); err != nil {
		t.Fatalf("decode fixture for canonical comparison: %v", err)
	}
	if !bytes.Equal(canonicalJSON(t, fixture), canonicalJSON(t, value)) {
		t.Errorf("canonical roundtrip mismatch\nfixture: %s\nvalue:   %s", canonicalJSON(t, fixture), canonicalJSON(t, value))
	}
}

func TestContractFixture_DecodeTaskErrorAndSSE(t *testing.T) {
	fixture := loadFixture(t)

	var task models.Task
	if err := json.Unmarshal(fixture["task"], &task); err != nil {
		t.Fatalf("unmarshal task: %v", err)
	}
	if task.TaskID != "task-001" || task.Status != "pending" {
		t.Errorf("task mismatch: %+v", task)
	}
	assertCanonicalRoundTrip(t, fixture["task"], task)

	var apiError models.ErrorResponse
	if err := json.Unmarshal(fixture["error_response"], &apiError); err != nil {
		t.Fatalf("unmarshal error_response: %v", err)
	}
	if apiError.Code != "incompatible_protocol_version" {
		t.Errorf("error code = %q", apiError.Code)
	}
	assertCanonicalRoundTrip(t, fixture["error_response"], apiError)

	var event struct {
		Data models.Task `json:"data"`
	}
	if err := json.Unmarshal(fixture["sse_event"], &event); err != nil {
		t.Fatalf("unmarshal sse_event: %v", err)
	}
	if event.Data.TaskID != task.TaskID || event.Data.Status != "active" {
		t.Errorf("SSE event mismatch: %+v", event)
	}
	assertCanonicalRoundTrip(t, fixture["sse_event"], event)
}

func TestContractFixture_ErrorCategories(t *testing.T) {
	fixture := loadFixture(t)
	var cases []struct {
		Name     string          `json:"name"`
		Category string          `json:"category"`
		Message  json.RawMessage `json:"message"`
	}
	if err := json.Unmarshal(fixture["error_cases"], &cases); err != nil {
		t.Fatalf("unmarshal error_cases: %v", err)
	}
	for _, tc := range cases {
		t.Run(tc.Name, func(t *testing.T) {
			var raw map[string]json.RawMessage
			if err := json.Unmarshal(tc.Message, &raw); err != nil {
				t.Fatalf("unmarshal message fields: %v", err)
			}
			category := ""
			for _, required := range []string{"message_id", "task_id", "from_agent_id", "to_agent_id", "parts"} {
				if _, ok := raw[required]; !ok {
					category = "invalid_request"
					break
				}
			}
			if category == "" {
				var msg models.Message
				if err := json.Unmarshal(tc.Message, &msg); err != nil {
					category = "invalid_message_parts"
				} else if err := checkProtocolVersion(msg.ProtocolVersion); err != nil {
					category = "incompatible_protocol_version"
				} else if err := validateMessageParts(&msg); err != nil {
					category = "invalid_message_parts"
				}
			}
			if category != tc.Category {
				t.Errorf("category = %q, want %q", category, tc.Category)
			}
		})
	}
}

func TestContractFixture_DecodeDelegation(t *testing.T) {
	fixture := loadFixture(t)
	var req models.DelegationRequest
	if err := json.Unmarshal(fixture["delegation_request"], &req); err != nil {
		t.Fatalf("unmarshal delegation_request: %v", err)
	}
	if req.RequestID != "req-001" {
		t.Errorf("request_id = %q, want req-001", req.RequestID)
	}
	if req.ProtocolVersion != "0.37.0" {
		t.Errorf("protocol_version = %q, want 0.37.0", req.ProtocolVersion)
	}

	var resp models.DelegationResponse
	if err := json.Unmarshal(fixture["delegation_response"], &resp); err != nil {
		t.Fatalf("unmarshal delegation_response: %v", err)
	}
	if !resp.Allowed {
		t.Errorf("allowed = false, want true")
	}
	if resp.TargetEntrypoint.Type != "http" {
		t.Errorf("target_entrypoint.type = %q, want http", resp.TargetEntrypoint.Type)
	}
}
