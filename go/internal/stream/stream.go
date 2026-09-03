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

const currentProtocolVersion = models.CurrentProtocolVersion

// TaskEventPublisher publishes task updates and allows resumable subscriptions.
type TaskEventPublisher interface {
	Subscribe(ctx context.Context, taskID, afterEventID string) (<-chan models.TaskEvent, error)
	Publish(ctx context.Context, task models.Task) error
	PublishCommitted(ev models.TaskEvent)
}

// EventStore is the subset of store.EventStore required by the publisher.
type EventStore interface {
	Append(ctx context.Context, ev models.TaskEvent) error
	ListPending(ctx context.Context, taskID string) ([]models.TaskEvent, error)
	ListAfter(ctx context.Context, taskID, afterEventID string) ([]models.TaskEvent, error)
	MarkPublished(ctx context.Context, eventIDs []string) error
}

// SQLitePublisher persists events and fans committed events out to local SSE clients.
type SQLitePublisher struct {
	store   EventStore
	mu      sync.RWMutex
	subs    map[string][]chan models.TaskEvent
	counter uint64
}

// NewPublisher creates a publisher backed by the given EventStore.
func NewPublisher(store EventStore) *SQLitePublisher {
	return &SQLitePublisher{store: store, subs: make(map[string][]chan models.TaskEvent)}
}

// Subscribe registers a subscriber and replays all events after afterEventID.
func (p *SQLitePublisher) Subscribe(ctx context.Context, taskID, afterEventID string) (<-chan models.TaskEvent, error) {
	if taskID == "" {
		return nil, fmt.Errorf("task_id is required")
	}

	// Holding the fan-out lock closes the gap between history lookup and live
	// registration: a concurrent Publish may persist, but cannot fan out until
	// this subscriber is registered.
	p.mu.Lock()
	history, err := p.store.ListAfter(ctx, taskID, afterEventID)
	if err != nil {
		p.mu.Unlock()
		return nil, err
	}
	ch := make(chan models.TaskEvent, len(history)+16)
	p.subs[taskID] = append(p.subs[taskID], ch)
	for _, ev := range history {
		ch <- withProtocolVersion(ev)
	}
	p.mu.Unlock()

	go func() {
		<-ctx.Done()
		p.mu.Lock()
		for i, sub := range p.subs[taskID] {
			if sub == ch {
				p.subs[taskID] = append(p.subs[taskID][:i], p.subs[taskID][i+1:]...)
				close(ch)
				break
			}
		}
		p.mu.Unlock()
	}()
	return ch, nil
}

// Publish persists a task event before making it visible to subscribers.
func (p *SQLitePublisher) Publish(ctx context.Context, task models.Task) error {
	payload, err := json.Marshal(task)
	if err != nil {
		return fmt.Errorf("marshal task event payload: %w", err)
	}
	ev := models.TaskEvent{
		ProtocolVersion: currentProtocolVersion,
		EventID:         p.nextEventID(task.TaskID),
		TaskID:          task.TaskID,
		EventType:       eventTypeForStatus(task.Status),
		Payload:         payload,
		PublishedAt:     time.Now().UTC(),
	}
	if err := p.store.Append(ctx, ev); err != nil {
		return fmt.Errorf("append task event: %w", err)
	}

	p.PublishCommitted(ev)
	return nil
}

// PublishCommitted fans an already persisted event out to local subscribers.
func (p *SQLitePublisher) PublishCommitted(ev models.TaskEvent) {
	p.mu.RLock()
	defer p.mu.RUnlock()
	for _, ch := range p.subs[ev.TaskID] {
		select {
		case ch <- withProtocolVersion(ev):
		default:
			// The durable event remains available for Last-Event-ID replay.
		}
	}
}

func withProtocolVersion(ev models.TaskEvent) models.TaskEvent {
	ev.ProtocolVersion = currentProtocolVersion
	return ev
}

func (p *SQLitePublisher) nextEventID(taskID string) string {
	n := atomic.AddUint64(&p.counter, 1)
	return fmt.Sprintf("ev-stream-%s-%d-%d", taskID, time.Now().UTC().UnixNano(), n)
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

// ServeTaskStream writes resumable TaskEvent values as Server-Sent Events.
func ServeTaskStream(publisher TaskEventPublisher, w http.ResponseWriter, r *http.Request, taskID string) error {
	if taskID == "" {
		http.Error(w, "task_id required", http.StatusBadRequest)
		return fmt.Errorf("task_id required")
	}

	ctx := r.Context()
	ch, err := publisher.Subscribe(ctx, taskID, r.Header.Get("Last-Event-ID"))
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return err
	}

	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")
	w.WriteHeader(http.StatusOK)
	flusher, _ := w.(http.Flusher)
	if flusher != nil {
		flusher.Flush()
	}

	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case ev, ok := <-ch:
			if !ok {
				return nil
			}
			data, err := json.Marshal(ev)
			if err != nil {
				continue
			}
			fmt.Fprintf(w, "id: %s\nevent: %s\ndata: %s\n\n", ev.EventID, ev.EventType, data)
			if flusher != nil {
				flusher.Flush()
			}
		}
	}
}
