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

// subscriber tracks one live SSE connection and its resumable cursor.
type subscriber struct {
	ch          chan models.TaskEvent
	lastEventID string
}

// SQLitePublisher persists events and streams them to SSE clients. Events are
// read back from the shared event store rather than fanned out only in-process,
// so a client connected to one kernel instance still observes events committed
// by a different instance against the same database.
type SQLitePublisher struct {
	store        EventStore
	pollInterval time.Duration
	instanceID   string
	mu           sync.Mutex
	subs         map[string]map[*subscriber]struct{}
	notify       chan struct{}
	counter      uint64
}

// NewPublisher creates a publisher backed by the given EventStore.
func NewPublisher(store EventStore) *SQLitePublisher {
	return &SQLitePublisher{
		store:        store,
		pollInterval: 50 * time.Millisecond,
		subs:         make(map[string]map[*subscriber]struct{}),
		notify:       make(chan struct{}, 1),
	}
}

// WithInstanceID prefixes generated event IDs with the owning kernel instance,
// keeping them unique across instances sharing the same event store.
func (p *SQLitePublisher) WithInstanceID(id string) *SQLitePublisher {
	p.instanceID = id
	return p
}

// Subscribe registers a subscriber and replays all events after afterEventID.
func (p *SQLitePublisher) Subscribe(ctx context.Context, taskID, afterEventID string) (<-chan models.TaskEvent, error) {
	if taskID == "" {
		return nil, fmt.Errorf("task_id is required")
	}

	history, err := p.store.ListAfter(ctx, taskID, afterEventID)
	if err != nil {
		return nil, err
	}

	ch := make(chan models.TaskEvent, len(history)+16)
	sub := &subscriber{ch: ch, lastEventID: afterEventID}

	p.mu.Lock()
	if p.subs[taskID] == nil {
		p.subs[taskID] = make(map[*subscriber]struct{})
	}
	p.subs[taskID][sub] = struct{}{}
	p.mu.Unlock()

	for _, ev := range history {
		ch <- withProtocolVersion(ev)
		sub.lastEventID = ev.EventID
	}

	go p.run(ctx, taskID, sub)
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

// PublishCommitted signals subscribers that an already persisted event is
// available. Delivery reads the shared store, so cross-instance commits are
// visible to every subscriber regardless of which instance wrote them.
func (p *SQLitePublisher) PublishCommitted(ev models.TaskEvent) {
	select {
	case p.notify <- struct{}{}:
	default:
	}
}

func (p *SQLitePublisher) run(ctx context.Context, taskID string, sub *subscriber) {
	ticker := time.NewTicker(p.pollInterval)
	defer ticker.Stop()
	defer p.remove(taskID, sub)

	for {
		select {
		case <-ctx.Done():
			return
		case <-p.notify:
			p.drain(ctx, taskID, sub)
		case <-ticker.C:
			p.drain(ctx, taskID, sub)
		}
	}
}

func (p *SQLitePublisher) drain(ctx context.Context, taskID string, sub *subscriber) {
	events, err := p.store.ListAfter(ctx, taskID, sub.lastEventID)
	if err != nil {
		return
	}
	for _, ev := range events {
		select {
		case sub.ch <- withProtocolVersion(ev):
			sub.lastEventID = ev.EventID
		case <-ctx.Done():
			return
		}
	}
}

func (p *SQLitePublisher) remove(taskID string, sub *subscriber) {
	p.mu.Lock()
	defer p.mu.Unlock()
	subs := p.subs[taskID]
	if subs == nil {
		return
	}
	if _, ok := subs[sub]; !ok {
		return
	}
	delete(subs, sub)
	close(sub.ch)
	if len(subs) == 0 {
		delete(p.subs, taskID)
	}
}

func withProtocolVersion(ev models.TaskEvent) models.TaskEvent {
	ev.ProtocolVersion = currentProtocolVersion
	return ev
}

func (p *SQLitePublisher) nextEventID(taskID string) string {
	n := atomic.AddUint64(&p.counter, 1)
	if p.instanceID == "" {
		return fmt.Sprintf("ev-stream-%s-%d-%d", taskID, time.Now().UTC().UnixNano(), n)
	}
	return fmt.Sprintf("ev-stream-%s-%s-%d-%d", p.instanceID, taskID, time.Now().UTC().UnixNano(), n)
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
