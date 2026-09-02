// Package stream provides task event publishing and SSE streaming.
package stream

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"sync"
	"sync/atomic"
	"time"

	"github.com/loop-controller/go/internal/models"
)

// TaskEventPublisher publishes task updates and allows subscriptions.
type TaskEventPublisher interface {
	Subscribe(ctx context.Context, taskID string) (<-chan models.Task, error)
	Publish(ctx context.Context, task models.Task) error
}

// EventStore is the subset of store.EventStore required by the publisher.
type EventStore interface {
	Append(ctx context.Context, ev models.TaskEvent) error
	ListPending(ctx context.Context, taskID string) ([]models.TaskEvent, error)
	MarkPublished(ctx context.Context, eventIDs []string) error
}

// SQLitePublisher persists events to an EventStore and pushes them to SSE
// subscribers over an in-memory channel fan-out.
type SQLitePublisher struct {
	store   EventStore
	mu      sync.RWMutex
	subs    map[string][]chan models.Task
	counter uint64
}

// NewPublisher creates a publisher backed by the given EventStore.
func NewPublisher(store EventStore) *SQLitePublisher {
	return &SQLitePublisher{
		store: store,
		subs:  make(map[string][]chan models.Task),
	}
}

// Subscribe registers a new subscriber for a task and replays pending events
// from the store before listening for new events.
func (p *SQLitePublisher) Subscribe(ctx context.Context, taskID string) (<-chan models.Task, error) {
	if taskID == "" {
		return nil, fmt.Errorf("task_id is required")
	}
	ch := make(chan models.Task, 8)

	p.mu.Lock()
	p.subs[taskID] = append(p.subs[taskID], ch)
	p.mu.Unlock()

	go p.replayPending(ctx, taskID, ch)

	go func() {
		<-ctx.Done()
		p.mu.Lock()
		for i, sub := range p.subs[taskID] {
			if sub == ch {
				p.subs[taskID] = append(p.subs[taskID][:i], p.subs[taskID][i+1:]...)
				break
			}
		}
		p.mu.Unlock()
		close(ch)
	}()
	return ch, nil
}

func (p *SQLitePublisher) replayPending(ctx context.Context, taskID string, ch chan<- models.Task) {
	pending, err := p.store.ListPending(ctx, taskID)
	if err != nil {
		return
	}
	var ids []string
	for _, ev := range pending {
		var t models.Task
		if err := json.Unmarshal(ev.Payload, &t); err != nil {
			continue
		}
		select {
		case ch <- t:
			ids = append(ids, ev.EventID)
		case <-ctx.Done():
			return
		}
	}
	if len(ids) > 0 {
		_ = p.store.MarkPublished(ctx, ids)
	}
}

// Publish persists a task event and pushes it to active subscribers.
func (p *SQLitePublisher) Publish(ctx context.Context, task models.Task) error {
	payload, err := json.Marshal(task)
	if err != nil {
		return fmt.Errorf("marshal task event payload: %w", err)
	}
	ev := models.TaskEvent{
		EventID:     p.nextEventID(task.TaskID),
		TaskID:      task.TaskID,
		EventType:   eventTypeForStatus(task.Status),
		Payload:     payload,
		PublishedAt: time.Now().UTC(),
	}
	if err := p.store.Append(ctx, ev); err != nil {
		return fmt.Errorf("append task event: %w", err)
	}

	p.mu.RLock()
	chans := p.subs[task.TaskID]
	p.mu.RUnlock()
	var sent int
	for _, ch := range chans {
		select {
		case ch <- task:
			sent++
		default:
		}
	}
	if sent > 0 {
		_ = p.store.MarkPublished(ctx, []string{ev.EventID})
	}
	return nil
}

func (p *SQLitePublisher) nextEventID(taskID string) string {
	n := atomic.AddUint64(&p.counter, 1)
	return fmt.Sprintf("ev-%s-%d-%d", taskID, time.Now().UTC().UnixNano(), n)
}

func eventTypeForStatus(status string) string {
	switch status {
	case "pending":
		return "task_created"
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
