// Package task manages the lifecycle of inter-agent tasks.
package task

import (
	"errors"
	"fmt"
	"sync"
	"time"

	"github.com/loop-controller/go/internal/models"
)

var (
	ErrTaskNotFound = errors.New("task not found")
	ErrInvalidStatus = errors.New("invalid status transition")
)

// Manager stores tasks in memory.
type Manager struct {
	mu     sync.RWMutex
	tasks  map[string]models.Task
	nextID int
}

// New creates an empty Manager.
func New() *Manager {
	return &Manager{
		tasks: make(map[string]models.Task),
	}
}

// Create creates a new task and returns it.
func (m *Manager) Create(sessionID, initiatorAgentID, targetAgentID string) models.Task {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.nextID++
	now := time.Now().UTC()
	task := models.Task{
		TaskID:           generateID(m.nextID),
		SessionID:        sessionID,
		InitiatorAgentID: initiatorAgentID,
		TargetAgentID:    targetAgentID,
		Status:           "pending",
		CreatedAt:        now,
		UpdatedAt:        now,
	}
	m.tasks[task.TaskID] = task
	return task
}

// Get returns a task by id.
func (m *Manager) Get(taskID string) (models.Task, error) {
	m.mu.RLock()
	defer m.mu.RUnlock()
	task, ok := m.tasks[taskID]
	if !ok {
		return models.Task{}, ErrTaskNotFound
	}
	return task, nil
}

// UpdateStatus transitions a task to a new status.
func (m *Manager) UpdateStatus(taskID, status string) (models.Task, error) {
	allowed := map[string]bool{
		"pending":    true,
		"active":     true,
		"completed":  true,
		"failed":     true,
		"cancelled":  true,
	}
	if !allowed[status] {
		return models.Task{}, ErrInvalidStatus
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	task, ok := m.tasks[taskID]
	if !ok {
		return models.Task{}, ErrTaskNotFound
	}
	task.Status = status
	task.UpdatedAt = time.Now().UTC()
	m.tasks[taskID] = task
	return task, nil
}

func generateID(n int) string {
	return fmt.Sprintf("task-%s-%d", time.Now().UTC().Format("20060102"), n)
}
