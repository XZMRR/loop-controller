package store

import (
	"context"
	"database/sql"
	"errors"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"testing"
	"time"
)

const frozenMigration1Checksum = "sha256:e56cf5b571742cfa8e2777650e9c392d65e88caf4f0a685f227c6ff76109b0d9"

func TestMigration1ChecksumFrozen(t *testing.T) {
	if got := migrationChecksum(storeMigrations[0]); got != frozenMigration1Checksum {
		t.Fatalf("migration 1 checksum = %q, want %q", got, frozenMigration1Checksum)
	}
}

func TestMigrationFreshAndReopen(t *testing.T) {
	ctx := context.Background()
	path := filepath.Join(t.TempDir(), "fresh.db")
	for range 2 {
		db, err := Open(ctx, path)
		if err != nil {
			t.Fatal(err)
		}
		var version int
		var name, checksum string
		if err := db.QueryRowContext(ctx, `SELECT version, name, checksum FROM schema_migrations WHERE version=1`).Scan(&version, &name, &checksum); err != nil {
			db.Close()
			t.Fatal(err)
		}
		if version != 1 || name != "adopt_current_store_schema" || checksum != migrationChecksum(storeMigrations[0]) {
			db.Close()
			t.Fatalf("migration identity = (%d, %q, %q), want frozen migration 1", version, name, checksum)
		}
		if !regexp.MustCompile(`^sha256:[0-9a-f]{64}$`).MatchString(checksum) {
			db.Close()
			t.Fatalf("migration checksum = %q, want sha256: followed by 64 lowercase hex characters", checksum)
		}
		if err := db.Close(); err != nil {
			t.Fatal(err)
		}
	}
}

func TestMigrationAdoptsUnversionedCurrentDatabase(t *testing.T) {
	ctx := context.Background()
	path := filepath.Join(t.TempDir(), "current.db")
	db, err := Open(ctx, path)
	if err != nil {
		t.Fatal(err)
	}
	_, err = db.ExecContext(ctx, `INSERT INTO tasks(task_id, session_id, initiator_agent_id, target_agent_id, status, created_at, updated_at) VALUES('task-1','session-1','a','b','pending','now','now')`)
	if err == nil {
		_, err = db.ExecContext(ctx, `INSERT INTO task_assignments(assignment_id, tenant_id, task_id, agent_id, state, revision, delivery_id, created_at, updated_at) VALUES('assignment-1','tenant-1','task-1','b','queued',1,'delivery-1','now','now')`)
	}
	if err == nil {
		_, err = db.ExecContext(ctx, `DELETE FROM schema_migrations`)
	}
	if closeErr := db.Close(); err == nil {
		err = closeErr
	}
	if err != nil {
		t.Fatal(err)
	}

	db, err = Open(ctx, path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	var assignmentID string
	if err := db.QueryRowContext(ctx, `SELECT assignment_id FROM task_assignments WHERE task_id='task-1'`).Scan(&assignmentID); err != nil {
		t.Fatal(err)
	}
	if assignmentID != "assignment-1" {
		t.Fatalf("assignment_id = %q", assignmentID)
	}
}

func TestMigrationUpgradesLegacyColumnsAndBackfillsAuditDelivery(t *testing.T) {
	ctx := context.Background()
	path := filepath.Join(t.TempDir(), "legacy.db")
	raw, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatal(err)
	}
	legacy := `
CREATE TABLE tasks (
 task_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, initiator_agent_id TEXT NOT NULL,
 target_agent_id TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE lifecycle_outbox (
 outbox_id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, event TEXT NOT NULL,
 payload_json TEXT NOT NULL, created_at TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
 next_attempt_at TEXT NOT NULL, delivered_at TEXT, last_error TEXT, UNIQUE(task_id,event)
);
CREATE TABLE approval_audit_outbox (
 outbox_id INTEGER PRIMARY KEY AUTOINCREMENT, approval_id TEXT NOT NULL, event TEXT NOT NULL,
 payload_json TEXT NOT NULL, created_at TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
 next_attempt_at TEXT NOT NULL, delivered_at TEXT, last_error TEXT NOT NULL DEFAULT '',
 claimed_by TEXT NOT NULL DEFAULT '', claim_token TEXT NOT NULL DEFAULT '', claim_expires_at INTEGER NOT NULL DEFAULT 0,
 UNIQUE(approval_id,event)
);
INSERT INTO tasks(task_id,session_id,initiator_agent_id,target_agent_id,status,created_at,updated_at)
 VALUES('legacy-task','legacy-session','a','b','pending','now','now');
INSERT INTO approval_audit_outbox(approval_id,event,payload_json,created_at,next_attempt_at) VALUES('approval-1','approved','{}','now','now');`
	if _, err := raw.ExecContext(ctx, legacy); err != nil {
		raw.Close()
		t.Fatal(err)
	}
	if err := raw.Close(); err != nil {
		t.Fatal(err)
	}

	db, err := Open(ctx, path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	for _, tc := range []struct{ table, column string }{{"tasks", "tenant_id"}, {"tasks", "deadline"}, {"lifecycle_outbox", "claim_token"}, {"approval_audit_outbox", "delivery_id"}, {"task_assignments", "assignment_id"}} {
		if !columnExists(t, db.DB, tc.table, tc.column) {
			t.Errorf("missing %s.%s", tc.table, tc.column)
		}
	}
	var sessionID string
	if err := db.QueryRowContext(ctx, `SELECT session_id FROM tasks WHERE task_id='legacy-task'`).Scan(&sessionID); err != nil {
		t.Fatal(err)
	}
	if sessionID != "legacy-session" {
		t.Fatalf("legacy task session_id = %q", sessionID)
	}
	var deliveryID string
	if err := db.QueryRowContext(ctx, `SELECT delivery_id FROM approval_audit_outbox WHERE approval_id='approval-1'`).Scan(&deliveryID); err != nil {
		t.Fatal(err)
	}
	if deliveryID != "approval-audit:approval-1:approved" {
		t.Fatalf("delivery_id = %q", deliveryID)
	}
}

func TestMigration6AddsRetryDeadLetterSchema(t *testing.T) {
	ctx := context.Background()
	db, err := Open(ctx, filepath.Join(t.TempDir(), "retry.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	for _, column := range []string{"retry_policy_json", "replay_count", "route_attempt", "supersedes_assignment_id", "dispatch_disposition", "failover_transition_id"} {
		if !columnExists(t, db.DB, "task_assignments", column) {
			t.Fatalf("missing task_assignments.%s", column)
		}
	}
	for _, column := range []string{"actor", "action", "result", "correlation_id"} {
		if !columnExists(t, db.DB, "assignment_retry_events", column) {
			t.Fatalf("missing assignment_retry_events.%s", column)
		}
	}
	var version int
	if err = db.QueryRowContext(ctx, `SELECT version FROM schema_migrations WHERE version=6 AND name='budget_aware_retry_dead_letter'`).Scan(&version); err != nil || version != 6 {
		t.Fatalf("migration 6 version=%d err=%v", version, err)
	}
	var count int
	if err = db.QueryRowContext(ctx, `SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='assignment_retry_events'`).Scan(&count); err != nil || count != 1 {
		t.Fatalf("retry events table count=%d err=%v", count, err)
	}
	if err = db.QueryRowContext(ctx, `SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name='idx_task_assignments_dead_letter_tenant'`).Scan(&count); err != nil || count != 1 {
		t.Fatalf("dead-letter index count=%d err=%v", count, err)
	}
}

func TestMigration7UpgradesV6AndBackfillsPerTaskSequence(t *testing.T) {
	ctx := context.Background()
	path := filepath.Join(t.TempDir(), "v6.db")
	dsn := path + "?_busy_timeout=5000&_journal_mode=WAL&_foreign_keys=ON"
	if err := runMigrations(ctx, dsn, storeMigrations[:6]); err != nil {
		t.Fatal(err)
	}
	raw, err := sql.Open("sqlite", dsn)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now().UTC().Format(time.RFC3339Nano)
	for _, taskID := range []string{"task-a", "task-b"} {
		if _, err := raw.ExecContext(ctx, `INSERT INTO tasks(task_id,session_id,initiator_agent_id,target_agent_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)`, taskID, "s", "a", "b", "pending", now, now); err != nil {
			raw.Close()
			t.Fatal(err)
		}
	}
	for _, item := range []struct{ eventID, taskID string }{{"a-1", "task-a"}, {"b-1", "task-b"}, {"a-2", "task-a"}, {"b-2", "task-b"}, {"a-3", "task-a"}} {
		if _, err := raw.ExecContext(ctx, `INSERT INTO events(event_id,task_id,event_type,payload_json,published_at) VALUES(?,?, 'test','{}',?)`, item.eventID, item.taskID, now); err != nil {
			raw.Close()
			t.Fatal(err)
		}
	}
	if err := raw.Close(); err != nil {
		t.Fatal(err)
	}

	if err := runMigrations(ctx, dsn, storeMigrations); err != nil {
		t.Fatal(err)
	}
	raw, err = sql.Open("sqlite", dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer raw.Close()
	rows, err := raw.QueryContext(ctx, `SELECT event_id,task_id,sequence,schema_version FROM events ORDER BY task_id,sequence`)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	want := []struct {
		eventID, taskID string
		sequence        int64
	}{{"a-1", "task-a", 1}, {"a-2", "task-a", 2}, {"a-3", "task-a", 3}, {"b-1", "task-b", 1}, {"b-2", "task-b", 2}}
	for i := 0; rows.Next(); i++ {
		if i >= len(want) {
			t.Fatal("too many backfilled events")
		}
		var eventID, taskID string
		var sequence int64
		var schemaVersion int
		if err := rows.Scan(&eventID, &taskID, &sequence, &schemaVersion); err != nil {
			t.Fatal(err)
		}
		if eventID != want[i].eventID || taskID != want[i].taskID || sequence != want[i].sequence || schemaVersion != 1 {
			t.Fatalf("backfill row %d = %s/%s/%d/v%d", i, eventID, taskID, sequence, schemaVersion)
		}
	}
	var version int
	if err := raw.QueryRowContext(ctx, `SELECT version FROM schema_migrations WHERE version=7`).Scan(&version); err != nil || version != 7 {
		t.Fatalf("migration 7 version=%d err=%v", version, err)
	}
}

func TestMigrationConcurrentOpen(t *testing.T) {
	ctx := context.Background()
	path := filepath.Join(t.TempDir(), "concurrent.db")
	start := make(chan struct{})
	var wg sync.WaitGroup
	errs := make(chan error, 2)
	for range 2 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			<-start
			db, err := Open(ctx, path)
			if err == nil {
				err = db.Close()
			}
			errs <- err
		}()
	}
	close(start)
	wg.Wait()
	close(errs)
	for err := range errs {
		if err != nil {
			t.Fatal(err)
		}
	}
	db, err := Open(ctx, path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	var count int
	if err := db.QueryRowContext(ctx, `SELECT COUNT(*) FROM schema_migrations`).Scan(&count); err != nil {
		t.Fatal(err)
	}
	if count != len(storeMigrations) {
		t.Fatalf("migration count = %d, want %d", count, len(storeMigrations))
	}
}

func TestMigrationRejectsChecksumMismatch(t *testing.T) {
	path := filepath.Join(t.TempDir(), "checksum.db")
	db := openAndClose(t, path)
	if _, err := db.Exec(`UPDATE schema_migrations SET checksum='bad' WHERE version=1`); err != nil {
		t.Fatal(err)
	}
	db.Close()
	_, err := Open(context.Background(), path)
	if err == nil || !strings.Contains(err.Error(), "identity mismatch") {
		t.Fatalf("Open error = %v, want identity mismatch", err)
	}
}

func TestMigrationRejectsVersionGap(t *testing.T) {
	ctx := context.Background()
	path := filepath.Join(t.TempDir(), "gap.db")
	raw, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatal(err)
	}
	_, err = raw.ExecContext(ctx, `CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,name TEXT NOT NULL,checksum TEXT NOT NULL,applied_at TEXT NOT NULL);
INSERT INTO schema_migrations(version,name,checksum,applied_at) VALUES(2,'future','future','now')`)
	if closeErr := raw.Close(); err == nil {
		err = closeErr
	}
	if err != nil {
		t.Fatal(err)
	}
	_, err = Open(ctx, path)
	if err == nil || !strings.Contains(err.Error(), "version gap") {
		t.Fatalf("Open error = %v, want version gap", err)
	}
}

func TestMigrationRejectsUnknownVersion(t *testing.T) {
	path := filepath.Join(t.TempDir(), "unknown.db")
	db := openAndClose(t, path)
	if _, err := db.Exec(`INSERT INTO schema_migrations(version,name,checksum,applied_at) VALUES(8,'future','future','now')`); err != nil {
		t.Fatal(err)
	}
	db.Close()
	_, err := Open(context.Background(), path)
	if err == nil || !strings.Contains(err.Error(), "unknown schema migration version 8") {
		t.Fatalf("Open error = %v, want unknown version 8", err)
	}
}

func TestMigrationFailureRollsBackEntireTransaction(t *testing.T) {
	ctx := context.Background()
	path := filepath.Join(t.TempDir(), "rollback.db")
	dsn := path + "?_busy_timeout=5000&_journal_mode=WAL&_foreign_keys=ON"
	injected := []migration{{
		version: 1,
		name:    "injected_failure",
		source:  "create marker then fail",
		apply: func(ctx context.Context, db queryExecer) error {
			if _, err := db.ExecContext(ctx, `CREATE TABLE migration_marker(value TEXT)`); err != nil {
				return err
			}
			return errors.New("injected")
		},
	}}
	if err := runMigrations(ctx, dsn, injected); err == nil || !strings.Contains(err.Error(), "injected") {
		t.Fatalf("runMigrations error = %v", err)
	}
	raw, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatal(err)
	}
	defer raw.Close()
	var count int
	if err := raw.QueryRowContext(ctx, `SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN ('schema_migrations','migration_marker')`).Scan(&count); err != nil {
		t.Fatal(err)
	}
	if count != 0 {
		t.Fatalf("rolled back table count = %d, want 0", count)
	}
}

func openAndClose(t *testing.T, path string) *sql.DB {
	t.Helper()
	db, err := Open(context.Background(), path)
	if err != nil {
		t.Fatal(err)
	}
	if err := db.Close(); err != nil {
		t.Fatal(err)
	}
	raw, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func columnExists(t *testing.T, db *sql.DB, table, column string) bool {
	t.Helper()
	rows, err := db.Query(`PRAGMA table_info(` + table + `)`)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	for rows.Next() {
		var cid, notNull, primaryKey int
		var name, typ string
		var defaultValue any
		if err := rows.Scan(&cid, &name, &typ, &notNull, &defaultValue, &primaryKey); err != nil {
			t.Fatal(err)
		}
		if name == column {
			return true
		}
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	return false
}
