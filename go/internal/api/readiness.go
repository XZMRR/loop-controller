package api

import (
	"context"
	"net/http"
	"time"

	"github.com/loop-controller/go/internal/models"
	"github.com/loop-controller/go/internal/observability"
	"github.com/loop-controller/go/internal/store"
)

func (s *Server) SchedulingStore() *store.SchedulingStore {
	s.db.SetDelegationTokenIssuer(s.issuer, 5*time.Minute)
	return s.db.SchedulingStore()
}

type ReadinessConfig struct {
	Strict                       bool
	MaxQueueDepth                int64
	MaxQueueOldestLag            time.Duration
	MaxRecoveryStaleness         time.Duration
	MaxDiscoveryStaleness        time.Duration
	RequireAssignmentWorker      bool
	RequireOutboundWorker        bool
	RequireSchedulableTarget     bool
	RequiredSecurityCapabilities []string
}

func DefaultReadinessConfig() ReadinessConfig {
	return ReadinessConfig{Strict: true, MaxQueueDepth: 10000, MaxQueueOldestLag: 5 * time.Minute, MaxRecoveryStaleness: 2 * time.Minute, MaxDiscoveryStaleness: 5 * time.Minute, RequiredSecurityCapabilities: []string{"scheduler_fencing_v1", "idempotency_v1", "workload_identity_v1"}}
}

func (s *Server) SetReadinessConfig(c ReadinessConfig) { s.readiness = c }
func (s *Server) SetWorkerStates(assignment, outbound observability.StateProvider) {
	s.assignmentWorker, s.outboundWorker = assignment, outbound
}

type readinessCheck struct {
	Status   string `json:"status"`
	Observed any    `json:"observed,omitempty"`
}
type readinessResponse struct {
	ProtocolVersion string                    `json:"protocol_version"`
	SchemaVersion   string                    `json:"schema_version"`
	Status          string                    `json:"status"`
	Checks          map[string]readinessCheck `json:"checks"`
	Reasons         []string                  `json:"reasons"`
	ObservedAt      time.Time                 `json:"observed_at"`
}

func (s *Server) handleMetrics(w http.ResponseWriter, r *http.Request) {
	now := time.Now().UTC()
	for _, kind := range []models.AssignmentKind{models.AssignmentKindTargetExecution, models.AssignmentKindOutboundDelegation} {
		q, err := s.db.QueueSnapshot(r.Context(), kind, now)
		if err == nil {
			observability.Default.Set("assignment_queue_depth", float64(q.Depth), string(kind))
			observability.Default.Set("assignment_queue_oldest_lag_seconds", q.OldestLagSeconds, string(kind))
		}
	}
	if facts, err := s.db.ReadinessSnapshot(r.Context(), now, nil); err == nil {
		observability.Default.Set("discovery_stale_agents", float64(facts.StaleAgents))
	}
	w.Header().Set("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
	w.WriteHeader(http.StatusOK)
	_ = observability.Default.WritePrometheus(w)
}

func (s *Server) handleReady(w http.ResponseWriter, r *http.Request) {
	now := time.Now().UTC()
	resp := readinessResponse{ProtocolVersion: currentProtocolVersion, SchemaVersion: "p54-09.v1", Status: "ready", Checks: make(map[string]readinessCheck), Reasons: []string{}, ObservedAt: now}
	fail := func(name, reason string, observed any) {
		resp.Checks[name] = readinessCheck{Status: "failed", Observed: observed}
		resp.Reasons = append(resp.Reasons, reason)
	}
	pass := func(name string, observed any) {
		resp.Checks[name] = readinessCheck{Status: "passed", Observed: observed}
	}
	skip := func(name string) { resp.Checks[name] = readinessCheck{Status: "skipped"} }
	if s.closed.Load() {
		fail("lifecycle", "server_stopped", nil)
	} else {
		pass("lifecycle", "running")
	}
	ctx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
	defer cancel()
	if err := s.db.Ping(ctx); err != nil {
		fail("database", "database_unavailable", nil)
	} else {
		pass("database", "reachable")
	}
	facts, factsErr := s.db.ReadinessSnapshot(ctx, now, s.readiness.RequiredSecurityCapabilities)
	if factsErr != nil {
		fail("store", "store_query_failed", nil)
	} else {
		if facts.MigrationVersion != store.CurrentMigrationVersion() {
			fail("migration", "migration_not_current", facts.MigrationVersion)
		} else {
			pass("migration", facts.MigrationVersion)
		}
		observability.Default.Set("discovery_stale_agents", float64(facts.StaleAgents))
		if s.readiness.RequireSchedulableTarget && facts.EligibleTargets == 0 {
			fail("target_capability", "strict_target_unavailable", 0)
		} else if s.readiness.RequireSchedulableTarget {
			pass("target_capability", facts.EligibleTargets)
		} else {
			skip("target_capability")
		}
	}
	if s.schedulingEnabled {
		pass("scheduler", "enabled")
	} else if s.readiness.Strict {
		fail("scheduler", "scheduler_disabled", nil)
	} else {
		resp.Checks["scheduler"] = readinessCheck{Status: "degraded", Observed: "disabled"}
	}
	checkWorker := func(name string, required bool, worker observability.StateProvider) {
		if !required {
			skip(name)
			return
		}
		if worker == nil {
			fail(name, name+"_missing", nil)
			return
		}
		snapshot := worker.Snapshot()
		if !snapshot.Started || !snapshot.Running {
			fail(name, name+"_not_running", snapshot)
			return
		}
		pass(name, snapshot)
	}
	checkWorker("assignment_worker", s.readiness.RequireAssignmentWorker, s.assignmentWorker)
	checkWorker("outbound_worker", s.readiness.RequireOutboundWorker, s.outboundWorker)
	if recovery := s.recoveryState.Snapshot(); !recovery.Running || recovery.LastRecoveryAt == nil || now.Sub(*recovery.LastRecoveryAt) > s.readiness.MaxRecoveryStaleness {
		fail("expired_lease_recovery", "recovery_stale", recovery)
	} else {
		pass("expired_lease_recovery", recovery)
	}
	if s.discovery != nil && s.discovery.Required() {
		d := s.discovery.Snapshot()
		if d.LastSuccessAt == nil || now.Sub(*d.LastSuccessAt) > s.readiness.MaxDiscoveryStaleness || d.LastErrorAt != nil && (d.LastSuccessAt == nil || d.LastErrorAt.After(*d.LastSuccessAt)) {
			fail("discovery", "discovery_stale", d)
		} else {
			pass("discovery", d)
		}
	} else {
		skip("discovery")
	}
	for _, kind := range []models.AssignmentKind{models.AssignmentKindTargetExecution, models.AssignmentKindOutboundDelegation} {
		q, err := s.db.QueueSnapshot(ctx, kind, now)
		name := "queue_" + string(kind)
		if err != nil {
			fail(name, "queue_query_failed", nil)
			continue
		}
		observability.Default.Set("assignment_queue_depth", float64(q.Depth), string(kind))
		observability.Default.Set("assignment_queue_oldest_lag_seconds", q.OldestLagSeconds, string(kind))
		if q.Depth > s.readiness.MaxQueueDepth {
			fail(name, "queue_depth_exceeded", q)
		} else if q.OldestLagSeconds > s.readiness.MaxQueueOldestLag.Seconds() {
			fail(name, "queue_lag_exceeded", q)
		} else {
			pass(name, q)
		}
	}
	status := http.StatusOK
	if len(resp.Reasons) > 0 {
		resp.Status, status = "not_ready", http.StatusServiceUnavailable
	}
	writeJSON(w, status, resp)
}
