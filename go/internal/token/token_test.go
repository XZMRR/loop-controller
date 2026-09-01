package token

import (
	"testing"
	"time"
)

func TestIssueAndValidate(t *testing.T) {
	issuer := NewHMACIssuer([]byte("super-secret"))
	claims := DelegationClaims{
		RequestID:        "req-1",
		InitiatorAgentID: "planner",
		TargetAgentID:    "executor",
		ToolName:         "query_sales",
		TaskID:           "task-1",
	}
	token, err := issuer.Issue(claims, time.Hour)
	if err != nil {
		t.Fatalf("issue failed: %v", err)
	}

	got, err := issuer.Validate(token)
	if err != nil {
		t.Fatalf("validate failed: %v", err)
	}
	if got.RequestID != "req-1" {
		t.Errorf("unexpected request_id: %q", got.RequestID)
	}
	if got.ToolName != "query_sales" {
		t.Errorf("unexpected tool_name: %q", got.ToolName)
	}
}

func TestValidateTamperedToken(t *testing.T) {
	issuer := NewHMACIssuer([]byte("super-secret"))
	claims := DelegationClaims{RequestID: "req-1"}
	token, _ := issuer.Issue(claims, time.Hour)
	tampered := token + "x"
	_, err := issuer.Validate(tampered)
	if err == nil {
		t.Fatal("expected validation to fail for tampered token")
	}
}

func TestValidateWrongSecret(t *testing.T) {
	issuer := NewHMACIssuer([]byte("super-secret"))
	claims := DelegationClaims{RequestID: "req-1"}
	token, _ := issuer.Issue(claims, time.Hour)

	other := NewHMACIssuer([]byte("other-secret"))
	_, err := other.Validate(token)
	if err == nil {
		t.Fatal("expected validation to fail for wrong secret")
	}
}

func TestValidateExpiredToken(t *testing.T) {
	issuer := NewHMACIssuer([]byte("super-secret"))
	claims := DelegationClaims{RequestID: "req-1"}
	token, _ := issuer.Issue(claims, -time.Second)
	_, err := issuer.Validate(token)
	if err == nil {
		t.Fatal("expected validation to fail for expired token")
	}
}

func TestIssueEmptySecret(t *testing.T) {
	issuer := NewHMACIssuer([]byte{})
	_, err := issuer.Issue(DelegationClaims{}, time.Hour)
	if err == nil {
		t.Fatal("expected error for empty secret")
	}
}
