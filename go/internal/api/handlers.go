// Package api exposes the Go kernel via HTTP/JSON.
package api

import (
	"bytes"
	"context"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"regexp"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/loop-controller/go/internal/delegation"
	"github.com/loop-controller/go/internal/discovery"
	"github.com/loop-controller/go/internal/entrypointpolicy"
	"github.com/loop-controller/go/internal/execution"
	"github.com/loop-controller/go/internal/models"
	"github.com/loop-controller/go/internal/observability"
	"github.com/loop-controller/go/internal/registry"
	"github.com/loop-controller/go/internal/router"
	"github.com/loop-controller/go/internal/store"
	"github.com/loop-controller/go/internal/stream"
	"github.com/loop-controller/go/internal/task"
	"github.com/loop-controller/go/internal/token"
)

// Server holds the kernel state.
type executionSlot struct {
	starting         bool
	handle           execution.Handle
	cancelRequested  bool
	cancelRequestedC chan struct{}
	ready            chan struct{}
}

type DurableAssignmentCanceler interface {
	Cancel(taskID string) bool
}

type Server struct {
	registry            *registry.Registry
	tasks               *task.Manager
	router              *router.Router
	delegation          *delegation.Delegator
	publisher           stream.TaskEventPublisher
	discovery           *discovery.Manager
	db                  *store.DB
	issuer              *token.HMACIssuer
	messages            store.MessageStore
	idempotency         store.IdempotencyStore
	entrypointClient    delegation.EntrypointClient
	executor            execution.TargetExecutor
	strictExecution     bool
	entrypointPolicy    entrypointpolicy.Policy
	receiptValidator    *execution.HTTPExecutor
	autoAcceptTarget    bool
	autoStartTarget     bool
	durableAssignments  bool
	schedulingEnabled   bool
	dagEnabled          bool
	durableCanceler     DurableAssignmentCanceler
	executionsMu        sync.Mutex
	executions          map[string]*executionSlot
	outboxCancel        context.CancelFunc
	approvalAuditCancel context.CancelFunc
	notificationCancel  context.CancelFunc
	recoveryCancel      context.CancelFunc
	backgroundWG        sync.WaitGroup
	controlToken        string
	controlInitiatorID  string
	controlTenantID     string
	approvalAuth        approvalAuthConfig
	streamConfig        stream.Config
	readiness           ReadinessConfig
	assignmentWorker    observability.StateProvider
	outboundWorker      observability.StateProvider
	recoveryState       observability.State
	closed              atomic.Bool
	closeOnce           sync.Once
	closeErr            error
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
	parts := strings.Split(v, ".")
	minor := parts[0] + "." + parts[1]
	if minor != "0.53" && minor != "0.54" {
		return fmt.Errorf("incompatible protocol version %q, expected 0.53.x or 0.54.x", v)
	}
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
	reg := registry.NewStore(db.AgentStore())
	pub := stream.NewPublisher(db.EventStore()).WithInstanceID(db.InstanceID())
	tasks := task.New(db.TaskStore()).WithEventFanout(pub).WithInstanceID(db.InstanceID())
	r := router.New(reg, db.RoutedMessageStore())
	issuer := token.NewHMACIssuer(secret)
	d := delegation.New(reg, tasks, issuer, pub, 5*time.Minute)
	mgr := discovery.NewManager(reg, providers...)
	srv := &Server{
		registry:         reg,
		tasks:            tasks,
		router:           r,
		delegation:       d,
		publisher:        pub,
		discovery:        mgr,
		db:               db,
		issuer:           issuer,
		messages:         db.MessageStore(),
		idempotency:      db.IdempotencyStore(),
		executions:       make(map[string]*executionSlot),
		streamConfig:     stream.DefaultConfig(),
		readiness:        DefaultReadinessConfig(),
		entrypointPolicy: entrypointpolicy.Development(),
	}
	srv.startRecoveryLoop()
	return srv, nil
}

// startRecoveryLoop periodically reclaims running tasks whose execution lease
// expired, which typically means the owning instance crashed or lost contact.
func (s *Server) startRecoveryLoop() {
	s.recoveryState.Start()
	interval := s.db.ExecutionLease() / 3
	if interval <= 0 {
		interval = time.Second
	}
	ctx, cancel := context.WithCancel(context.Background())
	s.recoveryCancel = cancel
	s.backgroundWG.Add(1)
	go func() {
		defer s.backgroundWG.Done()
		s.runRecoveryLoop(ctx, interval)
	}()
}

func (s *Server) runRecoveryLoop(ctx context.Context, interval time.Duration) {
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	recover := func() {
		now := time.Now().UTC()
		_, recoverErr := s.tasks.RecoverExpiredRunning(ctx, now, 100)
		s.recoveryState.Recovery(now, recoverErr)
		if recoverErr != nil {
			observability.Default.Add("assignment_recovery_errors_total", 1, "task_execution")
		} else {
			observability.Default.Set("assignment_recovery_last_success_unixtime", float64(now.Unix()), "task_execution")
		}
		_, _ = s.db.DelegationApprovalStore().ExpireDue(ctx, now, 100)
	}
	recover()
	for {
		select {
		case <-ctx.Done():
			s.recoveryState.Stop()
			return
		case <-ticker.C:
			recover()
		}
	}
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
		s.backgroundWG.Add(1)
		go func() {
			defer s.backgroundWG.Done()
			dispatcher.Run(ctx)
		}()
	}
	if s.approvalAuditCancel != nil {
		s.approvalAuditCancel()
		s.approvalAuditCancel = nil
	}
	if auditor, ok := a.(store.ApprovalAuditor); ok {
		ctx, cancel := context.WithCancel(context.Background())
		s.approvalAuditCancel = cancel
		dispatcher := store.NewApprovalAuditOutboxDispatcher(s.db.ApprovalAuditOutboxStore(), auditor, time.Second)
		s.backgroundWG.Add(1)
		go func() {
			defer s.backgroundWG.Done()
			dispatcher.Run(ctx)
		}()
	}
}

// SetApprovalNotifier enables or pauses webhook delivery. Pausing preserves existing queued rows.
func (s *Server) SetApprovalNotifier(notifier store.ApprovalNotifier) {
	if s.notificationCancel != nil {
		s.notificationCancel()
		s.notificationCancel = nil
	}
	destination := ""
	if notifier != nil {
		destination = notifier.DestinationURL()
	}
	s.db.SetApprovalNotificationDestination(destination)
	if notifier != nil {
		ctx, cancel := context.WithCancel(context.Background())
		s.notificationCancel = cancel
		dispatcher := store.NewApprovalNotificationDispatcher(s.db.ApprovalNotificationOutboxStore(), notifier, time.Second)
		s.backgroundWG.Add(1)
		go func() {
			defer s.backgroundWG.Done()
			dispatcher.Run(ctx)
		}()
	}
}

// SetEntrypointClient enables delivery of authorized tasks to target Agents.
func (s *Server) SetEntrypointClient(client delegation.EntrypointClient) {
	if httpClient, ok := client.(*delegation.HTTPEntrypointClient); ok {
		httpClient.Policy = s.entrypointPolicy
	}
	s.entrypointClient = client
	s.delegation.WithEntrypointClient(client)
}

func (s *Server) EnableDurableOutboundDelegation() {
	s.delegation.WithOutboundQueue(s.db.DelegationDispatchOutboxStore())
}

// SetTargetExecutor configures execution of accepted target-side tasks.
func (s *Server) SetTargetExecutor(executor execution.TargetExecutor) {
	s.executor = executor
	if httpExecutor, ok := executor.(*execution.HTTPExecutor); ok {
		s.strictExecution = httpExecutor.Strict
		s.delegation.WithStrict(httpExecutor.Strict)
		s.receiptValidator = httpExecutor
	}
}

// SetControlAuth enables Bearer authentication on initiator-facing task APIs.
// Empty values keep authentication disabled for tests and local embedding.
func (s *Server) SetControlAuth(controlToken, initiatorAgentID string) {
	s.SetControlAuthForTenant(controlToken, initiatorAgentID, "")
}

// SetControlAuthForTenant binds the authenticated control principal to a tenant.
func (s *Server) SetControlAuthForTenant(controlToken, initiatorAgentID, tenantID string) {
	s.controlToken = strings.TrimSpace(controlToken)
	s.controlInitiatorID = strings.TrimSpace(initiatorAgentID)
	s.controlTenantID = strings.TrimSpace(tenantID)
}

// SetTargetTaskAutomation configures automatic target-side acceptance and execution.
func (s *Server) SetTargetTaskAutomation(autoAccept, autoStart bool) {
	s.autoAcceptTarget = autoAccept || autoStart
	s.autoStartTarget = autoStart
}

func (s *Server) EnableDurableAssignments(canceler DurableAssignmentCanceler) {
	s.durableAssignments = true
	s.durableCanceler = canceler
}

func (s *Server) SetEntrypointPolicy(policy entrypointpolicy.Policy) { s.entrypointPolicy = policy }
func (s *Server) EnableScheduling()                                  { s.schedulingEnabled = true }
func (s *Server) EnableDAG()                                         { s.dagEnabled = true }

func (s *Server) SetStreamConfig(cfg stream.Config) {
	s.streamConfig = cfg
	if publisher, ok := s.publisher.(*stream.SQLitePublisher); ok {
		publisher.WithConfig(cfg)
	}
}

func (s *Server) Store() *store.DB { return s.db }

func (s *Server) ExecutionLoader() func(context.Context, models.Task) (execution.Request, error) {
	return s.executionRequest
}

// Close releases streaming, background, and database resources held by the server.
func (s *Server) Close() error {
	s.closeOnce.Do(func() {
		s.closed.Store(true)
		s.recoveryState.Stop()
		if s.discovery != nil {
			s.discovery.Close()
		}
		if publisher, ok := s.publisher.(*stream.SQLitePublisher); ok {
			_ = publisher.Close()
			publisher.Wait()
		}
		if s.recoveryCancel != nil {
			s.recoveryCancel()
		}
		if s.outboxCancel != nil {
			s.outboxCancel()
		}
		if s.approvalAuditCancel != nil {
			s.approvalAuditCancel()
		}
		if s.notificationCancel != nil {
			s.notificationCancel()
		}
		s.backgroundWG.Wait()
		s.approvalAuth = approvalAuthConfig{}
		s.controlToken = ""
		if s.db != nil {
			s.closeErr = s.db.Close()
		}
	})
	return s.closeErr
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
	mux.HandleFunc("POST /a2a/v1/agents", s.withControlAuth(true, s.handleRegisterAgent))
	mux.HandleFunc("GET /a2a/v1/agents", s.withControlAuth(true, s.handleListAgents))
	mux.HandleFunc("GET /a2a/v1/agents/{id}", s.withControlAuth(true, s.handleGetAgent))
	mux.HandleFunc("POST /a2a/v1/tasks", s.withControlAuth(true, s.handleCreateTask))
	mux.HandleFunc("GET /a2a/v1/tasks", s.withControlAuth(true, s.handleListTasks))
	mux.HandleFunc("GET /a2a/v1/tasks/{id}", s.withControlAuth(false, s.handleGetTask))
	mux.HandleFunc("GET /a2a/v1/tasks/{id}/snapshot", s.withControlAuth(false, s.handleTaskSnapshot))
	mux.HandleFunc("GET /a2a/v1/tasks/{id}/stream", s.withControlAuth(false, s.handleTaskStream))
	mux.HandleFunc("POST /a2a/v1/messages", s.handleSendMessage)
	mux.HandleFunc("POST /a2a/v1/delegations", s.withControlAuth(true, s.handleDelegation))
	mux.HandleFunc("GET /a2a/v1/delegation-approvals/{id}", s.withInitiatorApprovalAuth(s.handleGetDelegationApproval))
	mux.HandleFunc("POST /a2a/v1/delegation-approvals/{id}/approve", s.withApproverAuth(s.handleApproveDelegation))
	mux.HandleFunc("POST /a2a/v1/delegation-approvals/{id}/reject", s.withApproverAuth(s.handleRejectDelegation))
	mux.HandleFunc("POST /a2a/v1/delegation-approvals/{id}/cancel", s.withInitiatorApprovalAuth(s.handleCancelDelegation))
	mux.HandleFunc("POST /a2a/v1/tasks/{id}/cancel", s.withControlAuth(false, s.handleCancelTask))
	mux.HandleFunc("GET /a2a/v1/dead-letters", s.withControlAuth(true, s.handleListDeadLetters))
	mux.HandleFunc("POST /a2a/v1/dead-letters/{id}/replay", s.withControlAuth(true, s.handleReplayDeadLetter))
	if s.dagEnabled {
		mux.HandleFunc("POST /a2a/v1/task-graphs", s.withControlAuth(true, s.handleCreateGraph))
		mux.HandleFunc("GET /a2a/v1/task-graphs/{id}", s.withControlAuth(true, s.handleGetGraph))
		mux.HandleFunc("POST /a2a/v1/task-graphs/{id}/cancel", s.withControlAuth(true, s.handleCancelGraph))
	}
	mux.HandleFunc("POST /a2a/v1/entrypoint/tasks", s.withEntrypointToken("create", true, s.handleEntrypointCreate))
	mux.HandleFunc("POST /a2a/v1/entrypoint/tasks/{id}/accept", s.withEntrypointToken("accept", true, s.handleEntrypointAccept))
	mux.HandleFunc("POST /a2a/v1/entrypoint/tasks/{id}/start", s.withEntrypointToken("start", true, s.handleEntrypointStart))
	mux.HandleFunc("POST /a2a/v1/entrypoint/tasks/{id}/cancel", s.withEntrypointToken("cancel", true, s.handleEntrypointCancel))
	mux.HandleFunc("GET /a2a/v1/entrypoint/tasks/{id}", s.withEntrypointToken("get", false, s.handleEntrypointGet))
	mux.HandleFunc("POST /a2a/v1/entrypoint/tasks/{id}/results", s.withEntrypointToken("results", true, s.handleEntrypointResults))
	mux.HandleFunc("GET /health", s.handleHealth)
	mux.HandleFunc("GET /ready", s.handleReady)
	mux.HandleFunc("GET /metrics", s.handleMetrics)
}

func (s *Server) withControlAuth(bindRequestInitiator bool, next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if s.controlToken == "" {
			next(w, r)
			return
		}
		tokenStr, err := bearerToken(r.Header.Get("Authorization"))
		if err != nil || subtle.ConstantTimeCompare([]byte(tokenStr), []byte(s.controlToken)) != 1 {
			writeError(w, http.StatusUnauthorized, "invalid_control_token", "valid Bearer control token is required")
			return
		}
		if s.controlInitiatorID == "" {
			writeError(w, http.StatusInternalServerError, "control_auth_misconfigured", "control initiator_agent_id is required")
			return
		}
		if !bindRequestInitiator {
			var t models.Task
			if s.controlTenantID != "" {
				t, err = s.db.TaskStore().GetForTenant(r.Context(), s.controlTenantID, r.PathValue("id"))
			} else {
				t, err = s.tasks.Get(r.PathValue("id"))
			}
			if err != nil {
				writeError(w, http.StatusNotFound, "task_not_found", "task not found")
				return
			}
			if t.InitiatorAgentID != s.controlInitiatorID {
				if s.controlTenantID != "" {
					writeError(w, http.StatusNotFound, "task_not_found", "task not found")
				} else {
					writeError(w, http.StatusForbidden, "task_access_denied", "task is owned by another initiator")
				}
				return
			}
		}
		next(w, r)
	}
}

func (s *Server) controlTenant(requestTenant string) (string, bool) {
	requestTenant = strings.TrimSpace(requestTenant)
	if s.controlTenantID != "" {
		return s.controlTenantID, requestTenant == "" || requestTenant == s.controlTenantID
	}
	// An unscoped control principal is an explicitly configured global/development authority.
	return requestTenant, true
}

func (s *Server) handleListDeadLetters(w http.ResponseWriter, r *http.Request) {
	tenant, ok := s.controlTenant(r.URL.Query().Get("tenant_id"))
	if !ok {
		writeError(w, http.StatusNotFound, "resource_not_found", "resource not found")
		return
	}
	items, err := s.db.AssignmentStore().ListDeadLetters(r.Context(), tenant, 100)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "dead_letter_query_failed", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"assignments": items})
}

func (s *Server) handleReplayDeadLetter(w http.ResponseWriter, r *http.Request) {
	var req struct {
		TenantID         string     `json:"tenant_id"`
		ExpectedRevision int64      `json:"expected_revision"`
		NotBefore        *time.Time `json:"not_before,omitempty"`
	}
	if !decodeJSONPost(w, r, &req) {
		return
	}
	var ok bool
	req.TenantID, ok = s.controlTenant(req.TenantID)
	if !ok {
		writeError(w, http.StatusNotFound, "resource_not_found", "resource not found")
		return
	}
	if req.ExpectedRevision <= 0 {
		writeError(w, http.StatusBadRequest, "dead_letter_replay_invalid", "positive expected_revision is required")
		return
	}
	correlationID := strings.TrimSpace(r.Header.Get("X-Correlation-ID"))
	assignment, err := s.db.AssignmentStore().ReplayDeadLetter(r.Context(), store.DeadLetterReplayParams{TenantID: req.TenantID, AssignmentID: r.PathValue("id"), ExpectedRevision: req.ExpectedRevision, NotBefore: req.NotBefore, Actor: s.controlInitiatorID, CorrelationID: correlationID, Now: time.Now().UTC()})
	if err != nil {
		status := http.StatusConflict
		if errors.Is(err, store.ErrAssignmentNotFound) {
			status = http.StatusNotFound
		}
		writeError(w, status, "dead_letter_replay_failed", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, assignment)
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
	if err := stream.ServeTaskStreamWithConfig(s.publisher, w, r, taskID, s.streamConfig); err != nil {
		return
	}
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
	if card.ProtocolVersion == "" {
		card.ProtocolVersion = currentProtocolVersion
	}
	if err := checkProtocolVersion(card.ProtocolVersion); err != nil {
		writeError(w, http.StatusBadRequest, "incompatible_protocol_version", err.Error())
		return
	}
	var ok bool
	card.TenantID, ok = s.controlTenant(card.TenantID)
	if !ok {
		writeError(w, http.StatusNotFound, "resource_not_found", "resource not found")
		return
	}
	if card.Entrypoint.URL != "" {
		if err := s.entrypointPolicy.ValidateRegistration(r.Context(), card.Entrypoint.URL); err != nil {
			writeError(w, http.StatusBadRequest, "entrypoint_url_rejected", "entrypoint URL is not approved")
			return
		}
	}
	if err := s.registry.Register(card); err != nil {
		writeError(w, http.StatusBadRequest, "register_failed", err.Error())
		return
	}
	writeJSON(w, http.StatusCreated, models.AgentRegistrationResponse{ProtocolVersion: currentProtocolVersion, AgentID: card.AgentID})
}

func (s *Server) handleListAgents(w http.ResponseWriter, r *http.Request) {
	tenant := ""
	if s.controlToken != "" {
		tenant = s.controlTenantID
	}
	agents, err := s.registry.ListContext(r.Context(), tenant)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "agent_list_failed", "failed to list agents")
		return
	}
	for i := range agents {
		if agents[i].ProtocolVersion == "" {
			agents[i].ProtocolVersion = currentProtocolVersion
		}
	}
	writeJSON(w, http.StatusOK, models.AgentList{ProtocolVersion: currentProtocolVersion, Agents: agents})
}

func (s *Server) handleGetAgent(w http.ResponseWriter, r *http.Request) {
	agentID := r.PathValue("id")
	tenant := ""
	if s.controlToken != "" {
		tenant = s.controlTenantID
	}
	card, err := s.registry.GetContext(r.Context(), tenant, agentID)
	if err != nil {
		writeError(w, http.StatusNotFound, "agent_not_found", "agent not found")
		return
	}
	if card.ProtocolVersion == "" {
		card.ProtocolVersion = currentProtocolVersion
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
	if s.controlToken != "" && req.InitiatorAgentID != s.controlInitiatorID {
		writeError(w, http.StatusForbidden, "initiator_mismatch", "initiator_agent_id does not match authenticated principal")
		return
	}
	t, err := s.tasks.CreateInteractionTask(models.Task{
		SessionID:        req.SessionID,
		TenantID:         s.controlTenantID,
		InitiatorAgentID: req.InitiatorAgentID,
		TargetAgentID:    req.TargetAgentID,
	})
	if err != nil {
		writeError(w, http.StatusInternalServerError, "task_create_failed", err.Error())
		return
	}
	writeJSON(w, http.StatusCreated, t)
}

func (s *Server) handleListTasks(w http.ResponseWriter, r *http.Request) {
	rootOnly := false
	if values, present := r.URL.Query()["root_only"]; present {
		if len(values) != 1 || (values[0] != "true" && values[0] != "false") {
			writeError(w, http.StatusBadRequest, "invalid_root_only", "root_only must be true or false")
			return
		}
		rootOnly = values[0] == "true"
	}
	tasks, err := s.tasks.ListForTenantInitiator(r.Context(), s.controlTenantID, s.controlInitiatorID, rootOnly)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "task_list_failed", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"protocol_version": models.CurrentProtocolVersion,
		"tasks":            tasks,
	})
}

func (s *Server) handleGetTask(w http.ResponseWriter, r *http.Request) {
	taskID := r.PathValue("id")
	var t models.Task
	var err error
	if s.controlToken != "" && s.controlTenantID != "" {
		t, err = s.db.TaskStore().GetForTenant(r.Context(), s.controlTenantID, taskID)
	} else {
		t, err = s.tasks.Get(taskID)
	}
	if err != nil {
		writeError(w, http.StatusNotFound, "task_not_found", "task not found")
		return
	}
	writeJSON(w, http.StatusOK, t)
}

func (s *Server) handleTaskSnapshot(w http.ResponseWriter, r *http.Request) {
	tenant, ok := s.controlTenant(r.URL.Query().Get("tenant_id"))
	if !ok {
		writeError(w, http.StatusNotFound, "task_not_found", "task not found")
		return
	}
	snapshot, err := s.db.SnapshotStore().GetTaskSnapshot(r.Context(), tenant, r.PathValue("id"))
	if err != nil {
		writeError(w, http.StatusNotFound, "task_not_found", "task not found")
		return
	}
	writeJSON(w, http.StatusOK, snapshot)
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
	resp.ProtocolVersion = currentProtocolVersion
	if err != nil {
		writeJSON(w, http.StatusBadRequest, resp)
		return
	}
	writeJSON(w, http.StatusOK, resp)
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
	if s.controlToken != "" && req.InitiatorAgentID != s.controlInitiatorID {
		writeError(w, http.StatusForbidden, "initiator_mismatch", "initiator_agent_id does not match authenticated principal")
		return
	}
	var tenantOK bool
	req.TenantID, tenantOK = s.controlTenant(req.TenantID)
	if !tenantOK {
		writeError(w, http.StatusNotFound, "resource_not_found", "resource not found")
		return
	}
	if s.strictExecution && (req.TenantID == "" || req.TargetWorkloadID == "") {
		writeError(w, http.StatusBadRequest, "delegation_binding_required", "strict delegation requires tenant_id and target_workload_id")
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
		errorResponse := models.ErrorResponse{ProtocolVersion: currentProtocolVersion, Error: delegationErr.Error(), Code: "delegation_failed"}
		body, _ := json.Marshal(errorResponse)
		_ = s.idempotency.Complete(r.Context(), req.RequestID, scope, http.StatusBadRequest, body)
		writeJSON(w, http.StatusBadRequest, errorResponse)
		return
	}
	resp.ProtocolVersion = currentProtocolVersion
	status := http.StatusOK
	switch {
	case resp.Verdict == "require_approval":
		status = http.StatusAccepted
	case !resp.Allowed:
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
	// 未配置控制面认证时，保持兼容：若携带 delegation token 则校验任务绑定。
	if auth := r.Header.Get("Authorization"); s.controlToken == "" && auth != "" {
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
	descendants, err := s.tasks.ListDescendants(r.Context(), taskID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "descendant_lookup_failed", err.Error())
		return
	}
	for _, descendant := range descendants {
		if descendant.IsTerminal() {
			continue
		}
		_, _ = s.cancelTaskExecution(r.Context(), descendant)
	}
	updated, err := s.cancelTaskExecution(r.Context(), t)
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

func (s *Server) cancelTaskExecution(ctx context.Context, t models.Task) (models.Task, error) {
	if a, err := s.db.AssignmentStore().Get(ctx, "outbound-assignment-"+t.TaskID); err == nil && a.Kind == models.AssignmentKindOutboundDelegation {
		updated, _, cancelErr := s.db.ExecutionQueueStore().CancelAssignmentAndRelease(ctx, store.CancelAssignmentParams{Kind: a.Kind, AssignmentID: a.AssignmentID, ExpectedRevision: a.Revision, ExpectedAttempt: a.Attempt, ExpectedFence: a.ExecutionFence, Now: time.Now().UTC()})
		if cancelErr != nil {
			return models.Task{}, cancelErr
		}
		if s.entrypointClient != nil && (a.State == models.AssignmentStateDispatched || a.State == models.AssignmentStateSettled) {
			_, _ = s.delegation.Cancel(ctx, t.TargetAgentID, t.TaskID, t.DelegationToken)
		}
		return updated, nil
	} else if err != nil && !errors.Is(err, store.ErrAssignmentNotFound) {
		return models.Task{}, err
	}
	confirmed := true
	if s.entrypointClient != nil {
		var cancelErr error
		confirmed, cancelErr = s.delegation.Cancel(ctx, t.TargetAgentID, t.TaskID, t.DelegationToken)
		if cancelErr != nil {
			confirmed = false
		}
	}
	if confirmed || t.Status != "running" {
		return s.tasks.UpdateStatusFrom(t.TaskID, t.Status, "cancelled")
	}
	return s.tasks.MarkOutcomeUnknown(t.TaskID)
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
		t, err = s.tasks.CreateWithDelegation(
			req.TaskID, req.SessionID, req.InitiatorAgentID, req.TargetAgentID,
			req.InteractionID, req.DecisionID, req.RootInteractionID, req.ParentInteractionID,
			req.RootTaskID, req.ParentTaskID, req.DelegationDepth, req.Deadline,
			req.AllowedTools, req.AllowedCapabilities, req.AllowRedelegation, req.Budget,
			req.RequestID, req.TenantID, req.TargetWorkloadID, req.TargetInstanceID,
		)
		if err != nil {
			writeError(w, http.StatusInternalServerError, "task_create_failed", err.Error())
			return
		}
	}
	if t.SessionID != req.SessionID || t.InteractionID != req.InteractionID || t.DecisionID != req.DecisionID ||
		t.RootInteractionID != req.RootInteractionID || t.ParentInteractionID != req.ParentInteractionID ||
		t.RootTaskID != req.RootTaskID || t.ParentTaskID != req.ParentTaskID || t.DelegationDepth != req.DelegationDepth ||
		t.InitiatorAgentID != req.InitiatorAgentID || t.TargetAgentID != req.TargetAgentID ||
		t.RequestID != req.RequestID || t.TenantID != req.TenantID || t.TargetWorkloadID != req.TargetWorkloadID || t.TargetInstanceID != req.TargetInstanceID ||
		!sameScope(t.AllowedTools, req.AllowedTools) || !sameScope(t.AllowedCapabilities, req.AllowedCapabilities) ||
		t.AllowRedelegation != req.AllowRedelegation || t.Budget != req.Budget || !sameDeadline(t.Deadline, req.Deadline) {
		writeError(w, http.StatusForbidden, "entrypoint_task_mismatch", "existing task does not match entrypoint request")
		return
	}
	if t.Status != "pending" {
		writeError(w, http.StatusConflict, "invalid_status_transition", fmt.Sprintf("task is %s, expected pending", t.Status))
		return
	}
	if err := s.tasks.SetDelegationToken(req.TaskID, req.DelegationToken); err != nil {
		writeError(w, http.StatusInternalServerError, "delegation_scope_persist_failed", err.Error())
		return
	}
	t.DelegationToken = req.DelegationToken

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
	if s.autoAcceptTarget {
		t, err = s.tasks.UpdateStatus(req.TaskID, "accepted")
		if err != nil {
			writeError(w, http.StatusInternalServerError, "auto_accept_failed", err.Error())
			return
		}
	}
	if s.autoStartTarget {
		t, err = s.startTargetExecution(ctx, t)
		if err != nil {
			writeError(w, http.StatusServiceUnavailable, "auto_start_failed", err.Error())
			return
		}
	}

	writeJSON(w, http.StatusCreated, t)
}

func (s *Server) handleEntrypointAccept(w http.ResponseWriter, r *http.Request) {
	taskID := r.PathValue("id")
	if expired, err := s.rejectExpiredTask(w, taskID); err != nil || expired {
		return
	}
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
	if current.Status != "accepted" && !(s.durableAssignments && current.Status == "running") {
		writeError(w, http.StatusConflict, "invalid_status_transition", fmt.Sprintf("task is %s, expected accepted", current.Status))
		return
	}
	if current.Deadline != nil && !current.Deadline.After(time.Now().UTC()) {
		_, _ = s.tasks.UpdateStatusFrom(taskID, current.Status, "cancelled")
		writeError(w, http.StatusConflict, "task_deadline_expired", "task deadline has expired")
		return
	}
	updated, err := s.startTargetExecution(r.Context(), current)
	if err != nil {
		writeError(w, http.StatusBadGateway, "execution_start_failed", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, updated)
}

func (s *Server) startTargetExecution(ctx context.Context, current models.Task) (models.Task, error) {
	if current.Deadline != nil && !current.Deadline.After(time.Now().UTC()) {
		return models.Task{}, errors.New("task deadline has expired")
	}
	if s.durableAssignments {
		return s.enqueueTargetExecution(ctx, current)
	}
	if s.executor == nil {
		return models.Task{}, errors.New("target executor is not configured")
	}
	executionReq, err := s.executionRequest(ctx, current)
	if err != nil {
		return models.Task{}, err
	}
	slot := &executionSlot{starting: true, cancelRequestedC: make(chan struct{}), ready: make(chan struct{})}
	s.executionsMu.Lock()
	if _, exists := s.executions[current.TaskID]; exists {
		s.executionsMu.Unlock()
		return models.Task{}, errors.New("target execution is already starting or running")
	}
	s.executions[current.TaskID] = slot
	s.executionsMu.Unlock()
	updated, err := s.tasks.UpdateStatus(current.TaskID, "running")
	if err != nil {
		s.executionsMu.Lock()
		if s.executions[current.TaskID] == slot {
			delete(s.executions, current.TaskID)
		}
		slot.starting = false
		close(slot.ready)
		s.executionsMu.Unlock()
		return models.Task{}, err
	}

	handle, err := s.executor.Start(ctx, executionReq)
	if err != nil {
		s.executionsMu.Lock()
		if s.executions[current.TaskID] == slot {
			delete(s.executions, current.TaskID)
		}
		cancelRequested := slot.cancelRequested
		s.executionsMu.Unlock()
		if cancelRequested {
			_, _ = s.tasks.UpdateStatusFrom(current.TaskID, "running", "cancelled")
		} else {
			_, _ = s.tasks.Complete(current.TaskID, "failed", nil, "execution_start_failed")
		}
		s.executionsMu.Lock()
		slot.starting = false
		close(slot.ready)
		s.executionsMu.Unlock()
		return models.Task{}, err
	}

	s.executionsMu.Lock()
	cancelRequested := slot.cancelRequested
	if cancelRequested {
		delete(s.executions, current.TaskID)
	} else {
		slot.starting = false
		slot.handle = handle
		close(slot.ready)
	}
	s.executionsMu.Unlock()
	if cancelRequested {
		confirmed := handle.Cancel()
		status := "cancelled"
		if !confirmed {
			status = "outcome_unknown"
		}
		cancelled, transitionErr := s.tasks.UpdateStatusFrom(current.TaskID, "running", status)
		s.executionsMu.Lock()
		slot.starting = false
		close(slot.ready)
		s.executionsMu.Unlock()
		if transitionErr != nil {
			return models.Task{}, transitionErr
		}
		return cancelled, nil
	}
	go s.finishExecution(current.TaskID, slot, handle)
	return updated, nil
}

func (s *Server) enqueueTargetExecution(ctx context.Context, current models.Task) (models.Task, error) {
	assignmentID := "assignment-" + current.TaskID
	if s.schedulingEnabled {
		_, err := s.db.SchedulingStore().ScheduleAndReserve(ctx, store.ScheduleAndReserveParams{
			Request:       models.SchedulingRequest{RequestID: "execution-" + current.TaskID, TenantID: current.TenantID, TaskID: current.TaskID, RequiredAgentCapabilities: current.AllowedCapabilities, RequiredTools: current.AllowedTools, Deadline: current.Deadline, BudgetEnvelope: current.Budget},
			PinnedAgentID: current.TargetAgentID, ProtocolVersion: currentProtocolVersion, WorkloadID: current.TargetWorkloadID,
			AssignmentID: assignmentID, DeliveryID: "delivery-" + current.TaskID, CapacityUnits: 1,
		})
		if err != nil {
			return models.Task{}, err
		}
		return s.tasks.Get(current.TaskID)
	}
	updated, _, err := s.db.ExecutionQueueStore().EnqueueAcceptedTask(ctx, store.EnqueueAcceptedTaskParams{
		Kind:         models.AssignmentKindTargetExecution,
		AssignmentID: assignmentID, DeliveryID: "delivery-" + current.TaskID,
		IdempotencyKey: "execution-" + current.TaskID, TaskID: current.TaskID,
		TenantID: current.TenantID, TargetAgentID: current.TargetAgentID, Deadline: current.Deadline,
	})
	return updated, err
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
	if s.durableAssignments && t.Status == "running" {
		a, getErr := s.db.AssignmentStore().Get(r.Context(), "assignment-"+taskID)
		if getErr == nil {
			if s.durableCanceler != nil {
				s.durableCanceler.Cancel(taskID)
			}
			updated, _, cancelErr := s.db.ExecutionQueueStore().CancelAssignmentAndRelease(r.Context(), store.CancelAssignmentParams{Kind: models.AssignmentKindTargetExecution, AssignmentID: a.AssignmentID, ExpectedRevision: a.Revision, ExpectedAttempt: a.Attempt, ExpectedFence: a.ExecutionFence, Now: time.Now().UTC()})
			if cancelErr != nil {
				writeError(w, http.StatusConflict, "invalid_status_transition", cancelErr.Error())
				return
			}
			writeJSON(w, http.StatusOK, updated)
			return
		}
	}
	s.executionsMu.Lock()
	slot := s.executions[taskID]
	if slot != nil && slot.starting {
		if !slot.cancelRequested {
			slot.cancelRequested = true
			close(slot.cancelRequestedC)
		}
		ready := slot.ready
		s.executionsMu.Unlock()
		select {
		case <-ready:
		case <-r.Context().Done():
			writeError(w, http.StatusRequestTimeout, "cancel_interrupted", r.Context().Err().Error())
			return
		}
		updated, getErr := s.tasks.Get(taskID)
		if getErr != nil {
			writeError(w, http.StatusNotFound, "task_not_found", getErr.Error())
			return
		}
		writeJSON(w, http.StatusOK, updated)
		return
	}
	var handle execution.Handle
	if slot != nil {
		handle = slot.handle
		delete(s.executions, taskID)
	}
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
	claims, err := s.validateDelegationToken(t.DelegationToken)
	if err != nil {
		return execution.Request{}, fmt.Errorf("load delegated scope: %w", err)
	}
	for i := len(messages) - 1; i >= 0; i-- {
		msg := messages[i]
		if msg.Role != "delegation" || len(msg.Parts) != 2 {
			continue
		}
		return execution.Request{
			RequestID:           t.RequestID,
			InteractionID:       t.InteractionID,
			DecisionID:          t.DecisionID,
			CallID:              "call-" + t.TaskID,
			DelegationJTI:       claims.TokenID,
			DelegationToken:     t.DelegationToken,
			TenantID:            t.TenantID,
			TargetWorkloadID:    t.TargetWorkloadID,
			TargetInstanceID:    t.TargetInstanceID,
			TaskID:              t.TaskID,
			SessionID:           t.SessionID,
			InitiatorAgentID:    t.InitiatorAgentID,
			TargetAgentID:       t.TargetAgentID,
			ToolName:            msg.Parts[0].Text,
			Arguments:           msg.Parts[1].Data,
			AllowedTools:        claims.AllowedTools,
			AllowedCapabilities: claims.AllowedCapabilities,
			AllowRedelegation:   claims.AllowRedelegation,
			Deadline:            t.Deadline,
		}, nil
	}
	return execution.Request{}, errors.New("delegated execution request not found")
}

func (s *Server) finishExecution(taskID string, slot *executionSlot, handle execution.Handle) {
	result, ok := s.awaitExecution(taskID, handle)
	s.executionsMu.Lock()
	ownsExecution := s.executions[taskID] == slot && slot.handle == handle
	if ownsExecution {
		delete(s.executions, taskID)
	}
	s.executionsMu.Unlock()
	if !ok || !ownsExecution {
		return
	}
	if result.MayBeSent {
		_, _ = s.tasks.MarkOutcomeUnknown(taskID)
		return
	}
	_, _ = s.tasks.Complete(taskID, result.Status, result.Outcome, result.ErrorCode)
}

// awaitExecution waits for an execution result while periodically renewing the
// task's execution lease, so a long-running execution is not reclaimed by the
// recovery loop of a sibling instance.
func (s *Server) awaitExecution(taskID string, handle execution.Handle) (execution.Result, bool) {
	interval := s.db.ExecutionLease() / 3
	if interval <= 0 {
		interval = time.Second
	}
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		select {
		case result, ok := <-handle.Done():
			return result, ok
		case <-ticker.C:
			if err := s.tasks.RenewExecutionLease(context.Background(), taskID); err != nil {
				handle.Cancel()
				return execution.Result{}, false
			}
		}
	}
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
	if current.Status == req.Status {
		writeJSON(w, http.StatusOK, current)
		return
	}
	if current.Status != "running" && current.Status != "outcome_unknown" {
		writeError(w, http.StatusConflict, "invalid_status_transition", fmt.Sprintf("task is %s, expected running or outcome_unknown", current.Status))
		return
	}
	if s.strictExecution {
		if s.receiptValidator == nil {
			writeError(w, http.StatusServiceUnavailable, "execution_receipt_validation_unavailable", "strict receipt validator is not configured")
			return
		}
		claims, claimErr := s.validateDelegationToken(current.DelegationToken)
		if claimErr != nil {
			writeError(w, http.StatusForbidden, "invalid_delegation_token", claimErr.Error())
			return
		}
		responseStatus := "deny"
		if req.Status == "completed" {
			responseStatus = "allow"
		}
		result := req.Outcome
		var envelope struct {
			Result json.RawMessage `json:"result"`
		}
		if json.Unmarshal(req.Outcome, &envelope) == nil && len(envelope.Result) > 0 {
			result = envelope.Result
		}
		executionReq := execution.Request{RequestID: current.RequestID, InteractionID: current.InteractionID, DecisionID: current.DecisionID, CallID: "call-" + current.TaskID, DelegationJTI: claims.TokenID, TaskID: current.TaskID}
		terminalStatus := "error"
		if req.Status == "completed" {
			terminalStatus = "success"
		} else if req.Status == "cancelled" {
			terminalStatus = "cancelled"
		}
		if err := s.receiptValidator.ValidateExecutionReceipt(req.ExecutionReceipt, responseStatus, terminalStatus, result, executionReq); err != nil {
			writeError(w, http.StatusForbidden, "execution_receipt_invalid", err.Error())
			return
		}
	}
	updated, err := s.db.DelegationDispatchOutboxStore().CommitOutboundTaskResult(r.Context(), taskID, req.Status, req.Outcome, req.ErrorCode, req.ConsumedBudget, time.Now().UTC())
	if errors.Is(err, store.ErrAssignmentNotFound) {
		updated, err = s.tasks.CompleteWithConsumption(taskID, req.Status, req.Outcome, req.ErrorCode, req.ConsumedBudget)
	}
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
	if claims.SessionID != t.SessionID || claims.InteractionID != t.InteractionID || claims.DecisionID != t.DecisionID ||
		claims.RootInteractionID != t.RootInteractionID || claims.ParentInteractionID != t.ParentInteractionID {
		return fmt.Errorf("token interaction linkage mismatch")
	}
	if claims.InitiatorAgentID == "" || claims.InitiatorAgentID != t.InitiatorAgentID {
		return fmt.Errorf("token initiator mismatch")
	}
	if claims.TargetAgentID == "" || claims.TargetAgentID != t.TargetAgentID {
		return fmt.Errorf("token target mismatch")
	}
	if (t.RequestID != "" && claims.RequestID != t.RequestID) || claims.TenantID != t.TenantID || claims.TargetWorkloadID != t.TargetWorkloadID || claims.TargetInstanceID != t.TargetInstanceID {
		return fmt.Errorf("token task/request/workload binding mismatch")
	}
	if claims.Audience != t.TargetAgentID {
		return fmt.Errorf("token audience mismatch")
	}
	if claims.ToolName == "" || claims.ArgumentsSHA256 == "" {
		return fmt.Errorf("token execution scope missing")
	}
	if claims.RootTaskID != t.RootTaskID || claims.ParentTaskID != t.ParentTaskID || claims.DelegationDepth != t.DelegationDepth {
		return fmt.Errorf("token delegation lineage mismatch")
	}
	if !sameScope(claims.AllowedTools, t.AllowedTools) || !sameScope(claims.AllowedCapabilities, t.AllowedCapabilities) || claims.AllowRedelegation != t.AllowRedelegation {
		return fmt.Errorf("token delegation scope mismatch")
	}
	if (claims.Deadline == 0) != (t.Deadline == nil) || (t.Deadline != nil && claims.Deadline != t.Deadline.Unix()) {
		return fmt.Errorf("token deadline mismatch")
	}
	if claims.BudgetTokenCount != t.Budget.TokenCount || claims.BudgetPaymentAmount != t.Budget.PaymentAmount || claims.BudgetCurrency != t.Budget.Currency {
		return fmt.Errorf("token budget mismatch")
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
	if claims.RequestID == "" || claims.RequestID != req.RequestID {
		return fmt.Errorf("token request_id mismatch")
	}
	if claims.TaskID == "" || claims.TaskID != req.TaskID {
		return fmt.Errorf("token task_id mismatch")
	}
	if claims.SessionID != req.SessionID || claims.InteractionID != req.InteractionID || claims.DecisionID != req.DecisionID ||
		claims.RootInteractionID != req.RootInteractionID || claims.ParentInteractionID != req.ParentInteractionID {
		return fmt.Errorf("token interaction linkage mismatch")
	}
	if claims.InitiatorAgentID == "" || claims.InitiatorAgentID != req.InitiatorAgentID {
		return fmt.Errorf("token initiator mismatch")
	}
	if claims.TargetAgentID == "" || claims.TargetAgentID != req.TargetAgentID {
		return fmt.Errorf("token target mismatch")
	}
	if claims.TenantID != req.TenantID || claims.TargetWorkloadID != req.TargetWorkloadID || claims.TargetInstanceID != req.TargetInstanceID {
		return fmt.Errorf("token request workload binding mismatch")
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
	if !sameScope(claims.AllowedTools, req.AllowedTools) || !containsScope(claims.AllowedTools, req.ToolName) {
		return fmt.Errorf("token allowed_tools mismatch")
	}
	if !sameScope(claims.AllowedCapabilities, req.AllowedCapabilities) {
		return fmt.Errorf("token allowed_capabilities mismatch")
	}
	if claims.AllowRedelegation != req.AllowRedelegation {
		return fmt.Errorf("token allow_redelegation mismatch")
	}
	if claims.RootTaskID != req.RootTaskID || claims.ParentTaskID != req.ParentTaskID || claims.DelegationDepth != req.DelegationDepth {
		return fmt.Errorf("token delegation lineage mismatch")
	}
	if (claims.Deadline == 0) != (req.Deadline == nil) || (req.Deadline != nil && claims.Deadline != req.Deadline.Unix()) {
		return fmt.Errorf("token deadline mismatch")
	}
	if claims.BudgetTokenCount != req.Budget.TokenCount || claims.BudgetPaymentAmount != req.Budget.PaymentAmount || claims.BudgetCurrency != req.Budget.Currency {
		return fmt.Errorf("token budget mismatch")
	}
	return nil
}

func containsScope(scope []string, value string) bool {
	for _, item := range scope {
		if item == value {
			return true
		}
	}
	return false
}

func sameDeadline(left, right *time.Time) bool {
	if left == nil || right == nil {
		return left == nil && right == nil
	}
	return left.Equal(*right)
}

func sameScope(left, right []string) bool {
	if len(left) != len(right) {
		return false
	}
	for _, item := range left {
		if !containsScope(right, item) {
			return false
		}
	}
	return true
}

func (s *Server) rejectExpiredTask(w http.ResponseWriter, taskID string) (bool, error) {
	t, err := s.tasks.Get(taskID)
	if err != nil {
		writeError(w, http.StatusNotFound, "task_not_found", err.Error())
		return false, err
	}
	if t.Deadline == nil || t.Deadline.After(time.Now().UTC()) {
		return false, nil
	}
	if !t.IsTerminal() && t.Status != "outcome_unknown" {
		_, _ = s.tasks.UpdateStatusFrom(taskID, t.Status, "cancelled")
	}
	writeError(w, http.StatusConflict, "task_deadline_expired", "task deadline has expired")
	return true, nil
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
	if status >= http.StatusInternalServerError {
		message = "service unavailable"
	}
	writeJSON(w, status, models.ErrorResponse{ProtocolVersion: currentProtocolVersion, Error: message, Code: code})
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
