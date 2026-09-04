package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"time"

	"github.com/loop-controller/go/internal/models"
)

// RoutedMessageStore persists and queries routed A2A messages that are not
// bound to a task.
type RoutedMessageStore interface {
	Save(ctx context.Context, msg models.Message) error
	ListByAgent(ctx context.Context, agentID string) ([]models.Message, error)
}

type routedMessageStore struct {
	db *sql.DB
}

func (s *routedMessageStore) Save(ctx context.Context, msg models.Message) error {
	parts, err := json.Marshal(msg.Parts)
	if err != nil {
		return fmt.Errorf("marshal routed message parts: %w", err)
	}
	_, err = s.db.ExecContext(ctx, `
		INSERT INTO routed_messages (message_id, from_agent_id, to_agent_id, role, parts_json, timestamp, protocol_version)
		VALUES (?, ?, ?, ?, ?, ?, ?)
	`, msg.MessageID, msg.FromAgentID, msg.ToAgentID, msg.Role, string(parts),
		msg.Timestamp.Format(time.RFC3339), msg.ProtocolVersion)
	if err != nil {
		return fmt.Errorf("insert routed message: %w", err)
	}
	return nil
}

func (s *routedMessageStore) ListByAgent(ctx context.Context, agentID string) ([]models.Message, error) {
	rows, err := s.db.QueryContext(ctx, `
		SELECT message_id, from_agent_id, to_agent_id, role, parts_json, timestamp, protocol_version
		FROM routed_messages
		WHERE from_agent_id = ? OR to_agent_id = ?
		ORDER BY timestamp ASC
	`, agentID, agentID)
	if err != nil {
		return nil, fmt.Errorf("list routed messages: %w", err)
	}
	defer rows.Close()

	var out []models.Message
	for rows.Next() {
		var m models.Message
		var partsJSON string
		var ts string
		if err := rows.Scan(&m.MessageID, &m.FromAgentID, &m.ToAgentID, &m.Role, &partsJSON, &ts, &m.ProtocolVersion); err != nil {
			return nil, fmt.Errorf("scan routed message: %w", err)
		}
		t, err := time.Parse(time.RFC3339, ts)
		if err != nil {
			return nil, fmt.Errorf("parse routed message timestamp: %w", err)
		}
		m.Timestamp = t
		if err := json.Unmarshal([]byte(partsJSON), &m.Parts); err != nil {
			return nil, fmt.Errorf("unmarshal routed message parts: %w", err)
		}
		out = append(out, m)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate routed messages: %w", err)
	}
	return out, nil
}
