// Package stream provides task event publishing and SSE streaming.
package stream

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"sync"

	"github.com/loop-controller/go/internal/models"
)

// TaskEventPublisher publishes task updates and allows subscriptions.
type TaskEventPublisher interface {
	Subscribe(ctx context.Context, taskID string) (<-chan models.Task, error)
	Publish(ctx context.Context, task models.Task) error
}

// InMemoryPublisher is a simple in-memory implementation.
type InMemoryPublisher struct {
	mu          sync.RWMutex
	subscribers map[string][]chan models.Task
}

// NewInMemoryPublisher creates an in-memory publisher.
func NewInMemoryPublisher() *InMemoryPublisher {
	return &InMemoryPublisher{
		subscribers: make(map[string][]chan models.Task),
	}
}

// Subscribe registers a new subscriber for a task.
func (p *InMemoryPublisher) Subscribe(ctx context.Context, taskID string) (<-chan models.Task, error) {
	if taskID == "" {
		return nil, fmt.Errorf("task_id is required")
	}
	ch := make(chan models.Task, 1)
	p.mu.Lock()
	p.subscribers[taskID] = append(p.subscribers[taskID], ch)
	p.mu.Unlock()

	go func() {
		<-ctx.Done()
		p.mu.Lock()
		defer p.mu.Unlock()
		for i, sub := range p.subscribers[taskID] {
			if sub == ch {
				p.subscribers[taskID] = append(p.subscribers[taskID][:i], p.subscribers[taskID][i+1:]...)
				break
			}
		}
		close(ch)
	}()
	return ch, nil
}

// Publish sends a task update to all subscribers.
func (p *InMemoryPublisher) Publish(ctx context.Context, task models.Task) error {
	p.mu.RLock()
	subs := p.subscribers[task.TaskID]
	p.mu.RUnlock()
	for _, ch := range subs {
		select {
		case ch <- task:
		default:
		}
	}
	return nil
}

// ServeTaskStream writes task updates as Server-Sent Events.
func ServeTaskStream(publisher TaskEventPublisher, w http.ResponseWriter, r *http.Request, taskID string) error {
	if taskID == "" {
		http.Error(w, "task_id required", http.StatusBadRequest)
		return fmt.Errorf("task_id required")
	}

	ctx := r.Context()
	ch, err := publisher.Subscribe(ctx, taskID)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return err
	}

	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")
	w.WriteHeader(http.StatusOK)
	if f, ok := w.(http.Flusher); ok {
		f.Flush()
	}

	flusher, _ := w.(http.Flusher)
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case t, ok := <-ch:
			if !ok {
				return nil
			}
			data, err := json.Marshal(t)
			if err != nil {
				continue
			}
			fmt.Fprintf(w, "data: %s\n\n", data)
			if flusher != nil {
				flusher.Flush()
			}
		}
	}
}
