// Package task manages the lifecycle of inter-agent tasks.
package task

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"sync/atomic"
	"time"

	"github.com/loop-controller/go/internal/models"
	"github.com/loop-controller/go/internal/store"
)

var (
	ErrTaskNotFound  = errors.New("task not found")
	ErrInvalidStatus = errors.New("invalid status transition")
)

var taskIDSequence atomic.Uint64

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
	RecordLifecycle(context.Context, models.Task, string, string) error
}

type Manager struct {
	store      store.TaskStore
	fanout     EventFanout
	auditor    LifecycleAuditor
	instanceID string
}

// New creates a Manager backed by the given store.
func New(s store.TaskStore) *Manager {
	return &Manager{store: s}
}

// WithInstanceID sets the owning kernel instance identity used to prefix
// generated task IDs, keeping them unique across instances sharing a store.
func (m *Manager) WithInstanceID(id string) *Manager {
	m.instanceID = id
	return m
}

func (m *Manager) DelegationApprovalStore() store.DelegationApprovalStore {
	if provider, ok := m.store.(interface {
		DelegationApprovalStore() store.DelegationApprovalStore
	}); ok {
		return provider.DelegationApprovalStore()
	}
	return nil
}

func (m *Manager) InstanceID() string { return m.instanceID }

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
		return m.auditor.RecordLifecycle(ctx, task, lifecycle, fmt.Sprintf("lifecycle:%s:%s", task.TaskID, lifecycle))
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
	return m.CreateInteractionTask(models.Task{
		SessionID:           sessionID,
		InteractionID:       interactionID,
		DecisionID:          decisionID,
		RootInteractionID:   rootInteractionID,
		ParentInteractionID: parentInteractionID,
		InitiatorAgentID:    initiatorAgentID,
		TargetAgentID:       targetAgentID,
	})
}

// PrepareInteractionTask derives a task identity and initial state without persisting it.
func (m *Manager) PrepareInteractionTask(task models.Task) models.Task {
	now := time.Now().UTC()
	task.ProtocolVersion = models.CurrentProtocolVersion
	task.TaskID = m.generateID()
	task.Status = "pending"
	task.CreatedAt = now
	task.UpdatedAt = now
	return task
}

// CreateInteractionTask creates a task with kernel-derived delegation linkage.
func (m *Manager) CreateInteractionTask(task models.Task) (models.Task, error) {
	task = m.PrepareInteractionTask(task)
	var event models.TaskEvent
	var err error
	if task.ParentTaskID == "" {
		event, err = m.store.CreateWithEvent(context.Background(), task)
	} else {
		event, err = m.store.CreateChildWithBudget(context.Background(), task)
	}
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

func (m *Manager) ListDescendants(ctx context.Context, taskID string) ([]models.Task, error) {
	return m.store.ListDescendants(ctx, taskID)
}

func (m *Manager) CancelDescendants(ctx context.Context, taskID string) ([]models.Task, error) {
	cancelled, err := m.store.CancelDescendants(ctx, taskID)
	if err != nil {
		return nil, err
	}
	if m.fanout != nil {
		for _, t := range cancelled {
			m.fanout.PublishCommitted(models.TaskEvent{ProtocolVersion: models.CurrentProtocolVersion, TaskID: t.TaskID, EventType: "task_cancelled", PublishedAt: t.UpdatedAt})
		}
	}
	return cancelled, nil
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

// RenewExecutionLease extends the execution lease of a running task owned by
// this instance. It returns store.ErrExecutionLeaseLost when ownership or the
// running state no longer matches.
func (m *Manager) RenewExecutionLease(ctx context.Context, taskID string) error {
	return m.store.RenewExecutionLease(ctx, taskID)
}

// RecoverExpiredRunning transitions running tasks whose execution lease expired
// to outcome_unknown. Each transition is guarded by an atomic lease-expiry
// compare-and-set, so concurrent instances cannot double-recover a task. The
// recovery events are already persisted in the shared event store; this only
// wakes in-process SSE subscribers to read them.
func (m *Manager) RecoverExpiredRunning(ctx context.Context, now time.Time, limit int) ([]models.Task, error) {
	recovered, err := m.store.RecoverExpiredRunning(ctx, now, limit)
	if err != nil {
		return recovered, err
	}
	if m.fanout != nil {
		for _, t := range recovered {
			m.fanout.PublishCommitted(models.TaskEvent{
				ProtocolVersion: models.CurrentProtocolVersion,
				TaskID:          t.TaskID,
				EventType:       "task_outcome_unknown",
				PublishedAt:     now,
			})
		}
	}
	return recovered, nil
}

// CreateWithID creates a new task with the supplied identifiers. It is used by
// the entrypoint when the task record does not yet exist locally.
func (m *Manager) CreateWithID(taskID, sessionID, initiatorAgentID, targetAgentID string) (models.Task, error) {
	return m.CreateWithInteractionID(taskID, sessionID, initiatorAgentID, targetAgentID, "", "", "", "")
}

// CreateWithInteractionID creates a target-side task retaining authorization linkage.
func (m *Manager) CreateWithInteractionID(taskID, sessionID, initiatorAgentID, targetAgentID, interactionID, decisionID, rootInteractionID, parentInteractionID string) (models.Task, error) {
	return m.CreateWithDelegation(taskID, sessionID, initiatorAgentID, targetAgentID, interactionID, decisionID, rootInteractionID, parentInteractionID, "", "", 0, nil)
}

func (m *Manager) CreateWithDelegation(taskID, sessionID, initiatorAgentID, targetAgentID, interactionID, decisionID, rootInteractionID, parentInteractionID, rootTaskID, parentTaskID string, delegationDepth int, deadline *time.Time, scope ...any) (models.Task, error) {
	now := time.Now().UTC()
	task := models.Task{
		ProtocolVersion:     models.CurrentProtocolVersion,
		TaskID:              taskID,
		SessionID:           sessionID,
		InteractionID:       interactionID,
		DecisionID:          decisionID,
		RootInteractionID:   rootInteractionID,
		ParentInteractionID: parentInteractionID,
		RootTaskID:          rootTaskID,
		ParentTaskID:        parentTaskID,
		DelegationDepth:     delegationDepth,
		Deadline:            deadline,
		InitiatorAgentID:    initiatorAgentID,
		TargetAgentID:       targetAgentID,
		Status:              "pending",
		CreatedAt:           now,
		UpdatedAt:           now,
	}
	if len(scope) >= 3 {
		task.AllowedTools, _ = scope[0].([]string)
		task.AllowedCapabilities, _ = scope[1].([]string)
		task.AllowRedelegation, _ = scope[2].(bool)
	}
	if len(scope) >= 4 {
		task.Budget, _ = scope[3].(models.DelegationBudget)
	}
	if len(scope) >= 8 {
		task.RequestID, _ = scope[4].(string)
		task.TenantID, _ = scope[5].(string)
		task.TargetWorkloadID, _ = scope[6].(string)
		task.TargetInstanceID, _ = scope[7].(string)
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
	return m.CompleteWithConsumption(taskID, status, outcome, errorCode, models.DelegationBudget{})
}

// CompleteWithConsumption settles a child reservation using measured consumption.
// A zero value means no measured consumption; the assigned envelope is never
// treated as actual consumption.
func (m *Manager) CompleteWithConsumption(taskID, status string, outcome json.RawMessage, errorCode string, consumed models.DelegationBudget) (models.Task, error) {
	if status != "completed" && status != "failed" {
		return models.Task{}, ErrInvalidStatus
	}
	current, err := m.store.Get(context.Background(), taskID)
	if err != nil {
		return models.Task{}, ErrTaskNotFound
	}
	if current.Status == status {
		return current, nil
	}
	if current.Status != "running" && current.Status != "outcome_unknown" {
		return models.Task{}, ErrInvalidStatus
	}
	updated, event, err := m.store.UpdateStatusWithConsumption(context.Background(), taskID, current.Status, status, outcome, errorCode, consumed)
	if errors.Is(err, store.ErrStatusConflict) {
		settled, getErr := m.store.Get(context.Background(), taskID)
		if getErr == nil && settled.Status == status {
			return settled, nil
		}
	}
	if err != nil {
		if errors.Is(err, store.ErrStatusConflict) || errors.Is(err, store.ErrInvalidTransition) || errors.Is(err, store.ErrBudgetExceeded) {
			return models.Task{}, ErrInvalidStatus
		}
		return models.Task{}, ErrTaskNotFound
	}
	if m.fanout != nil {
		m.fanout.PublishCommitted(event)
	}
	return updated, nil
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

func (m *Manager) generateID() string {
	now := time.Now().UTC()
	sequence := taskIDSequence.Add(1)
	if m.instanceID == "" {
		return fmt.Sprintf("task-%s-%d-%d", now.Format("20060102-150405"), now.UnixNano(), sequence)
	}
	return fmt.Sprintf("task-%s-%s-%d-%d", m.instanceID, now.Format("20060102-150405"), now.UnixNano(), sequence)
}
