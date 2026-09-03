// Package api exposes the Go kernel via HTTP/JSON.
package api

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"regexp"
	"strings"
	"sync"
	"time"

	"github.com/loop-controller/go/internal/delegation"
	"github.com/loop-controller/go/internal/discovery"
	"github.com/loop-controller/go/internal/execution"
	"github.com/loop-controller/go/internal/models"
	"github.com/loop-controller/go/internal/registry"
	"github.com/loop-controller/go/internal/router"
	"github.com/loop-controller/go/internal/store"
	"github.com/loop-controller/go/internal/stream"
	"github.com/loop-controller/go/internal/task"
	"github.com/loop-controller/go/internal/token"
)

// Server holds the kernel state.
type Server struct {
	registry         *registry.Registry
	tasks            *task.Manager
	router           *router.Router
	delegation       *delegation.Delegator
	publisher        stream.TaskEventPublisher
	discovery        *discovery.Manager
	db               *store.DB
	issuer           *token.HMACIssuer
	messages         store.MessageStore
	idempotency      store.IdempotencyStore
	entrypointClient delegation.EntrypointClient
	executor         execution.TargetExecutor
	executionsMu     sync.Mutex
	executions       map[string]execution.Handle
	outboxCancel     context.CancelFunc
}

// currentProtocolVersion is the A2A HTTP/JSON protocol version implemented by
// this kernel. Patch differences are allowed; major/minor differences are
// fail-closed.
const (
	currentProtocolVersion = models.CurrentProtocolVersion
	maxJSONBodyBytes       = 1 << 20
)

var strictSemverPattern = regexp.MustCompile(`^[0-9]+\.[0-9]+\.[0-9]+$`)

// checkProtocolVersion returns an error if the client-supplied protocol version
// is missing, malformed, or incompatible with the current kernel.
func checkProtocolVersion(v string) error {
	if !strictSemverPattern.MatchString(v) {
		return fmt.Errorf("invalid protocol version %q: expected major.minor.patch", v)
	}
	if v == currentProtocolVersion {
		return nil
	}
	parts := strings.Split(v, ".")
	current := strings.Split(currentProtocolVersion, ".")
	if parts[0] != current[0] || parts[1] != current[1] {
		return fmt.Errorf("incompatible protocol version %q, expected %s", v, currentProtocolVersion)
	}
	// patch-level drift is tolerated
	return nil
}

// validateMessageParts rejects unknown Part types and empty data payloads.
func validateMessageParts(msg *models.Message) error {
	for i, p := range msg.Parts {
		switch p.Type {
		case "text":
			// text parts may carry arbitrary text; no further validation
		case "data":
			if len(p.Data) == 0 {
				return fmt.Errorf("part %d: data part has empty payload", i)
			}
		default:
			return fmt.Errorf("part %d: unknown part type %q", i, p.Type)
		}
	}
	return nil
}

// NewServer creates a new Server backed by SQLite. If dbPath is empty, the
// default path ./data/a2a.db is used.
func NewServer(secret []byte, dbPath string, providers ...discovery.AgentDiscoveryProvider) (*Server, error) {
	db, err := store.Open(context.Background(), dbPath)
	if err != nil {
		return nil, fmt.Errorf("open store: %w", err)
	}
	reg := registry.New()
	pub := stream.NewPublisher(db.EventStore())
	tasks := task.New(db.TaskStore()).WithEventFanout(pub)
	r := router.New(reg)
	issuer := token.NewHMACIssuer(secret)
	d := delegation.New(reg, tasks, issuer, pub, 5*time.Minute)
	mgr := discovery.NewManager(reg, providers...)
	return &Server{
		registry:    reg,
		tasks:       tasks,
		router:      r,
		delegation:  d,
		publisher:   pub,
		discovery:   mgr,
		db:          db,
		issuer:      issuer,
		messages:    db.MessageStore(),
		idempotency: db.IdempotencyStore(),
		executions:  make(map[string]execution.Handle),
	}, nil
}

// SetR2Authorizer injects an interaction authorizer into the delegator.
func (s *Server) SetR2Authorizer(a delegation.R2Authorizer) {
	s.delegation.WithR2Authorizer(a)
	if s.outboxCancel != nil {
		s.outboxCancel()
		s.outboxCancel = nil
	}
	if auditor, ok := a.(store.LifecycleAuditor); ok {
		ctx, cancel := context.WithCancel(context.Background())
		s.outboxCancel = cancel
		dispatcher := store.NewLifecycleOutboxDispatcher(s.db.LifecycleOutboxStore(), auditor, time.Second)
		go dispatcher.Run(ctx)
	}
}

// SetEntrypointClient enables delivery of authorized tasks to target Agents.
func (s *Server) SetEntrypointClient(client delegation.EntrypointClient) {
	s.entrypointClient = client
	s.delegation.WithEntrypointClient(client)
}

// SetTargetExecutor configures execution of accepted target-side tasks.
func (s *Server) SetTargetExecutor(executor execution.TargetExecutor) {
	s.executor = executor
}

// Close releases database resources held by the server.
func (s *Server) Close() error {
	if s.outboxCancel != nil {
		s.outboxCancel()
		s.outboxCancel = nil
	}
	if s.db != nil {
		return s.db.Close()
	}
	return nil
}

// SyncDiscovery runs a one-time sync of all discovery providers.
func (s *Server) SyncDiscovery(ctx context.Context) error {
	if s.discovery == nil {
		return nil
	}
	return s.discovery.Sync(ctx)
}

// RegisterRoutes attaches handlers to the given mux.
func (s *Server) RegisterRoutes(mux *http.ServeMux) {
	mux.HandleFunc("POST /a2a/v1/agents", s.handleRegisterAgent)
	mux.HandleFunc("GET /a2a/v1/agents", s.handleListAgents)
	mux.HandleFunc("GET /a2a/v1/agents/{id}", s.handleGetAgent)
	mux.HandleFunc("POST /a2a/v1/tasks", s.handleCreateTask)
	mux.HandleFunc("GET /a2a/v1/tasks/{id}", s.handleGetTask)
	mux.HandleFunc("GET /a2a/v1/tasks/{id}/stream", s.handleTaskStream)
	mux.HandleFunc("POST /a2a/v1/messages", s.handleSendMessage)
	mux.HandleFunc("POST /a2a/v1/delegations", s.handleDelegation)
	mux.HandleFunc("POST /a2a/v1/tasks/{id}/cancel", s.handleCancelTask)
	mux.HandleFunc("POST /a2a/v1/entrypoint/tasks", s.withEntrypointToken("create", true, s.handleEntrypointCreate))
	mux.HandleFunc("POST /a2a/v1/entrypoint/tasks/{id}/accept", s.withEntrypointToken("accept", true, s.handleEntrypointAccept))
	mux.HandleFunc("POST /a2a/v1/entrypoint/tasks/{id}/start", s.withEntrypointToken("start", true, s.handleEntrypointStart))
	mux.HandleFunc("POST /a2a/v1/entrypoint/tasks/{id}/cancel", s.withEntrypointToken("cancel", true, s.handleEntrypointCancel))
	mux.HandleFunc("GET /a2a/v1/entrypoint/tasks/{id}", s.withEntrypointToken("get", false, s.handleEntrypointGet))
	mux.HandleFunc("POST /a2a/v1/entrypoint/tasks/{id}/results", s.withEntrypointToken("results", true, s.handleEntrypointResults))
	mux.HandleFunc("GET /health", s.handleHealth)
}

func (s *Server) handleTaskStream(w http.ResponseWriter, r *http.Request) {
	taskID := r.PathValue("id")
	if taskID == "" {
		writeError(w, http.StatusBadRequest, "task_id_required", "task_id is required")
		return
	}
	if _, err := s.tasks.Get(taskID); err != nil {
		writeError(w, http.StatusNotFound, "task_not_found", err.Error())
		return
	}
	_ = stream.ServeTaskStream(s.publisher, w, r, taskID)
}

func decodeJSONPost(w http.ResponseWriter, r *http.Request, dst any) bool {
	r.Body = http.MaxBytesReader(w, r.Body, maxJSONBodyBytes)
	decoder := json.NewDecoder(r.Body)
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(dst); err != nil {
		var maxBytesErr *http.MaxBytesError
		var partErr *models.PartValidationError
		switch {
		case errors.As(err, &maxBytesErr):
			writeError(w, http.StatusRequestEntityTooLarge, "request_too_large", "request body exceeds limit")
		case errors.As(err, &partErr):
			writeError(w, http.StatusBadRequest, "invalid_message_parts", partErr.Error())
		default:
			writeError(w, http.StatusBadRequest, "invalid_json", err.Error())
		}
		return false
	}
	if err := decoder.Decode(&struct{}{}); err != io.EOF {
		writeError(w, http.StatusBadRequest, "invalid_json", "request body must contain a single JSON value")
		return false
	}
	return true
}

func (s *Server) handleRegisterAgent(w http.ResponseWriter, r *http.Request) {
	var card models.AgentCard
	if !decodeJSONPost(w, r, &card) {
		return
	}
	if err := s.registry.Register(card); err != nil {
		writeError(w, http.StatusBadRequest, "register_failed", err.Error())
		return
	}
	w.WriteHeader(http.StatusCreated)
	json.NewEncoder(w).Encode(map[string]string{"agent_id": card.AgentID})
}

func (s *Server) handleListAgents(w http.ResponseWriter, r *http.Request) {
	agents := s.registry.List()
	writeJSON(w, http.StatusOK, models.AgentList{Agents: agents})
}

func (s *Server) handleGetAgent(w http.ResponseWriter, r *http.Request) {
	agentID := r.PathValue("id")
	card, err := s.registry.Get(agentID)
	if err != nil {
		writeError(w, http.StatusNotFound, "agent_not_found", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, card)
}

func (s *Server) handleCreateTask(w http.ResponseWriter, r *http.Request) {
	var req struct {
		ProtocolVersion  string `json:"protocol_version"`
		SessionID        string `json:"session_id"`
		InitiatorAgentID string `json:"initiator_agent_id"`
		TargetAgentID    string `json:"target_agent_id"`
	}
	if !decodeJSONPost(w, r, &req) {
		return
	}
	if err := checkProtocolVersion(req.ProtocolVersion); err != nil {
		writeError(w, http.StatusBadRequest, "incompatible_protocol_version", err.Error())
		return
	}
	t, err := s.tasks.CreateReliable(req.SessionID, req.InitiatorAgentID, req.TargetAgentID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "task_create_failed", err.Error())
		return
	}
	writeJSON(w, http.StatusCreated, t)
}

func (s *Server) handleGetTask(w http.ResponseWriter, r *http.Request) {
	taskID := r.PathValue("id")
	t, err := s.tasks.Get(taskID)
	if err != nil {
		writeError(w, http.StatusNotFound, "task_not_found", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, t)
}

func (s *Server) handleSendMessage(w http.ResponseWriter, r *http.Request) {
	var msg models.Message
	if !decodeJSONPost(w, r, &msg) {
		return
	}
	if err := checkProtocolVersion(msg.ProtocolVersion); err != nil {
		writeError(w, http.StatusBadRequest, "incompatible_protocol_version", err.Error())
		return
	}
	if err := validateMessageParts(&msg); err != nil {
		writeError(w, http.StatusBadRequest, "invalid_message_parts", err.Error())
		return
	}
	resp, err := s.router.Route(msg)
	if err != nil {
		writeError(w, http.StatusBadRequest, "route_failed", err.Error())
		return
	}
	resp.ProtocolVersion = currentProtocolVersion
	status := http.StatusOK
	if !resp.Accepted {
		status = http.StatusBadRequest
	}
	writeJSON(w, status, resp)
}

func (s *Server) handleDelegation(w http.ResponseWriter, r *http.Request) {
	var req models.DelegationRequest
	if !decodeJSONPost(w, r, &req) {
		return
	}
	if err := checkProtocolVersion(req.ProtocolVersion); err != nil {
		writeError(w, http.StatusBadRequest, "incompatible_protocol_version", err.Error())
		return
	}
	scope := "delegation:" + req.InitiatorAgentID
	requestData, err := json.Marshal(req)
	if err != nil {
		writeError(w, http.StatusBadRequest, "invalid_request", err.Error())
		return
	}
	requestDigest := token.HashArguments(requestData)
	idempotencyResult, err := s.idempotency.TryBegin(
		r.Context(), req.RequestID, scope, requestDigest,
	)
	if err != nil {
		writeError(w, http.StatusConflict, "idempotency_conflict", err.Error())
		return
	}
	if !idempotencyResult.Locked && idempotencyResult.CompletedAt != nil {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(idempotencyResult.ResponseStatus)
		_, _ = w.Write(idempotencyResult.ResponseBody)
		return
	}

	resp, delegationErr := s.delegation.Request(r.Context(), req)
	if delegationErr != nil {
		body, _ := json.Marshal(models.ErrorResponse{Error: delegationErr.Error(), Code: "delegation_failed"})
		_ = s.idempotency.Complete(r.Context(), req.RequestID, scope, http.StatusBadRequest, body)
		writeJSON(w, http.StatusBadRequest, models.ErrorResponse{Error: delegationErr.Error(), Code: "delegation_failed"})
		return
	}
	resp.ProtocolVersion = currentProtocolVersion
	status := http.StatusOK
	if !resp.Allowed {
		status = http.StatusForbidden
	}
	body, err := json.Marshal(resp)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "response_encode_failed", err.Error())
		return
	}
	if err := s.idempotency.Complete(r.Context(), req.RequestID, scope, status, body); err != nil {
		writeError(w, http.StatusInternalServerError, "idempotency_persist_failed", err.Error())
		return
	}
	writeJSON(w, status, resp)
}

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{
		"status":           "ok",
		"protocol_version": currentProtocolVersion,
	})
}

func (s *Server) handleCancelTask(w http.ResponseWriter, r *http.Request) {
	taskID := r.PathValue("id")
	var req models.CancelTaskRequest
	if !decodeJSONPost(w, r, &req) {
		return
	}
	if err := checkProtocolVersion(req.ProtocolVersion); err != nil {
		writeError(w, http.StatusBadRequest, "incompatible_protocol_version", err.Error())
		return
	}
	t, err := s.tasks.Get(taskID)
	if err != nil {
		writeError(w, http.StatusNotFound, "task_not_found", err.Error())
		return
	}
	// 调用方若携带 delegation token，则必须是绑定到该 task 的 token。
	if auth := r.Header.Get("Authorization"); auth != "" {
		tokenStr, berr := bearerToken(auth)
		if berr != nil {
			writeError(w, http.StatusUnauthorized, "invalid_delegation_token", berr.Error())
			return
		}
		claims, verr := s.validateDelegationToken(tokenStr)
		if verr != nil {
			writeError(w, http.StatusUnauthorized, "invalid_delegation_token", verr.Error())
			return
		}
		if err := s.verifyTokenMatchesTask(r.Context(), claims, t); err != nil {
			writeError(w, http.StatusForbidden, "token_scope_mismatch", err.Error())
			return
		}
	}
	if t.IsTerminal() {
		writeJSON(w, http.StatusOK, t)
		return
	}
	confirmed := true
	if s.entrypointClient != nil {
		var cancelErr error
		confirmed, cancelErr = s.delegation.Cancel(r.Context(), t.TargetAgentID, taskID, t.DelegationToken)
		if cancelErr != nil {
			confirmed = false
		}
	}
	var updated models.Task
	switch {
	case confirmed:
		updated, err = s.tasks.UpdateStatusFrom(taskID, t.Status, "cancelled")
	case t.Status == "running":
		// 已进入执行阶段且无法确认目标已停止时，必须进入 outcome_unknown。
		updated, err = s.tasks.MarkOutcomeUnknown(taskID)
	default:
		// pending/accepted/outcome_unknown 阶段本地没有正在执行的副作用，
		// 目标不可达时本地撤销不会伪报执行结果。
		updated, err = s.tasks.UpdateStatusFrom(taskID, t.Status, "cancelled")
	}
	if err != nil {
		if errors.Is(err, task.ErrInvalidStatus) {
			writeError(w, http.StatusConflict, "invalid_status_transition", err.Error())
			return
		}
		writeError(w, http.StatusNotFound, "task_not_found", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, updated)
}

func (s *Server) withEntrypointToken(operation string, replayProtected bool, next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		tokenStr, err := bearerToken(r.Header.Get("Authorization"))
		if err != nil {
			writeError(w, http.StatusUnauthorized, "invalid_delegation_token", err.Error())
			return
		}
		claims, err := s.validateDelegationToken(tokenStr)
		if err != nil {
			writeError(w, http.StatusUnauthorized, "invalid_delegation_token", err.Error())
			return
		}

		var body []byte
		if r.Body != nil {
			body, err = io.ReadAll(io.LimitReader(r.Body, maxJSONBodyBytes+1))
			if err != nil {
				writeError(w, http.StatusBadRequest, "invalid_json", err.Error())
				return
			}
			if len(body) > maxJSONBodyBytes {
				writeError(w, http.StatusRequestEntityTooLarge, "request_too_large", "request body exceeds limit")
				return
			}
			r.Body = io.NopCloser(bytes.NewReader(body))
		}

		if operation == "create" {
			var req models.EntrypointTaskRequest
			decoder := json.NewDecoder(bytes.NewReader(body))
			decoder.DisallowUnknownFields()
			if err := decoder.Decode(&req); err != nil {
				writeError(w, http.StatusBadRequest, "invalid_json", err.Error())
				return
			}
			if req.DelegationToken != tokenStr {
				writeError(w, http.StatusForbidden, "token_scope_mismatch", "body delegation_token does not match bearer token")
				return
			}
			if err := s.verifyTokenMatchesRequest(claims, req); err != nil {
				writeError(w, http.StatusForbidden, "token_scope_mismatch", err.Error())
				return
			}
		} else {
			taskID := r.PathValue("id")
			if claims.TaskID == "" || claims.TaskID != taskID {
				writeError(w, http.StatusForbidden, "token_scope_mismatch", "token task_id mismatch")
				return
			}
			t, err := s.tasks.Get(taskID)
			if err == nil {
				if err := s.verifyTokenMatchesTask(r.Context(), claims, t); err != nil {
					writeError(w, http.StatusForbidden, "token_scope_mismatch", err.Error())
					return
				}
			}
		}

		if !replayProtected {
			next(w, r)
			return
		}
		requestDigest := token.HashArguments(body)
		scope := "entrypoint:" + operation
		result, err := s.idempotency.TryBegin(r.Context(), claims.TokenID, scope, requestDigest)
		if err != nil {
			writeError(w, http.StatusConflict, "replay_conflict", err.Error())
			return
		}
		if !result.Locked && result.CompletedAt != nil {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(result.ResponseStatus)
			_, _ = w.Write(result.ResponseBody)
			return
		}

		recorder := httptest.NewRecorder()
		next(recorder, r)
		responseBody := recorder.Body.Bytes()
		if err := s.idempotency.Complete(r.Context(), claims.TokenID, scope, recorder.Code, responseBody); err != nil {
			writeError(w, http.StatusInternalServerError, "idempotency_persist_failed", err.Error())
			return
		}
		for key, values := range recorder.Header() {
			w.Header()[key] = values
		}
		w.WriteHeader(recorder.Code)
		_, _ = w.Write(responseBody)
	}
}

func bearerToken(header string) (string, error) {
	parts := strings.Fields(header)
	if len(parts) != 2 || !strings.EqualFold(parts[0], "Bearer") || parts[1] == "" {
		return "", errors.New("Authorization header must use Bearer delegation token")
	}
	return parts[1], nil
}

func (s *Server) handleEntrypointCreate(w http.ResponseWriter, r *http.Request) {
	var req models.EntrypointTaskRequest
	if !decodeJSONPost(w, r, &req) {
		return
	}
	if err := checkProtocolVersion(req.ProtocolVersion); err != nil {
		writeError(w, http.StatusBadRequest, "incompatible_protocol_version", err.Error())
		return
	}
	claims, err := s.validateDelegationToken(req.DelegationToken)
	if err != nil {
		writeError(w, http.StatusForbidden, "invalid_delegation_token", err.Error())
		return
	}
	if err := s.verifyTokenMatchesRequest(claims, req); err != nil {
		writeError(w, http.StatusForbidden, "token_scope_mismatch", err.Error())
		return
	}

	ctx := r.Context()
	t, err := s.tasks.Get(req.TaskID)
	if err != nil {
		if !errors.Is(err, task.ErrTaskNotFound) {
			writeError(w, http.StatusInternalServerError, "task_lookup_failed", err.Error())
			return
		}
		t, err = s.tasks.CreateWithInteractionID(
			req.TaskID, req.SessionID, req.InitiatorAgentID, req.TargetAgentID,
			req.InteractionID, req.DecisionID, req.RootInteractionID, req.ParentInteractionID,
		)
		if err != nil {
			writeError(w, http.StatusInternalServerError, "task_create_failed", err.Error())
			return
		}
	}
	if t.Status != "pending" {
		writeError(w, http.StatusConflict, "invalid_status_transition", fmt.Sprintf("task is %s, expected pending", t.Status))
		return
	}

	msg := models.Message{
		MessageID:       fmt.Sprintf("msg-%s-%d", req.TaskID, time.Now().UTC().UnixNano()),
		TaskID:          req.TaskID,
		FromAgentID:     req.InitiatorAgentID,
		ToAgentID:       req.TargetAgentID,
		Role:            "delegation",
		Timestamp:       time.Now().UTC(),
		ProtocolVersion: req.ProtocolVersion,
		Parts: []models.Part{
			{Type: "text", Text: req.ToolName},
			{Type: "data", Data: req.Arguments},
		},
	}
	if err := s.messages.Save(ctx, msg); err != nil {
		writeError(w, http.StatusInternalServerError, "message_save_failed", err.Error())
		return
	}

	writeJSON(w, http.StatusCreated, t)
}

func (s *Server) handleEntrypointAccept(w http.ResponseWriter, r *http.Request) {
	taskID := r.PathValue("id")
	if err := s.requireTaskPending(taskID); err != nil {
		if errors.Is(err, task.ErrTaskNotFound) {
			writeError(w, http.StatusNotFound, "task_not_found", err.Error())
			return
		}
		writeError(w, http.StatusConflict, "invalid_status_transition", err.Error())
		return
	}
	updated, err := s.tasks.UpdateStatus(taskID, "accepted")
	if err != nil {
		writeError(w, http.StatusConflict, "invalid_status_transition", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, updated)
}

func (s *Server) handleEntrypointStart(w http.ResponseWriter, r *http.Request) {
	var req struct {
		ProtocolVersion string `json:"protocol_version"`
	}
	if !decodeJSONPost(w, r, &req) {
		return
	}
	if err := checkProtocolVersion(req.ProtocolVersion); err != nil {
		writeError(w, http.StatusBadRequest, "incompatible_protocol_version", err.Error())
		return
	}
	taskID := r.PathValue("id")
	current, err := s.tasks.Get(taskID)
	if err != nil {
		writeError(w, http.StatusNotFound, "task_not_found", err.Error())
		return
	}
	if current.Status != "accepted" {
		writeError(w, http.StatusConflict, "invalid_status_transition", fmt.Sprintf("task is %s, expected accepted", current.Status))
		return
	}
	if s.executor == nil {
		writeError(w, http.StatusServiceUnavailable, "executor_unavailable", "target executor is not configured")
		return
	}
	executionReq, err := s.executionRequest(r.Context(), current)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "execution_scope_unavailable", err.Error())
		return
	}
	updated, err := s.tasks.UpdateStatus(taskID, "running")
	if err != nil {
		writeError(w, http.StatusConflict, "invalid_status_transition", err.Error())
		return
	}
	handle, err := s.executor.Start(r.Context(), executionReq)
	if err != nil {
		_, _ = s.tasks.Complete(taskID, "failed", nil, "execution_start_failed")
		writeError(w, http.StatusBadGateway, "execution_start_failed", err.Error())
		return
	}
	s.executionsMu.Lock()
	s.executions[taskID] = handle
	s.executionsMu.Unlock()
	go s.finishExecution(taskID, handle)
	writeJSON(w, http.StatusOK, updated)
}

func (s *Server) handleEntrypointCancel(w http.ResponseWriter, r *http.Request) {
	var req models.CancelTaskRequest
	if !decodeJSONPost(w, r, &req) {
		return
	}
	if err := checkProtocolVersion(req.ProtocolVersion); err != nil {
		writeError(w, http.StatusBadRequest, "incompatible_protocol_version", err.Error())
		return
	}
	taskID := r.PathValue("id")
	t, err := s.tasks.Get(taskID)
	if err != nil {
		writeError(w, http.StatusNotFound, "task_not_found", err.Error())
		return
	}
	if t.IsTerminal() {
		writeJSON(w, http.StatusOK, t)
		return
	}
	s.executionsMu.Lock()
	handle := s.executions[taskID]
	delete(s.executions, taskID)
	s.executionsMu.Unlock()
	confirmed := handle == nil || handle.Cancel()
	status := "cancelled"
	if !confirmed {
		status = "outcome_unknown"
	}
	updated, err := s.tasks.UpdateStatusFrom(taskID, t.Status, status)
	if err != nil {
		writeError(w, http.StatusConflict, "invalid_status_transition", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, updated)
}

func (s *Server) executionRequest(ctx context.Context, t models.Task) (execution.Request, error) {
	messages, err := s.messages.ListByTask(ctx, t.TaskID)
	if err != nil {
		return execution.Request{}, fmt.Errorf("load delegated request: %w", err)
	}
	for i := len(messages) - 1; i >= 0; i-- {
		msg := messages[i]
		if msg.Role != "delegation" || len(msg.Parts) != 2 {
			continue
		}
		return execution.Request{
			TaskID:           t.TaskID,
			SessionID:        t.SessionID,
			InitiatorAgentID: t.InitiatorAgentID,
			TargetAgentID:    t.TargetAgentID,
			ToolName:         msg.Parts[0].Text,
			Arguments:        msg.Parts[1].Data,
		}, nil
	}
	return execution.Request{}, errors.New("delegated execution request not found")
}

func (s *Server) finishExecution(taskID string, handle execution.Handle) {
	result, ok := <-handle.Done()
	s.executionsMu.Lock()
	if s.executions[taskID] == handle {
		delete(s.executions, taskID)
	}
	s.executionsMu.Unlock()
	if !ok {
		return
	}
	if result.MayBeSent {
		_, _ = s.tasks.MarkOutcomeUnknown(taskID)
		return
	}
	_, _ = s.tasks.Complete(taskID, result.Status, result.Outcome, result.ErrorCode)
}

func (s *Server) handleEntrypointGet(w http.ResponseWriter, r *http.Request) {
	taskID := r.PathValue("id")
	t, err := s.tasks.Get(taskID)
	if err != nil {
		writeError(w, http.StatusNotFound, "task_not_found", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, t)
}

func (s *Server) handleEntrypointResults(w http.ResponseWriter, r *http.Request) {
	var req models.EntrypointResultRequest
	if !decodeJSONPost(w, r, &req) {
		return
	}
	if err := checkProtocolVersion(req.ProtocolVersion); err != nil {
		writeError(w, http.StatusBadRequest, "incompatible_protocol_version", err.Error())
		return
	}
	if req.Status != "completed" && req.Status != "failed" {
		writeError(w, http.StatusBadRequest, "invalid_status", "status must be completed or failed")
		return
	}
	taskID := r.PathValue("id")
	current, err := s.tasks.Get(taskID)
	if err != nil {
		writeError(w, http.StatusNotFound, "task_not_found", err.Error())
		return
	}
	if current.Status != "running" && current.Status != "outcome_unknown" {
		writeError(w, http.StatusConflict, "invalid_status_transition", fmt.Sprintf("task is %s, expected running or outcome_unknown", current.Status))
		return
	}
	updated, err := s.tasks.Complete(taskID, req.Status, req.Outcome, req.ErrorCode)
	if err != nil {
		writeError(w, http.StatusConflict, "invalid_status_transition", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, updated)
}

func (s *Server) validateDelegationToken(tokenStr string) (token.DelegationClaims, error) {
	var empty token.DelegationClaims
	if s.issuer == nil {
		return empty, errors.New("token issuer unavailable")
	}
	if tokenStr == "" {
		return empty, errors.New("delegation_token is required")
	}
	return s.issuer.Validate(tokenStr)
}

func (s *Server) verifyTokenMatchesTask(ctx context.Context, claims token.DelegationClaims, t models.Task) error {
	if claims.TaskID == "" || claims.TaskID != t.TaskID {
		return fmt.Errorf("token task_id mismatch")
	}
	if claims.InitiatorAgentID == "" || claims.InitiatorAgentID != t.InitiatorAgentID {
		return fmt.Errorf("token initiator mismatch")
	}
	if claims.TargetAgentID == "" || claims.TargetAgentID != t.TargetAgentID {
		return fmt.Errorf("token target mismatch")
	}
	if claims.Audience != t.TargetAgentID {
		return fmt.Errorf("token audience mismatch")
	}
	if claims.ToolName == "" || claims.ArgumentsSHA256 == "" {
		return fmt.Errorf("token execution scope missing")
	}
	messages, err := s.messages.ListByTask(ctx, t.TaskID)
	if err != nil {
		return fmt.Errorf("load task execution scope: %w", err)
	}
	for _, message := range messages {
		if message.Role != "delegation" || len(message.Parts) != 2 {
			continue
		}
		if message.Parts[0].Type == "text" && message.Parts[0].Text == claims.ToolName &&
			message.Parts[1].Type == "data" && token.HashArguments(message.Parts[1].Data) == claims.ArgumentsSHA256 {
			return nil
		}
	}
	return fmt.Errorf("token tool or arguments mismatch")
}

func (s *Server) verifyTokenMatchesRequest(claims token.DelegationClaims, req models.EntrypointTaskRequest) error {
	if claims.TaskID == "" || claims.TaskID != req.TaskID {
		return fmt.Errorf("token task_id mismatch")
	}
	if claims.InitiatorAgentID == "" || claims.InitiatorAgentID != req.InitiatorAgentID {
		return fmt.Errorf("token initiator mismatch")
	}
	if claims.TargetAgentID == "" || claims.TargetAgentID != req.TargetAgentID {
		return fmt.Errorf("token target mismatch")
	}
	if claims.ToolName == "" || claims.ToolName != req.ToolName {
		return fmt.Errorf("token tool_name mismatch")
	}
	if claims.Audience != req.TargetAgentID {
		return fmt.Errorf("token audience mismatch")
	}
	if claims.ArgumentsSHA256 == "" || claims.ArgumentsSHA256 != token.HashArguments(req.Arguments) {
		return fmt.Errorf("token arguments mismatch")
	}
	return nil
}

func (s *Server) requireTaskPending(taskID string) error {
	t, err := s.tasks.Get(taskID)
	if err != nil {
		return err
	}
	if t.Status != "pending" {
		return task.ErrInvalidStatus
	}
	return nil
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, status int, code, message string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(models.ErrorResponse{Error: message, Code: code})
}

// ListenAndServe starts the HTTP server on the given address.
func ListenAndServe(addr string, srv *Server) error {
	mux := http.NewServeMux()
	srv.RegisterRoutes(mux)
	return http.ListenAndServe(addr, mux)
}

// IsNotFound can be used by callers to detect missing resources.
func IsNotFound(err error) bool {
	return errors.Is(err, registry.ErrAgentNotFound) || errors.Is(err, task.ErrTaskNotFound)
}
