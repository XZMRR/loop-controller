// Package api exposes the Go kernel via HTTP/JSON.
package api

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"regexp"
	"strings"
	"time"

	"github.com/loop-controller/go/internal/delegation"
	"github.com/loop-controller/go/internal/discovery"
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
	registry   *registry.Registry
	tasks      *task.Manager
	router     *router.Router
	delegation *delegation.Delegator
	publisher  stream.TaskEventPublisher
	discovery  *discovery.Manager
	db         *store.DB
	issuer     *token.HMACIssuer
	messages   store.MessageStore
}

// currentProtocolVersion is the A2A HTTP/JSON protocol version implemented by
// this kernel. Patch differences are allowed; major/minor differences are
// fail-closed.
const (
	currentProtocolVersion = "0.39.0"
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
	tasks := task.New(db.TaskStore())
	r := router.New(reg)
	pub := stream.NewPublisher(db.EventStore())
	issuer := token.NewHMACIssuer(secret)
	d := delegation.New(reg, tasks, issuer, pub, 5*time.Minute)
	mgr := discovery.NewManager(reg, providers...)
	return &Server{
		registry:   reg,
		tasks:      tasks,
		router:     r,
		delegation: d,
		publisher:  pub,
		discovery:  mgr,
		db:         db,
		issuer:     issuer,
		messages:   db.MessageStore(),
	}, nil
}

// SetR2Authorizer injects an R2 authorizer into the delegator. When nil the
// delegator falls back to the local capability check only.
func (s *Server) SetR2Authorizer(a delegation.R2Authorizer) {
	s.delegation.WithR2Authorizer(a)
}

// Close releases database resources held by the server.
func (s *Server) Close() error {
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
	mux.HandleFunc("POST /a2a/v1/entrypoint/tasks", s.handleEntrypointCreate)
	mux.HandleFunc("POST /a2a/v1/entrypoint/tasks/{id}/accept", s.handleEntrypointAccept)
	mux.HandleFunc("POST /a2a/v1/entrypoint/tasks/{id}/cancel", s.handleEntrypointCancel)
	mux.HandleFunc("GET /a2a/v1/entrypoint/tasks/{id}", s.handleEntrypointGet)
	mux.HandleFunc("POST /a2a/v1/entrypoint/tasks/{id}/results", s.handleEntrypointResults)
	mux.HandleFunc("GET /health", s.handleHealth)
}

func (s *Server) handleTaskStream(w http.ResponseWriter, r *http.Request) {
	taskID := r.PathValue("id")
	if taskID == "" {
		writeError(w, http.StatusBadRequest, "task_id_required", "task_id is required")
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
		SessionID        string `json:"session_id"`
		InitiatorAgentID string `json:"initiator_agent_id"`
		TargetAgentID    string `json:"target_agent_id"`
	}
	if !decodeJSONPost(w, r, &req) {
		return
	}
	t := s.tasks.Create(req.SessionID, req.InitiatorAgentID, req.TargetAgentID)
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
	resp, err := s.delegation.Request(r.Context(), req)
	if err != nil {
		writeError(w, http.StatusBadRequest, "delegation_failed", err.Error())
		return
	}
	resp.ProtocolVersion = currentProtocolVersion
	status := http.StatusOK
	if !resp.Allowed {
		status = http.StatusForbidden
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
	t, err := s.tasks.Get(taskID)
	if err != nil {
		writeError(w, http.StatusNotFound, "task_not_found", err.Error())
		return
	}
	if t.IsTerminal() {
		writeJSON(w, http.StatusOK, t)
		return
	}
	// Single-instance mode: directly update the local task to cancelled. In a
	// multi-instance deployment this would forward to the target agent's
	// entrypoint cancel endpoint.
	updated, err := s.tasks.UpdateStatus(taskID, "cancelled")
	if err != nil {
		if errors.Is(err, task.ErrInvalidStatus) {
			writeError(w, http.StatusConflict, "invalid_status_transition", err.Error())
			return
		}
		writeError(w, http.StatusNotFound, "task_not_found", err.Error())
		return
	}
	_ = s.publisher.Publish(r.Context(), updated)
	writeJSON(w, http.StatusOK, updated)
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
		t, err = s.tasks.CreateWithID(req.TaskID, req.SessionID, req.InitiatorAgentID, req.TargetAgentID)
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

	_ = s.publisher.Publish(ctx, t)
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
	_ = s.publisher.Publish(r.Context(), updated)
	writeJSON(w, http.StatusOK, updated)
}

func (s *Server) handleEntrypointCancel(w http.ResponseWriter, r *http.Request) {
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
	updated, err := s.tasks.UpdateStatus(taskID, "cancelled")
	if err != nil {
		writeError(w, http.StatusConflict, "invalid_status_transition", err.Error())
		return
	}
	_ = s.publisher.Publish(r.Context(), updated)
	writeJSON(w, http.StatusOK, updated)
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
	_ = s.publisher.Publish(r.Context(), updated)
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

func (s *Server) verifyTokenMatchesRequest(claims token.DelegationClaims, req models.EntrypointTaskRequest) error {
	if claims.TaskID != "" && claims.TaskID != req.TaskID {
		return fmt.Errorf("token task_id mismatch")
	}
	if claims.InitiatorAgentID != "" && claims.InitiatorAgentID != req.InitiatorAgentID {
		return fmt.Errorf("token initiator mismatch")
	}
	if claims.TargetAgentID != "" && claims.TargetAgentID != req.TargetAgentID {
		return fmt.Errorf("token target mismatch")
	}
	if claims.ToolName != "" && claims.ToolName != req.ToolName {
		return fmt.Errorf("token tool_name mismatch")
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
