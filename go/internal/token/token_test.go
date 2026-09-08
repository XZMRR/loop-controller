package token

import (
	"crypto/sha256"
	"encoding/hex"
	"testing"
	"time"
)

func TestIssueAndValidate(t *testing.T) {
	issuer := NewHMACIssuer([]byte("super-secret"))
	claims := DelegationClaims{
		RequestID:           "req-1",
		SessionID:           "session-1",
		InteractionID:       "interaction-1",
		DecisionID:          "decision-1",
		RootInteractionID:   "interaction-root",
		ParentInteractionID: "interaction-parent",
		InitiatorAgentID:    "planner",
		TargetAgentID:       "executor",
		ToolName:            "query_sales",
		TaskID:              "task-1",
		ArgumentsSHA256:     "args-digest",
		AllowedTools:        []string{"query_sales"},
		AllowedCapabilities: []string{"read_data"},
		AllowRedelegation:   true,
		RootTaskID:          "task-root",
		ParentTaskID:        "task-parent",
		DelegationDepth:     2,
		Deadline:            1_800_000_000,
		BudgetTokenCount:    100,
		BudgetPaymentAmount: 2.5,
		BudgetCurrency:      "USD",
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
	if got.SessionID != claims.SessionID || got.InteractionID != claims.InteractionID || got.DecisionID != claims.DecisionID ||
		got.RootInteractionID != claims.RootInteractionID || got.ParentInteractionID != claims.ParentInteractionID ||
		got.ArgumentsSHA256 != claims.ArgumentsSHA256 || got.RootTaskID != claims.RootTaskID || got.ParentTaskID != claims.ParentTaskID ||
		got.DelegationDepth != claims.DelegationDepth || got.Deadline != claims.Deadline ||
		got.BudgetTokenCount != claims.BudgetTokenCount || got.BudgetPaymentAmount != claims.BudgetPaymentAmount || got.BudgetCurrency != claims.BudgetCurrency {
		t.Fatalf("delegation context was not fully bound: %+v", got)
	}
	if len(got.AllowedTools) != 1 || got.AllowedTools[0] != "query_sales" ||
		len(got.AllowedCapabilities) != 1 || got.AllowedCapabilities[0] != "read_data" || !got.AllowRedelegation {
		t.Fatalf("delegation scope was not fully bound: %+v", got)
	}
}

func TestHashArgumentsCanonicalJSON(t *testing.T) {
	tests := []struct {
		name  string
		left  string
		right string
	}{
		{
			name:  "object key order and whitespace",
			left:  `{"region":"APAC","limit":10}`,
			right: "{\n  \"limit\": 10,\n  \"region\": \"APAC\"\n}",
		},
		{
			name:  "nested object key order",
			left:  `{"filters":{"active":true,"tags":["a","b"]},"limit":10}`,
			right: `{"limit":10,"filters":{"tags":["a","b"],"active":true}}`,
		},
		{
			name:  "number lexeme",
			left:  `{"value":1e3}`,
			right: `{"value":1e3}`,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if HashArguments([]byte(tt.left)) != HashArguments([]byte(tt.right)) {
				t.Fatal("equivalent JSON arguments produced different hashes")
			}
		})
	}
}

func TestHashArgumentsNonJSONBytesAreHashedUnchanged(t *testing.T) {
	arguments := []byte("  not json  \n")
	sum := sha256.Sum256(arguments)
	want := hex.EncodeToString(sum[:])

	if got := HashArguments(arguments); got != want {
		t.Fatalf("HashArguments() = %q, want raw bytes digest %q", got, want)
	}
	if HashArguments(arguments) == HashArguments([]byte("not json")) {
		t.Fatal("non-JSON whitespace must remain significant")
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
