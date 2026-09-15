package execution

import (
	"bytes"
	"context"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"net/http/httptrace"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestSupportsSecurityCapabilities(t *testing.T) {
	if !SupportsSecurityCapabilities([]string{WorkloadIdentityV1}, []string{ExecutionReceiptV1, WorkloadIdentityV1}) {
		t.Fatal("required subset should be supported")
	}
	if SupportsSecurityCapabilities([]string{WorkloadIdentityV1, ExecutionReceiptV1}, []string{WorkloadIdentityV1}) {
		t.Fatal("missing required capability must be rejected")
	}
}

func TestHTTPExecutorStrictRequiresHTTPSAndCompleteBinding(t *testing.T) {
	executor := &HTTPExecutor{BaseURL: "http://executor.test", BearerToken: "independent", Strict: true}
	_, err := executor.Start(context.Background(), Request{TaskID: "task-1", ToolName: "echo", Arguments: json.RawMessage(`{}`)})
	if err == nil {
		t.Fatal("strict HTTP executor accepted insecure/incomplete request")
	}
}

func TestHTTPExecutorStrictRequiresTrustedIdentitiesAndPeerPin(t *testing.T) {
	req := Request{RequestID: "r", InteractionID: "i", DecisionID: "d", CallID: "c", DelegationJTI: "j", DelegationToken: "token", TenantID: "tenant", TargetWorkloadID: "workload", TaskID: "task", ToolName: "echo", Arguments: json.RawMessage(`{}`)}
	executor := &HTTPExecutor{BaseURL: "https://executor.test", BearerToken: "independent", Strict: true}
	if _, err := executor.Start(context.Background(), req); err == nil {
		t.Fatal("strict executor accepted missing trusted receipt identities and TLS peer pin")
	}
}

func TestHTTPExecutorCompletes(t *testing.T) {
	var got map[string]any
	var gotHeaders http.Header
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotHeaders = r.Header.Clone()
		if r.URL.Path != "/v1/govern/tool-call" {
			t.Errorf("path = %q", r.URL.Path)
		}
		if got := r.Header.Get("Authorization"); got != "Bearer service-token" {
			t.Errorf("Authorization = %q", got)
		}
		if err := json.NewDecoder(r.Body).Decode(&got); err != nil {
			t.Errorf("decode request: %v", err)
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"status": "allow", "result": map[string]any{"value": "ok"}})
	}))
	defer server.Close()

	executor := &HTTPExecutor{BaseURL: server.URL, BearerToken: "service-token", Client: server.Client()}
	handle, err := executor.Start(context.Background(), Request{
		TaskID: "task-1", SessionID: "session-1", InitiatorAgentID: "planner", TargetAgentID: "executor",
		ToolName: "echo", Arguments: json.RawMessage(`{"x":"hello"}`),
		AssignmentID: "assignment-1", DeliveryID: "delivery-1", AssignmentAttempt: 2, AssignmentFence: 3,
		AllowedTools: []string{"echo"}, AllowedCapabilities: []string{"read_data"}, AllowRedelegation: true,
	})
	if err != nil {
		t.Fatalf("start: %v", err)
	}
	select {
	case result := <-handle.Done():
		if result.Status != "completed" || string(result.Outcome) != `{"result":{"value":"ok"}}` {
			t.Fatalf("unexpected result: %+v", result)
		}
	case <-time.After(time.Second):
		t.Fatal("execution did not complete")
	}
	if got["agent_id"] != "executor" || got["tool_name"] != "echo" {
		t.Fatalf("unexpected execution request: %+v", got)
	}
	if got["user_id"] != nil {
		t.Fatalf("initiator must not be forwarded as user: %+v", got)
	}
	if gotHeaders.Get("X-LC-Assignment-ID") != "assignment-1" || gotHeaders.Get("X-LC-Delivery-ID") != "delivery-1" || gotHeaders.Get("X-LC-Assignment-Attempt") != "2" || gotHeaders.Get("X-LC-Assignment-Fence") != "3" {
		t.Fatalf("assignment headers=%v", gotHeaders)
	}
	if tools, ok := got["allowed_tools"].([]any); !ok || len(tools) != 1 || tools[0] != "echo" ||
		got["allow_redelegation"] != true {
		t.Fatalf("execution scope was not forwarded: %+v", got)
	}
}

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(req *http.Request) (*http.Response, error) { return f(req) }

func TestHTTPExecutorTransportFailureTracksWhetherRequestWasSent(t *testing.T) {
	for _, tc := range []struct {
		name      string
		wrote     bool
		mayBeSent bool
	}{
		{name: "connection failure", wrote: false, mayBeSent: false},
		{name: "disconnect after send", wrote: true, mayBeSent: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			client := &http.Client{Transport: roundTripFunc(func(req *http.Request) (*http.Response, error) {
				if tc.wrote {
					httptrace.ContextClientTrace(req.Context()).WroteRequest(httptrace.WroteRequestInfo{})
				}
				return nil, errors.New("connection lost")
			})}
			executor := &HTTPExecutor{BaseURL: "http://executor.test", Client: client}
			handle, err := executor.Start(context.Background(), Request{
				TaskID: "task-1", InitiatorAgentID: "planner", TargetAgentID: "executor",
				ToolName: "echo", Arguments: json.RawMessage(`{}`),
			})
			if err != nil {
				t.Fatalf("start: %v", err)
			}
			result := <-handle.Done()
			if result.Status != "failed" || result.ErrorCode != "executor_unreachable" || result.MayBeSent != tc.mayBeSent {
				t.Fatalf("unexpected result: %+v", result)
			}
		})
	}
}

func TestHTTPExecutorInvalidResponseAfterSendIsUncertain(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`not-json`))
	}))
	defer server.Close()

	executor := &HTTPExecutor{BaseURL: server.URL, Client: server.Client()}
	handle, err := executor.Start(context.Background(), Request{
		TaskID: "task-1", InitiatorAgentID: "planner", TargetAgentID: "executor",
		ToolName: "echo", Arguments: json.RawMessage(`{}`),
	})
	if err != nil {
		t.Fatalf("start: %v", err)
	}
	result := <-handle.Done()
	if result.ErrorCode != "executor_invalid_response" || !result.MayBeSent {
		t.Fatalf("unexpected result: %+v", result)
	}
}

func TestHTTPExecutorDeadlineCancelsSentRequestAsUncertain(t *testing.T) {
	client := &http.Client{Transport: roundTripFunc(func(req *http.Request) (*http.Response, error) {
		httptrace.ContextClientTrace(req.Context()).WroteRequest(httptrace.WroteRequestInfo{})
		<-req.Context().Done()
		return nil, req.Context().Err()
	})}
	deadline := time.Now().Add(30 * time.Millisecond)
	handle, err := (&HTTPExecutor{BaseURL: "http://executor.test", Client: client}).Start(context.Background(), Request{
		TaskID: "task-deadline", InitiatorAgentID: "planner", TargetAgentID: "executor",
		ToolName: "wait", Arguments: json.RawMessage(`{}`), Deadline: &deadline,
	})
	if err != nil {
		t.Fatalf("start: %v", err)
	}
	result := <-handle.Done()
	if result.ErrorCode != "execution_deadline_exceeded" || !result.MayBeSent {
		t.Fatalf("unexpected deadline result: %+v", result)
	}
}

func TestHTTPExecutorStrictAcceptsTimeoutAndCancelledReceipts(t *testing.T) {
	request := Request{RequestID: "req-1", InteractionID: "int-1", DecisionID: "dec-1", CallID: "call-1", DelegationJTI: "jti-1", TaskID: "task-1"}
	executor := &HTTPExecutor{ExpectedAttesterWorkloadID: "python-controller", ExpectedReceiptExecutor: "http", ExpectedReceiptBackend: "protected"}
	for _, terminal := range []string{"timeout", "cancelled"} {
		receipt, _ := json.Marshal(map[string]any{
			"receipt_id": "receipt-1", "type": "controller_execution_record", "attester_workload_id": "python-controller",
			"request_id": "req-1", "interaction_id": "int-1", "task_id": "task-1", "call_id": "call-1",
			"decision_id": "dec-1", "delegation_jti": "jti-1", "executor": "http", "backend": "protected",
			"status": terminal, "result_sha256": "16cfa6ba3d308e6c52a96d7d50018be09d175a518b74d5bcc6e39281ef75fa9b",
			"media_type": "application/json", "encoding": "utf-8", "issued_at": time.Now().UTC(),
		})
		if err := executor.ValidateExecutionReceipt(receipt, "error", terminal, json.RawMessage(`{"value":"ok"}`), request); err != nil {
			t.Fatalf("%s receipt rejected: %v", terminal, err)
		}
	}
}

func TestHTTPExecutorStrictRejectsReportedTerminalMismatch(t *testing.T) {
	request := Request{RequestID: "req-1", InteractionID: "int-1", DecisionID: "dec-1", CallID: "call-1", DelegationJTI: "jti-1", TaskID: "task-1"}
	receipt, _ := json.Marshal(map[string]any{
		"receipt_id": "receipt-1", "type": "controller_execution_record", "attester_workload_id": "python-controller",
		"request_id": "req-1", "interaction_id": "int-1", "task_id": "task-1", "call_id": "call-1",
		"decision_id": "dec-1", "delegation_jti": "jti-1", "executor": "http", "backend": "protected",
		"status": "cancelled", "result_sha256": "16cfa6ba3d308e6c52a96d7d50018be09d175a518b74d5bcc6e39281ef75fa9b",
		"media_type": "application/json", "encoding": "utf-8", "issued_at": time.Now().UTC(),
	})
	executor := &HTTPExecutor{ExpectedAttesterWorkloadID: "python-controller", ExpectedReceiptExecutor: "http", ExpectedReceiptBackend: "protected"}
	if err := executor.ValidateExecutionReceipt(receipt, "error", "timeout", json.RawMessage(`{"value":"ok"}`), request); err == nil {
		t.Fatal("mismatched terminal status was accepted")
	}
}

func TestHTTPExecutorStrictReceiptValidationTable(t *testing.T) {
	peer := &x509.Certificate{Raw: []byte("trusted-peer")}
	peerHash := sha256.Sum256(peer.Raw)
	request := Request{
		RequestID: "req-1", InteractionID: "int-1", DecisionID: "dec-1", CallID: "call-1",
		DelegationJTI: "jti-1", DelegationToken: "signed-token", TenantID: "tenant-1",
		TargetWorkloadID: "workload-1", TaskID: "task-1", TargetAgentID: "agent-1",
		ToolName: "echo", Arguments: json.RawMessage(`{}`), AllowedTools: []string{"echo"}, AllowedCapabilities: []string{},
	}
	valid := map[string]any{
		"receipt_id": "receipt-1", "type": "controller_execution_record", "attester_workload_id": "python-controller",
		"request_id": "req-1", "interaction_id": "int-1", "task_id": "task-1", "call_id": "call-1",
		"decision_id": "dec-1", "delegation_jti": "jti-1", "executor": "http", "backend": "protected",
		"status": "success", "result_sha256": "16cfa6ba3d308e6c52a96d7d50018be09d175a518b74d5bcc6e39281ef75fa9b",
		"media_type": "application/json", "encoding": "utf-8", "issued_at": time.Now().UTC(),
	}
	tests := []struct {
		name, field string
		missing     bool
		wantDone    bool
	}{
		{name: "valid", wantDone: true}, {name: "missing", field: "execution_receipt", missing: true},
		{name: "type mismatch", field: "type"}, {name: "attester mismatch", field: "attester_workload_id"},
		{name: "request mismatch", field: "request_id"}, {name: "interaction mismatch", field: "interaction_id"},
		{name: "task mismatch", field: "task_id"}, {name: "call mismatch", field: "call_id"},
		{name: "decision mismatch", field: "decision_id"}, {name: "jti mismatch", field: "delegation_jti"},
		{name: "status mismatch", field: "status"}, {name: "hash mismatch", field: "result_sha256"},
		{name: "executor mismatch", field: "executor"}, {name: "backend mismatch", field: "backend"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			receipt := make(map[string]any, len(valid))
			for key, value := range valid {
				receipt[key] = value
			}
			if tc.field != "" && tc.field != "execution_receipt" {
				receipt[tc.field] = "mismatch"
			}
			payload := map[string]any{"status": "allow", "result": map[string]any{"value": "ok"}, "supported_security_capabilities": StrictRequiredSecurityCapabilities}
			if !tc.missing {
				payload["execution_receipt"] = receipt
			}
			client := &http.Client{Transport: roundTripFunc(func(req *http.Request) (*http.Response, error) {
				body, _ := json.Marshal(payload)
				return &http.Response{StatusCode: 200, Body: io.NopCloser(bytes.NewReader(body)), Header: make(http.Header), TLS: &tls.ConnectionState{PeerCertificates: []*x509.Certificate{peer}}}, nil
			})}
			executor := &HTTPExecutor{BaseURL: "https://executor.test", BearerToken: "independent", Client: client, Strict: true,
				ExpectedAttesterWorkloadID: "python-controller", ExpectedReceiptExecutor: "http", ExpectedReceiptBackend: "protected", ExpectedPeerCertificateSHA256: hex.EncodeToString(peerHash[:])}
			handle, err := executor.Start(context.Background(), request)
			if err != nil {
				t.Fatalf("start: %v", err)
			}
			result := <-handle.Done()
			if tc.wantDone && result.Status != "completed" {
				t.Fatalf("valid receipt rejected: %+v", result)
			}
			if !tc.wantDone && (result.Status == "completed" || result.ErrorCode != "execution_receipt_invalid") {
				t.Fatalf("invalid receipt accepted: %+v", result)
			}
		})
	}
}

func TestResultSHA256SharedCrossLanguageVectors(t *testing.T) {
	payload, err := os.ReadFile(filepath.Join("..", "..", "..", "contract", "result_sha256_vectors.json"))
	if err != nil {
		t.Fatal(err)
	}
	var fixture struct {
		Accepted []struct {
			Name            string          `json:"name"`
			MediaType       string          `json:"media_type"`
			Encoding        *string         `json:"encoding"`
			WireResult      json.RawMessage `json:"wire_result"`
			CanonicalBase64 string          `json:"canonical_base64"`
			SHA256          string          `json:"sha256"`
		} `json:"accepted"`
		Rejected []struct {
			Name     string `json:"name"`
			WireJSON string `json:"wire_json"`
		} `json:"rejected_json"`
	}
	if err := json.Unmarshal(payload, &fixture); err != nil {
		t.Fatal(err)
	}
	for _, vector := range fixture.Accepted {
		t.Run(vector.Name, func(t *testing.T) {
			got, err := receiptResultBytes(vector.WireResult, vector.MediaType, vector.Encoding)
			if err != nil {
				t.Fatal(err)
			}
			want, err := base64.StdEncoding.DecodeString(vector.CanonicalBase64)
			if err != nil {
				t.Fatal(err)
			}
			hash := sha256.Sum256(got)
			if !bytes.Equal(got, want) || hex.EncodeToString(hash[:]) != vector.SHA256 {
				t.Fatalf("canonical=%q sha256=%s", got, hex.EncodeToString(hash[:]))
			}
		})
	}
	utf8 := "utf-8"
	for _, vector := range fixture.Rejected {
		t.Run(vector.Name, func(t *testing.T) {
			if _, err := receiptResultBytes(json.RawMessage(vector.WireJSON), "application/json", &utf8); err == nil {
				t.Fatal("out-of-contract JSON was accepted")
			}
		})
	}
}

func TestReceiptResultBytesContract(t *testing.T) {
	utf8 := "utf-8"
	tests := []struct {
		name, media string
		encoding    *string
		result      json.RawMessage
		want        []byte
	}{
		{name: "canonical json", media: "application/json", encoding: &utf8, result: json.RawMessage(`{"z":"Привет","a":{"β":1}}`), want: []byte(`{"a":{"β":1},"z":"Привет"}`)},
		{name: "utf8 text", media: "text/plain", encoding: &utf8, result: json.RawMessage(`"Привет"`), want: []byte("Привет")},
		{name: "base64 binary", media: "application/octet-stream", result: json.RawMessage(`"AP9wYXlsb2Fk"`), want: []byte{0, 255, 'p', 'a', 'y', 'l', 'o', 'a', 'd'}},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got, err := receiptResultBytes(tc.result, tc.media, tc.encoding)
			if err != nil || !bytes.Equal(got, tc.want) {
				t.Fatalf("got=%q err=%v want=%q", got, err, tc.want)
			}
		})
	}
}

func TestHTTPExecutorCancelCancelsRequestContext(t *testing.T) {
	started := make(chan struct{})
	cancelled := make(chan struct{})
	client := &http.Client{Transport: roundTripFunc(func(req *http.Request) (*http.Response, error) {
		close(started)
		<-req.Context().Done()
		close(cancelled)
		return nil, req.Context().Err()
	})}

	executor := &HTTPExecutor{BaseURL: "http://executor.test", Client: client}
	handle, err := executor.Start(context.Background(), Request{
		TaskID: "task-1", InitiatorAgentID: "planner", TargetAgentID: "executor",
		ToolName: "wait", Arguments: json.RawMessage(`{}`),
	})
	if err != nil {
		t.Fatalf("start: %v", err)
	}
	select {
	case <-started:
	case <-time.After(time.Second):
		t.Fatal("HTTP request did not start")
	}
	handle.Cancel()
	select {
	case <-cancelled:
	case <-time.After(time.Second):
		t.Fatal("HTTP request context was not cancelled")
	}
}
