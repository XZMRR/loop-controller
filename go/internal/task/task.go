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
	"pending":         {"accepted": true, "failed": true, "cancelled": true, "outcome_unknown": true},
	"accepted":        {"running": true, "cancelled": true},
	"running":         {"completed": true, "failed": true, "cancelled": true, "outcome_unknown": true},
	"outcome_unknown": {"completed": true, "failed": true, "cancelled": true},
	"completed":       {},
	"failed":          {},
	"cancelled":       {},
}

// Manager persists and updates tasks through a TaskStore.
type EventFanout interface {
	PublishCommitted(models.TaskEvent)
}

type LifecycleAuditor interface {
	RecordLifecycle(context.Context, models.Task, string) error
}

type Manager struct {
	store   store.TaskStore
	fanout  EventFanout
	auditor LifecycleAuditor
}

// New creates a Manager backed by the given store.
func New(s store.TaskStore) *Manager {
	return &Manager{store: s}
}

// WithEventFanout delivers task events after their transaction commits.
func (m *Manager) WithEventFanout(fanout EventFanout) *Manager {
	m.fanout = fanout
	return m
}

func (m *Manager) WithLifecycleAuditor(auditor LifecycleAuditor) *Manager {
	m.auditor = auditor
	return m
}

func (m *Manager) RecordLifecycle(ctx context.Context, task models.Task, lifecycle string) error {
	if err := m.store.RecordLifecycle(ctx, task, lifecycle); err != nil {
		return err
	}
	if m.auditor != nil {
		return m.auditor.RecordLifecycle(ctx, task, lifecycle)
	}
	return nil
}

// Create creates a new task and returns it. It is retained for compatibility;
// lifecycle code should use CreateReliable so persistence failures are visible.
func (m *Manager) Create(sessionID, initiatorAgentID, targetAgentID string) models.Task {
	task, _ := m.CreateReliable(sessionID, initiatorAgentID, targetAgentID)
	return task
}

// CreateReliable creates and persists a task before returning success.
func (m *Manager) CreateReliable(sessionID, initiatorAgentID, targetAgentID string) (models.Task, error) {
	return m.CreateInteraction(sessionID, initiatorAgentID, targetAgentID, "", "", "", "")
}

// CreateInteraction creates a task bound to its interaction authorization.
func (m *Manager) CreateInteraction(sessionID, initiatorAgentID, targetAgentID, interactionID, decisionID, rootInteractionID, parentInteractionID string) (models.Task, error) {
	now := time.Now().UTC()
	task := models.Task{
		ProtocolVersion:     models.CurrentProtocolVersion,
		TaskID:              generateID(),
		SessionID:           sessionID,
		InteractionID:       interactionID,
		DecisionID:          decisionID,
		RootInteractionID:   rootInteractionID,
		ParentInteractionID: parentInteractionID,
		InitiatorAgentID:    initiatorAgentID,
		TargetAgentID:       targetAgentID,
		Status:              "pending",
		CreatedAt:           now,
		UpdatedAt:           now,
	}
	event, err := m.store.CreateWithEvent(context.Background(), task)
	if err != nil {
		return models.Task{}, fmt.Errorf("persist task and created event: %w", err)
	}
	if m.fanout != nil {
		m.fanout.PublishCommitted(event)
	}
	return task, nil
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

// UpdateStatus transitions a task using the protocol-defined compare-and-set expectation.
func (m *Manager) UpdateStatus(taskID, status string) (models.Task, error) {
	expectedStatus, ok := expectedStatusFor(status)
	if !ok {
		return models.Task{}, ErrInvalidStatus
	}
	return m.updateStatus(taskID, expectedStatus, status, nil, "")
}

// UpdateStatusFrom transitions a task from the explicitly expected source state.
func (m *Manager) UpdateStatusFrom(taskID, expectedStatus, status string) (models.Task, error) {
	return m.updateStatus(taskID, expectedStatus, status, nil, "")
}

// MarkOutcomeUnknown records an uncertain execution result. Only a running task
// may enter this recoverable state.
func (m *Manager) MarkOutcomeUnknown(taskID string) (models.Task, error) {
	return m.updateStatus(taskID, "running", "outcome_unknown", nil, "")
}

func (m *Manager) updateStatus(taskID, expectedStatus, status string, outcome json.RawMessage, errorCode string) (models.Task, error) {
	if !validTransitions[expectedStatus][status] {
		return models.Task{}, ErrInvalidStatus
	}
	updated, event, err := m.store.UpdateStatus(context.Background(), taskID, expectedStatus, status, outcome, errorCode)
	if err != nil {
		if errors.Is(err, store.ErrStatusConflict) || errors.Is(err, store.ErrInvalidTransition) {
			return models.Task{}, ErrInvalidStatus
		}
		if errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
			return models.Task{}, err
		}
		return models.Task{}, ErrTaskNotFound
	}
	if m.fanout != nil {
		m.fanout.PublishCommitted(event)
	}
	return updated, nil
}

// SetDelegationToken persists the token used to dispatch a task for later cancellation.
func (m *Manager) SetDelegationToken(taskID, delegationToken string) error {
	if err := m.store.SetDelegationToken(context.Background(), taskID, delegationToken); err != nil {
		return fmt.Errorf("persist task delegation token: %w", err)
	}
	return nil
}

// CreateWithID creates a new task with the supplied identifiers. It is used by
// the entrypoint when the task record does not yet exist locally.
func (m *Manager) CreateWithID(taskID, sessionID, initiatorAgentID, targetAgentID string) (models.Task, error) {
	return m.CreateWithInteractionID(taskID, sessionID, initiatorAgentID, targetAgentID, "", "", "", "")
}

// CreateWithInteractionID creates a target-side task retaining authorization linkage.
func (m *Manager) CreateWithInteractionID(taskID, sessionID, initiatorAgentID, targetAgentID, interactionID, decisionID, rootInteractionID, parentInteractionID string) (models.Task, error) {
	now := time.Now().UTC()
	task := models.Task{
		ProtocolVersion:     models.CurrentProtocolVersion,
		TaskID:              taskID,
		SessionID:           sessionID,
		InteractionID:       interactionID,
		DecisionID:          decisionID,
		RootInteractionID:   rootInteractionID,
		ParentInteractionID: parentInteractionID,
		InitiatorAgentID:    initiatorAgentID,
		TargetAgentID:       targetAgentID,
		Status:              "pending",
		CreatedAt:           now,
		UpdatedAt:           now,
	}
	event, err := m.store.CreateWithEvent(context.Background(), task)
	if err != nil {
		return models.Task{}, err
	}
	if m.fanout != nil {
		m.fanout.PublishCommitted(event)
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
	if current.Status != "running" && current.Status != "outcome_unknown" {
		return models.Task{}, ErrInvalidStatus
	}
	return m.updateStatus(taskID, current.Status, status, outcome, errorCode)
}

func expectedStatusFor(status string) (string, bool) {
	switch status {
	case "accepted":
		return "pending", true
	case "running":
		return "accepted", true
	}
	return "", false
}

func generateID() string {
	now := time.Now().UTC()
	return fmt.Sprintf("task-%s-%d", now.Format("20060102-150405"), now.UnixNano())
}
