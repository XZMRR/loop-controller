package router

import (
	"errors"
	"math/big"
	"sort"
	"time"

	"github.com/loop-controller/go/internal/models"
)

const SchedulingAlgorithmVersion = "capability-router-v1"

var ErrInvalidSchedulingRequest = errors.New("invalid scheduling request")

const (
	ReasonProtocolMismatch           = "protocol_mismatch"
	ReasonObservedGenerationMismatch = "observed_generation_mismatch"
	ReasonDeadlineExceeded           = "deadline_exceeded"
	ReasonBudgetUnavailable          = "budget_unavailable"
	ReasonExcluded                   = "excluded_agent"
	ReasonAntiAffinityMismatch       = "anti_affinity_mismatch"
)

type SchedulingInput struct {
	Request         models.SchedulingRequest
	ProtocolVersion string
	WorkloadID      string
	CapacityUnits   int
	BudgetAvailable bool
	Now             time.Time
}

type SchedulingAgent struct {
	Record        models.AgentRecord
	ReservedUnits int
}

type ScoreBreakdown struct {
	FailurePenalty  int `json:"failure_penalty"`
	ActiveLoad      int `json:"active_load"`
	MaxConcurrency  int `json:"max_concurrency"`
	AffinityMatches int `json:"affinity_matches"`
	Priority        int `json:"priority"`
	Weight          int `json:"weight"`
}

type SchedulingCandidate struct {
	AgentID        string         `json:"agent_id"`
	Eligible       bool           `json:"eligible"`
	ReasonCodes    []string       `json:"reason_codes"`
	ScoreBreakdown ScoreBreakdown `json:"score_breakdown"`
	SpecRevision   int64          `json:"spec_revision"`
	StatusRevision int64          `json:"status_revision"`
}

type SchedulingDecision struct {
	SelectedAgentID  string                `json:"selected_agent_id,omitempty"`
	AlgorithmVersion string                `json:"algorithm_version"`
	Candidates       []SchedulingCandidate `json:"candidates"`
}

func Validate(in SchedulingInput) error {
	r := in.Request
	if r.RequestID == "" || r.TenantID == "" || r.TaskID == "" || in.CapacityUnits <= 0 {
		return ErrInvalidSchedulingRequest
	}
	if r.BudgetEnvelope.TokenCount < 0 || r.BudgetEnvelope.PaymentAmount < 0 {
		return ErrInvalidSchedulingRequest
	}
	for _, items := range [][]string{r.RequiredAgentCapabilities, r.RequiredSecurityCapabilities, r.RequiredTools, r.ExcludedAgentIDs} {
		for _, item := range items {
			if item == "" {
				return ErrInvalidSchedulingRequest
			}
		}
	}
	for key := range r.Affinity {
		if key == "" {
			return ErrInvalidSchedulingRequest
		}
	}
	for key := range r.AntiAffinity {
		if key == "" {
			return ErrInvalidSchedulingRequest
		}
	}
	return nil
}

func Rank(in SchedulingInput, agents []SchedulingAgent) (SchedulingDecision, error) {
	if err := Validate(in); err != nil {
		return SchedulingDecision{}, err
	}
	if in.Now.IsZero() {
		in.Now = time.Now().UTC()
	}
	excluded := set(in.Request.ExcludedAgentIDs)
	previous := set(in.Request.RetryContext.PreviousAgentIDs)
	out := SchedulingDecision{AlgorithmVersion: SchedulingAlgorithmVersion, Candidates: make([]SchedulingCandidate, 0, len(agents))}
	for _, agent := range agents {
		spec, status := agent.Record.Spec, agent.Record.Status
		candidate := SchedulingCandidate{AgentID: spec.AgentID, SpecRevision: spec.ResourceVersion}
		add := func(condition bool, reason string) {
			if condition {
				candidate.ReasonCodes = append(candidate.ReasonCodes, reason)
			}
		}
		add(spec.TenantID != in.Request.TenantID, models.SchedulingReasonTenantMismatch)
		add(in.Request.TrustDomain != "" && spec.TrustDomain != in.Request.TrustDomain, models.SchedulingReasonTrustDomainMismatch)
		add(in.ProtocolVersion != "" && !contains(spec.SupportedProtocolVersions, in.ProtocolVersion), ReasonProtocolMismatch)
		add(in.WorkloadID != "" && spec.ExpectedWorkloadID != in.WorkloadID, models.SchedulingReasonWorkloadMismatch)
		add(!containsAll(spec.SupportedSecurityCapabilities, in.Request.RequiredSecurityCapabilities), models.SchedulingReasonSecurityMismatch)
		add(!containsAll(spec.Capabilities, in.Request.RequiredAgentCapabilities), models.SchedulingReasonCapabilityMismatch)
		add(!containsAll(spec.SupportedTools, in.Request.RequiredTools), models.SchedulingReasonToolMismatch)
		add(status == nil, models.SchedulingReasonStatusMissing)
		if status != nil {
			candidate.StatusRevision = status.StatusRevision
			add(status.ObservedGeneration != spec.Generation, ReasonObservedGenerationMismatch)
			add(!status.ExpiresAt.After(in.Now), models.SchedulingReasonStatusExpired)
			add(status.Health != models.AgentHealthHealthy, models.SchedulingReasonUnhealthy)
			add(status.Draining, models.SchedulingReasonDraining)
		}
		add(!spec.Schedulable, models.SchedulingReasonNotSchedulable)
		add(in.Request.Deadline != nil && !in.Request.Deadline.After(in.Now), ReasonDeadlineExceeded)
		add(!in.BudgetAvailable, ReasonBudgetUnavailable)
		add(spec.MaxConcurrency <= 0 || agent.ReservedUnits > spec.MaxConcurrency-in.CapacityUnits, models.SchedulingReasonCapacityUnavailable)
		add(excluded[spec.AgentID], ReasonExcluded)
		add(matchesAny(spec.Labels, in.Request.AntiAffinity), ReasonAntiAffinityMismatch)
		candidate.Eligible = len(candidate.ReasonCodes) == 0
		candidate.ScoreBreakdown = ScoreBreakdown{ActiveLoad: agent.ReservedUnits, MaxConcurrency: spec.MaxConcurrency, AffinityMatches: matchCount(spec.Labels, in.Request.Affinity), Priority: spec.Priority, Weight: spec.Weight}
		if status != nil {
			candidate.ScoreBreakdown.ActiveLoad += status.CurrentLoad
		}
		if previous[spec.AgentID] {
			candidate.ScoreBreakdown.FailurePenalty = max(1, in.Request.RetryContext.Attempt)
		}
		out.Candidates = append(out.Candidates, candidate)
	}
	sort.Slice(out.Candidates, func(i, j int) bool { return out.Candidates[i].AgentID < out.Candidates[j].AgentID })
	eligible := make([]SchedulingCandidate, 0, len(out.Candidates))
	for _, c := range out.Candidates {
		if c.Eligible {
			eligible = append(eligible, c)
		}
	}
	sort.SliceStable(eligible, func(i, j int) bool { return better(eligible[i], eligible[j]) })
	if len(eligible) > 0 {
		out.SelectedAgentID = eligible[0].AgentID
	}
	return out, nil
}

func better(a, b SchedulingCandidate) bool {
	x, y := a.ScoreBreakdown, b.ScoreBreakdown
	if x.FailurePenalty != y.FailurePenalty {
		return x.FailurePenalty < y.FailurePenalty
	}
	left := new(big.Int).Mul(big.NewInt(int64(x.ActiveLoad)), big.NewInt(int64(y.MaxConcurrency)))
	right := new(big.Int).Mul(big.NewInt(int64(y.ActiveLoad)), big.NewInt(int64(x.MaxConcurrency)))
	if cmp := left.Cmp(right); cmp != 0 {
		return cmp < 0
	}
	if x.AffinityMatches != y.AffinityMatches {
		return x.AffinityMatches > y.AffinityMatches
	}
	if x.Priority != y.Priority {
		return x.Priority > y.Priority
	}
	if x.Weight != y.Weight {
		return x.Weight > y.Weight
	}
	return a.AgentID < b.AgentID
}

func set(items []string) map[string]bool {
	out := make(map[string]bool, len(items))
	for _, item := range items {
		out[item] = true
	}
	return out
}
func contains(items []string, wanted string) bool {
	for _, item := range items {
		if item == wanted {
			return true
		}
	}
	return false
}
func containsAll(items, wanted []string) bool {
	have := set(items)
	for _, item := range wanted {
		if !have[item] {
			return false
		}
	}
	return true
}
func matchesAny(labels, wanted map[string]string) bool {
	for k, v := range wanted {
		if labels[k] == v {
			return true
		}
	}
	return false
}
func matchCount(labels, wanted map[string]string) int {
	n := 0
	for k, v := range wanted {
		if labels[k] == v {
			n++
		}
	}
	return n
}
