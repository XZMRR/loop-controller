package stream

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/loop-controller/go/internal/models"
	"github.com/loop-controller/go/internal/store"
)

func openTestEventStore(t *testing.T, taskID string) store.EventStore {
	t.Helper()
	db, err := store.Open(context.Background(), filepath.Join(t.TempDir(), "events.db"))
	if err != nil {
		t.Fatalf("open store: %v", err)
	}
	t.Cleanup(func() { db.Close() })
	if taskID != "" {
		if err := db.TaskStore().Create(context.Background(), models.Task{
			TaskID:           taskID,
			SessionID:        "session-1",
			InitiatorAgentID: "agent-a",
			TargetAgentID:    "agent-b",
			Status:           "pending",
			CreatedAt:        time.Now().UTC(),
			UpdatedAt:        time.Now().UTC(),
		}); err != nil {
			t.Fatalf("create task: %v", err)
		}
	}
	return db.EventStore()
}

func TestPublisherSubscribeAndPublish(t *testing.T) {
	pub := NewPublisher(openTestEventStore(t, "task-1"))
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	ch, err := pub.Subscribe(ctx, "task-1", "")
	if err != nil {
		t.Fatalf("subscribe failed: %v", err)
	}

	task := models.Task{TaskID: "task-1", Status: "running"}
	if err := pub.Publish(ctx, task); err != nil {
		t.Fatalf("publish failed: %v", err)
	}

	select {
	case got := <-ch:
		if got.EventType != "task_running" {
			t.Errorf("expected task_running, got %q", got.EventType)
		}
		if got.ProtocolVersion != currentProtocolVersion {
			t.Errorf("expected protocol %s, got %q", currentProtocolVersion, got.ProtocolVersion)
		}
	case <-time.After(time.Second):
		t.Fatal("timeout waiting for task event")
	}
}

func TestPublishCommittedDeliversPersistedEventWithoutAppending(t *testing.T) {
	es := openTestEventStore(t, "task-1")
	pub := NewPublisher(es)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	ch, err := pub.Subscribe(ctx, "task-1", "")
	if err != nil {
		t.Fatalf("subscribe failed: %v", err)
	}

	// PublishCommitted only signals subscribers to re-read the shared store, so
	// the event must already be persisted (as task.Manager does before fan-out).
	ev := models.TaskEvent{EventID: "ev-committed", TaskID: "task-1", EventType: "task_running", PublishedAt: time.Now().UTC()}
	if err := es.Append(context.Background(), ev); err != nil {
		t.Fatalf("append event: %v", err)
	}
	pub.PublishCommitted(ev)

	select {
	case got := <-ch:
		if got.EventID != ev.EventID {
			t.Fatalf("event id = %q, want %q", got.EventID, ev.EventID)
		}
	case <-time.After(time.Second):
		t.Fatal("timeout waiting for committed event")
	}
	history, err := es.ListAfter(context.Background(), "task-1", "")
	if err != nil {
		t.Fatalf("list history: %v", err)
	}
	if len(history) != 1 {
		t.Fatalf("PublishCommitted resulted in %d events, want exactly the 1 persisted event", len(history))
	}
}

func TestPublisherSeesCrossInstanceEvents(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "shared.db")

	dbA, err := store.Open(context.Background(), path)
	if err != nil {
		t.Fatalf("open db A: %v", err)
	}
	t.Cleanup(func() { dbA.Close() })

	dbB, err := store.Open(context.Background(), path)
	if err != nil {
		t.Fatalf("open db B: %v", err)
	}
	t.Cleanup(func() { dbB.Close() })

	if err := dbA.TaskStore().Create(context.Background(), models.Task{
		TaskID:           "task-1",
		SessionID:        "session-1",
		InitiatorAgentID: "agent-a",
		TargetAgentID:    "agent-b",
		Status:           "pending",
		CreatedAt:        time.Now().UTC(),
		UpdatedAt:        time.Now().UTC(),
	}); err != nil {
		t.Fatalf("create task: %v", err)
	}

	pubA := NewPublisher(dbA.EventStore())
	pubB := NewPublisher(dbB.EventStore())

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	ch, err := pubA.Subscribe(ctx, "task-1", "")
	if err != nil {
		t.Fatalf("subscribe on A: %v", err)
	}

	// Instance B commits an event to the shared store; a client connected to
	// instance A must observe it without any in-process fan-out.
	if err := pubB.Publish(context.Background(), models.Task{TaskID: "task-1", Status: "running"}); err != nil {
		t.Fatalf("publish on B: %v", err)
	}

	select {
	case got := <-ch:
		if got.EventType != "task_running" {
			t.Fatalf("event type = %q, want task_running", got.EventType)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("timeout waiting for cross-instance event")
	}
}

func TestPublisherCleansUpOnCancel(t *testing.T) {
	pub := NewPublisher(openTestEventStore(t, "task-1"))
	ctx, cancel := context.WithCancel(context.Background())

	ch, err := pub.Subscribe(ctx, "task-1", "")
	if err != nil {
		t.Fatalf("subscribe failed: %v", err)
	}
	cancel()

	select {
	case _, ok := <-ch:
		if ok {
			t.Fatal("expected channel to be closed")
		}
	case <-time.After(time.Second):
		t.Fatal("timeout waiting for channel close")
	}
}

func TestPublisherReplaysPendingEvents(t *testing.T) {
	es := openTestEventStore(t, "task-1")
	pub := NewPublisher(es)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	task := models.Task{TaskID: "task-1", Status: "pending"}
	if err := pub.Publish(context.Background(), task); err != nil {
		t.Fatalf("publish failed: %v", err)
	}

	// Allow the event to be persisted. A fresh subscriber should receive it
	// as a pending replay.
	time.Sleep(50 * time.Millisecond)
	ch, err := pub.Subscribe(ctx, "task-1", "")
	if err != nil {
		t.Fatalf("subscribe failed: %v", err)
	}

	select {
	case got := <-ch:
		if got.EventType != "task_created" {
			t.Errorf("expected task_created replay, got %q", got.EventType)
		}
	case <-time.After(time.Second):
		t.Fatal("timeout waiting for replayed event")
	}
}

func TestPublisherReplaysOnlyEventsAfterLastEventID(t *testing.T) {
	es := openTestEventStore(t, "task-1")
	pub := NewPublisher(es)
	for _, status := range []string{"pending", "accepted", "running"} {
		if err := pub.Publish(context.Background(), models.Task{TaskID: "task-1", Status: status}); err != nil {
			t.Fatalf("publish %s: %v", status, err)
		}
	}
	history, err := es.ListAfter(context.Background(), "task-1", "")
	if err != nil {
		t.Fatalf("list history: %v", err)
	}
	if len(history) != 3 {
		t.Fatalf("history length = %d, want 3", len(history))
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	ch, err := pub.Subscribe(ctx, "task-1", history[0].Cursor)
	if err != nil {
		t.Fatalf("subscribe failed: %v", err)
	}
	for _, want := range []string{"task_accepted", "task_running"} {
		select {
		case got := <-ch:
			if got.EventType != want {
				t.Fatalf("event type = %q, want %q", got.EventType, want)
			}
		case <-time.After(time.Second):
			t.Fatalf("timeout waiting for %s", want)
		}
	}
}

func TestConcurrentCancelAndPublish(t *testing.T) {
	pub := NewPublisher(openTestEventStore(t, "task-1"))
	for i := 0; i < 20; i++ {
		ctx, cancel := context.WithCancel(context.Background())
		if _, err := pub.Subscribe(ctx, "task-1", ""); err != nil {
			t.Fatalf("subscribe failed: %v", err)
		}
		done := make(chan struct{})
		go func() {
			defer close(done)
			for j := 0; j < 20; j++ {
				if err := pub.Publish(context.Background(), models.Task{TaskID: "task-1", Status: "running"}); err != nil {
					t.Errorf("publish failed: %v", err)
					return
				}
			}
		}()
		cancel()
		<-done
	}
}

func TestServeTaskStreamUsesOpaqueCursorAndMapsCursorErrors(t *testing.T) {
	es := openTestEventStore(t, "task-1")
	pub := NewPublisher(es)
	if err := pub.Publish(context.Background(), models.Task{TaskID: "task-1", Status: "pending"}); err != nil {
		t.Fatal(err)
	}
	history, err := es.ListAfter(context.Background(), "task-1", "")
	if err != nil || len(history) != 1 {
		t.Fatalf("history=%+v err=%v", history, err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	req := httptest.NewRequest(http.MethodGet, "/", nil).WithContext(ctx)
	response := httptest.NewRecorder()
	done := make(chan error, 1)
	go func() { done <- ServeTaskStream(pub, response, req, "task-1") }()
	time.Sleep(50 * time.Millisecond)
	cancel()
	<-done
	body := response.Body.String()
	if !strings.Contains(body, "id: "+history[0].Cursor) || strings.Contains(body, "id: "+history[0].EventID+"\n") {
		t.Fatalf("SSE id is not opaque cursor: %q", body)
	}

	if _, err := es.Compact(context.Background(), "task-1", 2); err != nil {
		t.Fatal(err)
	}
	req = httptest.NewRequest(http.MethodGet, "/", nil)
	req.Header.Set("Last-Event-ID", history[0].Cursor)
	response = httptest.NewRecorder()
	err = ServeTaskStream(pub, response, req, "task-1")
	if !errors.Is(err, store.ErrEventCursorExpired) || response.Code != http.StatusGone {
		t.Fatalf("expired response=%d err=%v", response.Code, err)
	}
	if response.Header().Get("Content-Type") != "application/json" || !strings.Contains(response.Body.String(), `"code":"event_cursor_expired"`) {
		t.Fatalf("expired response is not JSON envelope: headers=%v body=%q", response.Header(), response.Body.String())
	}
	req = httptest.NewRequest(http.MethodGet, "/", nil)
	req.Header.Set("Last-Event-ID", "bad")
	response = httptest.NewRecorder()
	err = ServeTaskStream(pub, response, req, "task-1")
	if !errors.Is(err, store.ErrEventCursorInvalid) || response.Code != http.StatusBadRequest {
		t.Fatalf("invalid response=%d err=%v", response.Code, err)
	}
	if response.Header().Get("Content-Type") != "application/json" || !strings.Contains(response.Body.String(), `"code":"event_cursor_invalid"`) {
		t.Fatalf("invalid response is not JSON envelope: headers=%v body=%q", response.Header(), response.Body.String())
	}

	future, err := store.EncodeEventCursor("task-1", 2, models.TaskEventSchemaVersion)
	if err != nil {
		t.Fatal(err)
	}
	req = httptest.NewRequest(http.MethodGet, "/", nil)
	req.Header.Set("Last-Event-ID", future)
	response = httptest.NewRecorder()
	err = ServeTaskStream(pub, response, req, "task-1")
	if !errors.Is(err, store.ErrEventCursorFuture) || response.Code != http.StatusBadRequest || !strings.Contains(response.Body.String(), `"code":"event_cursor_future"`) {
		t.Fatalf("future response=%d body=%q err=%v", response.Code, response.Body.String(), err)
	}
}

func TestSubscribeEmptyTaskID(t *testing.T) {
	pub := NewPublisher(openTestEventStore(t, ""))
	ctx := context.Background()
	_, err := pub.Subscribe(ctx, "", "")
	if err == nil {
		t.Fatal("expected error for empty task_id")
	}
}

func TestServeTaskStreamRetryHeartbeatAndCleanup(t *testing.T) {
	pub := NewPublisher(openTestEventStore(t, "task-1"))
	ctx, cancel := context.WithCancel(context.Background())
	req := httptest.NewRequest(http.MethodGet, "/", nil).WithContext(ctx)
	response := httptest.NewRecorder()
	done := make(chan error, 1)
	cfg := DefaultConfig()
	cfg.RetryInterval = 15 * time.Millisecond
	cfg.HeartbeatInterval = 10 * time.Millisecond
	go func() { done <- ServeTaskStreamWithConfig(pub, response, req, "task-1", cfg) }()
	time.Sleep(40 * time.Millisecond)
	cancel()
	<-done
	body := response.Body.String()
	if strings.Count(body, "retry: 15\n\n") < 2 {
		t.Fatalf("missing initial/periodic retry frames: %q", body)
	}
	if !strings.Contains(body, ": heartbeat\n\n") || strings.Contains(body, "event: heartbeat") {
		t.Fatalf("heartbeat is not an SSE comment: %q", body)
	}
	pub.mu.Lock()
	defer pub.mu.Unlock()
	if len(pub.subs) != 0 {
		t.Fatalf("subscriber leaked after cancellation: %d", len(pub.subs))
	}
}

func TestSlowSubscriberBoundariesAndFastSubscriber(t *testing.T) {
	for _, tc := range []struct {
		name string
		cfg  Config
	}{
		{name: "events", cfg: Config{PollInterval: time.Millisecond, MaxQueuedEvents: 1, MaxQueuedBytes: 1 << 20, MaxQueuedAge: time.Second}},
		{name: "bytes", cfg: Config{PollInterval: time.Millisecond, MaxQueuedEvents: 100, MaxQueuedBytes: 1, MaxQueuedAge: time.Second}},
		{name: "age", cfg: Config{PollInterval: time.Millisecond, MaxQueuedEvents: 100, MaxQueuedBytes: 1 << 20, MaxQueuedAge: 10 * time.Millisecond}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			pub := NewPublisher(openTestEventStore(t, "task-1")).WithConfig(tc.cfg)
			defer pub.Close()
			slow, err := pub.Subscribe(context.Background(), "task-1", "")
			if err != nil {
				t.Fatal(err)
			}
			fast, err := pub.Subscribe(context.Background(), "task-1", "")
			if err != nil {
				t.Fatal(err)
			}
			if err := pub.Publish(context.Background(), models.Task{TaskID: "task-1", Status: "running"}); err != nil {
				t.Fatal(err)
			}
			select {
			case <-fast:
			case <-time.After(time.Second):
				t.Fatal("fast subscriber was blocked by slow subscriber")
			}
			if tc.name == "events" {
				if err := pub.Publish(context.Background(), models.Task{TaskID: "task-1", Status: "accepted"}); err != nil {
					t.Fatal(err)
				}
			}
			if tc.name == "age" {
				time.Sleep(30 * time.Millisecond)
			}
			select {
			case _, ok := <-slow:
				if ok {
					select {
					case _, ok = <-slow:
						if ok {
							t.Fatal("slow subscriber remained connected")
						}
					case <-time.After(time.Second):
						t.Fatal("slow subscriber was not disconnected")
					}
				}
			case <-time.After(time.Second):
				t.Fatal("slow subscriber was not disconnected")
			}
		})
	}
}

type failingResponseWriter struct {
	header http.Header
}

func (w *failingResponseWriter) Header() http.Header {
	if w.header == nil {
		w.header = make(http.Header)
	}
	return w.header
}
func (w *failingResponseWriter) WriteHeader(int)           {}
func (w *failingResponseWriter) Write([]byte) (int, error) { return 0, errors.New("write failed") }
func (w *failingResponseWriter) FlushError() error         { return errors.New("flush failed") }

func TestServeTaskStreamWriterErrorCleansUp(t *testing.T) {
	pub := NewPublisher(openTestEventStore(t, "task-1"))
	err := ServeTaskStream(pub, &failingResponseWriter{}, httptest.NewRequest(http.MethodGet, "/", nil), "task-1")
	if err == nil {
		t.Fatal("expected writer error")
	}
	pub.Wait()
	pub.mu.Lock()
	defer pub.mu.Unlock()
	if len(pub.subs) != 0 {
		t.Fatalf("subscriber leaked after writer error: %d", len(pub.subs))
	}
}

func TestPublisherCloseIsIdempotentAndRejectsOperations(t *testing.T) {
	pub := NewPublisher(openTestEventStore(t, "task-1"))
	ch, err := pub.Subscribe(context.Background(), "task-1", "")
	if err != nil {
		t.Fatal(err)
	}
	if err := pub.Close(); err != nil {
		t.Fatal(err)
	}
	if err := pub.Close(); err != nil {
		t.Fatal(err)
	}
	pub.Wait()
	if _, ok := <-ch; ok {
		t.Fatal("subscriber channel remained open")
	}
	if _, err := pub.Subscribe(context.Background(), "task-1", ""); !errors.Is(err, ErrPublisherClosed) {
		t.Fatalf("subscribe after close: %v", err)
	}
	if err := pub.Publish(context.Background(), models.Task{TaskID: "task-1"}); !errors.Is(err, ErrPublisherClosed) {
		t.Fatalf("publish after close: %v", err)
	}
}

func TestConcurrentClosePublishSubscribe(t *testing.T) {
	pub := NewPublisher(openTestEventStore(t, "task-1"))
	var wg sync.WaitGroup
	for i := 0; i < 20; i++ {
		wg.Add(2)
		go func() {
			defer wg.Done()
			_, _ = pub.Subscribe(context.Background(), "task-1", "")
		}()
		go func() {
			defer wg.Done()
			_ = pub.Publish(context.Background(), models.Task{TaskID: "task-1", Status: "running"})
		}()
	}
	_ = pub.Close()
	wg.Wait()
	pub.Wait()
}
