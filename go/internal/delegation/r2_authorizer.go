// Package delegation decides whether an agent may delegate a tool call to another agent.
package delegation

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"regexp"
	"strings"
	"time"

	"github.com/loop-controller/go/internal/models"
)

const interactionProtocolVersion = models.CurrentProtocolVersion
const maxAuthorizationResponseBytes = 1 << 20

var strictProtocolVersion = regexp.MustCompile(`^[0-9]+\.[0-9]+\.[0-9]+$`)

// R2Authorizer is retained as a compatibility name for the interaction authorizer.
type R2Authorizer interface {
	Authorize(ctx context.Context, req models.DelegationRequest) (models.DelegationResponse, error)
}

// HTTPR2Authorizer calls the Python IIGE authorization endpoint. The legacy
// type name is retained for source compatibility.
type HTTPR2Authorizer struct {
	BaseURL string
	// BearerToken is the default IIGE credential (legacy single-agent mode).
	BearerToken string
	// Tokens maps initiator_agent_id to that agent's IIGE Bearer token, so
	// each agent's delegation is authorized under its own Python identity
	// (the IIGE authorize endpoint asserts source_agent_id == token identity).
	Tokens map[string]string
	Client *http.Client
}

// tokenFor returns the IIGE credential for the given initiator, falling back
// to the legacy default BearerToken.
func (a *HTTPR2Authorizer) tokenFor(initiatorAgentID string) string {
	if a.Tokens != nil {
		if token, ok := a.Tokens[initiatorAgentID]; ok && token != "" {
			return token
		}
	}
	return a.BearerToken
}

type interactionAuthorizationRequest struct {
	ProtocolVersion         string                  `json:"protocol_version"`
	InteractionID           string                  `json:"interaction_id"`
	RequestID               string                  `json:"request_id"`
	SourceAgentID           string                  `json:"source_agent_id"`
	TargetAgentID           string                  `json:"target_agent_id"`
	ToolName                string                  `json:"tool_name"`
	Arguments               json.RawMessage         `json:"arguments"`
	SessionID               string                  `json:"session_id"`
	TaskID                  string                  `json:"task_id"`
	RiskLevel               string                  `json:"risk_level"`
	AllowedTools            []string                `json:"allowed_tools"`
	AllowedCapabilities     []string                `json:"allowed_capabilities"`
	AllowRedelegation       bool                    `json:"allow_redelegation"`
	RootInteractionID       string                  `json:"root_interaction_id,omitempty"`
	ParentInteractionID     string                  `json:"parent_interaction_id,omitempty"`
	RootTaskID              string                  `json:"root_task_id,omitempty"`
	ParentTaskID            string                  `json:"parent_task_id,omitempty"`
	DelegationDepth         int                     `json:"delegation_depth"`
	Deadline                *time.Time              `json:"deadline,omitempty"`
	Budget                  models.DelegationBudget `json:"budget"`
	ParentAllowRedelegation bool                    `json:"parent_allow_redelegation"`
}

func checkAuthorizationProtocolVersion(version string) error {
	if !strictProtocolVersion.MatchString(version) {
		return fmt.Errorf("invalid protocol version %q", version)
	}
	parts := strings.Split(version, ".")
	current := strings.Split(interactionProtocolVersion, ".")
	if parts[0] != current[0] || parts[1] != current[1] {
		return fmt.Errorf(
			"incompatible protocol version %q, expected %s",
			version,
			interactionProtocolVersion,
		)
	}
	return nil
}

// Authorize sends the delegation request to IIGE. It only falls back to the
// legacy R2 path when the new endpoint returns 404. Every other failure denies.
func (a *HTTPR2Authorizer) Authorize(ctx context.Context, req models.DelegationRequest) (models.DelegationResponse, error) {
	if ctx == nil {
		ctx = context.Background()
	}
	if a.Client == nil {
		a.Client = &http.Client{Timeout: 10 * time.Second}
	}

	arguments := req.Arguments
	if len(arguments) == 0 {
		arguments = json.RawMessage(`{}`)
	}
	payload, err := json.Marshal(interactionAuthorizationRequest{
		ProtocolVersion:         req.ProtocolVersion,
		InteractionID:           req.RequestID,
		RequestID:               req.RequestID,
		SourceAgentID:           req.InitiatorAgentID,
		TargetAgentID:           req.TargetAgentID,
		ToolName:                req.ToolName,
		Arguments:               arguments,
		SessionID:               req.SessionID,
		TaskID:                  req.TaskID,
		RiskLevel:               req.RiskLevel,
		AllowedTools:            req.AllowedTools,
		AllowedCapabilities:     req.AllowedCapabilities,
		AllowRedelegation:       req.AllowRedelegation,
		RootInteractionID:       req.RootInteractionID,
		ParentInteractionID:     req.ParentInteractionID,
		RootTaskID:              req.RootTaskID,
		ParentTaskID:            req.ParentTaskID,
		DelegationDepth:         req.DelegationDepth,
		Deadline:                req.Deadline,
		Budget:                  req.Budget,
		ParentAllowRedelegation: req.ParentAllowRedelegation,
	})
	if err != nil {
		return denied("failed to marshal delegation request"), fmt.Errorf("marshal delegation request: %w", err)
	}

	response, status, err := a.authorizeAt(ctx, "/interaction/v1/delegations/authorize", payload, a.tokenFor(req.InitiatorAgentID))
	if err == nil || status != http.StatusNotFound {
		return response, err
	}
	return a.authorizeAtLegacy(ctx, payload)
}

func (a *HTTPR2Authorizer) authorizeAtLegacy(ctx context.Context, payload []byte) (models.DelegationResponse, error) {
	response, _, err := a.authorizeAt(ctx, "/r2/v1/delegations/authorize", payload, a.BearerToken)
	return response, err
}

func (a *HTTPR2Authorizer) authorizeAt(ctx context.Context, path string, payload []byte, bearerToken string) (models.DelegationResponse, int, error) {
	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, a.BaseURL+path, bytes.NewReader(payload))
	if err != nil {
		return denied("failed to build interaction authorization request"), 0, fmt.Errorf("build authorization request: %w", err)
	}
	httpReq.Header.Set("Content-Type", "application/json")
	if bearerToken != "" {
		httpReq.Header.Set("Authorization", "Bearer "+bearerToken)
	}

	httpResp, err := a.Client.Do(httpReq)
	if err != nil {
		return denied("interaction authorizer unreachable"), 0, fmt.Errorf("interaction authorizer unreachable: %w", err)
	}
	defer httpResp.Body.Close()

	body, err := io.ReadAll(io.LimitReader(httpResp.Body, maxAuthorizationResponseBytes+1))
	if err != nil {
		return denied("failed to read interaction authorization response"), httpResp.StatusCode, fmt.Errorf("read authorization response: %w", err)
	}
	if len(body) > maxAuthorizationResponseBytes {
		return denied("interaction authorization response too large"), httpResp.StatusCode, fmt.Errorf("authorization response exceeds %d bytes", maxAuthorizationResponseBytes)
	}
	if httpResp.StatusCode < 200 || httpResp.StatusCode >= 300 {
		return denied(fmt.Sprintf("interaction authorizer returned status %d", httpResp.StatusCode)), httpResp.StatusCode, fmt.Errorf("interaction authorizer returned status %d", httpResp.StatusCode)
	}

	var response models.DelegationResponse
	decoder := json.NewDecoder(bytes.NewReader(body))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&response); err != nil {
		return denied("invalid interaction authorization response"), httpResp.StatusCode, fmt.Errorf("decode authorization response: %w", err)
	}
	if err := checkAuthorizationProtocolVersion(response.ProtocolVersion); err != nil {
		return denied("incompatible interaction authorization protocol"), httpResp.StatusCode, err
	}
	switch response.Verdict {
	case "allow":
		if !response.Allowed {
			return denied("inconsistent interaction authorization response"), httpResp.StatusCode, fmt.Errorf("allow verdict must set allowed=true")
		}
	case "modify":
		if !response.Allowed || !isJSONObject(response.EffectiveArgs) {
			return denied("invalid modify interaction authorization response"), httpResp.StatusCode, fmt.Errorf("modify verdict requires allowed=true and effective_args object")
		}
	case "deny", "require_approval":
		if response.Allowed {
			return denied("inconsistent interaction authorization response"), httpResp.StatusCode, fmt.Errorf("%s verdict must set allowed=false", response.Verdict)
		}
	default:
		return denied("invalid interaction authorization verdict"), httpResp.StatusCode, fmt.Errorf("invalid interaction authorization verdict %q", response.Verdict)
	}
	if !response.Allowed {
		if response.Reason == "" {
			response.Reason = "IIGE denied delegation"
		}
		if response.Verdict == "require_approval" {
			return response, httpResp.StatusCode, nil
		}
		return response, httpResp.StatusCode, fmt.Errorf("IIGE denied delegation: %s", response.Reason)
	}
	if len(response.EffectiveArgs) > 0 && !isJSONObject(response.EffectiveArgs) {
		return denied("invalid interaction authorization effective_args"), httpResp.StatusCode, fmt.Errorf("effective_args must be a JSON object")
	}
	return response, httpResp.StatusCode, nil
}

func isJSONObject(value json.RawMessage) bool {
	if len(value) == 0 {
		return false
	}
	var object map[string]json.RawMessage
	return json.Unmarshal(value, &object) == nil && object != nil
}

func denied(reason string) models.DelegationResponse {
	return models.DelegationResponse{
		Allowed:         false,
		Reason:          reason,
		ProtocolVersion: interactionProtocolVersion,
	}
}

// RecordLifecycle appends a committed Task transition to the Python audit timeline.
func (a *HTTPR2Authorizer) RecordLifecycle(ctx context.Context, task models.Task, event, eventID string) error {
	payload, err := json.Marshal(map[string]any{
		"event_id":              eventID,
		"interaction_id":        task.InteractionID,
		"root_interaction_id":   task.RootInteractionID,
		"parent_interaction_id": task.ParentInteractionID,
		"decision_id":           task.DecisionID,
		"task_id":               task.TaskID,
		"session_id":            task.SessionID,
		"source_agent_id":       task.InitiatorAgentID,
		"target_agent_id":       task.TargetAgentID,
		"event":                 event,
		"root_task_id":          task.RootTaskID,
		"parent_task_id":        task.ParentTaskID,
		"delegation_depth":      task.DelegationDepth,
		"allowed_tools":         task.AllowedTools,
		"allowed_capabilities":  task.AllowedCapabilities,
		"allow_redelegation":    task.AllowRedelegation,
		"budget":                task.Budget,
		"reserved_budget":       task.ReservedBudget,
		"consumed_budget":       task.ConsumedBudget,
		"deadline":              task.Deadline,
	})
	if err != nil {
		return fmt.Errorf("marshal interaction lifecycle: %w", err)
	}
	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, a.BaseURL+"/interaction/v1/delegations/lifecycle", bytes.NewReader(payload))
	if err != nil {
		return fmt.Errorf("build interaction lifecycle request: %w", err)
	}
	httpReq.Header.Set("Content-Type", "application/json")
	if a.BearerToken != "" {
		httpReq.Header.Set("Authorization", "Bearer "+a.BearerToken)
	}
	client := a.Client
	if client == nil {
		client = &http.Client{Timeout: 10 * time.Second}
	}
	httpResp, err := client.Do(httpReq)
	if err != nil {
		return fmt.Errorf("interaction lifecycle endpoint unreachable: %w", err)
	}
	defer httpResp.Body.Close()
	if httpResp.StatusCode < 200 || httpResp.StatusCode >= 300 {
		return fmt.Errorf("interaction lifecycle endpoint returned status %d", httpResp.StatusCode)
	}
	return nil
}

// StaticR2Authorizer is a test/development authorizer that returns a fixed decision.
type StaticR2Authorizer struct {
	Decision models.DelegationResponse
	Err      error
}

// Authorize returns the configured decision.
func (s *StaticR2Authorizer) Authorize(ctx context.Context, req models.DelegationRequest) (models.DelegationResponse, error) {
	return s.Decision, s.Err
}
