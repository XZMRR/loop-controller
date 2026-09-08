// Package execution runs delegated tasks on the target agent.
package execution

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
	"sync"
)

const maxResponseBytes = 1 << 20

// Request contains the persisted execution scope of a delegated task.
type Request struct {
	TaskID           string
	SessionID        string
	InitiatorAgentID string
	TargetAgentID    string
	ToolName         string
	Arguments        json.RawMessage
}

// Result is the terminal outcome returned by a target executor.
type Result struct {
	Status    string
	Outcome   json.RawMessage
	ErrorCode string
	MayBeSent bool
}

// Handle represents one running execution and can cancel its actual context.
type Handle interface {
	Done() <-chan Result
	Cancel() bool
}

// TargetExecutor starts delegated work and returns its cancellable handle.
type TargetExecutor interface {
	Start(ctx context.Context, req Request) (Handle, error)
}

// HTTPExecutor dispatches execution through the Python tool-governance HTTP API.
type HTTPExecutor struct {
	BaseURL string
	Client  *http.Client
}

type httpHandle struct {
	cancel          context.CancelFunc
	done            chan Result
	stopped         chan struct{}
	cancelOnce      sync.Once
	stopOnce        sync.Once
	cancelConfirmed bool
}

func (h *httpHandle) Done() <-chan Result { return h.done }
func (h *httpHandle) Cancel() bool {
	h.cancelOnce.Do(h.cancel)
	<-h.stopped
	return h.cancelConfirmed
}

// Start begins an HTTP tool call without tying its lifetime to the start request.
func (e *HTTPExecutor) Start(parent context.Context, req Request) (Handle, error) {
	base, err := url.Parse(strings.TrimRight(e.BaseURL, "/"))
	if err != nil || (base.Scheme != "http" && base.Scheme != "https") || base.Host == "" {
		return nil, fmt.Errorf("invalid HTTP executor base URL")
	}
	base.Path = strings.TrimRight(base.Path, "/") + "/v1/govern/tool-call"

	var arguments map[string]any
	if err := json.Unmarshal(req.Arguments, &arguments); err != nil {
		return nil, fmt.Errorf("decode execution arguments: %w", err)
	}
	body, err := json.Marshal(map[string]any{
		"agent_id":     req.TargetAgentID,
		"user_id":      req.InitiatorAgentID,
		"tool_name":    req.ToolName,
		"arguments":    arguments,
		"session_id":   req.SessionID,
		"task_id":      req.TaskID,
		"task_context": "delegated task " + req.TaskID,
	})
	if err != nil {
		return nil, fmt.Errorf("marshal execution request: %w", err)
	}

	ctx, cancel := context.WithCancel(context.WithoutCancel(parent))
	handle := &httpHandle{cancel: cancel, done: make(chan Result, 1), stopped: make(chan struct{})}
	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, base.String(), bytes.NewReader(body))
	if err != nil {
		cancel()
		return nil, fmt.Errorf("create execution request: %w", err)
	}
	httpReq.Header.Set("Content-Type", "application/json")
	go e.run(ctx, httpReq, handle)
	return handle, nil
}

func (e *HTTPExecutor) run(ctx context.Context, req *http.Request, handle *httpHandle) {
	defer handle.stopOnce.Do(func() { close(handle.stopped) })
	defer close(handle.done)
	defer handle.cancelOnce.Do(handle.cancel)

	client := e.Client
	if client == nil {
		client = http.DefaultClient
	}
	wroteRequest := false
	trace := &httptrace.ClientTrace{WroteRequest: func(httptrace.WroteRequestInfo) { wroteRequest = true }}
	resp, err := client.Do(req.WithContext(httptrace.WithClientTrace(req.Context(), trace)))
	if err != nil {
		if ctx.Err() != nil {
			// 取消发生在请求真正写出发送之前时，可以确认工具调用并未发出；
			// 已经写出则无法确认远端是否已开始执行，交由调用方判定 outcome_unknown。
			if !wroteRequest {
				handle.cancelConfirmed = true
			}
			return
		}
		handle.done <- Result{Status: "failed", ErrorCode: "executor_unreachable", MayBeSent: wroteRequest}
		return
	}
	defer resp.Body.Close()
	payload, err := io.ReadAll(io.LimitReader(resp.Body, maxResponseBytes+1))
	if err != nil {
		handle.done <- Result{Status: "failed", ErrorCode: "executor_invalid_response", MayBeSent: true}
		return
	}
	if len(payload) > maxResponseBytes {
		handle.done <- Result{Status: "failed", ErrorCode: "executor_invalid_response", MayBeSent: true}
		return
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		handle.done <- Result{Status: "failed", ErrorCode: "executor_http_error"}
		return
	}
	var result struct {
		Status    string          `json:"status"`
		Result    json.RawMessage `json:"result"`
		ErrorCode string          `json:"error_code"`
	}
	if err := json.Unmarshal(payload, &result); err != nil {
		handle.done <- Result{Status: "failed", ErrorCode: "executor_invalid_response", MayBeSent: true}
		return
	}
	if result.Status != "allow" {
		errorCode := result.ErrorCode
		if errorCode == "" {
			errorCode = "tool_execution_failed"
		}
		handle.done <- Result{Status: "failed", ErrorCode: errorCode}
		return
	}
	outcome, _ := json.Marshal(map[string]json.RawMessage{"result": result.Result})
	handle.done <- Result{Status: "completed", Outcome: outcome}
}

var _ TargetExecutor = (*HTTPExecutor)(nil)
var _ Handle = (*httpHandle)(nil)
