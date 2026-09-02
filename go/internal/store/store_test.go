package store

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"github.com/loop-controller/go/internal/models"
)

func openTestDB(t *testing.T) *DB {
	t.Helper()
	db, err := Open(context.Background(), filepath.Join(t.TempDir(), "a2a.db"))
	if err != nil {
		t.Fatalf("open store: %v", err)
	}
	t.Cleanup(func() { _ = db.Close() })
	return db
}

func TestTaskCreateAndGet(t *testing.T) {
	db := openTestDB(t)
	ts := db.TaskStore()
	task := models.Task{
		TaskID:           "task-001",
		SessionID:        "session-1",
		InitiatorAgentID: "agent-a",
		TargetAgentID:    "agent-b",
		Status:           "pending",
		CreatedAt:        time.Now().UTC(),
		UpdatedAt:        time.Now().UTC(),
	}
	if err := ts.Create(context.Background(), task); err != nil {
		t.Fatalf("create task: %v", err)
	}
	got, err := ts.Get(context.Background(), task.TaskID)
	if err != nil {
		t.Fatalf("get task: %v", err)
	}
	if got.TaskID != task.TaskID {
		t.Errorf("task_id = %q, want %q", got.TaskID, task.TaskID)
	}
	if got.Status != "pending" {
		t.Errorf("status = %q, want pending", got.Status)
	}
}

func TestTaskStatusUpdate(t *testing.T) {
	db := openTestDB(t)
	ts := db.TaskStore()
	task := models.Task{
		TaskID:           "task-002",
		SessionID:        "session-1",
		InitiatorAgentID: "agent-a",
		TargetAgentID:    "agent-b",
		Status:           "pending",
		CreatedAt:        time.Now().UTC(),
		UpdatedAt:        time.Now().UTC(),
	}
	if err := ts.Create(context.Background(), task); err != nil {
		t.Fatalf("create task: %v", err)
	}

	outcome := json.RawMessage(`{"result":"ok"}`)
	updated, err := ts.UpdateStatus(context.Background(), task.TaskID, "completed", outcome, "")
	if err != nil {
		t.Fatalf("update status: %v", err)
	}
	if updated.Status != "completed" {
		t.Errorf("status = %q, want completed", updated.Status)
	}
	if string(updated.Outcome) != string(outcome) {
		t.Errorf("outcome = %q, want %q", updated.Outcome, outcome)
	}
	if updated.CompletedAt == nil {
		t.Error("expected completed_at to be set")
	}
}

func TestTaskListBySessionAndTarget(t *testing.T) {
	db := openTestDB(t)
	ts := db.TaskStore()
	for i := 0; i < 3; i++ {
		task := models.Task{
			TaskID:           fmt.Sprintf("task-list-%d", i),
			SessionID:        "session-x",
			InitiatorAgentID: "agent-a",
			TargetAgentID:    "agent-b",
			Status:           "pending",
			CreatedAt:        time.Now().UTC(),
			UpdatedAt:        time.Now().UTC(),
		}
		if err := ts.Create(context.Background(), task); err != nil {
			t.Fatalf("create task: %v", err)
		}
	}

	bySession, err := ts.ListBySession(context.Background(), "session-x")
	if err != nil {
		t.Fatalf("list by session: %v", err)
	}
	if len(bySession) != 3 {
		t.Errorf("by session count = %d, want 3", len(bySession))
	}

	byTarget, err := ts.ListByTarget(context.Background(), "agent-b")
	if err != nil {
		t.Fatalf("list by target: %v", err)
	}
	if len(byTarget) != 3 {
		t.Errorf("by target count = %d, want 3", len(byTarget))
	}
}

func TestMessageSaveAndListByTask(t *testing.T) {
	db := openTestDB(t)
	ts := db.TaskStore()
	ms := db.MessageStore()

	task := models.Task{
		TaskID:           "task-msg-001",
		SessionID:        "session-1",
		InitiatorAgentID: "agent-a",
		TargetAgentID:    "agent-b",
		Status:           "pending",
		CreatedAt:        time.Now().UTC(),
		UpdatedAt:        time.Now().UTC(),
	}
	if err := ts.Create(context.Background(), task); err != nil {
		t.Fatalf("create task: %v", err)
	}

	msg := models.Message{
		MessageID:       "msg-001",
		TaskID:          task.TaskID,
		FromAgentID:     "agent-a",
		ToAgentID:       "agent-b",
		Role:            "user",
		Parts:           []models.Part{{Type: "text", Text: "hello"}},
		Timestamp:       time.Now().UTC(),
		ProtocolVersion: "0.36.1",
	}
	if err := ms.Save(context.Background(), msg); err != nil {
		t.Fatalf("save message: %v", err)
	}

	got, err := ms.ListByTask(context.Background(), task.TaskID)
	if err != nil {
		t.Fatalf("list messages: %v", err)
	}
	if len(got) != 1 {
		t.Fatalf("message count = %d, want 1", len(got))
	}
	if got[0].MessageID != "msg-001" {
		t.Errorf("message_id = %q, want msg-001", got[0].MessageID)
	}
	if len(got[0].Parts) != 1 || got[0].Parts[0].Text != "hello" {
		t.Errorf("parts mismatch: %+v", got[0].Parts)
	}
}

func TestEventAppendAndListPending(t *testing.T) {
	db := openTestDB(t)
	ts := db.TaskStore()
	es := db.EventStore()

	task := models.Task{
		TaskID:           "task-ev-001",
		SessionID:        "session-1",
		InitiatorAgentID: "agent-a",
		TargetAgentID:    "agent-b",
		Status:           "pending",
		CreatedAt:        time.Now().UTC(),
		UpdatedAt:        time.Now().UTC(),
	}
	if err := ts.Create(context.Background(), task); err != nil {
		t.Fatalf("create task: %v", err)
	}

	ev := models.TaskEvent{
		EventID:     "ev-001",
		TaskID:      task.TaskID,
		EventType:   "task_created",
		Payload:     []byte(`{"task_id":"task-ev-001","status":"pending"}`),
		PublishedAt: time.Now().UTC(),
	}
	if err := es.Append(context.Background(), ev); err != nil {
		t.Fatalf("append event: %v", err)
	}

	pending, err := es.ListPending(context.Background(), task.TaskID)
	if err != nil {
		t.Fatalf("list pending: %v", err)
	}
	if len(pending) != 1 {
		t.Fatalf("pending count = %d, want 1", len(pending))
	}
	if pending[0].EventID != "ev-001" {
		t.Errorf("event_id = %q, want ev-001", pending[0].EventID)
	}

	if err := es.MarkPublished(context.Background(), []string{"ev-001"}); err != nil {
		t.Fatalf("mark published: %v", err)
	}
	pending, err = es.ListPending(context.Background(), task.TaskID)
	if err != nil {
		t.Fatalf("list pending after mark: %v", err)
	}
	if len(pending) != 0 {
		t.Errorf("pending count after mark = %d, want 0", len(pending))
	}
}

func TestIdempotencyConcurrentDuplicate(t *testing.T) {
	db := openTestDB(t)
	is := db.IdempotencyStore()

	var wg sync.WaitGroup
	errors := make(chan error, 20)
	for i := 0; i < 20; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			_, err := is.TryBegin(context.Background(), "key-1", "scope-1", "request-hash-1")
			errors <- err
		}()
	}
	wg.Wait()
	close(errors)

	lockedCount := 0
	successCount := 0
	for err := range errors {
		if err == ErrKeyLocked {
			lockedCount++
			continue
		}
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		successCount++
	}
	if successCount != 1 {
		t.Errorf("success count = %d, want 1", successCount)
	}
	if lockedCount != 19 {
		t.Errorf("locked count = %d, want 19", lockedCount)
	}
}

func TestIdempotencyRetrieveCachedResponse(t *testing.T) {
	db := openTestDB(t)
	is := db.IdempotencyStore()

	key := "key-2"
	scope := "scope-1"
	reqHash := "request-hash-2"

	_, err := is.TryBegin(context.Background(), key, scope, reqHash)
	if err != nil {
		t.Fatalf("try begin: %v", err)
	}
	respBody := []byte(`{"task_id":"task-001"}`)
	if err := is.Complete(context.Background(), key, scope, http.StatusCreated, respBody); err != nil {
		t.Fatalf("complete: %v", err)
	}

	cached, err := is.Retrieve(context.Background(), key, scope)
	if err != nil {
		t.Fatalf("retrieve: %v", err)
	}
	if cached.Locked {
		t.Error("expected completed key to be unlocked")
	}
	if cached.ResponseStatus != http.StatusCreated {
		t.Errorf("response_status = %d, want %d", cached.ResponseStatus, http.StatusCreated)
	}
	if string(cached.ResponseBody) != string(respBody) {
		t.Errorf("response_body = %q, want %q", cached.ResponseBody, respBody)
	}

	// Same key with a different request hash must be rejected.
	_, err = is.TryBegin(context.Background(), key, scope, "different-hash")
	if err == nil {
		t.Fatal("expected error for reused key with different request")
	}
}

func TestOpenCreatesMissingDirectory(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "nested", "path")
	db, err := Open(context.Background(), filepath.Join(dir, "a2a.db"))
	if err != nil {
		t.Fatalf("open store with missing directory: %v", err)
	}
	defer db.Close()
	if db.Path() == "" {
		t.Fatal("expected non-empty db path")
	}
}
