package api

import (
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
	fixturePath := filepath.Join(root, "..", "..", "..", "contract", "a2a_v0.36.1.json")
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
		{"0.36.1", false},
		{"0.36.0", false},
		{"0.36.99", false},
		{"", false},
		{"0.35.0", true},
		{"0.37.0", true},
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
	if card.AgentID != "agent-a" {
		t.Errorf("agent_id = %q, want agent-a", card.AgentID)
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
	if msg.ProtocolVersion != "0.36.1" {
		t.Errorf("protocol_version = %q, want 0.36.1", msg.ProtocolVersion)
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

func TestContractFixture_DecodeDelegation(t *testing.T) {
	fixture := loadFixture(t)
	var req models.DelegationRequest
	if err := json.Unmarshal(fixture["delegation_request"], &req); err != nil {
		t.Fatalf("unmarshal delegation_request: %v", err)
	}
	if req.RequestID != "req-001" {
		t.Errorf("request_id = %q, want req-001", req.RequestID)
	}
	if req.ProtocolVersion != "0.36.1" {
		t.Errorf("protocol_version = %q, want 0.36.1", req.ProtocolVersion)
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
