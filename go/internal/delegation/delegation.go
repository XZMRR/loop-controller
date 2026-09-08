// Package delegation decides whether an agent may delegate a tool call to another agent.
package delegation

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/loop-controller/go/internal/models"
	"github.com/loop-controller/go/internal/store"
	"github.com/loop-controller/go/internal/token"
)

// AgentQuerier is the subset of registry.Registry used by the delegator.
type AgentQuerier interface {
	Get(agentID string) (models.AgentCard, error)
}

// TaskStore is the subset of task.Manager used by the delegator.
type TaskStore interface {
	CreateInteraction(sessionID, initiatorAgentID, targetAgentID, interactionID, decisionID, rootInteractionID, parentInteractionID string) (models.Task, error)
	CreateInteractionTask(models.Task) (models.Task, error)
	Get(taskID string) (models.Task, error)
	RecordLifecycle(context.Context, models.Task, string) error
	UpdateStatus(taskID, status string) (models.Task, error)
	UpdateStatusFrom(taskID, expectedStatus, status string) (models.Task, error)
	Complete(taskID, status string, outcome json.RawMessage, errorCode string) (models.Task, error)
	SetDelegationToken(taskID, delegationToken string) error
}

// TokenIssuer issues delegation tokens.
type TokenIssuer interface {
	Issue(claims token.DelegationClaims, ttl time.Duration) (string, error)
}

type tokenValidator interface {
	Validate(string) (token.DelegationClaims, error)
}

// TaskEventPublisher publishes task updates.
type TaskEventPublisher interface {
	Publish(ctx context.Context, task models.Task) error
}

// Delegator performs delegation decisions.
type Delegator struct {
	registry   AgentQuerier
	tasks      TaskStore
	issuer     TokenIssuer
	authorizer R2Authorizer
	publisher  TaskEventPublisher
	dispatcher EntrypointClient
	approvals  store.DelegationApprovalStore
	instanceID string
	tokenTTL   time.Duration
}

// New creates a Delegator backed by the given dependencies.
func New(
	registry AgentQuerier,
	tasks TaskStore,
	issuer TokenIssuer,
	publisher TaskEventPublisher,
	tokenTTL time.Duration,
) *Delegator {
	if tokenTTL <= 0 {
		tokenTTL = 5 * time.Minute
	}
	d := &Delegator{
		registry:  registry,
		tasks:     tasks,
		issuer:    issuer,
		publisher: publisher,
		tokenTTL:  tokenTTL,
	}
	if provider, ok := tasks.(interface {
		DelegationApprovalStore() store.DelegationApprovalStore
		InstanceID() string
	}); ok {
		d.approvals = provider.DelegationApprovalStore()
		d.instanceID = provider.InstanceID()
	}
	return d
}

// WithR2Authorizer attaches the interaction authorizer after the local
// capability check. The legacy method name is retained for compatibility.
func (d *Delegator) WithR2Authorizer(a R2Authorizer) *Delegator {
	d.authorizer = a
	return d
}

// WithEntrypointClient enables delivery to the authorized target Agent.
func (d *Delegator) WithEntrypointClient(client EntrypointClient) *Delegator {
	d.dispatcher = client
	return d
}

func (d *Delegator) WithApprovalStore(approvals store.DelegationApprovalStore, instanceID string) *Delegator {
	d.approvals = approvals
	d.instanceID = instanceID
	return d
}

// Cancel asks the target Agent to cancel a delegated task.
func (d *Delegator) Cancel(ctx context.Context, targetAgentID, taskID, delegationToken string) (bool, error) {
	if d.dispatcher == nil {
		return false, errors.New("entrypoint client is not configured")
	}
	target, err := d.registry.Get(targetAgentID)
	if err != nil {
		return false, fmt.Errorf("target agent not registered: %w", err)
	}
	return d.dispatcher.Cancel(ctx, target.Entrypoint, taskID, delegationToken)
}

// Request evaluates a delegation request.
func (d *Delegator) Request(ctx context.Context, req models.DelegationRequest) (models.DelegationResponse, error) {
	if req.RequestID == "" || req.InitiatorAgentID == "" || req.TargetAgentID == "" || req.ToolName == "" {
		return models.DelegationResponse{
			Allowed: false,
			Reason:  "request_id, initiator_agent_id, target_agent_id and tool_name are required",
		}, errors.New("missing required fields")
	}

	now := time.Now().UTC()
	if req.Deadline != nil && !req.Deadline.After(now) {
		return models.DelegationResponse{Allowed: false, Reason: "delegation deadline has expired"}, nil
	}
	lineage, trustedReq, lineageErr := d.deriveLineage(req, req.RequestID)
	if lineageErr != nil {
		return models.DelegationResponse{Allowed: false, Reason: lineageErr.Error()}, nil
	}
	req = trustedReq
	if lineage.Deadline != nil && !lineage.Deadline.After(now) {
		return models.DelegationResponse{Allowed: false, Reason: "delegation deadline has expired"}, nil
	}

	target, err := d.registry.Get(req.TargetAgentID)
	if err != nil {
		return models.DelegationResponse{
			Allowed: false,
			Reason:  "target agent not registered",
		}, nil
	}

	if !hasCapability(target.Capabilities, "delegate_execution") {
		return models.DelegationResponse{
			Allowed: false,
			Reason:  "target agent does not support delegate_execution",
		}, nil
	}

	if d.authorizer == nil {
		return models.DelegationResponse{
			Allowed: false,
			Reason:  "interaction authorizer is not configured",
		}, errors.New("interaction authorizer is not configured")
	}
	interactionResp, err := d.authorizer.Authorize(ctx, req)
	if err != nil {
		if !interactionResp.Allowed && interactionResp.Reason == "" {
			interactionResp.Reason = "interaction authorization failed"
		}
		return interactionResp, err
	}
	if !interactionResp.Allowed {
		if interactionResp.Reason == "" {
			interactionResp.Reason = "IIGE denied delegation"
		}
		if interactionResp.Verdict == "require_approval" {
			return d.requireApproval(ctx, req, lineage, target, interactionResp)
		}
		return interactionResp, nil
	}

	effectiveArgs := req.Arguments
	if len(interactionResp.EffectiveArgs) > 0 {
		effectiveArgs = interactionResp.EffectiveArgs
	}
	verdict := interactionResp.Verdict
	if verdict == "" {
		verdict = "allow"
	}
	requestedTools := req.AllowedTools
	if requestedTools == nil {
		requestedTools = []string{req.ToolName}
	}
	allowedTools := intersectScope(requestedTools, []string{req.ToolName})
	allowedCapabilities := intersectScope(req.AllowedCapabilities, target.Capabilities)
	allowRedelegation := req.AllowRedelegation && hasCapability(allowedCapabilities, "delegate_execution")
	if req.ParentTaskID != "" {
		allowedTools = intersectScope(allowedTools, lineage.AllowedTools)
		allowedCapabilities = intersectScope(allowedCapabilities, lineage.AllowedCapabilities)
		allowRedelegation = allowRedelegation && lineage.AllowRedelegation
	}
	if len(allowedTools) == 0 {
		return models.DelegationResponse{Allowed: false, Reason: "delegation scope does not allow requested tool"}, nil
	}

	taskID := req.TaskID
	var task models.Task
	interactionID := interactionResp.InteractionID
	if interactionID == "" {
		interactionID = req.RequestID
	}
	if taskID == "" {
		if req.ParentTaskID == "" {
			lineage.RootInteractionID = interactionID
		}
		lineage.SessionID = req.SessionID
		lineage.InteractionID = interactionID
		lineage.DecisionID = interactionResp.DecisionID
		lineage.InitiatorAgentID = req.InitiatorAgentID
		lineage.TargetAgentID = req.TargetAgentID
		lineage.Budget = req.Budget
		lineage.AllowedTools = allowedTools
		lineage.AllowedCapabilities = allowedCapabilities
		lineage.AllowRedelegation = allowRedelegation
		var createErr error
		task, createErr = d.tasks.CreateInteractionTask(lineage)
		if createErr != nil {
			return models.DelegationResponse{
				Allowed:    false,
				DecisionID: interactionResp.DecisionID,
				Reason:     "delegation task persistence failed",
			}, createErr
		}
		taskID = task.TaskID
	} else {
		var err error
		task, err = d.tasks.Get(taskID)
		if err != nil {
			return models.DelegationResponse{
				Allowed: false,
				Reason:  "delegation task not found",
			}, nil
		}
		expectedRootInteractionID := lineage.RootInteractionID
		if req.ParentTaskID == "" {
			expectedRootInteractionID = interactionID
		}
		if task.Status != "pending" || task.SessionID != req.SessionID || task.InitiatorAgentID != req.InitiatorAgentID || task.TargetAgentID != req.TargetAgentID ||
			task.InteractionID != interactionID || task.DecisionID != interactionResp.DecisionID ||
			task.RootTaskID != lineage.RootTaskID || task.ParentTaskID != lineage.ParentTaskID || task.DelegationDepth != lineage.DelegationDepth ||
			task.RootInteractionID != expectedRootInteractionID || task.ParentInteractionID != lineage.ParentInteractionID ||
			!sameScope(task.AllowedTools, allowedTools) || !sameScope(task.AllowedCapabilities, allowedCapabilities) || task.AllowRedelegation != allowRedelegation ||
			task.Budget != req.Budget || !sameDeadline(task.Deadline, lineage.Deadline) {
			return models.DelegationResponse{Allowed: false, Reason: "existing task does not match delegation request"}, nil
		}
	}

	if d.issuer == nil {
		return models.DelegationResponse{
			Allowed: false,
			TaskID:  taskID,
			Reason:  "delegation token issuer unavailable",
		}, errors.New("delegation token issuer unavailable")
	}
	claims := token.DelegationClaims{
		RequestID:           req.RequestID,
		SessionID:           task.SessionID,
		InteractionID:       task.InteractionID,
		DecisionID:          task.DecisionID,
		RootInteractionID:   task.RootInteractionID,
		ParentInteractionID: task.ParentInteractionID,
		InitiatorAgentID:    req.InitiatorAgentID,
		TargetAgentID:       req.TargetAgentID,
		ToolName:            req.ToolName,
		TaskID:              taskID,
		ArgumentsSHA256:     token.HashArguments(effectiveArgs),
		AllowedTools:        allowedTools,
		AllowedCapabilities: allowedCapabilities,
		AllowRedelegation:   allowRedelegation,
		RootTaskID:          task.RootTaskID,
		ParentTaskID:        task.ParentTaskID,
		DelegationDepth:     task.DelegationDepth,
		BudgetTokenCount:    task.Budget.TokenCount,
		BudgetPaymentAmount: task.Budget.PaymentAmount,
		BudgetCurrency:      task.Budget.Currency,
	}
	if task.Deadline != nil {
		claims.Deadline = task.Deadline.Unix()
	}
	tokenStr, err := d.issuer.Issue(claims, d.tokenTTL)
	if err != nil || tokenStr == "" {
		_, _ = d.tasks.UpdateStatusFrom(taskID, task.Status, "failed")
		return models.DelegationResponse{
			Allowed: false,
			TaskID:  taskID,
			Reason:  "delegation token issuance failed",
		}, fmt.Errorf("delegation token issuance failed: %w", err)
	}
	if err := d.tasks.SetDelegationToken(taskID, tokenStr); err != nil {
		_, _ = d.tasks.UpdateStatusFrom(taskID, task.Status, "failed")
		return models.DelegationResponse{
			Allowed: false,
			TaskID:  taskID,
			Reason:  "delegation token persistence failed",
		}, err
	}

	if d.dispatcher != nil {
		dispatchReq := models.EntrypointTaskRequest{
			ProtocolVersion:     interactionProtocolVersion,
			RequestID:           req.RequestID,
			InteractionID:       interactionID,
			DecisionID:          interactionResp.DecisionID,
			RootInteractionID:   task.RootInteractionID,
			ParentInteractionID: task.ParentInteractionID,
			RootTaskID:          task.RootTaskID,
			ParentTaskID:        task.ParentTaskID,
			DelegationDepth:     task.DelegationDepth,
			Deadline:            task.Deadline,
			Budget:              task.Budget,
			TaskID:              taskID,
			SessionID:           req.SessionID,
			InitiatorAgentID:    req.InitiatorAgentID,
			TargetAgentID:       req.TargetAgentID,
			ToolName:            req.ToolName,
			Arguments:           effectiveArgs,
			DelegationToken:     tokenStr,
			AllowedTools:        allowedTools,
			AllowedCapabilities: allowedCapabilities,
			AllowRedelegation:   allowRedelegation,
		}
		if err := d.dispatcher.Dispatch(ctx, target.Entrypoint, dispatchReq); err != nil {
			var dispatchErr *DispatchError
			newStatus := "failed"
			if errors.As(err, &dispatchErr) && dispatchErr.MayBeSent {
				newStatus = "outcome_unknown"
			}
			if _, updateErr := d.tasks.UpdateStatusFrom(taskID, task.Status, newStatus); updateErr != nil {
				err = fmt.Errorf("%w (task status update: %v)", err, updateErr)
			}
			return models.DelegationResponse{
				Allowed:          false,
				DecisionID:       interactionResp.DecisionID,
				TaskID:           taskID,
				TargetEntrypoint: target.Entrypoint,
				Reason:           "target Agent dispatch failed",
			}, err
		}
		if err := d.tasks.RecordLifecycle(ctx, task, "dispatched"); err != nil {
			return models.DelegationResponse{
				Allowed: false, DecisionID: interactionResp.DecisionID, TaskID: taskID,
				TargetEntrypoint: target.Entrypoint, Reason: "dispatch audit persistence failed",
			}, err
		}
	}

	return models.DelegationResponse{
		Allowed:          true,
		Verdict:          verdict,
		InteractionID:    interactionID,
		DecisionID:       interactionResp.DecisionID,
		TaskID:           taskID,
		TargetEntrypoint: target.Entrypoint,
		DelegationToken:  tokenStr,
		OriginalArgs:     interactionResp.OriginalArgs,
		ModifiedArgs:     interactionResp.ModifiedArgs,
		EffectiveArgs:    effectiveArgs,
		Reason:           "target agent trusted and capable",
		ProtocolVersion:  interactionProtocolVersion,
	}, nil
}

func (d *Delegator) requireApproval(ctx context.Context, req models.DelegationRequest, lineage models.Task, target models.AgentCard, decision models.DelegationResponse) (models.DelegationResponse, error) {
	if d.approvals == nil || decision.DecisionID == "" {
		return models.DelegationResponse{Allowed: false, Verdict: "deny", Reason: "delegation approval persistence unavailable"}, errors.New("delegation approval persistence unavailable")
	}
	effectiveArgs := req.Arguments
	if len(decision.EffectiveArgs) > 0 {
		effectiveArgs = decision.EffectiveArgs
	}
	requestedTools := req.AllowedTools
	if requestedTools == nil {
		requestedTools = []string{req.ToolName}
	}
	allowedTools := intersectScope(requestedTools, []string{req.ToolName})
	allowedCapabilities := intersectScope(req.AllowedCapabilities, target.Capabilities)
	allowRedelegation := req.AllowRedelegation && hasCapability(allowedCapabilities, "delegate_execution")
	if req.ParentTaskID != "" {
		allowedTools = intersectScope(allowedTools, lineage.AllowedTools)
		allowedCapabilities = intersectScope(allowedCapabilities, lineage.AllowedCapabilities)
		allowRedelegation = allowRedelegation && lineage.AllowRedelegation
	}
	if len(allowedTools) == 0 {
		return models.DelegationResponse{Allowed: false, Verdict: "deny", Reason: "delegation scope does not allow requested tool"}, nil
	}
	snapshot := req
	snapshot.Arguments = effectiveArgs
	snapshot.AllowedTools = allowedTools
	snapshot.AllowedCapabilities = allowedCapabilities
	snapshot.AllowRedelegation = allowRedelegation
	encoded, _ := json.Marshal(snapshot)
	now := time.Now().UTC()
	approvalID := "approval-" + token.HashArguments([]byte(req.RequestID + "\x00" + decision.DecisionID))[:24]
	a, _, err := d.approvals.Create(ctx, models.DelegationApproval{
		ProtocolVersion: models.CurrentProtocolVersion, ApprovalID: approvalID, RequestID: req.RequestID,
		DecisionID: decision.DecisionID, RequestHash: token.HashArguments(encoded), InitiatorAgentID: req.InitiatorAgentID,
		TargetAgentID: req.TargetAgentID, SessionID: req.SessionID, RootTaskID: lineage.RootTaskID,
		ParentTaskID: lineage.ParentTaskID, DelegationDepth: lineage.DelegationDepth, EffectiveArgs: effectiveArgs,
		AllowedTools: allowedTools, AllowedCapabilities: allowedCapabilities, AllowRedelegation: allowRedelegation,
		Budget: req.Budget, TaskDeadline: lineage.Deadline, ExpiresAt: now.Add(15 * time.Minute), Status: "pending",
		CreatedAt: now, UpdatedAt: now, Version: 1,
	})
	if err != nil {
		return models.DelegationResponse{}, err
	}
	return models.DelegationResponse{Allowed: false, Verdict: "require_approval", ApprovalID: a.ApprovalID, InteractionID: decision.InteractionID, DecisionID: a.DecisionID, EffectiveArgs: effectiveArgs, Reason: decision.Reason, ProtocolVersion: models.CurrentProtocolVersion}, nil
}

func (d *Delegator) ResumeApproval(ctx context.Context, approvalID string) (models.DelegationApproval, error) {
	a, err := d.approvals.Get(ctx, approvalID)
	if err != nil || a.Status != "approved" {
		return a, err
	}
	req := models.DelegationRequest{RequestID: a.RequestID, InitiatorAgentID: a.InitiatorAgentID, TargetAgentID: a.TargetAgentID, ToolName: firstTool(a.AllowedTools), Arguments: a.EffectiveArgs, SessionID: a.SessionID, ParentTaskID: a.ParentTaskID, Deadline: a.TaskDeadline, Budget: a.Budget, ProtocolVersion: models.CurrentProtocolVersion, AllowedTools: a.AllowedTools, AllowedCapabilities: a.AllowedCapabilities, AllowRedelegation: a.AllowRedelegation}
	lineage, trusted, err := d.deriveLineage(req, req.RequestID)
	if err != nil {
		return a, err
	}
	target, err := d.registry.Get(a.TargetAgentID)
	if err != nil {
		return a, err
	}
	decision, err := d.authorizer.Authorize(ctx, trusted)
	if err != nil {
		return a, err
	}
	if !decision.Allowed {
		return a, fmt.Errorf("approval re-evaluation returned %s: %s", decision.Verdict, decision.Reason)
	}
	effectiveArgs := a.EffectiveArgs
	if len(decision.EffectiveArgs) > 0 {
		effectiveArgs = decision.EffectiveArgs
	}
	if !jsonSubset(effectiveArgs, a.EffectiveArgs) {
		return a, errors.New("approval re-evaluation attempted to widen arguments")
	}
	allowedTools := intersectScope(a.AllowedTools, []string{req.ToolName})
	allowedCaps := intersectScope(a.AllowedCapabilities, target.Capabilities)
	if len(allowedTools) == 0 {
		return a, errors.New("approval re-evaluation removed requested tool")
	}
	if req.ParentTaskID != "" {
		allowedTools = intersectScope(allowedTools, lineage.AllowedTools)
		allowedCaps = intersectScope(allowedCaps, lineage.AllowedCapabilities)
	}
	now := time.Now().UTC()
	taskID := fmt.Sprintf("task-%s-%d", d.instanceID, now.UnixNano())
	interactionID := decision.InteractionID
	if interactionID == "" {
		interactionID = req.RequestID
	}
	if req.ParentTaskID == "" {
		lineage.RootInteractionID = interactionID
	}
	allowRedelegation := a.AllowRedelegation
	if req.ParentTaskID != "" {
		allowRedelegation = allowRedelegation && lineage.AllowRedelegation
	}
	task := models.Task{ProtocolVersion: models.CurrentProtocolVersion, TaskID: taskID, SessionID: a.SessionID, InteractionID: interactionID, DecisionID: decision.DecisionID, RootInteractionID: lineage.RootInteractionID, ParentInteractionID: lineage.ParentInteractionID, RootTaskID: lineage.RootTaskID, ParentTaskID: lineage.ParentTaskID, DelegationDepth: lineage.DelegationDepth, Deadline: lineage.Deadline, Budget: a.Budget, AllowedTools: allowedTools, AllowedCapabilities: allowedCaps, AllowRedelegation: allowRedelegation, InitiatorAgentID: a.InitiatorAgentID, TargetAgentID: a.TargetAgentID, Status: "pending", CreatedAt: now, UpdatedAt: now}
	claims := token.DelegationClaims{RequestID: a.RequestID, SessionID: task.SessionID, InteractionID: task.InteractionID, DecisionID: task.DecisionID, RootInteractionID: task.RootInteractionID, ParentInteractionID: task.ParentInteractionID, InitiatorAgentID: a.InitiatorAgentID, TargetAgentID: a.TargetAgentID, ToolName: req.ToolName, TaskID: taskID, ArgumentsSHA256: token.HashArguments(effectiveArgs), AllowedTools: allowedTools, AllowedCapabilities: allowedCaps, AllowRedelegation: task.AllowRedelegation, RootTaskID: task.RootTaskID, ParentTaskID: task.ParentTaskID, DelegationDepth: task.DelegationDepth, BudgetTokenCount: task.Budget.TokenCount, BudgetPaymentAmount: task.Budget.PaymentAmount, BudgetCurrency: task.Budget.Currency}
	if task.Deadline != nil {
		claims.Deadline = task.Deadline.Unix()
	}
	tokenStr, err := d.issuer.Issue(claims, d.tokenTTL)
	if err != nil {
		return a, err
	}
	task.DelegationToken = tokenStr
	deliveryID := "delegation-dispatch:" + a.ApprovalID
	dispatch := models.EntrypointTaskRequest{ProtocolVersion: models.CurrentProtocolVersion, DeliveryID: deliveryID, RequestID: a.RequestID, InteractionID: task.InteractionID, DecisionID: task.DecisionID, RootInteractionID: task.RootInteractionID, ParentInteractionID: task.ParentInteractionID, RootTaskID: task.RootTaskID, ParentTaskID: task.ParentTaskID, DelegationDepth: task.DelegationDepth, Deadline: task.Deadline, Budget: task.Budget, TaskID: taskID, SessionID: a.SessionID, InitiatorAgentID: a.InitiatorAgentID, TargetAgentID: a.TargetAgentID, ToolName: req.ToolName, Arguments: effectiveArgs, DelegationToken: tokenStr, AllowedTools: allowedTools, AllowedCapabilities: allowedCaps, AllowRedelegation: task.AllowRedelegation}
	return d.approvals.Consume(ctx, a.ApprovalID, a.Version, task, target.Entrypoint, dispatch)
}

func firstTool(tools []string) string {
	if len(tools) == 0 {
		return ""
	}
	return tools[0]
}
func jsonSubset(candidate, approved json.RawMessage) bool {
	var c, a map[string]any
	if json.Unmarshal(candidate, &c) != nil || json.Unmarshal(approved, &a) != nil {
		return false
	}
	for k, v := range c {
		av, ok := a[k]
		if !ok {
			return false
		}
		vb, _ := json.Marshal(v)
		ab, _ := json.Marshal(av)
		if string(vb) != string(ab) {
			return false
		}
	}
	return true
}

const maxDelegationDepth = 8

func (d *Delegator) deriveLineage(req models.DelegationRequest, interactionID string) (models.Task, models.DelegationRequest, error) {
	if req.ParentTaskID == "" {
		if req.RootTaskID != "" || req.DelegationDepth != 0 || req.RootInteractionID != "" || req.ParentInteractionID != "" {
			return models.Task{}, req, errors.New("delegation lineage must be derived from parent_task_id")
		}
		req.RootInteractionID = interactionID
		return models.Task{RootInteractionID: interactionID, Deadline: req.Deadline}, req, nil
	}

	parent, err := d.tasks.Get(req.ParentTaskID)
	if err != nil {
		return models.Task{}, req, errors.New("parent task not found")
	}
	if parent.IsTerminal() || parent.Status == "outcome_unknown" {
		return models.Task{}, req, errors.New("parent task is not active")
	}
	if req.InitiatorAgentID != parent.TargetAgentID {
		return models.Task{}, req, errors.New("delegation source must match parent target")
	}
	if !parent.AllowRedelegation {
		return models.Task{}, req, errors.New("parent task does not allow redelegation")
	}
	validator, ok := d.issuer.(tokenValidator)
	if !ok || parent.DelegationToken == "" {
		return models.Task{}, req, errors.New("parent delegation token proof unavailable")
	}
	parentClaims, err := validator.Validate(parent.DelegationToken)
	if err != nil {
		return models.Task{}, req, errors.New("parent delegation token proof invalid")
	}
	if err := verifyParentToken(parentClaims, parent); err != nil {
		return models.Task{}, req, err
	}
	if req.TargetAgentID == req.InitiatorAgentID || req.TargetAgentID == parent.InitiatorAgentID {
		return models.Task{}, req, errors.New("delegation cycle detected")
	}
	for ancestor := parent; ancestor.ParentTaskID != ""; {
		ancestor, err = d.tasks.Get(ancestor.ParentTaskID)
		if err != nil {
			return models.Task{}, req, errors.New("invalid delegation ancestry")
		}
		if req.TargetAgentID == ancestor.InitiatorAgentID || req.TargetAgentID == ancestor.TargetAgentID {
			return models.Task{}, req, errors.New("delegation cycle detected")
		}
	}
	depth := parent.DelegationDepth + 1
	if depth > maxDelegationDepth {
		return models.Task{}, req, fmt.Errorf("delegation depth exceeds max %d", maxDelegationDepth)
	}
	rootTaskID := parent.RootTaskID
	if rootTaskID == "" {
		rootTaskID = parent.TaskID
	}
	rootInteractionID := parent.RootInteractionID
	if rootInteractionID == "" {
		rootInteractionID = parent.InteractionID
	}
	deadline := req.Deadline
	if parent.Deadline != nil && (deadline == nil || parent.Deadline.Before(*deadline)) {
		parentDeadline := *parent.Deadline
		deadline = &parentDeadline
	}
	req.RootTaskID = rootTaskID
	req.ParentTaskID = parent.TaskID
	req.DelegationDepth = depth
	req.RootInteractionID = rootInteractionID
	req.ParentInteractionID = parent.InteractionID
	req.Deadline = deadline
	req.ParentAllowRedelegation = parent.AllowRedelegation
	return models.Task{
		RootTaskID:          rootTaskID,
		ParentTaskID:        parent.TaskID,
		DelegationDepth:     depth,
		RootInteractionID:   rootInteractionID,
		ParentInteractionID: parent.InteractionID,
		Deadline:            deadline,
		AllowedTools:        parent.AllowedTools,
		AllowedCapabilities: parent.AllowedCapabilities,
		AllowRedelegation:   parent.AllowRedelegation,
	}, req, nil
}

func hasCapability(caps []string, target string) bool {
	for _, c := range caps {
		if c == target {
			return true
		}
	}
	return false
}

func verifyParentToken(claims token.DelegationClaims, parent models.Task) error {
	if claims.TaskID != parent.TaskID || claims.SessionID != parent.SessionID ||
		claims.InteractionID != parent.InteractionID || claims.DecisionID != parent.DecisionID ||
		claims.RootInteractionID != parent.RootInteractionID || claims.ParentInteractionID != parent.ParentInteractionID ||
		claims.InitiatorAgentID != parent.InitiatorAgentID || claims.TargetAgentID != parent.TargetAgentID ||
		claims.RootTaskID != parent.RootTaskID || claims.ParentTaskID != parent.ParentTaskID || claims.DelegationDepth != parent.DelegationDepth {
		return errors.New("parent delegation token does not match parent task")
	}
	if !sameScope(claims.AllowedTools, parent.AllowedTools) || !sameScope(claims.AllowedCapabilities, parent.AllowedCapabilities) ||
		claims.AllowRedelegation != parent.AllowRedelegation || !claims.AllowRedelegation {
		return errors.New("parent delegation token scope does not allow redelegation")
	}
	if claims.BudgetTokenCount != parent.Budget.TokenCount || claims.BudgetPaymentAmount != parent.Budget.PaymentAmount || claims.BudgetCurrency != parent.Budget.Currency {
		return errors.New("parent delegation token budget mismatch")
	}
	if (claims.Deadline == 0) != (parent.Deadline == nil) || claims.Deadline != deadlineUnix(parent.Deadline) {
		return errors.New("parent delegation token deadline mismatch")
	}
	return nil
}

func sameDeadline(left, right *time.Time) bool {
	if left == nil || right == nil {
		return left == nil && right == nil
	}
	return left.Equal(*right)
}

func deadlineUnix(deadline *time.Time) int64 {
	if deadline == nil {
		return 0
	}
	return deadline.Unix()
}

func sameScope(left, right []string) bool {
	if len(left) != len(right) {
		return false
	}
	for _, value := range left {
		if !hasCapability(right, value) {
			return false
		}
	}
	return true
}

func intersectScope(requested, permitted []string) []string {
	permittedSet := make(map[string]struct{}, len(permitted))
	for _, value := range permitted {
		permittedSet[value] = struct{}{}
	}
	result := make([]string, 0, len(requested))
	seen := make(map[string]struct{}, len(requested))
	for _, value := range requested {
		if _, ok := permittedSet[value]; !ok {
			continue
		}
		if _, duplicate := seen[value]; duplicate {
			continue
		}
		seen[value] = struct{}{}
		result = append(result, value)
	}
	return result
}
