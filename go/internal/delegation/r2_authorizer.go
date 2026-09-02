// Package delegation decides whether an agent may delegate a tool call to another agent.
package delegation

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"

	"github.com/loop-controller/go/internal/models"
)

// R2Authorizer requests authorization from the Python R2 governance layer for a
// delegation action.
type R2Authorizer interface {
	Authorize(ctx context.Context, req models.DelegationRequest) (models.DelegationResponse, error)
}

// HTTPR2Authorizer calls the Python R2 delegations/authorize endpoint over HTTP.
type HTTPR2Authorizer struct {
	BaseURL string
	Client  *http.Client
}

// Authorize sends the delegation request to R2 and returns R2's decision.
// It fail-closed: any network error or non-2xx/allowed=false response is
// treated as a denial.
func (a *HTTPR2Authorizer) Authorize(ctx context.Context, req models.DelegationRequest) (models.DelegationResponse, error) {
	if ctx == nil {
		ctx = context.Background()
	}
	if a.Client == nil {
		a.Client = &http.Client{Timeout: 10 * time.Second}
	}

	payload, err := json.Marshal(req)
	if err != nil {
		return models.DelegationResponse{
			Allowed: false,
			Reason:  "failed to marshal delegation request",
		}, fmt.Errorf("marshal delegation request: %w", err)
	}

	url := a.BaseURL + "/r2/v1/delegations/authorize"
	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(payload))
	if err != nil {
		return models.DelegationResponse{
			Allowed: false,
			Reason:  "failed to build R2 request",
		}, fmt.Errorf("build R2 request: %w", err)
	}
	httpReq.Header.Set("Content-Type", "application/json")

	httpResp, err := a.Client.Do(httpReq)
	if err != nil {
		return models.DelegationResponse{
			Allowed: false,
			Reason:  "R2 unreachable",
		}, fmt.Errorf("R2 unreachable: %w", err)
	}
	defer httpResp.Body.Close()

	body, err := io.ReadAll(httpResp.Body)
	if err != nil {
		return models.DelegationResponse{
			Allowed: false,
			Reason:  "failed to read R2 response",
		}, fmt.Errorf("read R2 response: %w", err)
	}

	if httpResp.StatusCode < 200 || httpResp.StatusCode >= 300 {
		return models.DelegationResponse{
			Allowed: false,
			Reason:  fmt.Sprintf("R2 returned status %d", httpResp.StatusCode),
		}, fmt.Errorf("R2 returned status %d", httpResp.StatusCode)
	}

	var r2Resp models.DelegationResponse
	if err := json.Unmarshal(body, &r2Resp); err != nil {
		return models.DelegationResponse{
			Allowed: false,
			Reason:  "invalid R2 response body",
		}, fmt.Errorf("decode R2 response: %w", err)
	}

	if !r2Resp.Allowed {
		if r2Resp.Reason == "" {
			r2Resp.Reason = "R2 denied delegation"
		}
		return r2Resp, fmt.Errorf("R2 denied delegation: %s", r2Resp.Reason)
	}

	return r2Resp, nil
}

// StaticR2Authorizer is a test/development authorizer that always returns the
// same decision without making a network call.
type StaticR2Authorizer struct {
	Decision models.DelegationResponse
	Err      error
}

// Authorize returns the configured decision.
func (s *StaticR2Authorizer) Authorize(ctx context.Context, req models.DelegationRequest) (models.DelegationResponse, error) {
	return s.Decision, s.Err
}
