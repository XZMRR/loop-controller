// Package delegation decides whether an agent may delegate a tool call to another agent.
package delegation

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/loop-controller/go/internal/models"
	"github.com/loop-controller/go/internal/token"
)

// AgentQuerier is the subset of registry.Registry used by the delegator.
type AgentQuerier interface {
	Get(agentID string) (models.AgentCard, error)
}

// TaskStore is the subset of task.Manager used by the delegator.
type TaskStore interface {
	CreateInteraction(sessionID, initiatorAgentID, targetAgentID, interactionID, decisionID, rootInteractionID, parentInteractionID string) (models.Task, error)
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
	return &Delegator{
		registry:  registry,
		tasks:     tasks,
		issuer:    issuer,
		publisher: publisher,
		tokenTTL:  tokenTTL,
	}
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
		return interactionResp, nil
	}

	taskID := req.TaskID
	var task models.Task
	interactionID := interactionResp.InteractionID
	if interactionID == "" {
		interactionID = req.RequestID
	}
	if taskID == "" {
		var createErr error
		task, createErr = d.tasks.CreateInteraction(req.SessionID, req.InitiatorAgentID, req.TargetAgentID, interactionID, interactionResp.DecisionID, req.RootInteractionID, req.ParentInteractionID)
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
	}

	if d.issuer == nil {
		return models.DelegationResponse{
			Allowed: false,
			TaskID:  taskID,
			Reason:  "delegation token issuer unavailable",
		}, errors.New("delegation token issuer unavailable")
	}
	claims := token.DelegationClaims{
		RequestID:        req.RequestID,
		InitiatorAgentID: req.InitiatorAgentID,
		TargetAgentID:    req.TargetAgentID,
		ToolName:         req.ToolName,
		TaskID:           taskID,
		ArgumentsSHA256:  token.HashArguments(req.Arguments),
	}
	tokenStr, err := d.issuer.Issue(claims, d.tokenTTL)
	if err != nil || tokenStr == "" {
		_, _ = d.tasks.Complete(taskID, "failed", nil, "delegation_token_issuance_failed")
		return models.DelegationResponse{
			Allowed: false,
			TaskID:  taskID,
			Reason:  "delegation token issuance failed",
		}, fmt.Errorf("delegation token issuance failed: %w", err)
	}
	if err := d.tasks.SetDelegationToken(taskID, tokenStr); err != nil {
		_, _ = d.tasks.Complete(taskID, "failed", nil, "delegation_token_persistence_failed")
		return models.DelegationResponse{
			Allowed: false,
			TaskID:  taskID,
			Reason:  "delegation token persistence failed",
		}, err
	}

	if d.dispatcher != nil {
		dispatchReq := models.EntrypointTaskRequest{
			ProtocolVersion:     interactionProtocolVersion,
			InteractionID:       interactionID,
			DecisionID:          interactionResp.DecisionID,
			RootInteractionID:   task.RootInteractionID,
			ParentInteractionID: task.ParentInteractionID,
			TaskID:              taskID,
			SessionID:           req.SessionID,
			InitiatorAgentID:    req.InitiatorAgentID,
			TargetAgentID:       req.TargetAgentID,
			ToolName:            req.ToolName,
			Arguments:           req.Arguments,
			DelegationToken:     tokenStr,
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
		InteractionID:    interactionID,
		DecisionID:       interactionResp.DecisionID,
		TaskID:           taskID,
		TargetEntrypoint: target.Entrypoint,
		DelegationToken:  tokenStr,
		Reason:           "target agent trusted and capable",
		ProtocolVersion:  interactionProtocolVersion,
	}, nil
}

func hasCapability(caps []string, target string) bool {
	for _, c := range caps {
		if c == target {
			return true
		}
	}
	return false
}
