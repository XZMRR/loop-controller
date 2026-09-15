package store

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"time"

	"github.com/loop-controller/go/internal/models"
)

type SnapshotStore struct {
	db *sql.DB
}

func (db *DB) SnapshotStore() *SnapshotStore { return &SnapshotStore{db: db.DB} }

func (s *SnapshotStore) GetTaskSnapshot(ctx context.Context, tenantID, taskID string) (models.TaskSnapshot, error) {
	return s.getTaskSnapshot(ctx, tenantID, taskID, nil)
}

func (s *SnapshotStore) getTaskSnapshot(ctx context.Context, tenantID, taskID string, afterTaskRead func()) (models.TaskSnapshot, error) {
	tx, err := s.db.BeginTx(ctx, &sql.TxOptions{ReadOnly: true})
	if err != nil {
		return models.TaskSnapshot{}, fmt.Errorf("begin task snapshot: %w", err)
	}
	defer tx.Rollback()

	out := models.TaskSnapshot{
		SchemaVersion: models.TaskSnapshotSchemaVersion,
		ReadAt:        time.Now().UTC(),
		Assignments:   []models.AssignmentSnapshot{},
	}
	out.Task, err = getTask(ctx, tx, "tenant_id = ? AND task_id = ?", tenantID, taskID)
	if err != nil {
		return models.TaskSnapshot{}, err
	}
	if afterTaskRead != nil {
		afterTaskRead()
	}

	var dagID string
	err = tx.QueryRowContext(ctx, `
		SELECT g.dag_id
		FROM task_graphs g
		LEFT JOIN task_graph_nodes n ON n.dag_id=g.dag_id AND n.tenant_id=g.tenant_id
		WHERE g.tenant_id=? AND (g.root_task_id=? OR n.task_id=?)
		LIMIT 1`, tenantID, taskID, taskID).Scan(&dagID)
	if err == nil {
		graph, loadErr := getGraphTx(ctx, tx, tenantID, dagID)
		if loadErr != nil {
			return models.TaskSnapshot{}, loadErr
		}
		out.Graph = &graph
	} else if !errors.Is(err, sql.ErrNoRows) {
		return models.TaskSnapshot{}, fmt.Errorf("find task graph: %w", err)
	}

	rows, err := tx.QueryContext(ctx, `SELECT assignment_id FROM task_assignments WHERE tenant_id=? AND task_id=? ORDER BY route_attempt,created_at,assignment_id`, tenantID, taskID)
	if err != nil {
		return models.TaskSnapshot{}, fmt.Errorf("list snapshot assignments: %w", err)
	}
	var assignmentIDs []string
	for rows.Next() {
		var id string
		if err = rows.Scan(&id); err != nil {
			rows.Close()
			return models.TaskSnapshot{}, err
		}
		assignmentIDs = append(assignmentIDs, id)
	}
	if err = rows.Err(); err != nil {
		rows.Close()
		return models.TaskSnapshot{}, err
	}
	rows.Close()

	for _, id := range assignmentIDs {
		assignment, loadErr := getAssignment(ctx, tx, `tenant_id=? AND task_id=? AND assignment_id=?`, tenantID, taskID, id)
		if loadErr != nil {
			return models.TaskSnapshot{}, loadErr
		}
		item := models.AssignmentSnapshot{Assignment: assignment, Attempts: []models.ExecutionAttempt{}}
		attemptRows, queryErr := tx.QueryContext(ctx, `
			SELECT e.assignment_id,e.tenant_id,e.task_id,e.agent_id,e.attempt,e.execution_fence,e.state,e.lease_owner,e.claim_token,e.lease_expires_at,e.failure_class,e.result_status,e.outcome_json,e.error_code,e.consumed_token_count,e.consumed_payment_amount,e.consumed_currency,e.execution_receipt_json,e.started_at,e.finished_at
			FROM execution_attempts e
			JOIN task_assignments a ON a.assignment_id=e.assignment_id AND a.tenant_id=e.tenant_id AND a.task_id=e.task_id
			WHERE a.tenant_id=? AND a.task_id=? AND a.assignment_id=?
			ORDER BY e.attempt,e.execution_fence`, tenantID, taskID, id)
		if queryErr != nil {
			return models.TaskSnapshot{}, fmt.Errorf("list snapshot attempts: %w", queryErr)
		}
		for attemptRows.Next() {
			var attempt models.ExecutionAttempt
			if queryErr = scanAttempt(attemptRows, &attempt); queryErr != nil {
				attemptRows.Close()
				return models.TaskSnapshot{}, queryErr
			}
			item.Attempts = append(item.Attempts, attempt)
		}
		if queryErr = attemptRows.Err(); queryErr != nil {
			attemptRows.Close()
			return models.TaskSnapshot{}, queryErr
		}
		attemptRows.Close()
		out.Assignments = append(out.Assignments, item)
	}
	if err = tx.Commit(); err != nil {
		return models.TaskSnapshot{}, fmt.Errorf("commit task snapshot: %w", err)
	}
	return out, nil
}
