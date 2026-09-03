package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"sync/atomic"
	"time"

	"github.com/loop-controller/go/internal/models"
)

var (
	ErrStatusConflict    = errors.New("task status conflict")
	ErrInvalidTransition = errors.New("invalid task status transition")
)

var validStatusTransitions = map[string]map[string]bool{
	"pending":         {"accepted": true, "failed": true, "cancelled": true, "outcome_unknown": true},
	"accepted":        {"running": true, "cancelled": true},
	"running":         {"completed": true, "failed": true, "cancelled": true, "outcome_unknown": true},
	"outcome_unknown": {"completed": true, "failed": true, "cancelled": true},
	"completed":       {},
	"failed":          {},
	"cancelled":       {},
}

// TaskStore persists and queries tasks.
type TaskStore interface {
	Create(ctx context.Context, t models.Task) error
	CreateWithEvent(ctx context.Context, t models.Task) (models.TaskEvent, error)
	Get(ctx context.Context, taskID string) (models.Task, error)
	UpdateStatus(ctx context.Context, taskID, expectedStatus, status string, outcome []byte, errorCode string) (models.Task, models.TaskEvent, error)
	RecordLifecycle(ctx context.Context, task models.Task, event string) error
	SetDelegationToken(ctx context.Context, taskID, delegationToken string) error
	ListBySession(ctx context.Context, sessionID string) ([]models.Task, error)
	ListByTarget(ctx context.Context, targetAgentID string) ([]models.Task, error)
}

type taskStore struct {
	db      *sql.DB
	counter uint64
}

func (s *taskStore) Create(ctx context.Context, t models.Task) error {
	_, err := insertTask(ctx, s.db, t)
	return err
}

// CreateWithEvent inserts a task and its task_created event atomically.
func (s *taskStore) CreateWithEvent(ctx context.Context, t models.Task) (models.TaskEvent, error) {
	if t.TaskID == "" {
		return models.TaskEvent{}, fmt.Errorf("task_id is required")
	}
	payload, err := json.Marshal(t)
	if err != nil {
		return models.TaskEvent{}, fmt.Errorf("marshal task event payload: %w", err)
	}
	event := models.TaskEvent{
		ProtocolVersion: "0.40.0",
		EventID:         fmt.Sprintf("ev-%s-%d-%d", t.TaskID, t.CreatedAt.UnixNano(), atomic.AddUint64(&s.counter, 1)),
		TaskID:          t.TaskID,
		EventType:       "task_created",
		Payload:         payload,
		PublishedAt:     t.CreatedAt,
	}

	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return models.TaskEvent{}, fmt.Errorf("begin task creation: %w", err)
	}
	defer tx.Rollback()
	if _, err := insertTask(ctx, tx, t); err != nil {
		return models.TaskEvent{}, err
	}
	if _, err := tx.ExecContext(ctx, `
		INSERT INTO events (event_id, task_id, event_type, payload_json, published_at, published)
		VALUES (?, ?, ?, ?, ?, 0)
	`, event.EventID, event.TaskID, event.EventType, string(event.Payload), event.PublishedAt.Format(time.RFC3339)); err != nil {
		return models.TaskEvent{}, fmt.Errorf("insert task event: %w", err)
	}
	if err := tx.Commit(); err != nil {
		return models.TaskEvent{}, fmt.Errorf("commit task creation: %w", err)
	}
	return event, nil
}

type taskExecer interface {
	ExecContext(context.Context, string, ...any) (sql.Result, error)
}

func insertTask(ctx context.Context, execer taskExecer, t models.Task) (sql.Result, error) {
	if t.TaskID == "" {
		return nil, fmt.Errorf("task_id is required")
	}
	completedAt := sql.NullString{}
	if t.CompletedAt != nil {
		completedAt = sql.NullString{String: t.CompletedAt.Format(time.RFC3339), Valid: true}
	}
	outcome := sql.NullString{}
	if len(t.Outcome) > 0 {
		outcome = sql.NullString{String: string(t.Outcome), Valid: true}
	}
	res, err := execer.ExecContext(ctx, `
		INSERT INTO tasks (task_id, session_id, interaction_id, decision_id, root_interaction_id, parent_interaction_id, initiator_agent_id, target_agent_id, status, created_at, updated_at, completed_at, outcome, error_code, delegation_token)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
	`, t.TaskID, t.SessionID, t.InteractionID, t.DecisionID, t.RootInteractionID, t.ParentInteractionID,
		t.InitiatorAgentID, t.TargetAgentID, t.Status, t.CreatedAt.Format(time.RFC3339), t.UpdatedAt.Format(time.RFC3339),
		completedAt, outcome, t.ErrorCode, t.DelegationToken)
	if err != nil {
		return nil, fmt.Errorf("insert task: %w", err)
	}
	return res, nil
}

func (s *taskStore) Get(ctx context.Context, taskID string) (models.Task, error) {
	row := s.db.QueryRowContext(ctx, `
		SELECT task_id, session_id, interaction_id, decision_id, root_interaction_id, parent_interaction_id, initiator_agent_id, target_agent_id, status, created_at, updated_at, completed_at, outcome, error_code, delegation_token
		FROM tasks
		WHERE task_id = ?
	`, taskID)
	return scanTask(row)
}

func (s *taskStore) UpdateStatus(ctx context.Context, taskID, expectedStatus, status string, outcome []byte, errorCode string) (models.Task, models.TaskEvent, error) {
	if !validStatusTransitions[expectedStatus][status] {
		return models.Task{}, models.TaskEvent{}, ErrInvalidTransition
	}
	now := time.Now().UTC()
	completedAt := sql.NullString{}
	if isFinalStatus(status) {
		completedAt = sql.NullString{String: now.Format(time.RFC3339), Valid: true}
	}
	outcomeStr := sql.NullString{}
	if len(outcome) > 0 {
		outcomeStr = sql.NullString{String: string(outcome), Valid: true}
	}

	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return models.Task{}, models.TaskEvent{}, fmt.Errorf("begin task transition: %w", err)
	}
	defer tx.Rollback()
	res, err := tx.ExecContext(ctx, `
		UPDATE tasks
		SET status = ?, updated_at = ?, completed_at = COALESCE(?, completed_at), outcome = COALESCE(?, outcome), error_code = CASE WHEN ? = '' THEN error_code ELSE ? END
		WHERE task_id = ? AND status = ?
	`, status, now.Format(time.RFC3339), completedAt, outcomeStr, errorCode, errorCode, taskID, expectedStatus)
	if err != nil {
		return models.Task{}, models.TaskEvent{}, fmt.Errorf("update task status: %w", err)
	}
	n, err := res.RowsAffected()
	if err != nil {
		return models.Task{}, models.TaskEvent{}, fmt.Errorf("rows affected: %w", err)
	}
	if n == 0 {
		return models.Task{}, models.TaskEvent{}, ErrStatusConflict
	}
	updated, err := scanTask(tx.QueryRowContext(ctx, `
		SELECT task_id, session_id, interaction_id, decision_id, root_interaction_id, parent_interaction_id, initiator_agent_id, target_agent_id, status, created_at, updated_at, completed_at, outcome, error_code, delegation_token
		FROM tasks WHERE task_id = ?
	`, taskID))
	if err != nil {
		return models.Task{}, models.TaskEvent{}, err
	}
	payload, err := json.Marshal(updated)
	if err != nil {
		return models.Task{}, models.TaskEvent{}, fmt.Errorf("marshal task event payload: %w", err)
	}
	event := models.TaskEvent{
		ProtocolVersion: models.CurrentProtocolVersion,
		EventID:         fmt.Sprintf("ev-%s-%d-%d", taskID, now.UnixNano(), atomic.AddUint64(&s.counter, 1)),
		TaskID:          taskID,
		EventType:       eventTypeForStatus(status),
		Payload:         payload,
		PublishedAt:     now,
	}
	if _, err := tx.ExecContext(ctx, `
		INSERT INTO events (event_id, task_id, event_type, payload_json, published_at, published)
		VALUES (?, ?, ?, ?, ?, 0)
	`, event.EventID, event.TaskID, event.EventType, string(event.Payload), event.PublishedAt.Format(time.RFC3339)); err != nil {
		return models.Task{}, models.TaskEvent{}, fmt.Errorf("insert task event: %w", err)
	}
	if err := insertLifecycleOutbox(ctx, tx, updated, status, now); err != nil {
		return models.Task{}, models.TaskEvent{}, err
	}
	if err := tx.Commit(); err != nil {
		return models.Task{}, models.TaskEvent{}, fmt.Errorf("commit task transition: %w", err)
	}
	return updated, event, nil
}

func (s *taskStore) RecordLifecycle(ctx context.Context, task models.Task, event string) error {
	now := time.Now().UTC()
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("begin lifecycle record: %w", err)
	}
	defer tx.Rollback()
	if err := insertLifecycleOutbox(ctx, tx, task, event, now); err != nil {
		return err
	}
	if err := tx.Commit(); err != nil {
		return fmt.Errorf("commit lifecycle record: %w", err)
	}
	return nil
}

func (s *taskStore) SetDelegationToken(ctx context.Context, taskID, delegationToken string) error {
	res, err := s.db.ExecContext(ctx, `UPDATE tasks SET delegation_token = ? WHERE task_id = ?`, delegationToken, taskID)
	if err != nil {
		return fmt.Errorf("persist delegation token: %w", err)
	}
	n, err := res.RowsAffected()
	if err != nil {
		return fmt.Errorf("delegation token rows affected: %w", err)
	}
	if n == 0 {
		return sql.ErrNoRows
	}
	return nil
}

func (s *taskStore) ListBySession(ctx context.Context, sessionID string) ([]models.Task, error) {
	rows, err := s.db.QueryContext(ctx, `
		SELECT task_id, session_id, interaction_id, decision_id, root_interaction_id, parent_interaction_id, initiator_agent_id, target_agent_id, status, created_at, updated_at, completed_at, outcome, error_code, delegation_token
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
		SELECT task_id, session_id, interaction_id, decision_id, root_interaction_id, parent_interaction_id, initiator_agent_id, target_agent_id, status, created_at, updated_at, completed_at, outcome, error_code, delegation_token
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
	err := row.Scan(&t.TaskID, &t.SessionID, &t.InteractionID, &t.DecisionID, &t.RootInteractionID, &t.ParentInteractionID,
		&t.InitiatorAgentID, &t.TargetAgentID, &t.Status, &createdAt, &updatedAt, &completedAt, &outcome, &t.ErrorCode, &t.DelegationToken)
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
	t.ProtocolVersion = models.CurrentProtocolVersion
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
	err := rows.Scan(&t.TaskID, &t.SessionID, &t.InteractionID, &t.DecisionID, &t.RootInteractionID, &t.ParentInteractionID,
		&t.InitiatorAgentID, &t.TargetAgentID, &t.Status, &createdAt, &updatedAt, &completedAt, &outcome, &t.ErrorCode, &t.DelegationToken)
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
	t.ProtocolVersion = models.CurrentProtocolVersion
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

func eventTypeForStatus(status string) string {
	switch status {
	case "accepted":
		return "task_accepted"
	case "running":
		return "task_running"
	case "completed":
		return "task_completed"
	case "failed":
		return "task_failed"
	case "cancelled":
		return "task_cancelled"
	case "outcome_unknown":
		return "task_outcome_unknown"
	}
	return "task_updated"
}

func isFinalStatus(status string) bool {
	switch status {
	case "completed", "failed", "cancelled":
		return true
	}
	return false
}
