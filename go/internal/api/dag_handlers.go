package api

import (
	"errors"
	"net/http"
	"strings"
	"time"

	"github.com/loop-controller/go/internal/models"
	"github.com/loop-controller/go/internal/store"
)

func (s *Server) handleCreateGraph(w http.ResponseWriter, r *http.Request) {
	var req models.TaskGraphCreate
	if !decodeJSONPost(w, r, &req) {
		return
	}
	if req.ProtocolVersion != models.DAGProtocolVersion {
		writeError(w, http.StatusBadRequest, "incompatible_protocol_version", "task graph protocol_version must be 0.54.0")
		return
	}
	key := strings.TrimSpace(r.Header.Get("Idempotency-Key"))
	if key == "" {
		writeError(w, http.StatusBadRequest, "idempotency_key_required", "Idempotency-Key is required")
		return
	}
	tenant, ok := s.controlTenant(req.TenantID)
	if !ok {
		writeError(w, http.StatusNotFound, "task_graph_not_found", "task graph not found")
		return
	}
	req.TenantID = tenant
	root, err := s.db.TaskStore().GetForTenant(r.Context(), tenant, req.RootTaskID)
	if err != nil || (s.controlToken != "" && root.InitiatorAgentID != s.controlInitiatorID) {
		writeError(w, http.StatusNotFound, "task_graph_not_found", "task graph not found")
		return
	}
	graph, err := s.db.DAGStore().CreateGraph(r.Context(), req, key, time.Now().UTC())
	if err != nil {
		status := http.StatusBadRequest
		code := "invalid_task_graph"
		if errors.Is(err, store.ErrDAGConflict) {
			status = http.StatusConflict
			code = "task_graph_conflict"
		}
		writeError(w, status, code, err.Error())
		return
	}
	writeJSON(w, http.StatusCreated, graph)
}
func (s *Server) handleGetGraph(w http.ResponseWriter, r *http.Request) {
	tenant, ok := s.controlTenant(r.URL.Query().Get("tenant_id"))
	if !ok {
		writeError(w, http.StatusNotFound, "task_graph_not_found", "task graph not found")
		return
	}
	g, err := s.db.DAGStore().GetGraph(r.Context(), tenant, r.PathValue("id"))
	if err != nil {
		writeError(w, http.StatusNotFound, "task_graph_not_found", "task graph not found")
		return
	}
	root, err := s.db.TaskStore().GetForTenant(r.Context(), tenant, g.RootTaskID)
	if err != nil || (s.controlToken != "" && root.InitiatorAgentID != s.controlInitiatorID) {
		writeError(w, http.StatusNotFound, "task_graph_not_found", "task graph not found")
		return
	}
	writeJSON(w, http.StatusOK, g)
}
func (s *Server) handleCancelGraph(w http.ResponseWriter, r *http.Request) {
	var req struct {
		ProtocolVersion string `json:"protocol_version"`
		TenantID        string `json:"tenant_id"`
		Reason          string `json:"reason,omitempty"`
	}
	if !decodeJSONPost(w, r, &req) {
		return
	}
	if req.ProtocolVersion != models.DAGProtocolVersion {
		writeError(w, http.StatusBadRequest, "incompatible_protocol_version", "task graph protocol_version must be 0.54.0")
		return
	}
	tenant, ok := s.controlTenant(req.TenantID)
	if !ok {
		writeError(w, http.StatusNotFound, "task_graph_not_found", "task graph not found")
		return
	}
	req.TenantID = tenant
	g, err := s.db.DAGStore().GetGraph(r.Context(), req.TenantID, r.PathValue("id"))
	if err != nil {
		writeError(w, http.StatusNotFound, "task_graph_not_found", err.Error())
		return
	}
	root, err := s.tasks.Get(g.RootTaskID)
	if err != nil || (s.controlToken != "" && root.InitiatorAgentID != s.controlInitiatorID) {
		writeError(w, http.StatusForbidden, "task_graph_access_denied", "task graph is owned by another initiator")
		return
	}
	g, err = s.db.DAGStore().CancelGraph(r.Context(), req.TenantID, g.DAGID, req.Reason, time.Now().UTC())
	if err != nil {
		writeError(w, http.StatusConflict, "task_graph_cancel_conflict", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, g)
}
