// Package store provides SQLite persistence for tasks, messages, events and
// idempotency keys.
package store

import (
	"context"
	"database/sql"
	"fmt"
	"os"
	"path/filepath"

	_ "modernc.org/sqlite"
)

const schema = `
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    initiator_agent_id TEXT NOT NULL,
    target_agent_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending','accepted','running','completed','failed','cancelled','outcome_unknown')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    outcome TEXT,
    error_code TEXT
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
`

// DefaultDBPath is used when no explicit database path is provided.
const DefaultDBPath = "./data/a2a.db"

// DB wraps a sql.DB with the Loop Controller schema.
type DB struct {
	*sql.DB
	path string
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

	db, err := sql.Open("sqlite", abs)
	if err != nil {
		return nil, fmt.Errorf("open sqlite: %w", err)
	}

	if _, err := db.ExecContext(ctx, "PRAGMA foreign_keys = ON"); err != nil {
		db.Close()
		return nil, fmt.Errorf("enable foreign keys: %w", err)
	}
	if _, err := db.ExecContext(ctx, "PRAGMA journal_mode = WAL"); err != nil {
		db.Close()
		return nil, fmt.Errorf("set wal mode: %w", err)
	}
	if _, err := db.ExecContext(ctx, schema); err != nil {
		db.Close()
		return nil, fmt.Errorf("create schema: %w", err)
	}
	return &DB{DB: db, path: abs}, nil
}

// Path returns the resolved filesystem path of the database.
func (db *DB) Path() string { return db.path }

// TaskStore returns a store backed by the underlying database.
func (db *DB) TaskStore() TaskStore { return &taskStore{db: db.DB} }

// MessageStore returns a store backed by the underlying database.
func (db *DB) MessageStore() MessageStore { return &messageStore{db: db.DB} }

// EventStore returns a store backed by the underlying database.
func (db *DB) EventStore() EventStore { return &eventStore{db: db.DB} }

// IdempotencyStore returns a store backed by the underlying database.
func (db *DB) IdempotencyStore() IdempotencyStore { return &idempotencyStore{db: db.DB} }
