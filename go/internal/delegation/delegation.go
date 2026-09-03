// Package delegation decides whether an agent may delegate a tool call to another agent.
package delegation

import (
	"context"
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
	Create(sessionID, initiatorAgentID, targetAgentID string) models.Task
	Get(taskID string) (models.Task, error)
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
	if taskID == "" {
		task = d.tasks.Create(req.SessionID, req.InitiatorAgentID, req.TargetAgentID)
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
	}
	tokenStr, err := d.issuer.Issue(claims, d.tokenTTL)
	if err != nil || tokenStr == "" {
		return models.DelegationResponse{
			Allowed: false,
			TaskID:  taskID,
			Reason:  "delegation token issuance failed",
		}, fmt.Errorf("delegation token issuance failed: %w", err)
	}

	if d.publisher != nil {
		_ = d.publisher.Publish(ctx, task)
	}

	return models.DelegationResponse{
		Allowed:          true,
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
