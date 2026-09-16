package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"

	"github.com/loop-controller/go/internal/models"
)

// AgentStore persists and queries registered Agent Cards.
type AgentStore interface {
	Upsert(ctx context.Context, card models.AgentCard) error
	Get(ctx context.Context, agentID string) (models.AgentCard, error)
	Delete(ctx context.Context, agentID string) error
	List(ctx context.Context) ([]models.AgentCard, error)
}

type agentStore struct {
	db *sql.DB
}

func (s *agentStore) Upsert(ctx context.Context, card models.AgentCard) error {
	caps, err := json.Marshal(card.Capabilities)
	if err != nil {
		return fmt.Errorf("marshal agent capabilities: %w", err)
	}
	_, err = s.db.ExecContext(ctx, `
		INSERT INTO agents (agent_id, name, description, entrypoint_type, entrypoint_url, capabilities_json, trust_domain, version, execution_mode)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
		ON CONFLICT(agent_id) DO UPDATE SET
			name = excluded.name,
			description = excluded.description,
			entrypoint_type = excluded.entrypoint_type,
			entrypoint_url = excluded.entrypoint_url,
			capabilities_json = excluded.capabilities_json,
			trust_domain = excluded.trust_domain,
			version = excluded.version,
			execution_mode = excluded.execution_mode
	`, card.AgentID, card.Name, card.Description, card.Entrypoint.Type, card.Entrypoint.URL,
		string(caps), card.TrustDomain, card.Version, card.ExecutionMode)
	if err != nil {
		return fmt.Errorf("upsert agent: %w", err)
	}
	return nil
}

func (s *agentStore) Get(ctx context.Context, agentID string) (models.AgentCard, error) {
	row := s.db.QueryRowContext(ctx, `
		SELECT agent_id, name, description, entrypoint_type, entrypoint_url, capabilities_json, trust_domain, version, execution_mode
		FROM agents
		WHERE agent_id = ?
	`, agentID)
	return scanAgent(row)
}

func (s *agentStore) Delete(ctx context.Context, agentID string) error {
	res, err := s.db.ExecContext(ctx, `DELETE FROM agents WHERE agent_id = ?`, agentID)
	if err != nil {
		return fmt.Errorf("delete agent: %w", err)
	}
	n, err := res.RowsAffected()
	if err != nil {
		return fmt.Errorf("delete agent rows affected: %w", err)
	}
	if n == 0 {
		return sql.ErrNoRows
	}
	return nil
}

func (s *agentStore) List(ctx context.Context) ([]models.AgentCard, error) {
	rows, err := s.db.QueryContext(ctx, `
		SELECT agent_id, name, description, entrypoint_type, entrypoint_url, capabilities_json, trust_domain, version, execution_mode
		FROM agents
		ORDER BY agent_id ASC
	`)
	if err != nil {
		return nil, fmt.Errorf("list agents: %w", err)
	}
	defer rows.Close()

	var out []models.AgentCard
	for rows.Next() {
		var card models.AgentCard
		var capsJSON string
		if err := rows.Scan(&card.AgentID, &card.Name, &card.Description,
			&card.Entrypoint.Type, &card.Entrypoint.URL, &capsJSON, &card.TrustDomain, &card.Version, &card.ExecutionMode); err != nil {
			return nil, fmt.Errorf("scan agent: %w", err)
		}
		if err := json.Unmarshal([]byte(capsJSON), &card.Capabilities); err != nil {
			return nil, fmt.Errorf("unmarshal agent capabilities: %w", err)
		}
		out = append(out, card)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate agents: %w", err)
	}
	return out, nil
}

func scanAgent(row *sql.Row) (models.AgentCard, error) {
	var card models.AgentCard
	var capsJSON string
	if err := row.Scan(&card.AgentID, &card.Name, &card.Description,
		&card.Entrypoint.Type, &card.Entrypoint.URL, &capsJSON, &card.TrustDomain, &card.Version, &card.ExecutionMode); err != nil {
		if err == sql.ErrNoRows {
			return models.AgentCard{}, err
		}
		return models.AgentCard{}, fmt.Errorf("scan agent: %w", err)
	}
	if err := json.Unmarshal([]byte(capsJSON), &card.Capabilities); err != nil {
		return models.AgentCard{}, fmt.Errorf("unmarshal agent capabilities: %w", err)
	}
	return card, nil
}
