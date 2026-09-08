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
	BaseURL     string
	BearerToken string
	Client      *http.Client
}

type interactionAuthorizationRequest struct {
	ProtocolVersion string          `json:"protocol_version"`
	InteractionID   string          `json:"interaction_id"`
	RequestID       string          `json:"request_id"`
	SourceAgentID   string          `json:"source_agent_id"`
	TargetAgentID   string          `json:"target_agent_id"`
	ToolName        string          `json:"tool_name"`
	Arguments       json.RawMessage `json:"arguments"`
	SessionID       string          `json:"session_id"`
	TaskID          string          `json:"task_id"`
	RiskLevel       string          `json:"risk_level"`
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
		ProtocolVersion: req.ProtocolVersion,
		InteractionID:   req.RequestID,
		RequestID:       req.RequestID,
		SourceAgentID:   req.InitiatorAgentID,
		TargetAgentID:   req.TargetAgentID,
		ToolName:        req.ToolName,
		Arguments:       arguments,
		SessionID:       req.SessionID,
		TaskID:          req.TaskID,
		RiskLevel:       req.RiskLevel,
	})
	if err != nil {
		return denied("failed to marshal delegation request"), fmt.Errorf("marshal delegation request: %w", err)
	}

	response, status, err := a.authorizeAt(ctx, "/interaction/v1/delegations/authorize", payload)
	if err == nil || status != http.StatusNotFound {
		return response, err
	}
	return a.authorizeAtLegacy(ctx, payload)
}

func (a *HTTPR2Authorizer) authorizeAtLegacy(ctx context.Context, payload []byte) (models.DelegationResponse, error) {
	response, _, err := a.authorizeAt(ctx, "/r2/v1/delegations/authorize", payload)
	return response, err
}

func (a *HTTPR2Authorizer) authorizeAt(ctx context.Context, path string, payload []byte) (models.DelegationResponse, int, error) {
	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, a.BaseURL+path, bytes.NewReader(payload))
	if err != nil {
		return denied("failed to build interaction authorization request"), 0, fmt.Errorf("build authorization request: %w", err)
	}
	httpReq.Header.Set("Content-Type", "application/json")
	if a.BearerToken != "" {
		httpReq.Header.Set("Authorization", "Bearer "+a.BearerToken)
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
	if !response.Allowed {
		if response.Reason == "" {
			response.Reason = "IIGE denied delegation"
		}
		return response, httpResp.StatusCode, fmt.Errorf("IIGE denied delegation: %s", response.Reason)
	}
	return response, httpResp.StatusCode, nil
}

func denied(reason string) models.DelegationResponse {
	return models.DelegationResponse{
		Allowed:         false,
		Reason:          reason,
		ProtocolVersion: interactionProtocolVersion,
	}
}

// RecordLifecycle appends a committed Task transition to the Python audit timeline.
func (a *HTTPR2Authorizer) RecordLifecycle(ctx context.Context, task models.Task, event string) error {
	payload, err := json.Marshal(map[string]any{
		"interaction_id":        task.InteractionID,
		"root_interaction_id":   task.RootInteractionID,
		"parent_interaction_id": task.ParentInteractionID,
		"decision_id":           task.DecisionID,
		"task_id":               task.TaskID,
		"session_id":            task.SessionID,
		"source_agent_id":       task.InitiatorAgentID,
		"target_agent_id":       task.TargetAgentID,
		"event":                 event,
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
