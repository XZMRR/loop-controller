// Package stream provides task event publishing and SSE streaming.
package stream

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"sync"
	"sync/atomic"
	"time"

	"github.com/loop-controller/go/internal/models"
	"github.com/loop-controller/go/internal/observability"
	"github.com/loop-controller/go/internal/store"
)

const currentProtocolVersion = models.CurrentProtocolVersion

var ErrPublisherClosed = errors.New("event publisher is closed")

// Config controls polling, subscriber backpressure, and SSE keepalive framing.
type Config struct {
	PollInterval      time.Duration
	RetryInterval     time.Duration
	HeartbeatInterval time.Duration
	MaxQueuedEvents   int
	MaxQueuedBytes    int64
	MaxQueuedAge      time.Duration
}

func DefaultConfig() Config {
	return Config{
		PollInterval:      50 * time.Millisecond,
		RetryInterval:     3 * time.Second,
		HeartbeatInterval: 15 * time.Second,
		MaxQueuedEvents:   256,
		MaxQueuedBytes:    1 << 20,
		MaxQueuedAge:      30 * time.Second,
	}
}

func normalizeConfig(cfg Config) Config {
	defaults := DefaultConfig()
	if cfg.PollInterval <= 0 {
		cfg.PollInterval = defaults.PollInterval
	}
	if cfg.RetryInterval <= 0 {
		cfg.RetryInterval = defaults.RetryInterval
	}
	if cfg.HeartbeatInterval <= 0 {
		cfg.HeartbeatInterval = defaults.HeartbeatInterval
	}
	if cfg.MaxQueuedEvents <= 0 {
		cfg.MaxQueuedEvents = defaults.MaxQueuedEvents
	}
	if cfg.MaxQueuedBytes <= 0 {
		cfg.MaxQueuedBytes = defaults.MaxQueuedBytes
	}
	if cfg.MaxQueuedAge <= 0 {
		cfg.MaxQueuedAge = defaults.MaxQueuedAge
	}
	return cfg
}

// TaskEventPublisher publishes task updates and allows resumable subscriptions.
type TaskEventPublisher interface {
	Subscribe(ctx context.Context, taskID, afterEventID string) (<-chan models.TaskEvent, error)
	Publish(ctx context.Context, task models.Task) error
	PublishCommitted(ev models.TaskEvent)
}

// EventStore is the subset of store.EventStore required by the publisher.
type EventStore interface {
	Append(ctx context.Context, ev models.TaskEvent) error
	ListAfter(ctx context.Context, taskID, afterEventID string) ([]models.TaskEvent, error)
}

type subscriber struct {
	ch   chan models.TaskEvent
	wake chan struct{}
}

type queuedEvent struct {
	event      models.TaskEvent
	enqueuedAt time.Time
}

// SQLitePublisher persists events and streams them to SSE clients. Events are
// read back from the shared event store so subscriptions work across instances.
type SQLitePublisher struct {
	store      EventStore
	instanceID string
	mu         sync.Mutex
	subs       map[string]map[*subscriber]struct{}
	closed     bool
	closeCh    chan struct{}
	wg         sync.WaitGroup
	counter    uint64
	config     Config
}

// NewPublisher creates a publisher backed by the given EventStore.
func NewPublisher(store EventStore) *SQLitePublisher {
	return &SQLitePublisher{
		store:   store,
		subs:    make(map[string]map[*subscriber]struct{}),
		closeCh: make(chan struct{}),
		config:  DefaultConfig(),
	}
}

func (p *SQLitePublisher) WithConfig(cfg Config) *SQLitePublisher {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.config = normalizeConfig(cfg)
	return p
}

// WithInstanceID prefixes generated event IDs with the owning kernel instance.
func (p *SQLitePublisher) WithInstanceID(id string) *SQLitePublisher {
	p.instanceID = id
	return p
}

// Subscribe validates the cursor, registers a subscriber, and replays events.
func (p *SQLitePublisher) Subscribe(ctx context.Context, taskID, afterEventID string) (<-chan models.TaskEvent, error) {
	if taskID == "" {
		return nil, fmt.Errorf("task_id is required")
	}
	history, err := p.store.ListAfter(ctx, taskID, afterEventID)
	if err != nil {
		return nil, err
	}

	sub := &subscriber{ch: make(chan models.TaskEvent), wake: make(chan struct{}, 1)}
	p.mu.Lock()
	if p.closed {
		p.mu.Unlock()
		return nil, ErrPublisherClosed
	}
	cfg := p.config
	if p.subs[taskID] == nil {
		p.subs[taskID] = make(map[*subscriber]struct{})
	}
	p.subs[taskID][sub] = struct{}{}
	p.wg.Add(1)
	active := p.subscriberCountLocked()
	p.mu.Unlock()
	observability.Default.Set("sse_active_subscribers", float64(active))

	go p.run(ctx, taskID, afterEventID, history, sub, cfg)
	return sub.ch, nil
}

// Publish persists a task event before making it visible to subscribers.
func (p *SQLitePublisher) Publish(ctx context.Context, task models.Task) error {
	p.mu.Lock()
	if p.closed {
		p.mu.Unlock()
		return ErrPublisherClosed
	}
	p.mu.Unlock()

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

// PublishCommitted wakes every local subscriber without blocking the caller.
func (p *SQLitePublisher) PublishCommitted(ev models.TaskEvent) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.closed {
		return
	}
	for sub := range p.subs[ev.TaskID] {
		select {
		case sub.wake <- struct{}{}:
		default:
		}
	}
}

func (p *SQLitePublisher) run(ctx context.Context, taskID, cursor string, history []models.TaskEvent, sub *subscriber, cfg Config) {
	defer p.wg.Done()
	defer p.remove(taskID, sub)
	defer close(sub.ch)

	queue := make([]queuedEvent, 0, len(history))
	var queuedBytes int64
	appendEvents := func(events []models.TaskEvent) bool {
		now := time.Now()
		for _, ev := range events {
			ev = withProtocolVersion(ev)
			size := eventSize(ev)
			if len(queue)+1 > cfg.MaxQueuedEvents || queuedBytes+size > cfg.MaxQueuedBytes {
				observability.Default.Add("sse_slow_consumer_disconnect_total", 1, "queue_limit")
				return false
			}
			queue = append(queue, queuedEvent{event: ev, enqueuedAt: now})
			queuedBytes += size
			cursor = ev.Cursor
		}
		return true
	}
	if !appendEvents(history) {
		return
	}

	poll := time.NewTicker(cfg.PollInterval)
	lagCheck := time.NewTicker(minDuration(cfg.MaxQueuedAge, cfg.PollInterval))
	defer poll.Stop()
	defer lagCheck.Stop()

	for {
		var out chan models.TaskEvent
		var next models.TaskEvent
		if len(queue) > 0 {
			out, next = sub.ch, queue[0].event
		}
		select {
		case <-ctx.Done():
			return
		case <-p.closeCh:
			return
		case out <- next:
			queuedBytes -= eventSize(next)
			queue = queue[1:]
		case <-lagCheck.C:
			if len(queue) > 0 && time.Since(queue[0].enqueuedAt) >= cfg.MaxQueuedAge {
				observability.Default.Add("sse_slow_consumer_disconnect_total", 1, "age_limit")
				return
			}
		case <-sub.wake:
			if !p.drain(ctx, taskID, cursor, appendEvents) {
				return
			}
		case <-poll.C:
			if !p.drain(ctx, taskID, cursor, appendEvents) {
				return
			}
		}
	}
}

func (p *SQLitePublisher) drain(ctx context.Context, taskID, cursor string, appendEvents func([]models.TaskEvent) bool) bool {
	events, err := p.store.ListAfter(ctx, taskID, cursor)
	if err != nil {
		return !errors.Is(err, context.Canceled)
	}
	return appendEvents(events)
}

func (p *SQLitePublisher) remove(taskID string, sub *subscriber) {
	p.mu.Lock()
	subs := p.subs[taskID]
	delete(subs, sub)
	if len(subs) == 0 {
		delete(p.subs, taskID)
	}
	active := p.subscriberCountLocked()
	p.mu.Unlock()
	observability.Default.Set("sse_active_subscribers", float64(active))
}

func (p *SQLitePublisher) subscriberCountLocked() int {
	total := 0
	for _, subscribers := range p.subs {
		total += len(subscribers)
	}
	return total
}

// Close disconnects subscribers and prevents future subscriptions or publishes.
func (p *SQLitePublisher) Close() error {
	p.mu.Lock()
	if !p.closed {
		p.closed = true
		close(p.closeCh)
	}
	p.mu.Unlock()
	return nil
}

// Wait waits for all subscriber workers to release their resources.
func (p *SQLitePublisher) Wait() { p.wg.Wait() }

func eventSize(ev models.TaskEvent) int64 {
	return int64(len(ev.EventID) + len(ev.Cursor) + len(ev.TaskID) + len(ev.EventType) + len(ev.Payload) + 64)
}

func minDuration(a, b time.Duration) time.Duration {
	if a < b {
		return a
	}
	return b
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
	return ServeTaskStreamWithConfig(publisher, w, r, taskID, DefaultConfig())
}

func ServeTaskStreamWithConfig(publisher TaskEventPublisher, w http.ResponseWriter, r *http.Request, taskID string, cfg Config) error {
	if taskID == "" {
		writeStreamError(w, http.StatusBadRequest, "task_id_required", "task_id required")
		return fmt.Errorf("task_id required")
	}
	cfg = normalizeConfig(cfg)
	ctx, cancel := context.WithCancel(r.Context())
	defer cancel()

	// Subscribe performs cursor validation before any streaming headers are committed.
	ch, err := publisher.Subscribe(ctx, taskID, r.Header.Get("Last-Event-ID"))
	if err != nil {
		status, code := http.StatusBadRequest, "event_cursor_invalid"
		if errors.Is(err, store.ErrEventCursorExpired) {
			status, code = http.StatusGone, "event_cursor_expired"
			observability.Default.Add("sse_cursor_expired_total", 1)
		} else if errors.Is(err, store.ErrEventCursorFuture) {
			code = "event_cursor_future"
		} else if errors.Is(err, ErrPublisherClosed) {
			status, code = http.StatusServiceUnavailable, "event_stream_closed"
		}
		writeStreamError(w, status, code, err.Error())
		return err
	}

	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")
	w.WriteHeader(http.StatusOK)
	if _, err := fmt.Fprintf(w, "retry: %d\n\n", cfg.RetryInterval.Milliseconds()); err != nil {
		return err
	}
	controller := http.NewResponseController(w)
	if err := controller.Flush(); err != nil {
		return err
	}

	heartbeat := time.NewTicker(cfg.HeartbeatInterval)
	retry := time.NewTicker(cfg.RetryInterval)
	defer heartbeat.Stop()
	defer retry.Stop()
	writeAndFlush := func(frame string) error {
		if _, err := fmt.Fprint(w, frame); err != nil {
			return err
		}
		return controller.Flush()
	}
	for {
		select {
		case <-ctx.Done():
			for range ch {
			}
			return ctx.Err()
		case <-heartbeat.C:
			if err := writeAndFlush(": heartbeat\n\n"); err != nil {
				return err
			}
		case <-retry.C:
			if err := writeAndFlush(fmt.Sprintf("retry: %d\n\n", cfg.RetryInterval.Milliseconds())); err != nil {
				return err
			}
		case ev, ok := <-ch:
			if !ok {
				return nil
			}
			data, err := json.Marshal(ev)
			if err != nil {
				return err
			}
			if err := writeAndFlush(fmt.Sprintf("id: %s\nevent: %s\ndata: %s\n\n", ev.Cursor, ev.EventType, data)); err != nil {
				return err
			}
		}
	}
}

func writeStreamError(w http.ResponseWriter, status int, code, message string) {
	if status >= http.StatusInternalServerError {
		message = "service unavailable"
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(models.ErrorResponse{ProtocolVersion: currentProtocolVersion, Error: message, Code: code})
}
