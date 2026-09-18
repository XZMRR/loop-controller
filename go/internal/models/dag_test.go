package models

import (
	"errors"
	"testing"
	"time"
)

func graphFixture() TaskGraphCreate {
	return TaskGraphCreate{ProtocolVersion: DAGProtocolVersion, DAGID: "g", TenantID: "t", RootTaskID: "root", MaxParallelism: 2, BudgetEnvelope: DelegationBudget{TokenCount: 10, Currency: "USD"}, Nodes: []TaskNode{{NodeID: "b", TaskID: "tb", FailurePolicy: TaskFailurePolicyFailFast, BudgetLimit: DelegationBudget{TokenCount: 3, Currency: "USD"}, Dependencies: []string{"a"}, SchedulingRequest: SchedulingRequest{RequestID: "rb", TenantID: "t", TaskID: "tb", DAGID: "g", NodeID: "b"}}, {NodeID: "a", TaskID: "ta", FailurePolicy: TaskFailurePolicyFailFast, BudgetLimit: DelegationBudget{TokenCount: 3, Currency: "USD"}, SchedulingRequest: SchedulingRequest{RequestID: "ra", TenantID: "t", TaskID: "ta", DAGID: "g", NodeID: "a"}}}}
}
func TestValidateTaskGraphDeterministicAndCycle(t *testing.T) {
	g := graphFixture()
	a, e1, h1, err := ValidateTaskGraph(g)
	if err != nil {
		t.Fatal(err)
	}
	b, e2, h2, err := ValidateTaskGraph(g)
	if err != nil || h1 != h2 || len(e1) != len(e2) || a.Nodes[0].NodeID != "a" || b.Nodes[1].Depth != 1 {
		t.Fatalf("non deterministic: %+v %+v %s %s %v", a, b, h1, h2, err)
	}
	g.Nodes[0].Dependencies = []string{"b"}
	g.Nodes[1].Dependencies = []string{"a"}
	_, _, _, err = ValidateTaskGraph(g)
	if !errors.Is(err, ErrDAGCycle) {
		t.Fatalf("cycle error=%v", err)
	}
}
func TestValidateTaskGraphRejectsMismatchedOrMissingSchedulingIdentity(t *testing.T) {
	for _, mutate := range []func(*TaskGraphCreate){
		func(g *TaskGraphCreate) { g.Nodes[0].SchedulingRequest.RequestID = "" },
		func(g *TaskGraphCreate) { g.Nodes[0].SchedulingRequest.TenantID = "other" },
		func(g *TaskGraphCreate) { g.Nodes[0].SchedulingRequest.TaskID = "other" },
		func(g *TaskGraphCreate) { g.Nodes[0].SchedulingRequest.DAGID = "other" },
		func(g *TaskGraphCreate) { g.Nodes[0].SchedulingRequest.NodeID = "other" },
	} {
		g := graphFixture()
		mutate(&g)
		if _, _, _, err := ValidateTaskGraph(g); !errors.Is(err, ErrDAGInvalid) {
			t.Fatalf("mismatch accepted: %v", err)
		}
	}
}

func TestValidateTaskGraphRejectsUnsafeRetryPolicy(t *testing.T) {
	deadline := time.Now().UTC().Add(time.Hour)
	for name, mutate := range map[string]func(*TaskGraphCreate){
		"forbidden class": func(g *TaskGraphCreate) {
			g.Nodes[0].RetryPolicy.RetryableFailureClasses = []FailureClass{FailureClassSentUnacknowledged}
		},
		"invalid backoff": func(g *TaskGraphCreate) {
			g.Nodes[0].RetryPolicy.InitialBackoff = time.Second
			g.Nodes[0].RetryPolicy.MaxBackoff = time.Millisecond
		},
		"invalid jitter": func(g *TaskGraphCreate) { g.Nodes[0].RetryPolicy.Jitter = 2 },
		"attempt budget": func(g *TaskGraphCreate) {
			g.Nodes[0].RetryPolicy.AttemptBudgetLimit = DelegationBudget{TokenCount: 4, Currency: "USD"}
		},
	} {
		t.Run(name, func(t *testing.T) {
			g := graphFixture()
			mutate(&g)
			if _, _, _, err := ValidateTaskGraph(g); !errors.Is(err, ErrDAGRetryPolicy) {
				t.Fatalf("err=%v", err)
			}
		})
	}
	g := graphFixture()
	g.Deadline = &deadline
	nodeDeadline := deadline.Add(time.Second)
	g.Nodes[0].RetryPolicy.Deadline = &nodeDeadline
	if _, _, _, err := ValidateTaskGraph(g); !errors.Is(err, ErrDAGDeadline) {
		t.Fatalf("deadline err=%v", err)
	}
}

func TestValidateTaskGraphLimitsAndRootNode(t *testing.T) {
	g := graphFixture()
	g.MaxParallelism = 33
	if _, _, _, e := ValidateTaskGraph(g); !errors.Is(e, ErrDAGLimit) {
		t.Fatal(e)
	}
	g = graphFixture()
	g.Nodes[0].TaskID = "root"
	if _, _, _, e := ValidateTaskGraph(g); !errors.Is(e, ErrDAGDuplicateNode) {
		t.Fatal(e)
	}
}
