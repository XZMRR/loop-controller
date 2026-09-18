package store

import (
	"context"
	"encoding/json"
	"time"

	"github.com/loop-controller/go/internal/models"
)

type QueueSnapshot struct {
	Depth            int64
	OldestLagSeconds float64
}

type ReadinessSnapshot struct {
	MigrationVersion int
	StaleAgents      int64
	EligibleTargets  int64
}

func CurrentMigrationVersion() int { return storeMigrations[len(storeMigrations)-1].version }

func (db *DB) Ping(ctx context.Context) error { return db.DB.PingContext(ctx) }

func (db *DB) QueueSnapshot(ctx context.Context, kind models.AssignmentKind, now time.Time) (QueueSnapshot, error) {
	var out QueueSnapshot
	var oldest string
	err := db.QueryRowContext(ctx, `SELECT COUNT(*),COALESCE(MIN(CASE WHEN state IN ('queued','retry_wait') AND (not_before IS NULL OR not_before<=?) THEN created_at END),'') FROM task_assignments WHERE assignment_kind=? AND state IN ('queued','retry_wait','claimed','dispatched','executing')`, formatTime(now), kind).Scan(&out.Depth, &oldest)
	if err != nil {
		return out, err
	}
	if oldest != "" {
		if t, e := time.Parse(time.RFC3339Nano, oldest); e == nil && now.After(t) {
			out.OldestLagSeconds = now.Sub(t).Seconds()
		}
	}
	return out, nil
}

func (db *DB) ReadinessSnapshot(ctx context.Context, now time.Time, requiredSecurity []string) (ReadinessSnapshot, error) {
	var out ReadinessSnapshot
	if err := db.QueryRowContext(ctx, `SELECT COALESCE(MAX(version),0) FROM schema_migrations`).Scan(&out.MigrationVersion); err != nil {
		return out, err
	}
	if err := db.QueryRowContext(ctx, `SELECT COUNT(*) FROM agent_status WHERE health='expired' OR expires_at<=?`, formatTime(now)).Scan(&out.StaleAgents); err != nil {
		return out, err
	}
	rows, err := db.QueryContext(ctx, `SELECT a.protocol_capabilities_json,a.security_capabilities_json,a.expected_workload_id FROM agents a JOIN agent_status s ON s.tenant_id=a.tenant_id AND s.agent_id=a.agent_id WHERE a.deleted_at IS NULL AND a.schedulable=1 AND a.max_concurrency>0 AND s.health='healthy' AND s.draining=0 AND s.observed_generation=a.generation AND s.expires_at>? AND s.available_capacity>0`, formatTime(now))
	if err != nil {
		return out, err
	}
	defer rows.Close()
	for rows.Next() {
		var protocolsJSON, securityJSON, workload string
		if err = rows.Scan(&protocolsJSON, &securityJSON, &workload); err != nil {
			return out, err
		}
		var protocols, security []string
		if unmarshalStringSlice(protocolsJSON, &protocols) != nil || unmarshalStringSlice(securityJSON, &security) != nil || workload == "" {
			continue
		}
		if stringSliceContains(protocols, models.CurrentProtocolVersion) && stringSliceContainsAll(security, requiredSecurity) {
			out.EligibleTargets++
		}
	}
	return out, rows.Err()
}

func unmarshalStringSlice(value string, out *[]string) error {
	return json.Unmarshal([]byte(value), out)
}
func stringSliceContains(items []string, wanted string) bool {
	for _, item := range items {
		if item == wanted {
			return true
		}
	}
	return false
}
func stringSliceContainsAll(items, wanted []string) bool {
	for _, value := range wanted {
		if !stringSliceContains(items, value) {
			return false
		}
	}
	return true
}
