// Package execution runs delegated tasks on the target agent.
package execution

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptrace"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/loop-controller/go/internal/models"
)

const maxResponseBytes = 1 << 20

const (
	WorkloadIdentityV1                 = "workload_identity_v1"
	DelegatedSubjectBindingV1          = "delegated_subject_binding_v1"
	WorkloadBoundDelegationTokenV1     = "workload_bound_delegation_token_v1"
	ExecutionReceiptV1                 = "execution_receipt_v1"
	TenantSecretNoFallbackV1           = "tenant_secret_no_fallback_v1"
	RequiredCapabilityUnavailableError = "required_security_capability_unavailable"
)

var StrictRequiredSecurityCapabilities = []string{
	WorkloadIdentityV1,
	DelegatedSubjectBindingV1,
	WorkloadBoundDelegationTokenV1,
	ExecutionReceiptV1,
	TenantSecretNoFallbackV1,
}

func SupportsSecurityCapabilities(required, supported []string) bool {
	available := make(map[string]struct{}, len(supported))
	for _, capability := range supported {
		available[capability] = struct{}{}
	}
	for _, capability := range required {
		if _, ok := available[capability]; !ok {
			return false
		}
	}
	return true
}

// Request contains the persisted execution scope of a delegated task.
type Request struct {
	AssignmentID        string
	DeliveryID          string
	AssignmentAttempt   int64
	AssignmentFence     int64
	RequestID           string
	InteractionID       string
	DecisionID          string
	CallID              string
	DelegationJTI       string
	DelegationToken     string
	TenantID            string
	TargetWorkloadID    string
	TargetInstanceID    string
	TaskID              string
	SessionID           string
	InitiatorAgentID    string
	TargetAgentID       string
	ToolName            string
	Arguments           json.RawMessage
	AllowedTools        []string
	AllowedCapabilities []string
	AllowRedelegation   bool
	Deadline            *time.Time
}

// Result is the terminal outcome returned by a target executor.
type Result struct {
	Status              string
	Outcome             json.RawMessage
	ErrorCode           string
	FailureClass        models.FailureClass
	DispatchDisposition models.DispatchDisposition
	MayBeSent           bool
	ExecutionReceipt    json.RawMessage
}

type DispatchError struct {
	Err          error
	FailureClass models.FailureClass
	Disposition  models.DispatchDisposition
}

func (e *DispatchError) Error() string { return e.Err.Error() }
func (e *DispatchError) Unwrap() error { return e.Err }

func dispatchError(err error, failureClass models.FailureClass, disposition models.DispatchDisposition) error {
	return &DispatchError{Err: err, FailureClass: failureClass, Disposition: disposition}
}

type executionReceipt struct {
	ReceiptID        string    `json:"receipt_id"`
	Type             string    `json:"type"`
	AttesterWorkload string    `json:"attester_workload_id"`
	RequestID        string    `json:"request_id"`
	InteractionID    string    `json:"interaction_id"`
	TaskID           string    `json:"task_id"`
	CallID           string    `json:"call_id"`
	DecisionID       string    `json:"decision_id"`
	DelegationJTI    string    `json:"delegation_jti"`
	Executor         string    `json:"executor"`
	Backend          string    `json:"backend"`
	Status           string    `json:"status"`
	ResultSHA256     string    `json:"result_sha256"`
	MediaType        string    `json:"media_type"`
	Encoding         *string   `json:"encoding"`
	IssuedAt         time.Time `json:"issued_at"`
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
	BaseURL                       string
	BearerToken                   string
	Client                        *http.Client
	Strict                        bool
	RequiredSecurityCapabilities  []string
	ExpectedReceiptType           string
	ExpectedAttesterWorkloadID    string
	ExpectedReceiptExecutor       string
	ExpectedReceiptBackend        string
	ExpectedPeerCertificateSHA256 string
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
	if req.Deadline != nil && !req.Deadline.After(time.Now().UTC()) {
		return nil, dispatchError(fmt.Errorf("task deadline has expired"), models.FailureClassDeadlineExceeded, models.DispatchDispositionConfirmedNotSent)
	}
	base, err := url.Parse(strings.TrimRight(e.BaseURL, "/"))
	if err != nil || (base.Scheme != "http" && base.Scheme != "https") || base.Host == "" {
		return nil, dispatchError(fmt.Errorf("invalid HTTP executor base URL"), models.FailureClassPreDispatchPermanent, models.DispatchDispositionConfirmedNotSent)
	}
	if e.Strict {
		if base.Scheme != "https" || e.BearerToken == "" || req.RequestID == "" || req.InteractionID == "" || req.DecisionID == "" || req.CallID == "" || req.DelegationJTI == "" || req.DelegationToken == "" || req.TenantID == "" || req.TargetWorkloadID == "" ||
			e.ExpectedAttesterWorkloadID == "" || e.ExpectedReceiptExecutor == "" || e.ExpectedReceiptBackend == "" || e.ExpectedPeerCertificateSHA256 == "" {
			return nil, dispatchError(fmt.Errorf("strict HTTP executor requires HTTPS, independent credential, complete workload binding, trusted receipt identities, and TLS peer pin"), models.FailureClassSecurityViolation, models.DispatchDispositionConfirmedNotSent)
		}
	}
	base.Path = strings.TrimRight(base.Path, "/") + "/v1/govern/tool-call"

	var arguments map[string]any
	if err := json.Unmarshal(req.Arguments, &arguments); err != nil {
		return nil, dispatchError(fmt.Errorf("decode execution arguments: %w", err), models.FailureClassPreDispatchPermanent, models.DispatchDispositionConfirmedNotSent)
	}
	requiredCapabilities := e.RequiredSecurityCapabilities
	if e.Strict && len(requiredCapabilities) == 0 {
		requiredCapabilities = StrictRequiredSecurityCapabilities
	}
	body, err := json.Marshal(map[string]any{
		"agent_id":                       req.TargetAgentID,
		"user_id":                        nil,
		"request_id":                     req.RequestID,
		"interaction_id":                 req.InteractionID,
		"decision_id":                    req.DecisionID,
		"call_id":                        req.CallID,
		"delegation_jti":                 req.DelegationJTI,
		"delegation_token":               req.DelegationToken,
		"tenant_id":                      req.TenantID,
		"target_workload_id":             req.TargetWorkloadID,
		"target_instance_id":             req.TargetInstanceID,
		"required_security_capabilities": requiredCapabilities,
		"tool_name":                      req.ToolName,
		"arguments":                      arguments,
		"session_id":                     req.SessionID,
		"task_id":                        req.TaskID,
		"task_context":                   "delegated task " + req.TaskID,
		"allowed_tools":                  req.AllowedTools,
		"allowed_capabilities":           req.AllowedCapabilities,
		"allow_redelegation":             req.AllowRedelegation,
		"deadline":                       req.Deadline,
	})
	if err != nil {
		return nil, dispatchError(fmt.Errorf("marshal execution request: %w", err), models.FailureClassPreDispatchPermanent, models.DispatchDispositionConfirmedNotSent)
	}

	baseCtx := context.WithoutCancel(parent)
	var ctx context.Context
	var cancel context.CancelFunc
	if req.Deadline != nil {
		ctx, cancel = context.WithDeadline(baseCtx, *req.Deadline)
	} else {
		ctx, cancel = context.WithCancel(baseCtx)
	}
	handle := &httpHandle{cancel: cancel, done: make(chan Result, 1), stopped: make(chan struct{})}
	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, base.String(), bytes.NewReader(body))
	if err != nil {
		cancel()
		return nil, dispatchError(fmt.Errorf("create execution request: %w", err), models.FailureClassPreDispatchPermanent, models.DispatchDispositionConfirmedNotSent)
	}
	httpReq.Header.Set("Content-Type", "application/json")
	if req.AssignmentID != "" {
		httpReq.Header.Set("X-LC-Assignment-ID", req.AssignmentID)
		httpReq.Header.Set("X-LC-Delivery-ID", req.DeliveryID)
		httpReq.Header.Set("X-LC-Assignment-Attempt", strconv.FormatInt(req.AssignmentAttempt, 10))
		httpReq.Header.Set("X-LC-Assignment-Fence", strconv.FormatInt(req.AssignmentFence, 10))
	}
	if e.BearerToken != "" {
		httpReq.Header.Set("Authorization", "Bearer "+e.BearerToken)
	}
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
				handle.done <- Result{Status: "failed", ErrorCode: "execution_deadline_exceeded", FailureClass: models.FailureClassPreDispatchTransient, DispatchDisposition: models.DispatchDispositionConfirmedNotSent}
			} else {
				handle.done <- Result{Status: "failed", ErrorCode: "execution_deadline_exceeded", FailureClass: models.FailureClassSentUnacknowledged, DispatchDisposition: models.DispatchDispositionSentUnacknowledged, MayBeSent: true}
			}
			return
		}
		if !wroteRequest {
			handle.done <- Result{Status: "failed", ErrorCode: "executor_unreachable", FailureClass: models.FailureClassPreDispatchTransient, DispatchDisposition: models.DispatchDispositionConfirmedNotSent}
		} else {
			handle.done <- Result{Status: "failed", ErrorCode: "executor_unreachable", FailureClass: models.FailureClassSentUnacknowledged, DispatchDisposition: models.DispatchDispositionSentUnacknowledged, MayBeSent: true}
		}
		return
	}
	defer resp.Body.Close()
	if e.Strict {
		if resp.TLS == nil || len(resp.TLS.PeerCertificates) == 0 {
			handle.done <- Result{Status: "failed", ErrorCode: "executor_peer_identity_invalid", MayBeSent: true}
			return
		}
		peerHash := sha256.Sum256(resp.TLS.PeerCertificates[0].Raw)
		if !strings.EqualFold(e.ExpectedPeerCertificateSHA256, hex.EncodeToString(peerHash[:])) {
			handle.done <- Result{Status: "failed", ErrorCode: "executor_peer_identity_invalid", MayBeSent: true}
			return
		}
	}
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
		Status                        string          `json:"status"`
		Result                        json.RawMessage `json:"result"`
		ErrorCode                     string          `json:"error_code"`
		TerminalStatus                string          `json:"terminal_status"`
		SupportedSecurityCapabilities []string        `json:"supported_security_capabilities"`
		ExecutionReceipt              json.RawMessage `json:"execution_receipt"`
	}
	if err := json.Unmarshal(payload, &result); err != nil {
		handle.done <- Result{Status: "failed", ErrorCode: "executor_invalid_response", MayBeSent: true}
		return
	}
	requiredCapabilities := e.RequiredSecurityCapabilities
	if e.Strict && len(requiredCapabilities) == 0 {
		requiredCapabilities = StrictRequiredSecurityCapabilities
	}
	if e.Strict && !SupportsSecurityCapabilities(requiredCapabilities, result.SupportedSecurityCapabilities) {
		handle.done <- Result{Status: "failed", ErrorCode: RequiredCapabilityUnavailableError, FailureClass: models.FailureClassSecurityViolation, DispatchDisposition: models.DispatchDispositionSentUnacknowledged}
		return
	}
	if e.Strict {
		if err := e.validateExecutionReceipt(result.ExecutionReceipt, result.Status, result.TerminalStatus, result.Result, req); err != nil {
			handle.done <- Result{Status: "failed", ErrorCode: "execution_receipt_invalid", FailureClass: models.FailureClassReceiptInvalid, DispatchDisposition: models.DispatchDispositionSentUnacknowledged}
			return
		}
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
	handle.done <- Result{Status: "completed", Outcome: outcome, ExecutionReceipt: result.ExecutionReceipt}
}

func (e *HTTPExecutor) validateExecutionReceipt(raw json.RawMessage, responseStatus, terminalStatus string, result json.RawMessage, req *http.Request) error {
	var body struct {
		RequestID     string `json:"request_id"`
		InteractionID string `json:"interaction_id"`
		DecisionID    string `json:"decision_id"`
		CallID        string `json:"call_id"`
		DelegationJTI string `json:"delegation_jti"`
		TaskID        string `json:"task_id"`
	}
	if err := json.NewDecoder(bytes.NewReader(mustReadRequestBody(req))).Decode(&body); err != nil {
		return err
	}
	return e.ValidateExecutionReceipt(raw, responseStatus, terminalStatus, result, Request{RequestID: body.RequestID, InteractionID: body.InteractionID, DecisionID: body.DecisionID, CallID: body.CallID, DelegationJTI: body.DelegationJTI, TaskID: body.TaskID})
}

func (e *HTTPExecutor) ValidateExecutionReceipt(raw json.RawMessage, responseStatus, terminalStatus string, result json.RawMessage, requestBody Request) error {
	if len(raw) == 0 || string(raw) == "null" {
		return fmt.Errorf("missing execution receipt")
	}
	var receipt executionReceipt
	if err := json.Unmarshal(raw, &receipt); err != nil {
		return err
	}
	terminal := terminalStatus
	if terminal == "" {
		terminal = "error"
		if responseStatus == "allow" {
			terminal = "success"
		}
	}
	if (responseStatus == "allow") != (terminal == "success") ||
		(terminal != "success" && terminal != "error" && terminal != "timeout" && terminal != "cancelled") {
		return fmt.Errorf("execution terminal status mismatch")
	}
	resultBytes, err := receiptResultBytes(result, receipt.MediaType, receipt.Encoding)
	if err != nil {
		return err
	}
	hash := sha256.Sum256(resultBytes)
	expectedType := e.ExpectedReceiptType
	if expectedType == "" {
		expectedType = "controller_execution_record"
	}
	attesterMismatch := receipt.AttesterWorkload == "" ||
		(e.ExpectedAttesterWorkloadID != "" && receipt.AttesterWorkload != e.ExpectedAttesterWorkloadID)
	executorMismatch := receipt.Executor == "" ||
		(e.ExpectedReceiptExecutor != "" && receipt.Executor != e.ExpectedReceiptExecutor)
	backendMismatch := receipt.Backend == "" ||
		(e.ExpectedReceiptBackend != "" && receipt.Backend != e.ExpectedReceiptBackend)
	if receipt.ReceiptID == "" || receipt.Type != expectedType || receipt.MediaType == "" || receipt.IssuedAt.IsZero() ||
		receipt.RequestID != requestBody.RequestID || receipt.InteractionID != requestBody.InteractionID ||
		receipt.TaskID != requestBody.TaskID || receipt.CallID != requestBody.CallID ||
		receipt.DecisionID != requestBody.DecisionID || receipt.DelegationJTI != requestBody.DelegationJTI ||
		attesterMismatch || executorMismatch || backendMismatch ||
		receipt.Status != terminal || receipt.ResultSHA256 != hex.EncodeToString(hash[:]) {
		return fmt.Errorf("execution receipt binding mismatch")
	}
	return nil
}

func receiptResultBytes(raw json.RawMessage, mediaType string, encoding *string) ([]byte, error) {
	switch mediaType {
	case "application/json":
		if encoding == nil || *encoding != "utf-8" {
			return nil, errors.New("JSON receipt requires utf-8 encoding")
		}
		return canonicalJSON(raw)
	case "text/plain":
		if encoding == nil || *encoding != "utf-8" {
			return nil, errors.New("text receipt requires utf-8 encoding")
		}
		var text string
		if err := json.Unmarshal(raw, &text); err != nil {
			return nil, errors.New("text result must be a JSON string")
		}
		return []byte(text), nil
	case "application/octet-stream":
		if encoding != nil {
			return nil, errors.New("binary receipt encoding must be null")
		}
		var encoded string
		if err := json.Unmarshal(raw, &encoded); err != nil {
			return nil, errors.New("binary result must be a base64 JSON string")
		}
		payload, err := base64.StdEncoding.DecodeString(encoded)
		if err != nil {
			return nil, errors.New("binary result is not valid base64")
		}
		return payload, nil
	default:
		return nil, errors.New("unsupported receipt media_type")
	}
}

const maxSafeJSONInteger int64 = (1 << 53) - 1

func canonicalJSON(raw json.RawMessage) ([]byte, error) {
	var value any
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	if err := decoder.Decode(&value); err != nil {
		return nil, err
	}
	var trailing any
	if err := decoder.Decode(&trailing); !errors.Is(err, io.EOF) {
		return nil, errors.New("multiple JSON values are not allowed")
	}
	var buffer bytes.Buffer
	if err := appendCanonicalJSON(&buffer, value); err != nil {
		return nil, err
	}
	return buffer.Bytes(), nil
}

func appendCanonicalJSON(buffer *bytes.Buffer, value any) error {
	switch typed := value.(type) {
	case nil:
		buffer.WriteString("null")
	case bool:
		buffer.WriteString(strconv.FormatBool(typed))
	case string:
		buffer.WriteString(quoteJSONString(typed))
	case json.Number:
		if strings.ContainsAny(typed.String(), ".eE") {
			return errors.New("JSON floating-point values are outside the result_sha256 wire subset")
		}
		integer, err := strconv.ParseInt(typed.String(), 10, 64)
		if err != nil || integer < -maxSafeJSONInteger || integer > maxSafeJSONInteger {
			return errors.New("JSON integers must be within the interoperable IEEE-754 safe range")
		}
		buffer.WriteString(strconv.FormatInt(integer, 10))
	case []any:
		buffer.WriteByte('[')
		for index, item := range typed {
			if index > 0 {
				buffer.WriteByte(',')
			}
			if err := appendCanonicalJSON(buffer, item); err != nil {
				return err
			}
		}
		buffer.WriteByte(']')
	case map[string]any:
		keys := make([]string, 0, len(typed))
		for key := range typed {
			keys = append(keys, key)
		}
		sort.Strings(keys)
		buffer.WriteByte('{')
		for index, key := range keys {
			if index > 0 {
				buffer.WriteByte(',')
			}
			buffer.WriteString(quoteJSONString(key))
			buffer.WriteByte(':')
			if err := appendCanonicalJSON(buffer, typed[key]); err != nil {
				return err
			}
		}
		buffer.WriteByte('}')
	default:
		return fmt.Errorf("unsupported result_sha256 JSON value %T", value)
	}
	return nil
}

func quoteJSONString(value string) string {
	var buffer bytes.Buffer
	buffer.WriteByte('"')
	for _, r := range value {
		switch r {
		case '"', '\\':
			buffer.WriteByte('\\')
			buffer.WriteRune(r)
		case '\b':
			buffer.WriteString(`\b`)
		case '\f':
			buffer.WriteString(`\f`)
		case '\n':
			buffer.WriteString(`\n`)
		case '\r':
			buffer.WriteString(`\r`)
		case '\t':
			buffer.WriteString(`\t`)
		default:
			if r < 0x20 {
				fmt.Fprintf(&buffer, `\u%04x`, r)
			} else {
				buffer.WriteRune(r)
			}
		}
	}
	buffer.WriteByte('"')
	return buffer.String()
}

func mustReadRequestBody(req *http.Request) []byte {
	if req.GetBody == nil {
		return nil
	}
	body, err := req.GetBody()
	if err != nil {
		return nil
	}
	defer body.Close()
	payload, _ := io.ReadAll(body)
	return payload
}

var _ TargetExecutor = (*HTTPExecutor)(nil)
var _ Handle = (*httpHandle)(nil)
