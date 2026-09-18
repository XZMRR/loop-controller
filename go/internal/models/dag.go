package models

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"sort"
	"time"
)

const DAGProtocolVersion = "0.54.0"

var (
	ErrDAGInvalid       = errors.New("invalid task graph")
	ErrDAGRetryPolicy   = errors.New("invalid task graph retry policy")
	ErrDAGLimit         = errors.New("task graph limit exceeded")
	ErrDAGDuplicateNode = errors.New("duplicate graph node or task")
	ErrDAGEdge          = errors.New("invalid graph edge")
	ErrDAGCycle         = errors.New("task graph contains cycle")
	ErrDAGDepth         = errors.New("task graph depth exceeds 32")
	ErrDAGWidth         = errors.New("task graph layered width exceeds 64")
	ErrDAGBudget        = errors.New("invalid task graph budget")
	ErrDAGDeadline      = errors.New("invalid task graph deadline")
)

type TaskGraphCreate struct {
	ProtocolVersion string           `json:"protocol_version"`
	DAGID           string           `json:"dag_id"`
	TenantID        string           `json:"tenant_id"`
	RootTaskID      string           `json:"root_task_id"`
	MaxParallelism  int              `json:"max_parallelism"`
	Deadline        *time.Time       `json:"deadline,omitempty"`
	BudgetEnvelope  DelegationBudget `json:"budget_envelope"`
	Nodes           []TaskNode       `json:"nodes"`
}

type TaskGraphEdge struct {
	FromNodeID string `json:"from_node_id"`
	ToNodeID   string `json:"to_node_id"`
}

func validDAGRetryPolicy(p RetryPolicy, nodeBudget DelegationBudget) bool {
	zero := p.MaxAttempts == 0 && p.InitialBackoff == 0 && p.MaxBackoff == 0 && p.BackoffMultiplier == 0 && p.Jitter == 0 && len(p.RetryableFailureClasses) == 0 && p.AttemptBudgetLimit == (DelegationBudget{}) && p.Deadline == nil
	if !zero && (p.MaxAttempts < 1 || p.InitialBackoff < 0 || p.MaxBackoff < p.InitialBackoff || p.BackoffMultiplier < 1 || p.Jitter < 0 || p.Jitter > 1) {
		return false
	}
	for _, class := range p.RetryableFailureClasses {
		if class != FailureClassPreDispatchTransient {
			return false
		}
	}
	attempt := p.AttemptBudgetLimit
	if attempt.TokenCount < 0 || attempt.PaymentAmount < 0 || attempt.TokenCount > nodeBudget.TokenCount || attempt.PaymentAmount > nodeBudget.PaymentAmount {
		return false
	}
	if (attempt.TokenCount != 0 || attempt.PaymentAmount != 0) && attempt.Currency != nodeBudget.Currency {
		return false
	}
	return true
}

func ValidateTaskGraph(in TaskGraphCreate) (TaskGraphCreate, []TaskGraphEdge, string, error) {
	if in.ProtocolVersion != DAGProtocolVersion || in.DAGID == "" || in.TenantID == "" || in.RootTaskID == "" || len(in.Nodes) == 0 {
		return in, nil, "", ErrDAGInvalid
	}
	if len(in.Nodes) > 256 || in.MaxParallelism < 1 || in.MaxParallelism > 32 {
		return in, nil, "", ErrDAGLimit
	}
	if in.BudgetEnvelope.TokenCount < 0 || in.BudgetEnvelope.PaymentAmount < 0 {
		return in, nil, "", ErrDAGBudget
	}
	byID, tasks := map[string]int{}, map[string]bool{}
	for i := range in.Nodes {
		n := &in.Nodes[i]
		if n.NodeID == "" || n.TaskID == "" || n.TaskID == in.RootTaskID || tasks[n.TaskID] || byID[n.NodeID] != 0 {
			return in, nil, "", ErrDAGDuplicateNode
		}
		byID[n.NodeID], tasks[n.TaskID] = i+1, true
		if n.FailurePolicy != TaskFailurePolicyFailFast && n.FailurePolicy != TaskFailurePolicyContinueIndependent {
			return in, nil, "", ErrDAGInvalid
		}
		if n.BudgetLimit.TokenCount < 0 || n.BudgetLimit.PaymentAmount < 0 || n.BudgetLimit.Currency != in.BudgetEnvelope.Currency {
			return in, nil, "", ErrDAGBudget
		}
		if n.SchedulingRequest.RequestID == "" || n.SchedulingRequest.TaskID != n.TaskID || n.SchedulingRequest.TenantID != in.TenantID || n.SchedulingRequest.DAGID != in.DAGID || n.SchedulingRequest.NodeID != n.NodeID {
			return in, nil, "", ErrDAGInvalid
		}
		if in.Deadline != nil && n.SchedulingRequest.Deadline != nil && n.SchedulingRequest.Deadline.After(*in.Deadline) {
			return in, nil, "", ErrDAGDeadline
		}
		effectiveDeadline := in.Deadline
		if n.SchedulingRequest.Deadline != nil && (effectiveDeadline == nil || n.SchedulingRequest.Deadline.Before(*effectiveDeadline)) {
			effectiveDeadline = n.SchedulingRequest.Deadline
		}
		if n.RetryPolicy.Deadline != nil && (effectiveDeadline == nil || n.RetryPolicy.Deadline.After(*effectiveDeadline)) {
			return in, nil, "", ErrDAGDeadline
		}
		if !validDAGRetryPolicy(n.RetryPolicy, n.BudgetLimit) {
			return in, nil, "", ErrDAGRetryPolicy
		}
	}
	edgeSet, edges, indegree, next := map[string]bool{}, []TaskGraphEdge{}, map[string]int{}, map[string][]string{}
	for i := range in.Nodes {
		sort.Strings(in.Nodes[i].Dependencies)
		for j, dep := range in.Nodes[i].Dependencies {
			if dep == in.Nodes[i].NodeID || byID[dep] == 0 || (j > 0 && dep == in.Nodes[i].Dependencies[j-1]) {
				return in, nil, "", ErrDAGEdge
			}
			key := dep + "\x00" + in.Nodes[i].NodeID
			if edgeSet[key] {
				return in, nil, "", ErrDAGEdge
			}
			edgeSet[key] = true
			edges = append(edges, TaskGraphEdge{dep, in.Nodes[i].NodeID})
			indegree[in.Nodes[i].NodeID]++
			next[dep] = append(next[dep], in.Nodes[i].NodeID)
		}
	}
	if len(edges) > 4096 {
		return in, nil, "", ErrDAGLimit
	}
	ready := []string{}
	depth := map[string]int{}
	for id := range byID {
		if indegree[id] == 0 {
			ready = append(ready, id)
		}
	}
	sort.Strings(ready)
	order := []string{}
	widths := map[int]int{}
	for len(ready) > 0 {
		id := ready[0]
		ready = ready[1:]
		order = append(order, id)
		widths[depth[id]]++
		if widths[depth[id]] > 64 {
			return in, nil, "", ErrDAGWidth
		}
		for _, to := range next[id] {
			if depth[to] < depth[id]+1 {
				depth[to] = depth[id] + 1
			}
			indegree[to]--
			if indegree[to] == 0 {
				ready = append(ready, to)
				sort.Strings(ready)
			}
		}
	}
	if len(order) != len(in.Nodes) {
		return in, nil, "", ErrDAGCycle
	}
	for topo, id := range order {
		i := byID[id] - 1
		in.Nodes[i].TopoOrder = topo
		in.Nodes[i].Depth = depth[id]
		if depth[id] > 32 {
			return in, nil, "", ErrDAGDepth
		}
	}
	sort.Slice(in.Nodes, func(i, j int) bool { return in.Nodes[i].NodeID < in.Nodes[j].NodeID })
	sort.Slice(edges, func(i, j int) bool {
		if edges[i].FromNodeID == edges[j].FromNodeID {
			return edges[i].ToNodeID < edges[j].ToNodeID
		}
		return edges[i].FromNodeID < edges[j].FromNodeID
	})
	b, _ := json.Marshal(struct {
		Graph TaskGraphCreate `json:"graph"`
		Edges []TaskGraphEdge `json:"edges"`
	}{in, edges})
	sum := sha256.Sum256(b)
	return in, edges, hex.EncodeToString(sum[:]), nil
}
