package delegation

import (
	"context"
	"errors"
	"path/filepath"
	"testing"
	"time"

	"github.com/loop-controller/go/internal/models"
	"github.com/loop-controller/go/internal/registry"
	"github.com/loop-controller/go/internal/store"
	"github.com/loop-controller/go/internal/stream"
	"github.com/loop-controller/go/internal/task"
	"github.com/loop-controller/go/internal/token"
)

func openTestDB(t *testing.T) *store.DB {
	t.Helper()
	db, err := store.Open(context.Background(), filepath.Join(t.TempDir(), "a2a.db"))
	if err != nil {
		t.Fatalf("open store: %v", err)
	}
	t.Cleanup(func() { db.Close() })
	return db
}

type recordingLifecycleAuditor struct {
	events []string
	tasks  []models.Task
}

func (a *recordingLifecycleAuditor) RecordLifecycle(_ context.Context, task models.Task, event string) error {
	a.events = append(a.events, event)
	a.tasks = append(a.tasks, task)
	return nil
}

func TestInteractionLifecycleCarriesDecisionAndTaskLinkage(t *testing.T) {
	reg := registry.New()
	reg.Register(models.AgentCard{
		AgentID: "executor", Entrypoint: models.AgentEntrypoint{Type: "http", URL: "http://executor"},
		Capabilities: []string{"delegate_execution"},
	})
	db := openTestDB(t)
	auditor := &recordingLifecycleAuditor{}
	tasks := task.New(db.TaskStore()).WithLifecycleAuditor(auditor)
	d := New(reg, tasks, token.NewHMACIssuer([]byte("secret")), nil, time.Hour).WithR2Authorizer(&StaticR2Authorizer{
		Decision: models.DelegationResponse{Allowed: true, InteractionID: "int-1", DecisionID: "dec-1"},
	}).WithEntrypointClient(succeedingDispatcher{})

	resp, err := d.Request(context.Background(), models.DelegationRequest{
		RequestID: "req-1", InitiatorAgentID: "planner", TargetAgentID: "executor", ToolName: "query_sales",
	})
	if err != nil {
		t.Fatalf("request: %v", err)
	}
	stored, err := tasks.Get(resp.TaskID)
	if err != nil {
		t.Fatalf("get task: %v", err)
	}
	if stored.InteractionID != "int-1" || stored.DecisionID != "dec-1" {
		t.Fatalf("missing audit linkage: %+v", stored)
	}
	if _, err := tasks.UpdateStatus(resp.TaskID, "accepted"); err != nil {
		t.Fatalf("accept: %v", err)
	}
	items, err := db.LifecycleOutboxStore().ListDue(context.Background(), time.Now().UTC().Add(time.Second), 10)
	if err != nil {
		t.Fatalf("list lifecycle outbox: %v", err)
	}
	if len(items) != 2 || items[0].Event != "dispatched" || items[1].Event != "accepted" {
		t.Fatalf("unexpected lifecycle outbox: %+v", items)
	}
	if items[1].Task.InteractionID != "int-1" || items[1].Task.DecisionID != "dec-1" {
		t.Fatalf("outbox lost linkage: %+v", items[1].Task)
	}
}

func TestRequestAllowed(t *testing.T) {
	reg := registry.New()
	reg.Register(models.AgentCard{
		AgentID:      "executor",
		Name:         "Executor",
		Entrypoint:   models.AgentEntrypoint{Type: "http", URL: "http://executor:8080"},
		Capabilities: []string{"delegate_execution"},
	})
	db := openTestDB(t)
	tasks := task.New(db.TaskStore())
	issuer := token.NewHMACIssuer([]byte("secret"))
	pub := stream.NewPublisher(db.EventStore())
	d := New(reg, tasks, issuer, pub, time.Hour).WithR2Authorizer(&StaticR2Authorizer{
		Decision: models.DelegationResponse{Allowed: true},
	})

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
	events, err := db.EventStore().ListPending(context.Background(), resp.TaskID)
	if err != nil {
		t.Fatalf("list task events: %v", err)
	}
	if len(events) != 1 || events[0].EventType != "task_created" {
		t.Fatalf("expected one task_created event, got %+v", events)
	}
}

type failingIssuer struct{}

func (failingIssuer) Issue(token.DelegationClaims, time.Duration) (string, error) {
	return "", errors.New("signing unavailable")
}

type failingDispatcher struct {
	mayBeSent bool
}

func (d failingDispatcher) Dispatch(context.Context, models.AgentEntrypoint, models.EntrypointTaskRequest) error {
	return &DispatchError{Err: errors.New("dispatch failed"), MayBeSent: d.mayBeSent}
}

func (failingDispatcher) Cancel(context.Context, models.AgentEntrypoint, string, string) (bool, error) {
	return true, nil
}

type succeedingDispatcher struct{}

func (succeedingDispatcher) Dispatch(context.Context, models.AgentEntrypoint, models.EntrypointTaskRequest) error {
	return nil
}

func (succeedingDispatcher) Cancel(context.Context, models.AgentEntrypoint, string, string) (bool, error) {
	return true, nil
}

func TestRequestWithoutInteractionAuthorizerIsDenied(t *testing.T) {
	reg := registry.New()
	reg.Register(models.AgentCard{
		AgentID: "executor", Capabilities: []string{"delegate_execution"},
	})
	db := openTestDB(t)
	d := New(reg, task.New(db.TaskStore()), token.NewHMACIssuer([]byte("secret")), nil, time.Hour)
	resp, err := d.Request(context.Background(), models.DelegationRequest{
		RequestID: "req-1", InitiatorAgentID: "planner",
		TargetAgentID: "executor", ToolName: "query_sales",
	})
	if err == nil || resp.Allowed || resp.Reason != "interaction authorizer is not configured" {
		t.Fatalf("expected missing authorizer denial, got %+v, %v", resp, err)
	}
}

func TestRequestTokenIssuanceFailureIsDenied(t *testing.T) {
	reg := registry.New()
	reg.Register(models.AgentCard{
		AgentID:      "executor",
		Capabilities: []string{"delegate_execution"},
	})
	db := openTestDB(t)
	d := New(reg, task.New(db.TaskStore()), failingIssuer{}, nil, time.Hour).WithR2Authorizer(&StaticR2Authorizer{
		Decision: models.DelegationResponse{Allowed: true},
	})

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

func TestRequestDispatchFailureUpdatesTaskStatus(t *testing.T) {
	for _, tc := range []struct {
		name       string
		mayBeSent  bool
		wantStatus string
	}{
		{name: "definitely not sent", wantStatus: "failed"},
		{name: "response uncertain", mayBeSent: true, wantStatus: "outcome_unknown"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			reg := registry.New()
			reg.Register(models.AgentCard{
				AgentID: "executor", Entrypoint: models.AgentEntrypoint{Type: "http", URL: "http://executor"},
				Capabilities: []string{"delegate_execution"},
			})
			db := openTestDB(t)
			tasks := task.New(db.TaskStore())
			d := New(reg, tasks, token.NewHMACIssuer([]byte("secret")), nil, time.Hour).
				WithR2Authorizer(&StaticR2Authorizer{Decision: models.DelegationResponse{Allowed: true}}).
				WithEntrypointClient(failingDispatcher{mayBeSent: tc.mayBeSent})

			resp, err := d.Request(context.Background(), models.DelegationRequest{
				RequestID: "req-dispatch", InitiatorAgentID: "planner", TargetAgentID: "executor", ToolName: "echo",
			})
			if err == nil || resp.TaskID == "" {
				t.Fatalf("expected dispatch error with task id, got %+v, %v", resp, err)
			}
			got, getErr := tasks.Get(resp.TaskID)
			if getErr != nil {
				t.Fatalf("get task: %v", getErr)
			}
			if got.Status != tc.wantStatus {
				t.Fatalf("status = %q, want %q", got.Status, tc.wantStatus)
			}
			if got.DelegationToken == "" {
				t.Fatal("delegation token was not persisted")
			}
		})
	}
}

func TestRequestMissingTarget(t *testing.T) {
	reg := registry.New()
	db := openTestDB(t)
	tasks := task.New(db.TaskStore())
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
	db := openTestDB(t)
	tasks := task.New(db.TaskStore())
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
	db := openTestDB(t)
	tasks := task.New(db.TaskStore())
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
	db := openTestDB(t)
	tasks := task.New(db.TaskStore())
	tsk := tasks.Create("session-1", "planner", "executor")
	issuer := token.NewHMACIssuer([]byte("secret"))
	d := New(reg, tasks, issuer, nil, time.Hour).WithR2Authorizer(&StaticR2Authorizer{
		Decision: models.DelegationResponse{Allowed: true},
	})

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
