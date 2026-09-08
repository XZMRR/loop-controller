package stream

import (
	"context"
	"path/filepath"
	"testing"
	"time"

	"github.com/loop-controller/go/internal/models"
	"github.com/loop-controller/go/internal/store"
)

func openTestEventStore(t *testing.T, taskID string) EventStore {
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
	ch, err := pub.Subscribe(ctx, "task-1", history[0].EventID)
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

func TestSubscribeEmptyTaskID(t *testing.T) {
	pub := NewPublisher(openTestEventStore(t, ""))
	ctx := context.Background()
	_, err := pub.Subscribe(ctx, "", "")
	if err == nil {
		t.Fatal("expected error for empty task_id")
	}
}
