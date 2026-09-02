package delegation

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/loop-controller/go/internal/models"
	"github.com/loop-controller/go/internal/registry"
	"github.com/loop-controller/go/internal/stream"
	"github.com/loop-controller/go/internal/task"
	"github.com/loop-controller/go/internal/token"
)

func TestRequestAllowed(t *testing.T) {
	reg := registry.New()
	reg.Register(models.AgentCard{
		AgentID:      "executor",
		Name:         "Executor",
		Entrypoint:   models.AgentEntrypoint{Type: "http", URL: "http://executor:8080"},
		Capabilities: []string{"delegate_execution"},
	})
	tasks := task.New()
	issuer := token.NewHMACIssuer([]byte("secret"))
	pub := stream.NewInMemoryPublisher()
	d := New(reg, tasks, issuer, pub, time.Hour)

	resp, err := d.Request(context.Background(), models.DelegationRequest{
		RequestID:        "req-1",
		InitiatorAgentID: "planner",
		TargetAgentID:    "executor",
		ToolName:         "query_sales",
		RiskLevel:        "critical",
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !resp.Allowed {
		t.Fatalf("expected allowed, got %v", resp)
	}
	if resp.TaskID == "" {
		t.Error("expected non-empty task_id")
	}
	if resp.TargetEntrypoint.URL != "http://executor:8080" {
		t.Errorf("unexpected entrypoint: %v", resp.TargetEntrypoint)
	}
	if resp.DelegationToken == "" {
		t.Error("expected non-empty delegation token")
	}
}

type failingIssuer struct{}

func (failingIssuer) Issue(token.DelegationClaims, time.Duration) (string, error) {
	return "", errors.New("signing unavailable")
}

func TestRequestTokenIssuanceFailureIsDenied(t *testing.T) {
	reg := registry.New()
	reg.Register(models.AgentCard{
		AgentID:      "executor",
		Capabilities: []string{"delegate_execution"},
	})
	d := New(reg, task.New(), failingIssuer{}, nil, time.Hour)

	resp, err := d.Request(context.Background(), models.DelegationRequest{
		RequestID:        "req-1",
		InitiatorAgentID: "planner",
		TargetAgentID:    "executor",
		ToolName:         "query_sales",
	})
	if err == nil {
		t.Fatal("expected token issuance error")
	}
	if resp.Allowed || resp.DelegationToken != "" {
		t.Fatalf("token failure must fail closed, got %+v", resp)
	}
}

func TestRequestMissingTarget(t *testing.T) {
	reg := registry.New()
	tasks := task.New()
	d := New(reg, tasks, nil, nil, time.Hour)

	resp, err := d.Request(context.Background(), models.DelegationRequest{
		RequestID:        "req-1",
		InitiatorAgentID: "planner",
		TargetAgentID:    "executor",
		ToolName:         "query_sales",
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if resp.Allowed {
		t.Fatalf("expected not allowed")
	}
}

func TestRequestMissingFields(t *testing.T) {
	reg := registry.New()
	tasks := task.New()
	d := New(reg, tasks, nil, nil, time.Hour)

	_, err := d.Request(context.Background(), models.DelegationRequest{})
	if err == nil {
		t.Fatal("expected error for empty request")
	}
}

func TestRequestNoCapability(t *testing.T) {
	reg := registry.New()
	reg.Register(models.AgentCard{
		AgentID:      "executor",
		Name:         "Executor",
		Entrypoint:   models.AgentEntrypoint{Type: "http", URL: "http://executor:8080"},
		Capabilities: []string{"chat"},
	})
	tasks := task.New()
	d := New(reg, tasks, nil, nil, time.Hour)

	resp, err := d.Request(context.Background(), models.DelegationRequest{
		RequestID:        "req-1",
		InitiatorAgentID: "planner",
		TargetAgentID:    "executor",
		ToolName:         "query_sales",
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if resp.Allowed {
		t.Fatal("expected not allowed")
	}
}

func TestRequestExistingTask(t *testing.T) {
	reg := registry.New()
	reg.Register(models.AgentCard{
		AgentID:      "executor",
		Capabilities: []string{"delegate_execution"},
	})
	tasks := task.New()
	tsk := tasks.Create("session-1", "planner", "executor")
	issuer := token.NewHMACIssuer([]byte("secret"))
	d := New(reg, tasks, issuer, nil, time.Hour)

	resp, err := d.Request(context.Background(), models.DelegationRequest{
		RequestID:        "req-1",
		InitiatorAgentID: "planner",
		TargetAgentID:    "executor",
		ToolName:         "query_sales",
		TaskID:           tsk.TaskID,
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !resp.Allowed {
		t.Fatalf("expected allowed, got %v", resp)
	}
	if resp.TaskID != tsk.TaskID {
		t.Errorf("expected task %q, got %q", tsk.TaskID, resp.TaskID)
	}
}
