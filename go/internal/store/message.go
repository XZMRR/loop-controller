package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"time"

	"github.com/loop-controller/go/internal/models"
)

// MessageStore persists and queries messages.
type MessageStore interface {
	Save(ctx context.Context, msg models.Message) error
	ListByTask(ctx context.Context, taskID string) ([]models.Message, error)
}

type messageStore struct {
	db *sql.DB
}

func (s *messageStore) Save(ctx context.Context, msg models.Message) error {
	parts, err := json.Marshal(msg.Parts)
	if err != nil {
		return fmt.Errorf("marshal message parts: %w", err)
	}
	_, err = s.db.ExecContext(ctx, `
		INSERT INTO messages (message_id, task_id, from_agent_id, to_agent_id, role, parts_json, timestamp, protocol_version)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?)
	`, msg.MessageID, msg.TaskID, msg.FromAgentID, msg.ToAgentID, msg.Role, string(parts),
		msg.Timestamp.Format(time.RFC3339), msg.ProtocolVersion)
	if err != nil {
		return fmt.Errorf("insert message: %w", err)
	}
	return nil
}

func (s *messageStore) ListByTask(ctx context.Context, taskID string) ([]models.Message, error) {
	rows, err := s.db.QueryContext(ctx, `
		SELECT message_id, task_id, from_agent_id, to_agent_id, role, parts_json, timestamp, protocol_version
		FROM messages
		WHERE task_id = ?
		ORDER BY timestamp ASC
	`, taskID)
	if err != nil {
		return nil, fmt.Errorf("list messages: %w", err)
	}
	defer rows.Close()
	return scanMessages(rows)
}

func scanMessages(rows *sql.Rows) ([]models.Message, error) {
	var out []models.Message
	for rows.Next() {
		var m models.Message
		var ts string
		var partsJSON string
		if err := rows.Scan(&m.MessageID, &m.TaskID, &m.FromAgentID, &m.ToAgentID, &m.Role, &partsJSON, &ts, &m.ProtocolVersion); err != nil {
			return nil, fmt.Errorf("scan message: %w", err)
		}
		t, err := time.Parse(time.RFC3339, ts)
		if err != nil {
			return nil, fmt.Errorf("parse message timestamp: %w", err)
		}
		m.Timestamp = t
		if err := json.Unmarshal([]byte(partsJSON), &m.Parts); err != nil {
			return nil, fmt.Errorf("unmarshal message parts: %w", err)
		}
		out = append(out, m)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate messages: %w", err)
	}
	return out, nil
}
