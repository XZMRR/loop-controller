package api

import (
	"crypto/subtle"
	"errors"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/loop-controller/go/internal/models"
	"github.com/loop-controller/go/internal/store"
	"github.com/loop-controller/go/internal/task"
)

type approvalAuthConfig struct {
	token     string
	principal string
}

var approvalAuth sync.Map

// SetApprovalAuth configures the independent credential used by approval principals.
func (s *Server) SetApprovalAuth(bearerToken, principal string) {
	approvalAuth.Store(s, approvalAuthConfig{token: strings.TrimSpace(bearerToken), principal: strings.TrimSpace(principal)})
}

func (s *Server) registerApprovalRoutes(mux *http.ServeMux) {
	mux.HandleFunc("GET /a2a/v1/delegation-approvals", s.withApprovalListAuth(s.handleListDelegationApprovals))
	mux.HandleFunc("GET /a2a/v1/delegation-approvals/{id}", s.withInitiatorApprovalAuth(s.handleGetDelegationApproval))
	mux.HandleFunc("POST /a2a/v1/delegation-approvals/{id}/approve", s.withApproverAuth(s.handleApproveDelegation))
	mux.HandleFunc("POST /a2a/v1/delegation-approvals/{id}/reject", s.withApproverAuth(s.handleRejectDelegation))
	mux.HandleFunc("POST /a2a/v1/delegation-approvals/{id}/cancel", s.withInitiatorApprovalAuth(s.handleCancelDelegation))
}

// withApprovalListAuth 约束枚举端点：配置 control token 时，调用方必须持有
// control token，且结果仅限该 token 绑定的 initiator；未配置时放行（开发模式）。
func (s *Server) withApprovalListAuth(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if s.controlToken == "" || s.controlInitiatorID == "" {
			next(w, r)
			return
		}
		tokenString, err := bearerToken(r.Header.Get("Authorization"))
		if err != nil || subtle.ConstantTimeCompare([]byte(tokenString), []byte(s.controlToken)) != 1 {
			writeError(w, http.StatusUnauthorized, "invalid_control_token", "valid Bearer control token is required")
			return
		}
		next(w, r)
	}
}

func (s *Server) handleListDelegationApprovals(w http.ResponseWriter, r *http.Request) {
	_, _ = s.db.DelegationApprovalStore().ExpireDue(r.Context(), nowUTC(), 100)
	filter := store.ApprovalFilter{
		Status:           strings.TrimSpace(r.URL.Query().Get("status")),
		InitiatorAgentID: s.controlInitiatorID,
	}
	if limitParam := strings.TrimSpace(r.URL.Query().Get("limit")); limitParam != "" {
		if limit, err := strconv.Atoi(limitParam); err == nil {
			filter.Limit = limit
		}
	}
	approvals, err := s.db.DelegationApprovalStore().List(r.Context(), filter)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "approval_list_failed", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"approvals": approvals})
}

func (s *Server) approvalConfig() approvalAuthConfig {
	value, _ := approvalAuth.Load(s)
	config, _ := value.(approvalAuthConfig)
	return config
}

func (s *Server) withInitiatorApprovalAuth(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if s.controlToken == "" || s.controlInitiatorID == "" {
			writeError(w, http.StatusUnauthorized, "approval_auth_required", "authenticated initiator principal is required")
			return
		}
		tokenString, err := bearerToken(r.Header.Get("Authorization"))
		if err != nil || subtle.ConstantTimeCompare([]byte(tokenString), []byte(s.controlToken)) != 1 {
			writeError(w, http.StatusUnauthorized, "invalid_control_token", "valid Bearer control token is required")
			return
		}
		approval, err := s.db.DelegationApprovalStore().Get(r.Context(), r.PathValue("id"))
		if errors.Is(err, store.ErrApprovalNotFound) {
			writeError(w, http.StatusNotFound, "approval_not_found", err.Error())
			return
		}
		if err != nil {
			writeError(w, http.StatusInternalServerError, "approval_lookup_failed", err.Error())
			return
		}
		if approval.InitiatorAgentID != s.controlInitiatorID {
			writeError(w, http.StatusForbidden, "approval_access_denied", "approval is owned by another initiator")
			return
		}
		next(w, r)
	}
}

func (s *Server) withApproverAuth(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		config := s.approvalConfig()
		if config.token == "" || config.principal == "" {
			writeError(w, http.StatusUnauthorized, "approval_auth_required", "authenticated approval principal is required")
			return
		}
		tokenString, err := bearerToken(r.Header.Get("Authorization"))
		if err != nil || subtle.ConstantTimeCompare([]byte(tokenString), []byte(config.token)) != 1 {
			writeError(w, http.StatusUnauthorized, "invalid_approver_token", "valid independent approver Bearer token is required")
			return
		}
		if config.principal == s.controlInitiatorID {
			writeError(w, http.StatusForbidden, "approver_not_independent", "approver principal must be independent from initiator")
			return
		}
		next(w, r)
	}
}

func (s *Server) handleGetDelegationApproval(w http.ResponseWriter, r *http.Request) {
	_, _ = s.db.DelegationApprovalStore().ExpireDue(r.Context(), nowUTC(), 100)
	approval, err := s.db.DelegationApprovalStore().Get(r.Context(), r.PathValue("id"))
	if err != nil {
		writeApprovalError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, approval)
}

func (s *Server) handleApproveDelegation(w http.ResponseWriter, r *http.Request) {
	approval, ok := s.transitionApproval(w, r, "pending", "approved", s.approvalConfig().principal)
	if !ok {
		return
	}
	consumed, err := s.delegation.ResumeApproval(r.Context(), approval.ApprovalID)
	if err != nil {
		writeError(w, http.StatusConflict, "approval_resume_failed", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, consumed)
}

func (s *Server) handleRejectDelegation(w http.ResponseWriter, r *http.Request) {
	approval, ok := s.transitionApproval(w, r, "pending", "rejected", s.approvalConfig().principal)
	if ok {
		writeJSON(w, http.StatusOK, approval)
	}
}

func (s *Server) handleCancelDelegation(w http.ResponseWriter, r *http.Request) {
	current, err := s.db.DelegationApprovalStore().Get(r.Context(), r.PathValue("id"))
	if err != nil {
		writeApprovalError(w, err)
		return
	}
	if current.Status == "consumed" {
		var req models.ApprovalActionRequest
		if !decodeJSONPost(w, r, &req) {
			return
		}
		if strings.TrimSpace(req.RequestID) == "" {
			writeError(w, http.StatusBadRequest, "request_id_required", "request_id is required for replay protection")
			return
		}
		t, err := s.tasks.Get(current.TaskID)
		if err != nil {
			writeError(w, http.StatusNotFound, "task_not_found", err.Error())
			return
		}
		if !t.IsTerminal() {
			t, err = s.cancelTaskExecution(r.Context(), t)
			if err != nil {
				writeError(w, http.StatusConflict, "task_cancel_failed", err.Error())
				return
			}
		}
		writeJSON(w, http.StatusOK, t)
		return
	}
	if current.Status != "pending" && current.Status != "approved" {
		writeError(w, http.StatusConflict, "approval_conflict", "approval is already decided")
		return
	}
	approval, ok := s.transitionApprovalFrom(w, r, current, "cancelled", s.controlInitiatorID)
	if ok {
		writeJSON(w, http.StatusOK, approval)
	}
}

func (s *Server) transitionApproval(w http.ResponseWriter, r *http.Request, from, to, principal string) (models.DelegationApproval, bool) {
	current, err := s.db.DelegationApprovalStore().Get(r.Context(), r.PathValue("id"))
	if err != nil {
		writeApprovalError(w, err)
		return models.DelegationApproval{}, false
	}
	return s.transitionApprovalFromAs(w, r, current, from, to, principal)
}

func (s *Server) transitionApprovalFrom(w http.ResponseWriter, r *http.Request, current models.DelegationApproval, to, principal string) (models.DelegationApproval, bool) {
	return s.transitionApprovalFromAs(w, r, current, current.Status, to, principal)
}

func (s *Server) transitionApprovalFromAs(w http.ResponseWriter, r *http.Request, current models.DelegationApproval, from, to, principal string) (models.DelegationApproval, bool) {
	var req models.ApprovalActionRequest
	if !decodeJSONPost(w, r, &req) {
		return models.DelegationApproval{}, false
	}
	if strings.TrimSpace(req.RequestID) == "" {
		writeError(w, http.StatusBadRequest, "request_id_required", "request_id is required for replay protection")
		return models.DelegationApproval{}, false
	}
	updated, err := s.db.DelegationApprovalStore().Transition(r.Context(), current.ApprovalID, current.Version, from, to, principal, req.Reason, req.RequestID)
	if err != nil {
		writeApprovalError(w, err)
		return models.DelegationApproval{}, false
	}
	return updated, true
}

func writeApprovalError(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, store.ErrApprovalNotFound):
		writeError(w, http.StatusNotFound, "approval_not_found", err.Error())
	case errors.Is(err, store.ErrApprovalConflict):
		writeError(w, http.StatusConflict, "approval_conflict", err.Error())
	case errors.Is(err, store.ErrApprovalExpired):
		writeError(w, http.StatusConflict, "approval_expired", err.Error())
	case errors.Is(err, task.ErrTaskNotFound):
		writeError(w, http.StatusNotFound, "task_not_found", err.Error())
	default:
		writeError(w, http.StatusInternalServerError, "approval_operation_failed", err.Error())
	}
}

var nowUTC = func() time.Time { return time.Now().UTC() }
