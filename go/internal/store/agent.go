package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/loop-controller/go/internal/models"
)

var (
	ErrAgentConflict  = errors.New("agent resource version conflict")
	ErrAgentOwnership = errors.New("agent tenant or source ownership conflict")
)

// AgentStore retains the legacy AgentCard API and provides the durable spec/status API.
type AgentStore interface {
	Upsert(context.Context, models.AgentCard) error
	Get(context.Context, string) (models.AgentCard, error)
	GetForTenant(context.Context, string, string) (models.AgentCard, error)
	Delete(context.Context, string) error
	DeleteForTenant(context.Context, string, string, string, string) error
	List(context.Context) ([]models.AgentCard, error)
	ListForTenant(context.Context, string) ([]models.AgentCard, error)
	CreateSpec(context.Context, models.AgentSpec) (models.AgentSpec, error)
	UpdateSpec(context.Context, models.AgentSpec, int64) (models.AgentSpec, error)
	GetSpec(context.Context, string, string) (models.AgentSpec, error)
	ListSpecs(context.Context, string) ([]models.AgentSpec, error)
	GetStatus(context.Context, string, string) (models.AgentStatus, error)
	HeartbeatStatus(context.Context, string, string, int64, models.AgentHeartbeatPatch, string) (models.AgentStatus, error)
	SetDraining(context.Context, string, string, int64, bool, string) (models.AgentStatus, error)
	SyncSource(context.Context, models.DiscoverySnapshot) error
	MarkSourceStale(context.Context, string, string, string, error) error
	ExpireStatuses(context.Context, time.Time) (int64, error)
}

type agentStore struct{ db *sql.DB }

func (s *agentStore) Upsert(ctx context.Context, card models.AgentCard) error {
	now := time.Now().UTC()
	spec := models.AgentSpec{TenantID: card.TenantID, AgentID: card.AgentID, Name: card.Name, Description: card.Description, Entrypoint: card.Entrypoint, Capabilities: card.Capabilities, TrustDomain: card.TrustDomain, SupportedProtocolVersions: []string{card.Version}, SourceType: "legacy", SourceID: "legacy-adapter", Schedulable: false, Weight: 1, MaxConcurrency: 1, CreatedAt: now, UpdatedAt: now}
	old, err := s.GetSpec(ctx, card.TenantID, card.AgentID)
	if err == sql.ErrNoRows {
		_, err = s.CreateSpec(ctx, spec)
		return err
	}
	if err != nil {
		return err
	}
	if old.SourceType != "legacy" || old.SourceID != "legacy-adapter" {
		return ErrAgentOwnership
	}
	spec.Generation, spec.CreatedAt = old.Generation, old.CreatedAt
	_, err = s.UpdateSpec(ctx, spec, old.ResourceVersion)
	return err
}

func (s *agentStore) Get(ctx context.Context, id string) (models.AgentCard, error) {
	return s.getCard(ctx, `agent_id=?`, id)
}

func (s *agentStore) GetForTenant(ctx context.Context, tenant, id string) (models.AgentCard, error) {
	return s.getCard(ctx, `tenant_id=? AND agent_id=?`, tenant, id)
}

func (s *agentStore) getCard(ctx context.Context, predicate string, args ...any) (models.AgentCard, error) {
	row := s.db.QueryRowContext(ctx, `SELECT tenant_id,agent_id,name,description,entrypoint_type,entrypoint_url,capabilities_json,trust_domain,protocol_capabilities_json FROM agents WHERE `+predicate+` AND deleted_at IS NULL`, args...)
	return scanCard(row)
}

func scanCard(row agentRowScanner) (models.AgentCard, error) {
	var c models.AgentCard
	var caps, protocols string
	if err := row.Scan(&c.TenantID, &c.AgentID, &c.Name, &c.Description, &c.Entrypoint.Type, &c.Entrypoint.URL, &caps, &c.TrustDomain, &protocols); err != nil {
		return c, err
	}
	if err := json.Unmarshal([]byte(caps), &c.Capabilities); err != nil {
		return c, err
	}
	var versions []string
	if err := json.Unmarshal([]byte(protocols), &versions); err != nil {
		return c, err
	}
	if len(versions) > 0 {
		c.Version = versions[0]
	}
	return c, nil
}
func (s *agentStore) Delete(ctx context.Context, id string) error {
	return requireAgentDelete(s.db.ExecContext(ctx, `DELETE FROM agents WHERE agent_id=?`, id))
}

func (s *agentStore) DeleteForTenant(ctx context.Context, tenant, id, sourceType, sourceID string) error {
	return requireAgentDelete(s.db.ExecContext(ctx, `DELETE FROM agents WHERE tenant_id=? AND agent_id=? AND source_type=? AND source_id=?`, tenant, id, sourceType, sourceID))
}

func requireAgentDelete(r sql.Result, e error) error {
	if e != nil {
		return e
	}
	n, _ := r.RowsAffected()
	if n == 0 {
		return sql.ErrNoRows
	}
	return nil
}
func (s *agentStore) List(ctx context.Context) ([]models.AgentCard, error) {
	return s.listCards(ctx, `deleted_at IS NULL`)
}

func (s *agentStore) ListForTenant(ctx context.Context, tenant string) ([]models.AgentCard, error) {
	return s.listCards(ctx, `tenant_id=? AND deleted_at IS NULL`, tenant)
}

func (s *agentStore) listCards(ctx context.Context, predicate string, args ...any) ([]models.AgentCard, error) {
	rows, e := s.db.QueryContext(ctx, `SELECT tenant_id,agent_id,name,description,entrypoint_type,entrypoint_url,capabilities_json,trust_domain,protocol_capabilities_json FROM agents WHERE `+predicate+` ORDER BY agent_id`, args...)
	if e != nil {
		return nil, e
	}
	defer rows.Close()
	var out []models.AgentCard
	for rows.Next() {
		c, x := scanCard(rows)
		if x != nil {
			return nil, x
		}
		out = append(out, c)
	}
	return out, rows.Err()
}

func validateSpec(spec models.AgentSpec) error {
	if spec.AgentID == "" || spec.SourceType == "" || spec.SourceID == "" {
		return errors.New("tenant_id, agent_id, source_type and source_id are required")
	}
	return nil
}
func (s *agentStore) CreateSpec(ctx context.Context, spec models.AgentSpec) (models.AgentSpec, error) {
	if err := validateSpec(spec); err != nil {
		return spec, err
	}
	now := time.Now().UTC()
	spec.Generation = 1
	spec.ResourceVersion = 1
	spec.CreatedAt = now
	spec.UpdatedAt = now
	caps, _ := json.Marshal(spec.Capabilities)
	tools, _ := json.Marshal(spec.SupportedTools)
	protos, _ := json.Marshal(spec.SupportedProtocolVersions)
	security, _ := json.Marshal(spec.SupportedSecurityCapabilities)
	labels, _ := json.Marshal(spec.Labels)
	_, err := s.db.ExecContext(ctx, `INSERT INTO agents(agent_id,tenant_id,name,description,entrypoint_type,entrypoint_url,capabilities_json,tools_json,trust_domain,protocol_capabilities_json,security_capabilities_json,expected_workload_id,labels_json,priority,weight,max_concurrency,schedulable,source_type,source_id,external_revision,generation,resource_version,created_at,updated_at,deleted_at,version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,'')`, spec.AgentID, spec.TenantID, spec.Name, spec.Description, spec.Entrypoint.Type, spec.Entrypoint.URL, string(caps), string(tools), spec.TrustDomain, string(protos), string(security), spec.ExpectedWorkloadID, string(labels), spec.Priority, spec.Weight, spec.MaxConcurrency, spec.Schedulable, spec.SourceType, spec.SourceID, spec.ExternalRevision, spec.Generation, spec.ResourceVersion, now.Format(time.RFC3339Nano), now.Format(time.RFC3339Nano))
	if err != nil {
		return spec, fmt.Errorf("create agent spec: %w", err)
	}
	return spec, nil
}

func (s *agentStore) UpdateSpec(ctx context.Context, spec models.AgentSpec, expected int64) (models.AgentSpec, error) {
	if err := validateSpec(spec); err != nil {
		return spec, err
	}
	old, err := s.GetSpec(ctx, spec.TenantID, spec.AgentID)
	if err != nil {
		return spec, err
	}
	if old.SourceType != spec.SourceType || old.SourceID != spec.SourceID {
		return spec, ErrAgentOwnership
	}
	now := time.Now().UTC()
	caps, _ := json.Marshal(spec.Capabilities)
	tools, _ := json.Marshal(spec.SupportedTools)
	protos, _ := json.Marshal(spec.SupportedProtocolVersions)
	security, _ := json.Marshal(spec.SupportedSecurityCapabilities)
	labels, _ := json.Marshal(spec.Labels)
	r, err := s.db.ExecContext(ctx, `UPDATE agents SET name=?,description=?,entrypoint_type=?,entrypoint_url=?,capabilities_json=?,tools_json=?,trust_domain=?,protocol_capabilities_json=?,security_capabilities_json=?,expected_workload_id=?,labels_json=?,priority=?,weight=?,max_concurrency=?,schedulable=?,external_revision=?,generation=generation+1,resource_version=resource_version+1,updated_at=?,deleted_at=? WHERE tenant_id=? AND agent_id=? AND source_type=? AND source_id=? AND resource_version=?`, spec.Name, spec.Description, spec.Entrypoint.Type, spec.Entrypoint.URL, string(caps), string(tools), spec.TrustDomain, string(protos), string(security), spec.ExpectedWorkloadID, string(labels), spec.Priority, spec.Weight, spec.MaxConcurrency, spec.Schedulable, spec.ExternalRevision, now.Format(time.RFC3339Nano), nullableTime(spec.DeletedAt), spec.TenantID, spec.AgentID, spec.SourceType, spec.SourceID, expected)
	if err != nil {
		return spec, err
	}
	n, _ := r.RowsAffected()
	if n == 0 {
		return spec, ErrAgentConflict
	}
	return s.GetSpec(ctx, spec.TenantID, spec.AgentID)
}

func nullableTime(t *time.Time) any {
	if t == nil {
		return nil
	}
	return t.UTC().Format(time.RFC3339Nano)
}

const specColumns = `tenant_id,agent_id,name,description,entrypoint_type,entrypoint_url,capabilities_json,tools_json,trust_domain,protocol_capabilities_json,security_capabilities_json,expected_workload_id,labels_json,priority,weight,max_concurrency,schedulable,source_type,source_id,external_revision,generation,resource_version,created_at,updated_at,deleted_at`

type agentRowScanner interface{ Scan(...any) error }

func scanSpec(row agentRowScanner) (models.AgentSpec, error) {
	var x models.AgentSpec
	var caps, tools, protos, security, labels, created, updated string
	var deleted sql.NullString
	err := row.Scan(&x.TenantID, &x.AgentID, &x.Name, &x.Description, &x.Entrypoint.Type, &x.Entrypoint.URL, &caps, &tools, &x.TrustDomain, &protos, &security, &x.ExpectedWorkloadID, &labels, &x.Priority, &x.Weight, &x.MaxConcurrency, &x.Schedulable, &x.SourceType, &x.SourceID, &x.ExternalRevision, &x.Generation, &x.ResourceVersion, &created, &updated, &deleted)
	if err != nil {
		return x, err
	}
	for _, v := range []struct {
		s string
		p any
	}{{caps, &x.Capabilities}, {tools, &x.SupportedTools}, {protos, &x.SupportedProtocolVersions}, {security, &x.SupportedSecurityCapabilities}, {labels, &x.Labels}} {
		if err = json.Unmarshal([]byte(v.s), v.p); err != nil {
			return x, err
		}
	}
	x.CreatedAt, _ = time.Parse(time.RFC3339Nano, created)
	x.UpdatedAt, _ = time.Parse(time.RFC3339Nano, updated)
	if deleted.Valid {
		d, _ := time.Parse(time.RFC3339Nano, deleted.String)
		x.DeletedAt = &d
	}
	return x, nil
}
func (s *agentStore) GetSpec(ctx context.Context, tenant, id string) (models.AgentSpec, error) {
	return scanSpec(s.db.QueryRowContext(ctx, `SELECT `+specColumns+` FROM agents WHERE tenant_id=? AND agent_id=? AND deleted_at IS NULL`, tenant, id))
}
func (s *agentStore) ListSpecs(ctx context.Context, tenant string) ([]models.AgentSpec, error) {
	rows, e := s.db.QueryContext(ctx, `SELECT `+specColumns+` FROM agents WHERE tenant_id=? AND deleted_at IS NULL ORDER BY agent_id`, tenant)
	if e != nil {
		return nil, e
	}
	defer rows.Close()
	var out []models.AgentSpec
	for rows.Next() {
		x, e := scanSpec(rows)
		if e != nil {
			return nil, e
		}
		out = append(out, x)
	}
	return out, rows.Err()
}

func scanStatus(row rowScanner) (models.AgentStatus, error) {
	var x models.AgentStatus
	var last, exp string
	err := row.Scan(&x.TenantID, &x.AgentID, &x.ObservedGeneration, &x.Health, &x.CurrentLoad, &x.AvailableCapacity, &x.Draining, &last, &exp, &x.StatusRevision, &x.ReportedBy)
	if err != nil {
		return x, err
	}
	x.LastSeenAt, _ = time.Parse(time.RFC3339Nano, last)
	x.ExpiresAt, _ = time.Parse(time.RFC3339Nano, exp)
	return x, nil
}
func (s *agentStore) GetStatus(ctx context.Context, tenant, id string) (models.AgentStatus, error) {
	return scanStatus(s.db.QueryRowContext(ctx, `SELECT tenant_id,agent_id,observed_generation,health,current_load,available_capacity,draining,last_seen_at,expires_at,status_revision,reported_by FROM agent_status WHERE tenant_id=? AND agent_id=?`, tenant, id))
}
func (s *agentStore) HeartbeatStatus(ctx context.Context, tenant, id string, expected int64, p models.AgentHeartbeatPatch, reportedBy string) (models.AgentStatus, error) {
	if reportedBy == "" {
		return models.AgentStatus{}, errors.New("reported_by is required")
	}
	spec, e := s.GetSpec(ctx, tenant, id)
	if e != nil {
		return models.AgentStatus{}, e
	}
	if spec.SourceID != reportedBy {
		return models.AgentStatus{}, ErrAgentOwnership
	}
	now := time.Now().UTC()
	if expected == 0 {
		_, e := s.db.ExecContext(ctx, `INSERT INTO agent_status(tenant_id,agent_id,observed_generation,health,current_load,available_capacity,draining,last_seen_at,expires_at,status_revision,reported_by) VALUES(?,?,?,?,?,?,0,?,?,1,?)`, tenant, id, p.ObservedGeneration, p.Health, p.CurrentLoad, p.AvailableCapacity, now.Format(time.RFC3339Nano), p.ExpiresAt.UTC().Format(time.RFC3339Nano), reportedBy)
		if e != nil {
			return models.AgentStatus{}, ErrAgentConflict
		}
	} else {
		r, e := s.db.ExecContext(ctx, `UPDATE agent_status SET observed_generation=?,health=?,current_load=?,available_capacity=?,last_seen_at=?,expires_at=?,status_revision=status_revision+1,reported_by=? WHERE tenant_id=? AND agent_id=? AND status_revision=?`, p.ObservedGeneration, p.Health, p.CurrentLoad, p.AvailableCapacity, now.Format(time.RFC3339Nano), p.ExpiresAt.UTC().Format(time.RFC3339Nano), reportedBy, tenant, id, expected)
		if e != nil {
			return models.AgentStatus{}, e
		}
		n, _ := r.RowsAffected()
		if n == 0 {
			return models.AgentStatus{}, ErrAgentConflict
		}
	}
	return s.GetStatus(ctx, tenant, id)
}
func (s *agentStore) SetDraining(ctx context.Context, tenant, id string, expected int64, draining bool, reportedBy string) (models.AgentStatus, error) {
	r, e := s.db.ExecContext(ctx, `UPDATE agent_status SET draining=?,status_revision=status_revision+1,reported_by=? WHERE tenant_id=? AND agent_id=? AND status_revision=? AND EXISTS (SELECT 1 FROM agents WHERE tenant_id=? AND agent_id=? AND source_id=?)`, draining, reportedBy, tenant, id, expected, tenant, id, reportedBy)
	if e != nil {
		return models.AgentStatus{}, e
	}
	n, _ := r.RowsAffected()
	if n == 0 {
		return models.AgentStatus{}, ErrAgentConflict
	}
	return s.GetStatus(ctx, tenant, id)
}

func (s *agentStore) SyncSource(ctx context.Context, snap models.DiscoverySnapshot) error {
	if snap.TenantID == "" || snap.SourceType == "" || snap.SourceID == "" {
		return errors.New("snapshot source metadata required")
	}
	seen := map[string]bool{}
	for _, x := range snap.Agents {
		if x.TenantID != snap.TenantID || x.SourceType != snap.SourceType || x.SourceID != snap.SourceID {
			return ErrAgentOwnership
		}
		if seen[x.AgentID] {
			return fmt.Errorf("duplicate agent_id %q", x.AgentID)
		}
		seen[x.AgentID] = true
	}
	tx, e := s.db.BeginTx(ctx, nil)
	if e != nil {
		return e
	}
	defer tx.Rollback()
	ts := &agentStore{db: s.db}
	_ = ts
	for _, x := range snap.Agents {
		var tenant, st, si string
		var rv int64
		e = tx.QueryRowContext(ctx, `SELECT tenant_id,source_type,source_id,resource_version FROM agents WHERE agent_id=?`, x.AgentID).Scan(&tenant, &st, &si, &rv)
		if e == nil && (tenant != snap.TenantID || st != snap.SourceType || si != snap.SourceID) {
			return ErrAgentOwnership
		}
		now := time.Now().UTC().Format(time.RFC3339Nano)
		caps, _ := json.Marshal(x.Capabilities)
		tools, _ := json.Marshal(x.SupportedTools)
		protos, _ := json.Marshal(x.SupportedProtocolVersions)
		sec, _ := json.Marshal(x.SupportedSecurityCapabilities)
		labels, _ := json.Marshal(x.Labels)
		if e == sql.ErrNoRows {
			_, e = tx.ExecContext(ctx, `INSERT INTO agents(agent_id,tenant_id,name,description,entrypoint_type,entrypoint_url,capabilities_json,tools_json,trust_domain,protocol_capabilities_json,security_capabilities_json,expected_workload_id,labels_json,priority,weight,max_concurrency,schedulable,source_type,source_id,external_revision,generation,resource_version,created_at,updated_at,version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,1,?,?,'')`, x.AgentID, x.TenantID, x.Name, x.Description, x.Entrypoint.Type, x.Entrypoint.URL, string(caps), string(tools), x.TrustDomain, string(protos), string(sec), x.ExpectedWorkloadID, string(labels), x.Priority, x.Weight, x.MaxConcurrency, x.Schedulable, x.SourceType, x.SourceID, x.ExternalRevision, now, now)
		} else if e == nil {
			_, e = tx.ExecContext(ctx, `UPDATE agents SET name=?,description=?,entrypoint_type=?,entrypoint_url=?,capabilities_json=?,tools_json=?,trust_domain=?,protocol_capabilities_json=?,security_capabilities_json=?,expected_workload_id=?,labels_json=?,priority=?,weight=?,max_concurrency=?,schedulable=?,external_revision=?,generation=generation+1,resource_version=resource_version+1,updated_at=?,deleted_at=NULL WHERE agent_id=?`, x.Name, x.Description, x.Entrypoint.Type, x.Entrypoint.URL, string(caps), string(tools), x.TrustDomain, string(protos), string(sec), x.ExpectedWorkloadID, string(labels), x.Priority, x.Weight, x.MaxConcurrency, x.Schedulable, x.ExternalRevision, now, x.AgentID)
		}
		if e != nil {
			return e
		}
	}
	rows, e := tx.QueryContext(ctx, `SELECT agent_id FROM agents WHERE tenant_id=? AND source_type=? AND source_id=? AND deleted_at IS NULL`, snap.TenantID, snap.SourceType, snap.SourceID)
	if e != nil {
		return e
	}
	var missing []string
	for rows.Next() {
		var id string
		if e = rows.Scan(&id); e != nil {
			rows.Close()
			return e
		}
		if !seen[id] {
			missing = append(missing, id)
		}
	}
	rows.Close()
	now := time.Now().UTC().Format(time.RFC3339Nano)
	for _, id := range missing {
		if _, e = tx.ExecContext(ctx, `UPDATE agents SET schedulable=0,deleted_at=?,updated_at=?,resource_version=resource_version+1 WHERE agent_id=?`, now, now, id); e != nil {
			return e
		}
	}
	_, e = tx.ExecContext(ctx, `INSERT INTO discovery_source_state(tenant_id,source_type,source_id,last_revision,last_success_at,last_attempt_at,stale,last_error,resource_version) VALUES(?,?,?,?,?,?,0,'',1) ON CONFLICT(tenant_id,source_type,source_id) DO UPDATE SET last_revision=excluded.last_revision,last_success_at=excluded.last_success_at,last_attempt_at=excluded.last_attempt_at,stale=0,last_error='',resource_version=resource_version+1`, snap.TenantID, snap.SourceType, snap.SourceID, snap.Revision, now, now)
	if e != nil {
		return e
	}
	return tx.Commit()
}
func (s *agentStore) MarkSourceStale(ctx context.Context, tenant, typ, id string, cause error) error {
	msg := ""
	if cause != nil {
		msg = cause.Error()
	}
	now := time.Now().UTC().Format(time.RFC3339Nano)
	_, e := s.db.ExecContext(ctx, `INSERT INTO discovery_source_state(tenant_id,source_type,source_id,last_attempt_at,stale,last_error,resource_version) VALUES(?,?,?,?,1,?,1) ON CONFLICT(tenant_id,source_type,source_id) DO UPDATE SET last_attempt_at=excluded.last_attempt_at,stale=1,last_error=excluded.last_error,resource_version=resource_version+1`, tenant, typ, id, now, msg)
	return e
}
func (s *agentStore) ExpireStatuses(ctx context.Context, now time.Time) (int64, error) {
	r, e := s.db.ExecContext(ctx, `UPDATE agent_status SET health='expired',available_capacity=0,status_revision=status_revision+1 WHERE expires_at<? AND health<>'expired'`, now.UTC().Format(time.RFC3339Nano))
	if e != nil {
		return 0, e
	}
	return r.RowsAffected()
}
