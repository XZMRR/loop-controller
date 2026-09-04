// Package store provides SQLite persistence for tasks, messages, events and
// idempotency keys.
package store

import (
	"context"
	"database/sql"
	"fmt"
	"os"
	"path/filepath"
	"time"

	"github.com/loop-controller/go/internal/instance"
	_ "modernc.org/sqlite"
)

const schema = `
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    interaction_id TEXT NOT NULL DEFAULT '',
    decision_id TEXT NOT NULL DEFAULT '',
    root_interaction_id TEXT NOT NULL DEFAULT '',
    parent_interaction_id TEXT NOT NULL DEFAULT '',
    initiator_agent_id TEXT NOT NULL,
    target_agent_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending','accepted','running','completed','failed','cancelled','outcome_unknown')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    outcome TEXT,
    error_code TEXT,
    delegation_token TEXT NOT NULL DEFAULT '',
    exec_owner TEXT NOT NULL DEFAULT '',
    exec_lease_expires_at INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tasks_session ON tasks(session_id);
CREATE INDEX IF NOT EXISTS idx_tasks_target ON tasks(target_agent_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status_updated ON tasks(status, updated_at);

CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    from_agent_id TEXT NOT NULL,
    to_agent_id TEXT NOT NULL,
    role TEXT NOT NULL,
    parts_json TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    protocol_version TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_task ON messages(task_id);

CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    published_at TEXT NOT NULL,
    published INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_task_published ON events(task_id, published);

CREATE TABLE IF NOT EXISTS lifecycle_outbox (
    outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    event TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    delivered_at TEXT,
    last_error TEXT,
    claimed_by TEXT NOT NULL DEFAULT '',
    claim_expires_at INTEGER NOT NULL DEFAULT 0,
    UNIQUE(task_id, event)
);
CREATE INDEX IF NOT EXISTS idx_lifecycle_outbox_pending ON lifecycle_outbox(delivered_at, next_attempt_at, outbox_id);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    key_hash TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    request_hash TEXT NOT NULL,
    response_body TEXT NOT NULL,
    response_status INTEGER NOT NULL,
    locked INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_idempotency_created ON idempotency_keys(created_at);

CREATE TABLE IF NOT EXISTS agents (
    agent_id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    entrypoint_type TEXT NOT NULL DEFAULT '',
    entrypoint_url TEXT NOT NULL DEFAULT '',
    capabilities_json TEXT NOT NULL DEFAULT '[]',
    trust_domain TEXT NOT NULL DEFAULT '',
    version TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS routed_messages (
    message_id TEXT PRIMARY KEY,
    from_agent_id TEXT NOT NULL,
    to_agent_id TEXT NOT NULL,
    role TEXT NOT NULL,
    parts_json TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    protocol_version TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_routed_messages_to ON routed_messages(to_agent_id);
CREATE INDEX IF NOT EXISTS idx_routed_messages_from ON routed_messages(from_agent_id);
`

// DefaultDBPath is used when no explicit database path is provided.
const DefaultDBPath = "./data/a2a.db"

// defaultExecutionLease is how long an instance owns a running task before the
// lease is considered expired and eligible for failover recovery.
const defaultExecutionLease = time.Minute

// DB wraps a sql.DB with the Loop Controller schema.
type DB struct {
	*sql.DB
	path           string
	instanceID     string
	executionLease time.Duration
}

// Open opens the SQLite database at path, creating the directory and schema
// as needed. If path is empty, DefaultDBPath is used.
func Open(ctx context.Context, path string) (*DB, error) {
	if path == "" {
		path = DefaultDBPath
	}
	abs, err := filepath.Abs(path)
	if err != nil {
		return nil, fmt.Errorf("resolve db path: %w", err)
	}
	dir := filepath.Dir(abs)
	if err := os.MkdirAll(dir, 0750); err != nil {
		return nil, fmt.Errorf("create db directory: %w", err)
	}

	// The DSN pragmas below are applied to every pooled connection, unlike a
	// one-shot PRAGMA statement which only affects the single connection that
	// ran it. Multiple kernel instances may share the same SQLite file, so
	// foreign_keys and busy_timeout must hold for all connections.
	dsn := abs + "?_busy_timeout=5000&_journal_mode=WAL&_foreign_keys=ON"
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, fmt.Errorf("open sqlite: %w", err)
	}

	if _, err := db.ExecContext(ctx, schema); err != nil {
		db.Close()
		return nil, fmt.Errorf("create schema: %w", err)
	}
	for _, column := range []string{"interaction_id", "decision_id", "root_interaction_id", "parent_interaction_id", "delegation_token"} {
		if err := ensureTaskTextColumn(ctx, db, column); err != nil {
			db.Close()
			return nil, err
		}
	}
	for _, column := range []string{"exec_owner", "exec_lease_expires_at"} {
		if err := ensureTaskColumn(ctx, db, column); err != nil {
			db.Close()
			return nil, err
		}
	}
	for _, column := range []string{"claimed_by", "claim_expires_at"} {
		if err := ensureOutboxColumn(ctx, db, column); err != nil {
			db.Close()
			return nil, err
		}
	}
	return &DB{DB: db, path: abs, instanceID: instance.New().String(), executionLease: defaultExecutionLease}, nil
}

func ensureTaskTextColumn(ctx context.Context, db *sql.DB, column string) error {
	rows, err := db.QueryContext(ctx, "PRAGMA table_info(tasks)")
	if err != nil {
		return fmt.Errorf("inspect tasks schema: %w", err)
	}
	defer rows.Close()
	for rows.Next() {
		var cid int
		var name, columnType string
		var notNull, primaryKey int
		var defaultValue any
		if err := rows.Scan(&cid, &name, &columnType, &notNull, &defaultValue, &primaryKey); err != nil {
			return fmt.Errorf("scan tasks schema: %w", err)
		}
		if name == column {
			return nil
		}
	}
	if err := rows.Err(); err != nil {
		return fmt.Errorf("inspect tasks schema: %w", err)
	}
	if _, err := db.ExecContext(ctx, "ALTER TABLE tasks ADD COLUMN "+column+" TEXT NOT NULL DEFAULT ''"); err != nil {
		return fmt.Errorf("add task %s: %w", column, err)
	}
	return nil
}

// ensureColumn adds a column to a table if it is absent. decl must be a full
// SQLite column declaration such as "TEXT NOT NULL DEFAULT ''".
func ensureColumn(ctx context.Context, db *sql.DB, table, column, decl string) error {
	rows, err := db.QueryContext(ctx, "PRAGMA table_info("+table+")")
	if err != nil {
		return fmt.Errorf("inspect %s schema: %w", table, err)
	}
	defer rows.Close()
	for rows.Next() {
		var cid int
		var name, columnType string
		var notNull, primaryKey int
		var defaultValue any
		if err := rows.Scan(&cid, &name, &columnType, &notNull, &defaultValue, &primaryKey); err != nil {
			return fmt.Errorf("scan %s schema: %w", table, err)
		}
		if name == column {
			return nil
		}
	}
	if err := rows.Err(); err != nil {
		return fmt.Errorf("inspect %s schema: %w", table, err)
	}
	if _, err := db.ExecContext(ctx, "ALTER TABLE "+table+" ADD COLUMN "+column+" "+decl); err != nil {
		return fmt.Errorf("add %s.%s: %w", table, column, err)
	}
	return nil
}

func ensureTaskColumn(ctx context.Context, db *sql.DB, column string) error {
	switch column {
	case "exec_owner":
		return ensureColumn(ctx, db, "tasks", column, "TEXT NOT NULL DEFAULT ''")
	case "exec_lease_expires_at":
		return ensureColumn(ctx, db, "tasks", column, "INTEGER NOT NULL DEFAULT 0")
	}
	return ensureColumn(ctx, db, "tasks", column, "TEXT NOT NULL DEFAULT ''")
}

func ensureOutboxColumn(ctx context.Context, db *sql.DB, column string) error {
	switch column {
	case "claimed_by":
		return ensureColumn(ctx, db, "lifecycle_outbox", column, "TEXT NOT NULL DEFAULT ''")
	case "claim_expires_at":
		return ensureColumn(ctx, db, "lifecycle_outbox", column, "INTEGER NOT NULL DEFAULT 0")
	}
	return ensureColumn(ctx, db, "lifecycle_outbox", column, "TEXT NOT NULL DEFAULT ''")
}

// Path returns the resolved filesystem path of the database.
func (db *DB) Path() string { return db.path }

// InstanceID returns this process's unique kernel instance identity.
func (db *DB) InstanceID() string { return db.instanceID }

// ExecutionLease returns how long an instance owns a running task.
func (db *DB) ExecutionLease() time.Duration { return db.executionLease }

// SetExecutionLease overrides the execution lease duration. It must be called
// before any task starts running; existing task stores share the value by
// reference, so late changes also affect them.
func (db *DB) SetExecutionLease(d time.Duration) {
	if d > 0 {
		db.executionLease = d
	}
}

// TaskStore returns a store backed by the underlying database.
func (db *DB) TaskStore() TaskStore {
	return &taskStore{db: db.DB, owner: db.instanceID, lease: &db.executionLease}
}

// MessageStore returns a store backed by the underlying database.
func (db *DB) MessageStore() MessageStore { return &messageStore{db: db.DB} }

// EventStore returns a store backed by the underlying database.
func (db *DB) EventStore() EventStore { return &eventStore{db: db.DB} }

// LifecycleOutboxStore returns a durable lifecycle delivery outbox.
func (db *DB) LifecycleOutboxStore() LifecycleOutboxStore {
	return &lifecycleOutboxStore{db: db.DB, owner: db.instanceID}
}

// IdempotencyStore returns a store backed by the underlying database.
func (db *DB) IdempotencyStore() IdempotencyStore { return &idempotencyStore{db: db.DB} }

// AgentStore returns a store for registered Agent Cards.
func (db *DB) AgentStore() AgentStore { return &agentStore{db: db.DB} }

// RoutedMessageStore returns a store for routed A2A messages.
func (db *DB) RoutedMessageStore() RoutedMessageStore { return &routedMessageStore{db: db.DB} }
