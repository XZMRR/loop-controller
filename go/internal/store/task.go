package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"time"

	"github.com/loop-controller/go/internal/models"
)

// TaskStore persists and queries tasks.
type TaskStore interface {
	Create(ctx context.Context, t models.Task) error
	Get(ctx context.Context, taskID string) (models.Task, error)
	UpdateStatus(ctx context.Context, taskID, status string, outcome []byte, errorCode string) (models.Task, error)
	ListBySession(ctx context.Context, sessionID string) ([]models.Task, error)
	ListByTarget(ctx context.Context, targetAgentID string) ([]models.Task, error)
}

type taskStore struct {
	db *sql.DB
}

func (s *taskStore) Create(ctx context.Context, t models.Task) error {
	if t.TaskID == "" {
		return fmt.Errorf("task_id is required")
	}
	completedAt := sql.NullString{}
	if t.CompletedAt != nil {
		completedAt = sql.NullString{String: t.CompletedAt.Format(time.RFC3339), Valid: true}
	}
	outcome := sql.NullString{}
	if len(t.Outcome) > 0 {
		outcome = sql.NullString{String: string(t.Outcome), Valid: true}
	}
	_, err := s.db.ExecContext(ctx, `
		INSERT INTO tasks (task_id, session_id, initiator_agent_id, target_agent_id, status, created_at, updated_at, completed_at, outcome, error_code)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
	`, t.TaskID, t.SessionID, t.InitiatorAgentID, t.TargetAgentID, t.Status,
		t.CreatedAt.Format(time.RFC3339), t.UpdatedAt.Format(time.RFC3339),
		completedAt, outcome, t.ErrorCode)
	if err != nil {
		return fmt.Errorf("insert task: %w", err)
	}
	return nil
}

func (s *taskStore) Get(ctx context.Context, taskID string) (models.Task, error) {
	row := s.db.QueryRowContext(ctx, `
		SELECT task_id, session_id, initiator_agent_id, target_agent_id, status, created_at, updated_at, completed_at, outcome, error_code
		FROM tasks
		WHERE task_id = ?
	`, taskID)
	return scanTask(row)
}

func (s *taskStore) UpdateStatus(ctx context.Context, taskID, status string, outcome []byte, errorCode string) (models.Task, error) {
	now := time.Now().UTC()
	completedAt := sql.NullString{}
	if isTerminalStatus(status) {
		completedAt = sql.NullString{String: now.Format(time.RFC3339), Valid: true}
	}
	outcomeStr := sql.NullString{}
	if len(outcome) > 0 {
		outcomeStr = sql.NullString{String: string(outcome), Valid: true}
	}

	res, err := s.db.ExecContext(ctx, `
		UPDATE tasks
		SET status = ?, updated_at = ?, completed_at = COALESCE(?, completed_at), outcome = COALESCE(?, outcome), error_code = COALESCE(?, error_code)
		WHERE task_id = ?
	`, status, now.Format(time.RFC3339), completedAt, outcomeStr, errorCode, taskID)
	if err != nil {
		return models.Task{}, fmt.Errorf("update task status: %w", err)
	}
	n, err := res.RowsAffected()
	if err != nil {
		return models.Task{}, fmt.Errorf("rows affected: %w", err)
	}
	if n == 0 {
		return models.Task{}, fmt.Errorf("task not found: %w", sql.ErrNoRows)
	}
	return s.Get(ctx, taskID)
}

func (s *taskStore) ListBySession(ctx context.Context, sessionID string) ([]models.Task, error) {
	rows, err := s.db.QueryContext(ctx, `
		SELECT task_id, session_id, initiator_agent_id, target_agent_id, status, created_at, updated_at, completed_at, outcome, error_code
		FROM tasks
		WHERE session_id = ?
		ORDER BY created_at DESC
	`, sessionID)
	if err != nil {
		return nil, fmt.Errorf("list tasks by session: %w", err)
	}
	defer rows.Close()
	return scanTasks(rows)
}

func (s *taskStore) ListByTarget(ctx context.Context, targetAgentID string) ([]models.Task, error) {
	rows, err := s.db.QueryContext(ctx, `
		SELECT task_id, session_id, initiator_agent_id, target_agent_id, status, created_at, updated_at, completed_at, outcome, error_code
		FROM tasks
		WHERE target_agent_id = ?
		ORDER BY created_at DESC
	`, targetAgentID)
	if err != nil {
		return nil, fmt.Errorf("list tasks by target: %w", err)
	}
	defer rows.Close()
	return scanTasks(rows)
}

func scanTask(row *sql.Row) (models.Task, error) {
	var t models.Task
	var createdAt, updatedAt, completedAt sql.NullString
	var outcome sql.NullString
	err := row.Scan(&t.TaskID, &t.SessionID, &t.InitiatorAgentID, &t.TargetAgentID, &t.Status,
		&createdAt, &updatedAt, &completedAt, &outcome, &t.ErrorCode)
	if err != nil {
		if err == sql.ErrNoRows {
			return models.Task{}, err
		}
		return models.Task{}, fmt.Errorf("scan task: %w", err)
	}
	ca, err := time.Parse(time.RFC3339, createdAt.String)
	if err != nil {
		return models.Task{}, fmt.Errorf("parse created_at: %w", err)
	}
	ua, err := time.Parse(time.RFC3339, updatedAt.String)
	if err != nil {
		return models.Task{}, fmt.Errorf("parse updated_at: %w", err)
	}
	t.CreatedAt = ca
	t.UpdatedAt = ua
	if completedAt.Valid {
		c, err := time.Parse(time.RFC3339, completedAt.String)
		if err != nil {
			return models.Task{}, fmt.Errorf("parse completed_at: %w", err)
		}
		t.CompletedAt = &c
	}
	if outcome.Valid {
		t.Outcome = json.RawMessage(outcome.String)
	}
	return t, nil
}

func scanTasks(rows *sql.Rows) ([]models.Task, error) {
	var out []models.Task
	for rows.Next() {
		t, err := scanTaskFromRows(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, t)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate tasks: %w", err)
	}
	return out, nil
}

func scanTaskFromRows(rows *sql.Rows) (models.Task, error) {
	var t models.Task
	var createdAt, updatedAt, completedAt sql.NullString
	var outcome sql.NullString
	err := rows.Scan(&t.TaskID, &t.SessionID, &t.InitiatorAgentID, &t.TargetAgentID, &t.Status,
		&createdAt, &updatedAt, &completedAt, &outcome, &t.ErrorCode)
	if err != nil {
		return models.Task{}, fmt.Errorf("scan task row: %w", err)
	}
	ca, err := time.Parse(time.RFC3339, createdAt.String)
	if err != nil {
		return models.Task{}, fmt.Errorf("parse created_at: %w", err)
	}
	ua, err := time.Parse(time.RFC3339, updatedAt.String)
	if err != nil {
		return models.Task{}, fmt.Errorf("parse updated_at: %w", err)
	}
	t.CreatedAt = ca
	t.UpdatedAt = ua
	if completedAt.Valid {
		c, err := time.Parse(time.RFC3339, completedAt.String)
		if err != nil {
			return models.Task{}, fmt.Errorf("parse completed_at: %w", err)
		}
		t.CompletedAt = &c
	}
	if outcome.Valid {
		t.Outcome = json.RawMessage(outcome.String)
	}
	return t, nil
}

func isTerminalStatus(status string) bool {
	switch status {
	case "completed", "failed", "cancelled", "outcome_unknown":
		return true
	}
	return false
}
