// Package task manages the lifecycle of inter-agent tasks.
package task

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/loop-controller/go/internal/models"
	"github.com/loop-controller/go/internal/store"
)

var (
	ErrTaskNotFound  = errors.New("task not found")
	ErrInvalidStatus = errors.New("invalid status transition")
)

var validTransitions = map[string]map[string]bool{
	"pending":         {"accepted": true, "cancelled": true},
	"accepted":        {"running": true, "cancelled": true},
	"running":         {"completed": true, "failed": true, "cancelled": true, "outcome_unknown": true},
	"outcome_unknown": {"completed": true, "failed": true},
	"completed":       {},
	"failed":          {},
	"cancelled":       {},
}

// Manager persists and updates tasks through a TaskStore.
type Manager struct {
	store store.TaskStore
}

// New creates a Manager backed by the given store.
func New(s store.TaskStore) *Manager {
	return &Manager{store: s}
}

// Create creates a new task and returns it.
func (m *Manager) Create(sessionID, initiatorAgentID, targetAgentID string) models.Task {
	now := time.Now().UTC()
	task := models.Task{
		TaskID:           generateID(),
		SessionID:        sessionID,
		InitiatorAgentID: initiatorAgentID,
		TargetAgentID:    targetAgentID,
		Status:           "pending",
		CreatedAt:        now,
		UpdatedAt:        now,
	}
	if err := m.store.Create(context.Background(), task); err != nil {
		// The memory-based implementation never failed; preserve that behaviour
		// by returning the unsaved task. Callers should treat persistence
		// failures as exceptional.
		return task
	}
	return task
}

// Get returns a task by id.
func (m *Manager) Get(taskID string) (models.Task, error) {
	task, err := m.store.Get(context.Background(), taskID)
	if err != nil {
		if errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
			return models.Task{}, err
		}
		return models.Task{}, ErrTaskNotFound
	}
	return task, nil
}

// UpdateStatus transitions a task to a new status.
func (m *Manager) UpdateStatus(taskID, status string) (models.Task, error) {
	if !isValidStatus(status) {
		return models.Task{}, ErrInvalidStatus
	}
	current, err := m.store.Get(context.Background(), taskID)
	if err != nil {
		return models.Task{}, ErrTaskNotFound
	}
	if !validTransitions[current.Status][status] {
		return models.Task{}, ErrInvalidStatus
	}
	updated, err := m.store.UpdateStatus(context.Background(), taskID, status, nil, "")
	if err != nil {
		if errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
			return models.Task{}, err
		}
		return models.Task{}, ErrTaskNotFound
	}
	return updated, nil
}

// CreateWithID creates a new task with the supplied identifiers. It is used by
// the entrypoint when the task record does not yet exist locally.
func (m *Manager) CreateWithID(taskID, sessionID, initiatorAgentID, targetAgentID string) (models.Task, error) {
	now := time.Now().UTC()
	task := models.Task{
		TaskID:           taskID,
		SessionID:        sessionID,
		InitiatorAgentID: initiatorAgentID,
		TargetAgentID:    targetAgentID,
		Status:           "pending",
		CreatedAt:        now,
		UpdatedAt:        now,
	}
	if err := m.store.Create(context.Background(), task); err != nil {
		return models.Task{}, err
	}
	return task, nil
}

// Complete transitions a task to a terminal completed/failed state and stores
// the outcome or error code.
func (m *Manager) Complete(taskID, status string, outcome json.RawMessage, errorCode string) (models.Task, error) {
	if status != "completed" && status != "failed" {
		return models.Task{}, ErrInvalidStatus
	}
	current, err := m.store.Get(context.Background(), taskID)
	if err != nil {
		return models.Task{}, ErrTaskNotFound
	}
	if !validTransitions[current.Status][status] {
		return models.Task{}, ErrInvalidStatus
	}
	updated, err := m.store.UpdateStatus(context.Background(), taskID, status, outcome, errorCode)
	if err != nil {
		if errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
			return models.Task{}, err
		}
		return models.Task{}, ErrTaskNotFound
	}
	return updated, nil
}

func isValidStatus(status string) bool {
	_, ok := validTransitions[status]
	return ok
}

func generateID() string {
	now := time.Now().UTC()
	return fmt.Sprintf("task-%s-%d", now.Format("20060102-150405"), now.UnixNano())
}
