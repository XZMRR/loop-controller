package stream

import (
	"context"
	"testing"
	"time"

	"github.com/loop-controller/go/internal/models"
)

func TestInMemoryPublisherSubscribeAndPublish(t *testing.T) {
	pub := NewInMemoryPublisher()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	ch, err := pub.Subscribe(ctx, "task-1")
	if err != nil {
		t.Fatalf("subscribe failed: %v", err)
	}

	task := models.Task{TaskID: "task-1", Status: "active"}
	if err := pub.Publish(ctx, task); err != nil {
		t.Fatalf("publish failed: %v", err)
	}

	select {
	case got := <-ch:
		if got.Status != "active" {
			t.Errorf("expected active, got %q", got.Status)
		}
	case <-time.After(time.Second):
		t.Fatal("timeout waiting for task event")
	}
}

func TestInMemoryPublisherCleansUpOnCancel(t *testing.T) {
	pub := NewInMemoryPublisher()
	ctx, cancel := context.WithCancel(context.Background())

	ch, err := pub.Subscribe(ctx, "task-1")
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

func TestConcurrentCancelAndPublish(t *testing.T) {
	pub := NewInMemoryPublisher()
	for i := 0; i < 100; i++ {
		ctx, cancel := context.WithCancel(context.Background())
		if _, err := pub.Subscribe(ctx, "task-1"); err != nil {
			t.Fatalf("subscribe failed: %v", err)
		}
		done := make(chan struct{})
		go func() {
			defer close(done)
			for j := 0; j < 100; j++ {
				if err := pub.Publish(context.Background(), models.Task{TaskID: "task-1"}); err != nil {
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
	pub := NewInMemoryPublisher()
	ctx := context.Background()
	_, err := pub.Subscribe(ctx, "")
	if err == nil {
		t.Fatal("expected error for empty task_id")
	}
}
