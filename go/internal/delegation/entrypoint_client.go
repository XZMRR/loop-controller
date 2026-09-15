package delegation

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptrace"
	"net/url"
	"strings"
	"time"

	"github.com/loop-controller/go/internal/entrypointpolicy"
	"github.com/loop-controller/go/internal/models"
)

const maxEntrypointResponseBytes = 1 << 20

// DispatchError records whether an entrypoint request may have reached the target.
type DispatchError struct {
	Err          error
	FailureClass models.FailureClass
	MayBeSent    bool
	Disposition  models.DispatchDisposition
	StatusCode   int
	RetryAfter   time.Duration
}

func (e *DispatchError) Error() string { return e.Err.Error() }
func (e *DispatchError) Unwrap() error { return e.Err }

// EntrypointClient delivers authorized task lifecycle requests to the target agent.
type EntrypointClient interface {
	Dispatch(ctx context.Context, entrypoint models.AgentEntrypoint, req models.EntrypointTaskRequest) error
	Cancel(ctx context.Context, entrypoint models.AgentEntrypoint, taskID, delegationToken string) (bool, error)
}

// HTTPEntrypointClient dispatches tasks to HTTP Agent entrypoints.
type HTTPEntrypointClient struct {
	Client *http.Client
	Policy entrypointpolicy.Policy
}

// Dispatch sends a delegated task to the target's standard entrypoint route.
func (c *HTTPEntrypointClient) Dispatch(ctx context.Context, entrypoint models.AgentEntrypoint, req models.EntrypointTaskRequest) error {
	base, err := entrypointURL(entrypoint, "/a2a/v1/entrypoint/tasks")
	if err != nil {
		return err
	}
	body, err := json.Marshal(req)
	if err != nil {
		return fmt.Errorf("marshal entrypoint request: %w", err)
	}
	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, base.String(), bytes.NewReader(body))
	if err != nil {
		return fmt.Errorf("create entrypoint request: %w", err)
	}
	httpReq.Header.Set("Content-Type", "application/json")
	httpReq.Header.Set("Authorization", "Bearer "+req.DelegationToken)
	if req.DeliveryID != "" {
		httpReq.Header.Set("Idempotency-Key", req.DeliveryID)
	}
	if req.AssignmentID != "" {
		httpReq.Header.Set("X-LC-Assignment-ID", req.AssignmentID)
		httpReq.Header.Set("X-LC-Delivery-ID", req.DeliveryID)
		httpReq.Header.Set("X-LC-Assignment-Attempt", fmt.Sprint(req.AssignmentAttempt))
		httpReq.Header.Set("X-LC-Assignment-Fence", fmt.Sprint(req.AssignmentFence))
	}
	approved, err := c.Policy.Validate(ctx, base.String())
	if err != nil && c.Policy.Development && c.Client != nil {
		if checked, checkErr := c.Policy.Check(base.String()); checkErr == nil {
			approved = entrypointpolicy.Approved{URL: checked}
			err = nil
		}
	}
	if err != nil {
		return &DispatchError{Err: err, FailureClass: models.FailureClassSecurityViolation, Disposition: models.DispatchDispositionConfirmedNotSent}
	}
	client, err := c.Policy.Client(c.Client, approved)
	if err != nil {
		return &DispatchError{Err: err, FailureClass: models.FailureClassSecurityViolation, Disposition: models.DispatchDispositionConfirmedNotSent}
	}
	wroteRequest := false
	trace := &httptrace.ClientTrace{WroteRequest: func(httptrace.WroteRequestInfo) { wroteRequest = true }}
	resp, err := client.Do(httpReq.WithContext(httptrace.WithClientTrace(httpReq.Context(), trace)))
	if err != nil {
		failure := models.FailureClassPreDispatchTransient
		if wroteRequest {
			failure = models.FailureClassSentUnacknowledged
		}
		disposition := models.DispatchDispositionConfirmedNotSent
		if wroteRequest {
			disposition = models.DispatchDispositionSentUnacknowledged
		}
		return &DispatchError{Err: fmt.Errorf("dispatch entrypoint request: %w", err), FailureClass: failure, MayBeSent: wroteRequest, Disposition: disposition}
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, maxEntrypointResponseBytes))
		failure := models.FailureClassRemoteRejected
		retryAfter, _ := time.ParseDuration(strings.TrimSpace(resp.Header.Get("Retry-After")) + "s")
		return &DispatchError{Err: fmt.Errorf("entrypoint returned status %d", resp.StatusCode), FailureClass: failure, MayBeSent: true, Disposition: models.DispatchDispositionSentUnacknowledged, StatusCode: resp.StatusCode, RetryAfter: retryAfter}
	}
	return nil
}

// Cancel asks the target entrypoint to cancel a delegated task.
func (c *HTTPEntrypointClient) Cancel(ctx context.Context, entrypoint models.AgentEntrypoint, taskID, delegationToken string) (bool, error) {
	base, err := entrypointURL(entrypoint, "/a2a/v1/entrypoint/tasks/"+url.PathEscape(taskID)+"/cancel")
	if err != nil {
		return false, err
	}
	body, err := json.Marshal(struct {
		ProtocolVersion string `json:"protocol_version"`
	}{ProtocolVersion: interactionProtocolVersion})
	if err != nil {
		return false, fmt.Errorf("marshal entrypoint cancel request: %w", err)
	}
	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, base.String(), bytes.NewReader(body))
	if err != nil {
		return false, fmt.Errorf("create entrypoint cancel request: %w", err)
	}
	httpReq.Header.Set("Content-Type", "application/json")
	httpReq.Header.Set("Authorization", "Bearer "+delegationToken)
	approved, err := c.Policy.Validate(ctx, base.String())
	if err != nil && c.Policy.Development && c.Client != nil {
		if checked, checkErr := c.Policy.Check(base.String()); checkErr == nil {
			approved = entrypointpolicy.Approved{URL: checked}
			err = nil
		}
	}
	if err != nil {
		return false, err
	}
	client, err := c.Policy.Client(c.Client, approved)
	if err != nil {
		return false, err
	}
	resp, err := client.Do(httpReq)
	if err != nil {
		return false, fmt.Errorf("cancel entrypoint task: %w", err)
	}
	defer resp.Body.Close()
	limited, err := io.ReadAll(io.LimitReader(resp.Body, maxEntrypointResponseBytes+1))
	if err != nil || len(limited) > maxEntrypointResponseBytes {
		return false, fmt.Errorf("invalid entrypoint cancel response")
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return false, fmt.Errorf("entrypoint cancel returned %d: %s", resp.StatusCode, strings.TrimSpace(string(limited)))
	}
	var result models.Task
	if err := json.Unmarshal(limited, &result); err != nil {
		return false, fmt.Errorf("decode entrypoint cancel response: %w", err)
	}
	return result.TaskID == taskID && result.Status == "cancelled", nil
}

func entrypointURL(entrypoint models.AgentEntrypoint, path string) (*url.URL, error) {
	if entrypoint.Type != "http" {
		return nil, fmt.Errorf("unsupported entrypoint type %q", entrypoint.Type)
	}
	base, err := url.Parse(entrypoint.URL)
	if err != nil || (base.Scheme != "http" && base.Scheme != "https") || base.Host == "" {
		return nil, fmt.Errorf("invalid HTTP entrypoint URL")
	}
	base.Path = strings.TrimRight(base.Path, "/") + path
	return base, nil
}
