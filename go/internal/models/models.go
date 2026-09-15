// Package models defines the JSON models for the Loop Controller A2A kernel.
// The HTTP/JSON contract is authoritative; these models do not mirror the archived proto.
package models

import (
	"encoding/json"
	"fmt"
	"time"
)

const (
	CurrentProtocolVersion       = "0.54.0"
	CompatibleProtocolVersionV53 = "0.53.0"
	TaskEventSchemaVersion       = 1
)

type AssignmentState string
type AssignmentKind string

const (
	AssignmentKindTargetExecution    AssignmentKind = "target_execution"
	AssignmentKindOutboundDelegation AssignmentKind = "outbound_delegation"

	AssignmentStateQueued     AssignmentState = "queued"
	AssignmentStateClaimed    AssignmentState = "claimed"
	AssignmentStateDispatched AssignmentState = "dispatched"
	AssignmentStateExecuting  AssignmentState = "executing"
	AssignmentStateSettled    AssignmentState = "settled"
	AssignmentStateRetryWait  AssignmentState = "retry_wait"
	AssignmentStateDeadLetter AssignmentState = "dead_letter"
	AssignmentStateCancelled  AssignmentState = "cancelled"
	AssignmentStateSuperseded AssignmentState = "superseded"
)

type FailureClass string
type DispatchDisposition string

const (
	DispatchDispositionConfirmedNotSent   DispatchDisposition = "confirmed_not_sent"
	DispatchDispositionSentUnacknowledged DispatchDisposition = "sent_unacknowledged"
	DispatchDispositionUnknown            DispatchDisposition = "unknown"

	FailureClassPreDispatchTransient FailureClass = "pre_dispatch_transient"
	FailureClassPreDispatchPermanent FailureClass = "pre_dispatch_permanent"
	FailureClassSentUnacknowledged   FailureClass = "sent_unacknowledged"
	FailureClassRemoteRejected       FailureClass = "remote_rejected"
	FailureClassRemoteTimeout        FailureClass = "remote_timeout"
	FailureClassReceiptInvalid       FailureClass = "receipt_invalid"
	FailureClassSecurityViolation    FailureClass = "security_violation"
	FailureClassCancelled            FailureClass = "cancelled"
	FailureClassBudgetExhausted      FailureClass = "budget_exhausted"
	FailureClassDeadlineExceeded     FailureClass = "deadline_exceeded"
)

type AgentHealth string

const (
	AgentHealthHealthy   AgentHealth = "healthy"
	AgentHealthDegraded  AgentHealth = "degraded"
	AgentHealthUnhealthy AgentHealth = "unhealthy"
	AgentHealthUnknown   AgentHealth = "unknown"
	AgentHealthExpired   AgentHealth = "expired"
)

type TaskFailurePolicy string

const (
	TaskFailurePolicyFailFast            TaskFailurePolicy = "fail_fast"
	TaskFailurePolicyContinueIndependent TaskFailurePolicy = "continue_independent"
)

type GraphStatus string

const (
	GraphStatusPending        GraphStatus = "pending"
	GraphStatusRunning        GraphStatus = "running"
	GraphStatusCancelling     GraphStatus = "cancelling"
	GraphStatusCompleted      GraphStatus = "completed"
	GraphStatusFailed         GraphStatus = "failed"
	GraphStatusCancelled      GraphStatus = "cancelled"
	GraphStatusOutcomeUnknown GraphStatus = "outcome_unknown"
)

type TaskNodeStatus string

const (
	TaskNodeStatusPending        TaskNodeStatus = "pending"
	TaskNodeStatusReady          TaskNodeStatus = "ready"
	TaskNodeStatusRunning        TaskNodeStatus = "running"
	TaskNodeStatusCompleted      TaskNodeStatus = "completed"
	TaskNodeStatusFailed         TaskNodeStatus = "failed"
	TaskNodeStatusCancelled      TaskNodeStatus = "cancelled"
	TaskNodeStatusSkipped        TaskNodeStatus = "skipped"
	TaskNodeStatusOutcomeUnknown TaskNodeStatus = "outcome_unknown"
)

// AgentSpec is the stable, administrator-controlled scheduling declaration.
type AgentSpec struct {
	TenantID                      string            `json:"tenant_id"`
	AgentID                       string            `json:"agent_id"`
	Name                          string            `json:"name"`
	Description                   string            `json:"description,omitempty"`
	Entrypoint                    AgentEntrypoint   `json:"entrypoint"`
	Capabilities                  []string          `json:"capabilities"`
	SupportedTools                []string          `json:"supported_tools"`
	TrustDomain                   string            `json:"trust_domain"`
	SupportedProtocolVersions     []string          `json:"supported_protocol_versions"`
	SupportedSecurityCapabilities []string          `json:"supported_security_capabilities"`
	ExpectedWorkloadID            string            `json:"expected_workload_id"`
	Labels                        map[string]string `json:"labels,omitempty"`
	Priority                      int               `json:"priority"`
	Weight                        int               `json:"weight"`
	MaxConcurrency                int               `json:"max_concurrency"`
	Schedulable                   bool              `json:"schedulable"`
	SourceType                    string            `json:"source_type"`
	SourceID                      string            `json:"source_id"`
	ExternalRevision              string            `json:"external_revision,omitempty"`
	Generation                    int64             `json:"generation"`
	ResourceVersion               int64             `json:"resource_version"`
	CreatedAt                     time.Time         `json:"created_at"`
	UpdatedAt                     time.Time         `json:"updated_at"`
	DeletedAt                     *time.Time        `json:"deleted_at,omitempty"`
}

// AgentStatus is the independently revised, short-lived scheduling status.
type AgentRecord struct {
	Spec   AgentSpec    `json:"spec"`
	Status *AgentStatus `json:"status,omitempty"`
}

type AgentHeartbeatPatch struct {
	ObservedGeneration int64       `json:"observed_generation"`
	Health             AgentHealth `json:"health"`
	CurrentLoad        int         `json:"current_load"`
	AvailableCapacity  int         `json:"available_capacity"`
	ExpiresAt          time.Time   `json:"expires_at"`
}

type DiscoverySnapshot struct {
	TenantID   string        `json:"tenant_id"`
	SourceType string        `json:"source_type"`
	SourceID   string        `json:"source_id"`
	Revision   string        `json:"revision,omitempty"`
	Agents     []AgentSpec   `json:"agents"`
	StatusTTL  time.Duration `json:"status_ttl,omitempty"`
}

type DiscoverySourceState struct {
	TenantID        string     `json:"tenant_id"`
	SourceType      string     `json:"source_type"`
	SourceID        string     `json:"source_id"`
	LastRevision    string     `json:"last_revision,omitempty"`
	LastSuccessAt   *time.Time `json:"last_success_at,omitempty"`
	LastAttemptAt   time.Time  `json:"last_attempt_at"`
	Stale           bool       `json:"stale"`
	LastError       string     `json:"last_error,omitempty"`
	ResourceVersion int64      `json:"resource_version"`
}

type AgentCapacityReservation struct {
	ReservationID string    `json:"reservation_id"`
	TenantID      string    `json:"tenant_id"`
	AgentID       string    `json:"agent_id"`
	TaskID        string    `json:"task_id"`
	Units         int       `json:"units"`
	ExpiresAt     time.Time `json:"expires_at"`
	CreatedAt     time.Time `json:"created_at"`
}

const (
	SchedulingReasonEligible            = "eligible"
	SchedulingReasonTenantMismatch      = "tenant_mismatch"
	SchedulingReasonNotSchedulable      = "not_schedulable"
	SchedulingReasonStatusMissing       = "status_missing"
	SchedulingReasonStatusExpired       = "status_expired"
	SchedulingReasonUnhealthy           = "unhealthy"
	SchedulingReasonDraining            = "draining"
	SchedulingReasonCapacityUnavailable = "capacity_unavailable"
	SchedulingReasonCapabilityMismatch  = "capability_mismatch"
	SchedulingReasonSecurityMismatch    = "security_capability_mismatch"
	SchedulingReasonToolMismatch        = "tool_mismatch"
	SchedulingReasonTrustDomainMismatch = "trust_domain_mismatch"
	SchedulingReasonWorkloadMismatch    = "workload_mismatch"
	SchedulingReasonSourceStale         = "source_stale"
)

type AgentStatus struct {
	TenantID           string      `json:"tenant_id"`
	AgentID            string      `json:"agent_id"`
	ObservedGeneration int64       `json:"observed_generation"`
	Health             AgentHealth `json:"health"`
	CurrentLoad        int         `json:"current_load"`
	AvailableCapacity  int         `json:"available_capacity"`
	Draining           bool        `json:"draining"`
	LastSeenAt         time.Time   `json:"last_seen_at"`
	ExpiresAt          time.Time   `json:"expires_at"`
	StatusRevision     int64       `json:"status_revision"`
	ReportedBy         string      `json:"reported_by"`
}

type SchedulingRetryContext struct {
	Attempt              int          `json:"attempt"`
	PreviousAgentIDs     []string     `json:"previous_agent_ids,omitempty"`
	PreviousFailureClass FailureClass `json:"previous_failure_class,omitempty"`
}

type SchedulingRequest struct {
	RequestID                    string                 `json:"request_id"`
	TenantID                     string                 `json:"tenant_id"`
	TaskID                       string                 `json:"task_id"`
	DAGID                        string                 `json:"dag_id,omitempty"`
	NodeID                       string                 `json:"node_id,omitempty"`
	RequiredAgentCapabilities    []string               `json:"required_agent_capabilities,omitempty"`
	RequiredSecurityCapabilities []string               `json:"required_security_capabilities,omitempty"`
	RequiredTools                []string               `json:"required_tools,omitempty"`
	TrustDomain                  string                 `json:"trust_domain"`
	Deadline                     *time.Time             `json:"deadline,omitempty"`
	BudgetEnvelope               DelegationBudget       `json:"budget_envelope"`
	Affinity                     map[string]string      `json:"affinity,omitempty"`
	AntiAffinity                 map[string]string      `json:"anti_affinity,omitempty"`
	ExcludedAgentIDs             []string               `json:"excluded_agent_ids,omitempty"`
	RetryContext                 SchedulingRetryContext `json:"retry_context,omitempty"`
}

type SchedulingCandidate struct {
	AgentID        string             `json:"agent_id"`
	Eligible       bool               `json:"eligible"`
	ReasonCodes    []string           `json:"reason_codes,omitempty"`
	ScoreBreakdown map[string]float64 `json:"score_breakdown,omitempty"`
	Score          float64            `json:"score,omitempty"`
}

type SchedulingDecision struct {
	RequestID        string                `json:"request_id"`
	TenantID         string                `json:"tenant_id"`
	TaskID           string                `json:"task_id"`
	DAGID            string                `json:"dag_id,omitempty"`
	NodeID           string                `json:"node_id,omitempty"`
	SelectedAgentID  string                `json:"selected_agent_id,omitempty"`
	AlgorithmVersion string                `json:"algorithm_version"`
	Candidates       []SchedulingCandidate `json:"candidates"`
	CreatedAt        time.Time             `json:"created_at"`
}

type TaskAssignment struct {
	AssignmentID           string              `json:"assignment_id"`
	Kind                   AssignmentKind      `json:"assignment_kind"`
	TenantID               string              `json:"tenant_id"`
	TaskID                 string              `json:"task_id"`
	AgentID                string              `json:"agent_id"`
	State                  AssignmentState     `json:"state"`
	Revision               int64               `json:"revision"`
	RouteAttempt           int64               `json:"route_attempt"`
	Attempt                int64               `json:"attempt"`
	ExecutionFence         int64               `json:"execution_fence"`
	SupersedesAssignmentID string              `json:"supersedes_assignment_id,omitempty"`
	DispatchDisposition    DispatchDisposition `json:"dispatch_disposition,omitempty"`
	FailoverTransitionID   string              `json:"failover_transition_id,omitempty"`
	LeaseOwner             string              `json:"lease_owner,omitempty"`
	ClaimToken             string              `json:"claim_token,omitempty"`
	LeaseExpiresAt         *time.Time          `json:"lease_expires_at,omitempty"`
	NotBefore              *time.Time          `json:"not_before,omitempty"`
	Deadline               *time.Time          `json:"deadline,omitempty"`
	DeliveryID             string              `json:"delivery_id"`
	IdempotencyKey         string              `json:"idempotency_key"`
	RetryPolicy            *RetryPolicy        `json:"retry_policy,omitempty"`
	ReplayCount            int64               `json:"replay_count"`
	FailureClass           FailureClass        `json:"failure_class,omitempty"`
	ResultStatus           string              `json:"result_status,omitempty"`
	Outcome                json.RawMessage     `json:"outcome,omitempty"`
	ErrorCode              string              `json:"error_code,omitempty"`
	ConsumedBudget         DelegationBudget    `json:"consumed_budget,omitempty"`
	ExecutionReceipt       json.RawMessage     `json:"execution_receipt,omitempty"`
	CreatedAt              time.Time           `json:"created_at"`
	UpdatedAt              time.Time           `json:"updated_at"`
}

type ExecutionAttempt struct {
	AssignmentID     string           `json:"assignment_id"`
	TenantID         string           `json:"tenant_id"`
	TaskID           string           `json:"task_id"`
	AgentID          string           `json:"agent_id"`
	Attempt          int64            `json:"attempt"`
	ExecutionFence   int64            `json:"execution_fence"`
	State            AssignmentState  `json:"state"`
	LeaseOwner       string           `json:"lease_owner,omitempty"`
	ClaimToken       string           `json:"claim_token,omitempty"`
	LeaseExpiresAt   *time.Time       `json:"lease_expires_at,omitempty"`
	FailureClass     FailureClass     `json:"failure_class,omitempty"`
	ResultStatus     string           `json:"result_status,omitempty"`
	Outcome          json.RawMessage  `json:"outcome,omitempty"`
	ErrorCode        string           `json:"error_code,omitempty"`
	ConsumedBudget   DelegationBudget `json:"consumed_budget,omitempty"`
	ExecutionReceipt json.RawMessage  `json:"execution_receipt,omitempty"`
	StartedAt        *time.Time       `json:"started_at,omitempty"`
	FinishedAt       *time.Time       `json:"finished_at,omitempty"`
}

type RetryPolicy struct {
	MaxAttempts             int              `json:"max_attempts"`
	InitialBackoff          time.Duration    `json:"initial_backoff"`
	MaxBackoff              time.Duration    `json:"max_backoff"`
	BackoffMultiplier       float64          `json:"backoff_multiplier"`
	Jitter                  float64          `json:"jitter"`
	RetryableFailureClasses []FailureClass   `json:"retryable_failure_classes"`
	AttemptBudgetLimit      DelegationBudget `json:"attempt_budget_limit"`
	Deadline                *time.Time       `json:"deadline,omitempty"`
}

type TaskGraph struct {
	ProtocolVersion string           `json:"protocol_version"`
	DAGID           string           `json:"dag_id"`
	TenantID        string           `json:"tenant_id"`
	RootTaskID      string           `json:"root_task_id"`
	Status          GraphStatus      `json:"status"`
	MaxParallelism  int              `json:"max_parallelism"`
	Deadline        *time.Time       `json:"deadline,omitempty"`
	BudgetEnvelope  DelegationBudget `json:"budget_envelope"`
	ReservedBudget  DelegationBudget `json:"reserved_budget"`
	ConsumedBudget  DelegationBudget `json:"consumed_budget"`
	Revision        int64            `json:"revision"`
	IdempotencyKey  string           `json:"-"`
	RequestHash     string           `json:"-"`
	CancelReason    string           `json:"cancel_reason,omitempty"`
	CreatedAt       time.Time        `json:"created_at"`
	UpdatedAt       time.Time        `json:"updated_at"`
	TerminalAt      *time.Time       `json:"terminal_at,omitempty"`
	Nodes           []TaskNode       `json:"nodes"`
}

type TaskNode struct {
	NodeID            string            `json:"node_id"`
	DAGID             string            `json:"dag_id,omitempty"`
	TaskID            string            `json:"task_id"`
	TenantID          string            `json:"tenant_id,omitempty"`
	Dependencies      []string          `json:"dependencies,omitempty"`
	FailurePolicy     TaskFailurePolicy `json:"failure_policy"`
	RetryPolicy       RetryPolicy       `json:"retry_policy"`
	BudgetLimit       DelegationBudget  `json:"budget_limit"`
	SchedulingRequest SchedulingRequest `json:"scheduling_request"`
	Status            TaskNodeStatus    `json:"status"`
	Depth             int               `json:"depth"`
	TopoOrder         int               `json:"topo_order"`
	Revision          int64             `json:"revision"`
	CreatedAt         time.Time         `json:"created_at"`
	UpdatedAt         time.Time         `json:"updated_at"`
	TerminalAt        *time.Time        `json:"terminal_at,omitempty"`
}

// AgentCard describes an agent that can participate in governed interactions.
type AgentCard struct {
	ProtocolVersion string          `json:"protocol_version,omitempty" yaml:"protocol_version,omitempty"`
	AgentID         string          `json:"agent_id" yaml:"agent_id"`
	Name            string          `json:"name" yaml:"name"`
	Description     string          `json:"description" yaml:"description"`
	Entrypoint      AgentEntrypoint `json:"entrypoint" yaml:"entrypoint"`
	Capabilities    []string        `json:"capabilities" yaml:"capabilities"`
	TrustDomain     string          `json:"trust_domain" yaml:"trust_domain"`
	Version         string          `json:"version" yaml:"version"`
	TenantID        string          `json:"tenant_id,omitempty" yaml:"tenant_id,omitempty"`
}

// AgentEntrypoint describes how to reach an agent.
type AgentEntrypoint struct {
	Type string `json:"type" yaml:"type"`
	URL  string `json:"url" yaml:"url"`
}

// DelegationBudget represents a quantity in a task's budget currency. On Task,
// Budget is the assigned envelope, ReservedBudget is currently held by active
// children, and ConsumedBudget is actual settled consumption (never an estimate).
type DelegationBudget struct {
	TokenCount    int64   `json:"token_count"`
	PaymentAmount float64 `json:"payment_amount"`
	Currency      string  `json:"currency,omitempty"`
}

// Task represents an interaction context between two agents.
type Task struct {
	ProtocolVersion     string           `json:"protocol_version"`
	TaskID              string           `json:"task_id"`
	RequestID           string           `json:"request_id,omitempty"`
	SessionID           string           `json:"session_id"`
	InteractionID       string           `json:"interaction_id,omitempty"`
	DecisionID          string           `json:"decision_id,omitempty"`
	RootInteractionID   string           `json:"root_interaction_id,omitempty"`
	ParentInteractionID string           `json:"parent_interaction_id,omitempty"`
	RootTaskID          string           `json:"root_task_id,omitempty"`
	ParentTaskID        string           `json:"parent_task_id,omitempty"`
	DelegationDepth     int              `json:"delegation_depth,omitempty"`
	Deadline            *time.Time       `json:"deadline,omitempty"`
	Budget              DelegationBudget `json:"budget,omitempty"`
	ReservedBudget      DelegationBudget `json:"reserved_budget,omitempty"`
	ConsumedBudget      DelegationBudget `json:"consumed_budget,omitempty"`
	AllowedTools        []string         `json:"allowed_tools,omitempty"`
	AllowedCapabilities []string         `json:"allowed_capabilities,omitempty"`
	AllowRedelegation   bool             `json:"allow_redelegation"`
	InitiatorAgentID    string           `json:"initiator_agent_id"`
	TargetAgentID       string           `json:"target_agent_id"`
	Status              string           `json:"status"`
	CreatedAt           time.Time        `json:"created_at"`
	UpdatedAt           time.Time        `json:"updated_at"`
	CompletedAt         *time.Time       `json:"completed_at,omitempty"`
	Outcome             json.RawMessage  `json:"outcome,omitempty"`
	ErrorCode           string           `json:"error_code,omitempty"`
	DelegationToken     string           `json:"-"`
	// TenantID 仅透传：内核不做 tenant 校验（v0.52 明确边界），audit 原样携带。
	TenantID         string `json:"tenant_id,omitempty"`
	TargetWorkloadID string `json:"target_workload_id,omitempty"`
	TargetInstanceID string `json:"target_instance_id,omitempty"`
}

// IsTerminal reports whether the task has reached an immutable final state.
func (t Task) IsTerminal() bool {
	switch t.Status {
	case "completed", "failed", "cancelled":
		return true
	}
	return false
}

// TaskEvent is a persisted SSE event for a task.
type TaskEvent struct {
	ProtocolVersion string          `json:"protocol_version"`
	SchemaVersion   int             `json:"schema_version,omitempty"`
	Sequence        int64           `json:"sequence,omitempty"`
	Cursor          string          `json:"-"`
	EventID         string          `json:"event_id"`
	TaskID          string          `json:"task_id"`
	EventType       string          `json:"event_type"`
	Payload         json.RawMessage `json:"payload"`
	PublishedAt     time.Time       `json:"published_at"`
	Published       bool            `json:"published,omitempty"`
}

// Message is a unit of communication between agents.
type Message struct {
	MessageID       string    `json:"message_id"`
	TaskID          string    `json:"task_id"`
	FromAgentID     string    `json:"from_agent_id"`
	ToAgentID       string    `json:"to_agent_id"`
	Role            string    `json:"role"`
	Parts           []Part    `json:"parts"`
	Timestamp       time.Time `json:"timestamp"`
	ProtocolVersion string    `json:"protocol_version,omitempty"`
}

// Part is a fragment of a message.
type Part struct {
	Type string          `json:"type"`
	Text string          `json:"text,omitempty"`
	Data json.RawMessage `json:"data,omitempty"`
}

// PartValidationError identifies an invalid message Part wire representation.
type PartValidationError struct {
	Message string
}

func (e *PartValidationError) Error() string { return e.Message }

func partErrorf(format string, args ...any) error {
	return &PartValidationError{Message: fmt.Sprintf(format, args...)}
}

// UnmarshalJSON enforces the wire-level field contract for message parts.
func (p *Part) UnmarshalJSON(data []byte) error {
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(data, &fields); err != nil {
		return err
	}
	for name := range fields {
		if name != "type" && name != "text" && name != "data" {
			return partErrorf("unknown part field %q", name)
		}
	}
	if err := json.Unmarshal(fields["type"], &p.Type); err != nil {
		return partErrorf("part type: %v", err)
	}
	text, hasText := fields["text"]
	dataField, hasData := fields["data"]
	switch p.Type {
	case "text":
		if !hasText || hasData || string(text) == "null" {
			return partErrorf("text part requires non-null text and forbids data")
		}
		if err := json.Unmarshal(text, &p.Text); err != nil {
			return partErrorf("text part text: %v", err)
		}
		p.Data = nil
	case "data":
		if !hasData || hasText || string(dataField) == "null" {
			return partErrorf("data part requires non-null data and forbids text")
		}
		var value any
		if err := json.Unmarshal(dataField, &value); err != nil {
			return partErrorf("data part data: %v", err)
		}
		p.Text = ""
		p.Data = append(p.Data[:0], dataField...)
	default:
		return partErrorf("unknown part type %q", p.Type)
	}
	return nil
}

// EntrypointTaskRequest is received by the target agent entrypoint to start a
// delegated task.
type EntrypointTaskRequest struct {
	AssignmentID        string           `json:"-"`
	AssignmentAttempt   int64            `json:"-"`
	AssignmentFence     int64            `json:"-"`
	ProtocolVersion     string           `json:"protocol_version"`
	DeliveryID          string           `json:"delivery_id,omitempty"`
	RequestID           string           `json:"request_id"`
	InteractionID       string           `json:"interaction_id,omitempty"`
	DecisionID          string           `json:"decision_id,omitempty"`
	RootInteractionID   string           `json:"root_interaction_id,omitempty"`
	ParentInteractionID string           `json:"parent_interaction_id,omitempty"`
	RootTaskID          string           `json:"root_task_id,omitempty"`
	ParentTaskID        string           `json:"parent_task_id,omitempty"`
	DelegationDepth     int              `json:"delegation_depth,omitempty"`
	Deadline            *time.Time       `json:"deadline,omitempty"`
	Budget              DelegationBudget `json:"budget"`
	TaskID              string           `json:"task_id"`
	SessionID           string           `json:"session_id"`
	InitiatorAgentID    string           `json:"initiator_agent_id"`
	TargetAgentID       string           `json:"target_agent_id"`
	ToolName            string           `json:"tool_name"`
	Arguments           json.RawMessage  `json:"arguments"`
	DelegationToken     string           `json:"delegation_token"`
	AllowedTools        []string         `json:"allowed_tools"`
	AllowedCapabilities []string         `json:"allowed_capabilities"`
	AllowRedelegation   bool             `json:"allow_redelegation"`
	TenantID            string           `json:"tenant_id,omitempty"`
	TargetWorkloadID    string           `json:"target_workload_id,omitempty"`
	TargetInstanceID    string           `json:"target_instance_id,omitempty"`
}

// CancelTaskRequest requests cancellation using the negotiated protocol version.
type CancelTaskRequest struct {
	ProtocolVersion string `json:"protocol_version"`
	Reason          string `json:"reason,omitempty"`
}

// EntrypointResultRequest is received by the target agent entrypoint to report
// the final outcome of a delegated task.
type EntrypointResultRequest struct {
	ProtocolVersion  string           `json:"protocol_version"`
	Status           string           `json:"status"`
	Outcome          json.RawMessage  `json:"outcome,omitempty"`
	ErrorCode        string           `json:"error_code,omitempty"`
	ConsumedBudget   DelegationBudget `json:"consumed_budget"`
	ExecutionReceipt json.RawMessage  `json:"execution_receipt,omitempty"`
}

// DelegationRequest asks the A2A kernel to forward a structured tool request
// to another agent after interaction governance authorization.
type DelegationRequest struct {
	RequestID               string           `json:"request_id"`
	InitiatorAgentID        string           `json:"initiator_agent_id"`
	TargetAgentID           string           `json:"target_agent_id"`
	ToolName                string           `json:"tool_name"`
	Arguments               json.RawMessage  `json:"arguments"`
	SessionID               string           `json:"session_id"`
	TaskID                  string           `json:"task_id"`
	RootInteractionID       string           `json:"root_interaction_id,omitempty"`
	ParentInteractionID     string           `json:"parent_interaction_id,omitempty"`
	RootTaskID              string           `json:"root_task_id,omitempty"`
	ParentTaskID            string           `json:"parent_task_id,omitempty"`
	DelegationDepth         int              `json:"delegation_depth,omitempty"`
	Deadline                *time.Time       `json:"deadline,omitempty"`
	Budget                  DelegationBudget `json:"budget"`
	RiskLevel               string           `json:"risk_level"`
	ProtocolVersion         string           `json:"protocol_version"`
	AllowedTools            []string         `json:"allowed_tools"`
	AllowedCapabilities     []string         `json:"allowed_capabilities"`
	AllowRedelegation       bool             `json:"allow_redelegation"`
	ParentAllowRedelegation bool             `json:"-"`
	// TenantID scopes authoritative target lookup whenever it is present.
	TenantID         string `json:"tenant_id,omitempty"`
	TargetWorkloadID string `json:"target_workload_id,omitempty"`
	TargetInstanceID string `json:"target_instance_id,omitempty"`
}

// DelegationResponse is returned by the Go kernel.
type DelegationResponse struct {
	Allowed          bool            `json:"allowed"`
	Verdict          string          `json:"verdict"`
	ApprovalID       string          `json:"approval_id,omitempty"`
	InteractionID    string          `json:"interaction_id,omitempty"`
	DecisionID       string          `json:"decision_id,omitempty"`
	TaskID           string          `json:"task_id"`
	TargetEntrypoint AgentEntrypoint `json:"target_entrypoint,omitempty"`
	DelegationToken  string          `json:"delegation_token,omitempty"`
	OriginalArgs     json.RawMessage `json:"original_args,omitempty"`
	ModifiedArgs     json.RawMessage `json:"modified_args,omitempty"`
	EffectiveArgs    json.RawMessage `json:"effective_args,omitempty"`
	Reason           string          `json:"reason"`
	ProtocolVersion  string          `json:"protocol_version,omitempty"`
}

// DelegationApproval is the durable authorization fact for a delegation awaiting approval.
type DelegationApproval struct {
	ProtocolVersion     string           `json:"protocol_version"`
	ApprovalID          string           `json:"approval_id"`
	RequestID           string           `json:"request_id"`
	DecisionID          string           `json:"decision_id"`
	RequestHash         string           `json:"request_hash"`
	InitiatorAgentID    string           `json:"initiator_agent_id"`
	TargetAgentID       string           `json:"target_agent_id"`
	SessionID           string           `json:"session_id"`
	TenantID            string           `json:"tenant_id,omitempty"`
	TargetWorkloadID    string           `json:"target_workload_id,omitempty"`
	TargetInstanceID    string           `json:"target_instance_id,omitempty"`
	RootTaskID          string           `json:"root_task_id,omitempty"`
	ParentTaskID        string           `json:"parent_task_id,omitempty"`
	DelegationDepth     int              `json:"delegation_depth"`
	EffectiveArgs       json.RawMessage  `json:"-"`
	AllowedTools        []string         `json:"allowed_tools"`
	AllowedCapabilities []string         `json:"allowed_capabilities"`
	AllowRedelegation   bool             `json:"allow_redelegation"`
	Budget              DelegationBudget `json:"budget"`
	TaskDeadline        *time.Time       `json:"task_deadline,omitempty"`
	ExpiresAt           time.Time        `json:"expires_at"`
	Status              string           `json:"status"`
	ApproverID          string           `json:"approver_id,omitempty"`
	Reason              string           `json:"reason,omitempty"`
	CreatedAt           time.Time        `json:"created_at"`
	UpdatedAt           time.Time        `json:"updated_at"`
	DecidedAt           *time.Time       `json:"decided_at,omitempty"`
	TaskID              string           `json:"task_id,omitempty"`
	Version             int64            `json:"version"`
}

// ApprovalActionRequest carries an idempotency key and optional decision reason.
type ApprovalActionRequest struct {
	RequestID string `json:"request_id"`
	Reason    string `json:"reason,omitempty"`
}

// SendMessageResponse indicates whether a message was accepted for routing.
type SendMessageResponse struct {
	Accepted        bool   `json:"accepted"`
	Reason          string `json:"reason"`
	ProtocolVersion string `json:"protocol_version,omitempty"`
}

// AgentList is the response for listing registered agents.
type AgentList struct {
	ProtocolVersion string      `json:"protocol_version"`
	Agents          []AgentCard `json:"agents"`
}

type AgentRegistrationResponse struct {
	ProtocolVersion string `json:"protocol_version"`
	AgentID         string `json:"agent_id"`
}

// ErrorResponse is the standard error envelope.
type ErrorResponse struct {
	ProtocolVersion string `json:"protocol_version"`
	Error           string `json:"error"`
	Code            string `json:"code"`
}
