package delegation

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"path/filepath"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/loop-controller/go/internal/models"
	"github.com/loop-controller/go/internal/registry"
	"github.com/loop-controller/go/internal/store"
	"github.com/loop-controller/go/internal/stream"
	"github.com/loop-controller/go/internal/task"
	"github.com/loop-controller/go/internal/token"
)

type countingIssuer struct {
	delegate TokenIssuer
	calls    int
}

func (i *countingIssuer) Issue(claims token.DelegationClaims, ttl time.Duration) (string, error) {
	i.calls++
	return i.delegate.Issue(claims, ttl)
}

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

func (a *recordingLifecycleAuditor) RecordLifecycle(_ context.Context, task models.Task, event, _ string) error {
	a.events = append(a.events, event)
	a.tasks = append(a.tasks, task)
	return nil
}

func TestDurableDelegationReplayReusesTokenAndBudget(t *testing.T) {
	reg := registry.New()
	_ = reg.Register(models.AgentCard{AgentID: "executor", TenantID: "tenant", Entrypoint: models.AgentEntrypoint{Type: "http", URL: "http://executor"}, Capabilities: []string{"delegate_execution"}})
	db := openTestDB(t)
	issuer := &countingIssuer{delegate: token.NewHMACIssuer([]byte("secret"))}
	d := New(reg, task.New(db.TaskStore()).WithInstanceID(db.InstanceID()), issuer, nil, time.Hour).
		WithR2Authorizer(&StaticR2Authorizer{Decision: models.DelegationResponse{Allowed: true, InteractionID: "int-1", DecisionID: "dec-1"}}).
		WithOutboundQueue(db.DelegationDispatchOutboxStore())
	req := models.DelegationRequest{RequestID: "req-1", SessionID: "session", InitiatorAgentID: "planner", TargetAgentID: "executor", ToolName: "echo", Arguments: json.RawMessage(`{"x":1}`), TenantID: "tenant"}
	first, err := d.Request(context.Background(), req)
	if err != nil || !first.Allowed {
		t.Fatalf("first=%+v err=%v", first, err)
	}
	req.TaskID = first.TaskID
	second, err := d.Request(context.Background(), req)
	if err != nil || !second.Allowed || second.TaskID != first.TaskID || second.DelegationToken != first.DelegationToken || issuer.calls != 1 {
		t.Fatalf("second=%+v issuer calls=%d err=%v", second, issuer.calls, err)
	}
	var events, assignments int
	if err = db.QueryRow(`SELECT COUNT(*) FROM events WHERE task_id=?`, first.TaskID).Scan(&events); err != nil {
		t.Fatal(err)
	}
	if err = db.QueryRow(`SELECT COUNT(*) FROM task_assignments WHERE task_id=?`, first.TaskID).Scan(&assignments); err != nil {
		t.Fatal(err)
	}
	if events != 1 || assignments != 1 {
		t.Fatalf("events=%d assignments=%d", events, assignments)
	}
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
	items, err := db.LifecycleOutboxStore().ClaimDue(context.Background(), time.Now().UTC().Add(time.Second), time.Minute, 10)
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

func TestRequireApprovalPersistsAndResumeReevaluates(t *testing.T) {
	reg := registry.New()
	_ = reg.Register(models.AgentCard{AgentID: "executor", Entrypoint: models.AgentEntrypoint{Type: "http", URL: "http://executor"}, Capabilities: []string{"delegate_execution"}})
	db := openTestDB(t)
	authorizer := &sequenceAuthorizer{decisions: []models.DelegationResponse{{Allowed: false, Verdict: "require_approval", DecisionID: "approval-decision", Reason: "review"}, {Allowed: true, Verdict: "allow", DecisionID: "allow-decision"}}}
	d := New(reg, task.New(db.TaskStore()).WithInstanceID(db.InstanceID()), token.NewHMACIssuer([]byte("secret")), nil, time.Hour).WithR2Authorizer(authorizer)
	resp, err := d.Request(context.Background(), models.DelegationRequest{RequestID: "approval-request", InitiatorAgentID: "planner", TargetAgentID: "executor", ToolName: "echo", Arguments: json.RawMessage(`{"x":1}`), AllowedTools: []string{"echo"}})
	if err != nil || resp.Verdict != "require_approval" || resp.ApprovalID == "" || resp.TaskID != "" {
		t.Fatalf("response=%+v err=%v", resp, err)
	}
	a, err := db.DelegationApprovalStore().Get(context.Background(), resp.ApprovalID)
	if err != nil {
		t.Fatal(err)
	}
	a, err = db.DelegationApprovalStore().Transition(context.Background(), a.ApprovalID, a.Version, "pending", "approved", "reviewer", "ok", "action-1")
	if err != nil {
		t.Fatal(err)
	}
	consumed, err := d.ResumeApproval(context.Background(), a.ApprovalID)
	if err != nil {
		t.Fatal(err)
	}
	if consumed.Status != "consumed" || consumed.TaskID == "" || authorizer.calls != 2 {
		t.Fatalf("approval=%+v calls=%d", consumed, authorizer.calls)
	}
	assignment, err := db.AssignmentStore().Get(context.Background(), "outbound-assignment-"+consumed.TaskID)
	if err != nil {
		t.Fatal(err)
	}
	item, err := db.DelegationDispatchOutboxStore().LoadOutboundDelegation(context.Background(), assignment.AssignmentID)
	if err != nil || item.Request.DeliveryID != "delegation-dispatch-"+consumed.TaskID {
		t.Fatalf("item=%+v err=%v", item, err)
	}
}

func TestStrictApprovalResumeRestoresSecurityBindings(t *testing.T) {
	reg := registry.New()
	_ = reg.Register(models.AgentCard{AgentID: "executor", TenantID: "tenant-1", Entrypoint: models.AgentEntrypoint{Type: "http", URL: "http://executor"}, Capabilities: []string{"delegate_execution"}})
	db := openTestDB(t)
	issuer := token.NewHMACIssuer([]byte("secret"))
	authorizer := &sequenceAuthorizer{decisions: []models.DelegationResponse{{Allowed: false, Verdict: "require_approval", DecisionID: "approval-decision"}, {Allowed: true, DecisionID: "allow-decision"}}}
	d := New(reg, task.New(db.TaskStore()).WithInstanceID(db.InstanceID()), issuer, nil, time.Hour).WithR2Authorizer(authorizer).WithStrict(true)
	resp, err := d.Request(context.Background(), models.DelegationRequest{
		RequestID: "strict-approval", InitiatorAgentID: "planner", TargetAgentID: "executor", ToolName: "echo",
		Arguments: json.RawMessage(`{"x":1}`), AllowedTools: []string{"echo"}, TenantID: "tenant-1",
		TargetWorkloadID: "spiffe://example/executor", TargetInstanceID: "instance-1",
	})
	if err != nil {
		t.Fatal(err)
	}
	a, err := db.DelegationApprovalStore().Get(context.Background(), resp.ApprovalID)
	if err != nil || a.TenantID != "tenant-1" || a.TargetWorkloadID != "spiffe://example/executor" || a.TargetInstanceID != "instance-1" {
		t.Fatalf("approval bindings not persisted: %+v err=%v", a, err)
	}
	a, err = db.DelegationApprovalStore().Transition(context.Background(), a.ApprovalID, a.Version, "pending", "approved", "reviewer", "ok", "strict-action")
	if err != nil {
		t.Fatal(err)
	}
	consumed, err := d.ResumeApproval(context.Background(), a.ApprovalID)
	if err != nil {
		t.Fatal(err)
	}
	if len(authorizer.requests) != 2 || authorizer.requests[1].TenantID != a.TenantID || authorizer.requests[1].TargetWorkloadID != a.TargetWorkloadID || authorizer.requests[1].TargetInstanceID != a.TargetInstanceID {
		t.Fatalf("strict re-evaluation lost bindings: %+v", authorizer.requests)
	}
	storedTask, err := db.TaskStore().Get(context.Background(), consumed.TaskID)
	if err != nil || storedTask.TenantID != a.TenantID || storedTask.TargetWorkloadID != a.TargetWorkloadID || storedTask.TargetInstanceID != a.TargetInstanceID {
		t.Fatalf("resumed task lost bindings: %+v err=%v", storedTask, err)
	}
	claims, err := issuer.Validate(storedTask.DelegationToken)
	if err != nil || claims.TenantID != a.TenantID || claims.TargetWorkloadID != a.TargetWorkloadID || claims.TargetInstanceID != a.TargetInstanceID {
		t.Fatalf("resumed claims lost bindings: %+v err=%v", claims, err)
	}
	assignment, err := db.AssignmentStore().Get(context.Background(), "outbound-assignment-"+consumed.TaskID)
	if err != nil {
		t.Fatal(err)
	}
	item, err := db.DelegationDispatchOutboxStore().LoadOutboundDelegation(context.Background(), assignment.AssignmentID)
	if err != nil || item.Request.TenantID != a.TenantID || item.Request.TargetWorkloadID != a.TargetWorkloadID || item.Request.TargetInstanceID != a.TargetInstanceID {
		t.Fatalf("resumed dispatch lost bindings: %+v err=%v", item, err)
	}
}

type sequenceAuthorizer struct {
	decisions []models.DelegationResponse
	requests  []models.DelegationRequest
	calls     int
}

func (a *sequenceAuthorizer) Authorize(_ context.Context, request models.DelegationRequest) (models.DelegationResponse, error) {
	a.requests = append(a.requests, request)
	d := a.decisions[a.calls]
	a.calls++
	return d, nil
}

func TestRequestModifyUsesEffectiveArgsForTokenAndDispatch(t *testing.T) {
	reg := registry.New()
	reg.Register(models.AgentCard{
		AgentID: "executor", Entrypoint: models.AgentEntrypoint{Type: "http", URL: "http://executor"},
		Capabilities: []string{"delegate_execution", "query_capability"},
	})
	db := openTestDB(t)
	dispatcher := &recordingDispatcher{}
	issuer := token.NewHMACIssuer([]byte("secret"))
	original := json.RawMessage(`{"region":"all"}`)
	effective := json.RawMessage(`{"region":"APAC"}`)
	d := New(reg, task.New(db.TaskStore()), issuer, nil, time.Hour).
		WithR2Authorizer(&StaticR2Authorizer{Decision: models.DelegationResponse{
			Allowed: true, Verdict: "modify", OriginalArgs: original,
			ModifiedArgs: effective, EffectiveArgs: effective,
		}}).
		WithEntrypointClient(dispatcher)

	resp, err := d.Request(context.Background(), models.DelegationRequest{
		RequestID: "req-modify", InitiatorAgentID: "planner", TargetAgentID: "executor",
		ToolName: "query_sales", Arguments: original,
		AllowedTools:        []string{"query_sales", "send_email"},
		AllowedCapabilities: []string{"query_capability", "delegate_execution", "unknown_capability"},
		AllowRedelegation:   true,
	})
	if err != nil {
		t.Fatalf("request: %v", err)
	}
	claims, err := issuer.Validate(resp.DelegationToken)
	if err != nil {
		t.Fatalf("validate token: %v", err)
	}
	if claims.ArgumentsSHA256 != token.HashArguments(effective) {
		t.Fatalf("token arguments digest did not use effective args")
	}
	if string(dispatcher.request.Arguments) != string(effective) {
		t.Fatalf("dispatched arguments = %s, want %s", dispatcher.request.Arguments, effective)
	}
	if len(claims.AllowedTools) != 1 || claims.AllowedTools[0] != "query_sales" ||
		len(claims.AllowedCapabilities) != 2 || claims.AllowedCapabilities[0] != "query_capability" || claims.AllowedCapabilities[1] != "delegate_execution" ||
		!claims.AllowRedelegation {
		t.Fatalf("token scope was not narrowed: %+v", claims)
	}
	if len(dispatcher.request.AllowedTools) != 1 || dispatcher.request.AllowedTools[0] != "query_sales" ||
		len(dispatcher.request.AllowedCapabilities) != 2 || dispatcher.request.AllowedCapabilities[0] != "query_capability" || dispatcher.request.AllowedCapabilities[1] != "delegate_execution" ||
		!dispatcher.request.AllowRedelegation {
		t.Fatalf("dispatch scope mismatch: %+v", dispatcher.request)
	}
	if resp.Verdict != "modify" || string(resp.EffectiveArgs) != string(effective) {
		t.Fatalf("response lost modify contract: %+v", resp)
	}
}

func TestRedelegationIntersectsParentScopeAndBindsEffectiveScope(t *testing.T) {
	reg := registry.New()
	for _, card := range []models.AgentCard{
		{AgentID: "agent-b", Capabilities: []string{"delegate_execution", "read_data"}},
		{AgentID: "agent-c", Capabilities: []string{"delegate_execution", "write_data"}},
	} {
		if err := reg.Register(card); err != nil {
			t.Fatalf("register %s: %v", card.AgentID, err)
		}
	}
	db := openTestDB(t)
	tasks := task.New(db.TaskStore())
	issuer := token.NewHMACIssuer([]byte("secret"))
	d := New(reg, tasks, issuer, nil, time.Hour).WithR2Authorizer(&StaticR2Authorizer{
		Decision: models.DelegationResponse{Allowed: true, Verdict: "allow"},
	})

	parentResp, err := d.Request(context.Background(), models.DelegationRequest{
		RequestID: "req-parent", SessionID: "session-1", InitiatorAgentID: "agent-a", TargetAgentID: "agent-b",
		ToolName: "echo", AllowedTools: []string{"echo"},
		AllowedCapabilities: []string{"delegate_execution", "read_data"}, AllowRedelegation: true,
		Budget: models.DelegationBudget{TokenCount: 100, Currency: "USD"},
	})
	if err != nil || !parentResp.Allowed {
		t.Fatalf("create parent: %+v, %v", parentResp, err)
	}
	childResp, err := d.Request(context.Background(), models.DelegationRequest{
		RequestID: "req-child", SessionID: "session-1", ParentTaskID: parentResp.TaskID,
		InitiatorAgentID: "agent-b", TargetAgentID: "agent-c", ToolName: "echo",
		AllowedTools:        []string{"echo", "other"},
		AllowedCapabilities: []string{"delegate_execution", "write_data"}, AllowRedelegation: true,
		Budget: models.DelegationBudget{TokenCount: 10, Currency: "USD"},
	})
	if err != nil || !childResp.Allowed {
		t.Fatalf("create child: %+v, %v", childResp, err)
	}
	child, err := tasks.Get(childResp.TaskID)
	if err != nil {
		t.Fatalf("get child: %v", err)
	}
	if len(child.AllowedTools) != 1 || child.AllowedTools[0] != "echo" ||
		len(child.AllowedCapabilities) != 1 || child.AllowedCapabilities[0] != "delegate_execution" || !child.AllowRedelegation {
		t.Fatalf("child scope was not the parent/request/target intersection: %+v", child)
	}
	claims, err := issuer.Validate(childResp.DelegationToken)
	if err != nil {
		t.Fatalf("validate child token: %v", err)
	}
	if !sameScope(claims.AllowedTools, child.AllowedTools) || !sameScope(claims.AllowedCapabilities, child.AllowedCapabilities) ||
		claims.ParentTaskID != parentResp.TaskID || claims.SessionID != child.SessionID || claims.InteractionID != child.InteractionID || claims.DecisionID != child.DecisionID {
		t.Fatalf("child token is not bound to effective task scope: %+v", claims)
	}
}

func TestRedelegationRejectsParentTaskWithoutMatchingTokenProof(t *testing.T) {
	reg := registry.New()
	_ = reg.Register(models.AgentCard{AgentID: "agent-c", Capabilities: []string{"delegate_execution"}})
	db := openTestDB(t)
	tasks := task.New(db.TaskStore())
	parent, err := tasks.CreateInteractionTask(models.Task{
		SessionID: "session-1", InitiatorAgentID: "agent-a", TargetAgentID: "agent-b",
		AllowedTools: []string{"echo"}, AllowedCapabilities: []string{"delegate_execution"}, AllowRedelegation: true,
	})
	if err != nil {
		t.Fatalf("create parent: %v", err)
	}
	d := New(reg, tasks, token.NewHMACIssuer([]byte("secret")), nil, time.Hour).WithR2Authorizer(&StaticR2Authorizer{
		Decision: models.DelegationResponse{Allowed: true},
	})
	resp, err := d.Request(context.Background(), models.DelegationRequest{
		RequestID: "req-child", SessionID: "session-1", ParentTaskID: parent.TaskID,
		InitiatorAgentID: "agent-b", TargetAgentID: "agent-c", ToolName: "echo",
		AllowedCapabilities: []string{"delegate_execution"},
	})
	if err != nil || resp.Allowed || resp.Reason != "parent delegation token proof unavailable" {
		t.Fatalf("expected parent token proof denial, got %+v, %v", resp, err)
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

type failingValidatingIssuer struct{ validator *token.HMACIssuer }

func (i failingValidatingIssuer) Issue(token.DelegationClaims, time.Duration) (string, error) {
	return "", errors.New("signing unavailable")
}

func (i failingValidatingIssuer) Validate(value string) (token.DelegationClaims, error) {
	return i.validator.Validate(value)
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

type recordingDispatcher struct {
	request models.EntrypointTaskRequest
}

func (d *recordingDispatcher) Dispatch(_ context.Context, _ models.AgentEntrypoint, req models.EntrypointTaskRequest) error {
	d.request = req
	return nil
}

func (d *recordingDispatcher) Cancel(context.Context, models.AgentEntrypoint, string, string) (bool, error) {
	return true, nil
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

func TestChildReservationRollsBackOnTokenAndDispatchFailure(t *testing.T) {
	for _, tc := range []struct {
		name       string
		issuer     TokenIssuer
		dispatcher EntrypointClient
		wantHeld   int64
	}{
		{name: "token issuance", issuer: failingIssuer{}, wantHeld: 0},
		{name: "dispatch definitely not sent", issuer: token.NewHMACIssuer([]byte("secret")), dispatcher: failingDispatcher{}, wantHeld: 0},
		{name: "dispatch outcome unknown", issuer: token.NewHMACIssuer([]byte("secret")), dispatcher: failingDispatcher{mayBeSent: true}, wantHeld: 60},
	} {
		t.Run(tc.name, func(t *testing.T) {
			reg := registry.New()
			reg.Register(models.AgentCard{AgentID: "child-target", Entrypoint: models.AgentEntrypoint{Type: "http", URL: "http://child"}, Capabilities: []string{"delegate_execution"}})
			db := openTestDB(t)
			tasks := task.New(db.TaskStore())
			parentIssuer := token.NewHMACIssuer([]byte("secret"))
			parent, err := tasks.CreateInteractionTask(models.Task{SessionID: "s", InitiatorAgentID: "root", TargetAgentID: "parent-agent", Budget: models.DelegationBudget{TokenCount: 100, Currency: "USD"}, AllowedTools: []string{"echo"}, AllowedCapabilities: []string{"delegate_execution"}, AllowRedelegation: true})
			if err != nil {
				t.Fatal(err)
			}
			parentToken, err := parentIssuer.Issue(token.DelegationClaims{SessionID: parent.SessionID, InteractionID: parent.InteractionID, DecisionID: parent.DecisionID, RootInteractionID: parent.RootInteractionID, ParentInteractionID: parent.ParentInteractionID, InitiatorAgentID: parent.InitiatorAgentID, TargetAgentID: parent.TargetAgentID, TaskID: parent.TaskID, AllowedTools: parent.AllowedTools, AllowedCapabilities: parent.AllowedCapabilities, AllowRedelegation: true, RootTaskID: parent.RootTaskID, ParentTaskID: parent.ParentTaskID, DelegationDepth: parent.DelegationDepth, BudgetTokenCount: 100, BudgetCurrency: "USD"}, time.Hour)
			if err != nil || tasks.SetDelegationToken(parent.TaskID, parentToken) != nil {
				t.Fatal("set parent token")
			}
			issuer := tc.issuer
			if _, ok := issuer.(failingIssuer); ok {
				issuer = failingValidatingIssuer{validator: parentIssuer}
			}
			d := New(reg, tasks, issuer, nil, time.Hour).WithR2Authorizer(&StaticR2Authorizer{Decision: models.DelegationResponse{Allowed: true}})
			if tc.dispatcher != nil {
				d.WithEntrypointClient(tc.dispatcher)
			}
			_, _ = d.Request(context.Background(), models.DelegationRequest{RequestID: "child-request", InitiatorAgentID: "parent-agent", TargetAgentID: "child-target", ToolName: "echo", SessionID: "s", ParentTaskID: parent.TaskID, Budget: models.DelegationBudget{TokenCount: 60, Currency: "USD"}, AllowedTools: []string{"echo"}, AllowedCapabilities: []string{"delegate_execution"}})
			storedParent, getErr := tasks.Get(parent.TaskID)
			if getErr != nil {
				t.Fatal(getErr)
			}
			if storedParent.ReservedBudget.TokenCount != tc.wantHeld {
				t.Fatalf("reserved = %d, want %d", storedParent.ReservedBudget.TokenCount, tc.wantHeld)
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

type concurrentAuthorizer struct{ calls atomic.Int64 }

func (a *concurrentAuthorizer) Authorize(context.Context, models.DelegationRequest) (models.DelegationResponse, error) {
	a.calls.Add(1)
	return models.DelegationResponse{Allowed: true, Verdict: "allow", DecisionID: "decision"}, nil
}

type concurrentIssuer struct{ calls atomic.Int64 }

func (i *concurrentIssuer) Issue(claims token.DelegationClaims, ttl time.Duration) (string, error) {
	i.calls.Add(1)
	return token.NewHMACIssuer([]byte("secret")).Issue(claims, ttl)
}

type concurrentDispatcher struct{ calls atomic.Int64 }

func (d *concurrentDispatcher) Dispatch(context.Context, models.AgentEntrypoint, models.EntrypointTaskRequest) error {
	d.calls.Add(1)
	return nil
}
func (d *concurrentDispatcher) Cancel(context.Context, models.AgentEntrypoint, string, string) (bool, error) {
	return true, nil
}

func TestTenantScopedTargetLookupRejectsCrossTenantBeforeSideEffects(t *testing.T) {
	db := openTestDB(t)
	reg := registry.NewStore(db.AgentStore())
	_ = reg.Register(models.AgentCard{AgentID: "executor-b", TenantID: "tenant-b", Entrypoint: models.AgentEntrypoint{Type: "http", URL: "http://tenant-b"}, Capabilities: []string{"delegate_execution"}})
	authorizer := &concurrentAuthorizer{}
	issuer := &concurrentIssuer{}
	dispatcher := &concurrentDispatcher{}
	d := New(reg, task.New(db.TaskStore()).WithInstanceID(db.InstanceID()), issuer, nil, time.Hour).
		WithR2Authorizer(authorizer).WithEntrypointClient(dispatcher).WithOutboundQueue(db.DelegationDispatchOutboxStore())

	request := func(id, target string) models.DelegationResponse {
		resp, err := d.Request(context.Background(), models.DelegationRequest{RequestID: id, InitiatorAgentID: "planner", TargetAgentID: target, ToolName: "echo", AllowedTools: []string{"echo"}, TenantID: "tenant-a"})
		if err != nil {
			t.Errorf("request %s: %v", id, err)
		}
		return resp
	}
	cross := request("cross", "executor-b")
	unknown := request("unknown", "missing")
	crossJSON, _ := json.Marshal(cross)
	unknownJSON, _ := json.Marshal(unknown)
	if string(crossJSON) != string(unknownJSON) || cross.Allowed || cross.Reason != "target agent not found" {
		t.Fatalf("cross=%+v unknown=%+v", cross, unknown)
	}
	for _, table := range []string{"tasks", "delegation_approvals", "delegation_dispatch_outbox", "task_assignments"} {
		var count int
		if err := db.QueryRow("SELECT COUNT(*) FROM " + table).Scan(&count); err != nil || count != 0 {
			t.Fatalf("%s count=%d err=%v", table, count, err)
		}
	}
	if authorizer.calls.Load() != 0 || issuer.calls.Load() != 0 || dispatcher.calls.Load() != 0 {
		t.Fatalf("side effects: authorizer=%d issuer=%d dispatcher=%d", authorizer.calls.Load(), issuer.calls.Load(), dispatcher.calls.Load())
	}
}

func TestTenantScopedTargetLookupAllowsSameTenantAndLegacyUnscoped(t *testing.T) {
	for _, tenantID := range []string{"tenant-a", ""} {
		db := openTestDB(t)
		reg := registry.NewStore(db.AgentStore())
		_ = reg.Register(models.AgentCard{AgentID: "executor", TenantID: "tenant-a", Capabilities: []string{"delegate_execution"}})
		d := New(reg, task.New(db.TaskStore()), token.NewHMACIssuer([]byte("secret")), nil, time.Hour).WithR2Authorizer(&StaticR2Authorizer{Decision: models.DelegationResponse{Allowed: true}})
		resp, err := d.Request(context.Background(), models.DelegationRequest{RequestID: "request-" + tenantID, InitiatorAgentID: "planner", TargetAgentID: "executor", ToolName: "echo", TenantID: tenantID})
		if err != nil || !resp.Allowed {
			t.Fatalf("tenant=%q response=%+v err=%v", tenantID, resp, err)
		}
	}
}

func TestApprovalResumeRechecksPersistedTenantAndTarget(t *testing.T) {
	for _, mutate := range []struct{ name, sql, value string }{
		{name: "tenant", sql: "UPDATE delegation_approvals SET tenant_id=? WHERE approval_id=?", value: "tenant-b"},
		{name: "target", sql: "UPDATE delegation_approvals SET target_agent_id=? WHERE approval_id=?", value: "executor-b"},
	} {
		t.Run(mutate.name, func(t *testing.T) {
			db := openTestDB(t)
			reg := registry.NewStore(db.AgentStore())
			_ = reg.Register(models.AgentCard{AgentID: "executor-a", TenantID: "tenant-a", Capabilities: []string{"delegate_execution"}})
			_ = reg.Register(models.AgentCard{AgentID: "executor-b", TenantID: "tenant-b", Capabilities: []string{"delegate_execution"}})
			authorizer := &sequenceAuthorizer{decisions: []models.DelegationResponse{{Allowed: false, Verdict: "require_approval", DecisionID: "approval-decision"}, {Allowed: true, Verdict: "allow"}}}
			issuer := &concurrentIssuer{}
			d := New(reg, task.New(db.TaskStore()).WithInstanceID(db.InstanceID()), issuer, nil, time.Hour).WithR2Authorizer(authorizer)
			resp, err := d.Request(context.Background(), models.DelegationRequest{RequestID: "approval", InitiatorAgentID: "planner", TargetAgentID: "executor-a", ToolName: "echo", AllowedTools: []string{"echo"}, TenantID: "tenant-a"})
			if err != nil {
				t.Fatal(err)
			}
			a, _ := db.DelegationApprovalStore().Get(context.Background(), resp.ApprovalID)
			a, err = db.DelegationApprovalStore().Transition(context.Background(), a.ApprovalID, a.Version, "pending", "approved", "reviewer", "ok", "action")
			if err != nil {
				t.Fatal(err)
			}
			if _, err = db.Exec(mutate.sql, mutate.value, a.ApprovalID); err != nil {
				t.Fatal(err)
			}
			if _, err = d.ResumeApproval(context.Background(), a.ApprovalID); err == nil || err.Error() != "target agent not found" {
				t.Fatalf("resume err=%v", err)
			}
			if authorizer.calls != 1 || issuer.calls.Load() != 0 {
				t.Fatalf("authorizer=%d issuer=%d", authorizer.calls, issuer.calls.Load())
			}
			for _, table := range []string{"tasks", "delegation_dispatch_outbox", "task_assignments"} {
				var count int
				_ = db.QueryRow("SELECT COUNT(*) FROM " + table).Scan(&count)
				if count != 0 {
					t.Fatalf("%s count=%d", table, count)
				}
			}
		})
	}
}

func TestConcurrentCrossTenantRequestsCannotBypassLookup(t *testing.T) {
	db := openTestDB(t)
	reg := registry.NewStore(db.AgentStore())
	_ = reg.Register(models.AgentCard{AgentID: "executor", TenantID: "tenant-b", Capabilities: []string{"delegate_execution"}})
	authorizer := &concurrentAuthorizer{}
	issuer := &concurrentIssuer{}
	dispatcher := &concurrentDispatcher{}
	d := New(reg, task.New(db.TaskStore()).WithInstanceID(db.InstanceID()), issuer, nil, time.Hour).WithR2Authorizer(authorizer).WithEntrypointClient(dispatcher).WithOutboundQueue(db.DelegationDispatchOutboxStore())
	var wg sync.WaitGroup
	for i := 0; i < 32; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			resp, err := d.Request(context.Background(), models.DelegationRequest{RequestID: fmt.Sprintf("cross-%d", i), InitiatorAgentID: "planner", TargetAgentID: "executor", ToolName: "echo", TenantID: "tenant-a"})
			if err != nil || resp.Allowed || resp.Reason != "target agent not found" {
				t.Errorf("response=%+v err=%v", resp, err)
			}
		}(i)
	}
	wg.Wait()
	if authorizer.calls.Load() != 0 || issuer.calls.Load() != 0 || dispatcher.calls.Load() != 0 {
		t.Fatalf("side effects: authorizer=%d issuer=%d dispatcher=%d", authorizer.calls.Load(), issuer.calls.Load(), dispatcher.calls.Load())
	}
}

func TestRequestExistingTaskMustMatchCompleteDelegationContext(t *testing.T) {
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
	if resp.Allowed || resp.Reason != "existing task does not match delegation request" {
		t.Fatalf("expected incomplete existing task to be rejected, got %+v", resp)
	}
}
