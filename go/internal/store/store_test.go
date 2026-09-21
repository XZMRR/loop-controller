package store

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"path/filepath"
	"strings"
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

func TestListTasksForTenantInitiator(t *testing.T) {
	db := openTestDB(t)
	ts := db.TaskStore()
	now := time.Now().UTC()
	tasks := []models.Task{
		{TaskID: "root-own", SessionID: "s", TenantID: "tenant-a", InitiatorAgentID: "agent-a", TargetAgentID: "target", Status: "pending", CreatedAt: now, UpdatedAt: now},
		{TaskID: "child-own", SessionID: "s", TenantID: "tenant-a", InitiatorAgentID: "agent-a", TargetAgentID: "target", ParentTaskID: "root-own", RootTaskID: "root-own", Status: "pending", CreatedAt: now.Add(time.Second), UpdatedAt: now.Add(time.Second)},
		{TaskID: "other-initiator", SessionID: "s", TenantID: "tenant-a", InitiatorAgentID: "agent-x", TargetAgentID: "target", Status: "pending", CreatedAt: now, UpdatedAt: now},
		{TaskID: "other-tenant", SessionID: "s", TenantID: "tenant-b", InitiatorAgentID: "agent-a", TargetAgentID: "target", Status: "pending", CreatedAt: now, UpdatedAt: now},
	}
	for _, task := range tasks {
		if err := ts.Create(context.Background(), task); err != nil {
			t.Fatal(err)
		}
	}
	all, err := ts.ListForTenantInitiator(context.Background(), "tenant-a", "agent-a", false)
	if err != nil || len(all) != 2 || all[0].TaskID != "child-own" || all[1].TaskID != "root-own" {
		t.Fatalf("all=%+v err=%v", all, err)
	}
	roots, err := ts.ListForTenantInitiator(context.Background(), "tenant-a", "agent-a", true)
	if err != nil || len(roots) != 1 || roots[0].TaskID != "root-own" {
		t.Fatalf("roots=%+v err=%v", roots, err)
	}
	empty, err := ts.ListForTenantInitiator(context.Background(), "tenant-missing", "agent-a", false)
	if err != nil || empty == nil || len(empty) != 0 {
		t.Fatalf("empty=%+v err=%v", empty, err)
	}
}

func TestListDescendantsScansCompleteTaskIdentity(t *testing.T) {
	db := openTestDB(t)
	ts := db.TaskStore()
	now := time.Now().UTC()
	parent := models.Task{TaskID: "desc-parent", SessionID: "session-1", InitiatorAgentID: "agent-a", TargetAgentID: "agent-b", Status: "pending", CreatedAt: now, UpdatedAt: now}
	child := models.Task{TaskID: "desc-child", ParentTaskID: parent.TaskID, SessionID: "session-1", InitiatorAgentID: "agent-b", TargetAgentID: "agent-c", Status: "pending", TenantID: "tenant-1", RequestID: "request-1", TargetWorkloadID: "workload-1", TargetInstanceID: "instance-1", CreatedAt: now.Add(time.Second), UpdatedAt: now.Add(time.Second)}
	for _, task := range []models.Task{parent, child} {
		if err := ts.Create(context.Background(), task); err != nil {
			t.Fatalf("create %s: %v", task.TaskID, err)
		}
	}

	descendants, err := ts.ListDescendants(context.Background(), parent.TaskID)
	if err != nil {
		t.Fatalf("list descendants: %v", err)
	}
	if len(descendants) != 1 {
		t.Fatalf("descendants = %d, want 1", len(descendants))
	}
	got := descendants[0]
	if got.TenantID != child.TenantID || got.RequestID != child.RequestID || got.TargetWorkloadID != child.TargetWorkloadID || got.TargetInstanceID != child.TargetInstanceID {
		t.Fatalf("descendant identity fields lost: %+v", got)
	}
}

func TestTaskCreateWithEventCommitsTogether(t *testing.T) {
	db := openTestDB(t)
	task := models.Task{
		TaskID:           "task-created-event",
		SessionID:        "session-1",
		InitiatorAgentID: "agent-a",
		TargetAgentID:    "agent-b",
		Status:           "pending",
		CreatedAt:        time.Now().UTC(),
		UpdatedAt:        time.Now().UTC(),
	}
	event, err := db.TaskStore().CreateWithEvent(context.Background(), task)
	if err != nil {
		t.Fatalf("create task with event: %v", err)
	}
	if event.EventType != "task_created" || event.TaskID != task.TaskID {
		t.Fatalf("unexpected created event: %+v", event)
	}
	var payload models.Task
	if err := json.Unmarshal(event.Payload, &payload); err != nil {
		t.Fatalf("decode event payload: %v", err)
	}
	if payload.TaskID != task.TaskID || payload.Status != "pending" {
		t.Fatalf("unexpected event payload: %+v", payload)
	}
	pending, err := db.EventStore().ListPending(context.Background(), task.TaskID)
	if err != nil {
		t.Fatalf("list pending events: %v", err)
	}
	if len(pending) != 1 || pending[0].EventID != event.EventID {
		t.Fatalf("persisted events = %+v, want event %q", pending, event.EventID)
	}
}

type retryingLifecycleAuditor struct {
	calls int
	tasks []models.Task
}

func (a *retryingLifecycleAuditor) RecordLifecycle(_ context.Context, task models.Task, _, _ string) error {
	a.calls++
	a.tasks = append(a.tasks, task)
	if a.calls == 1 {
		return errors.New("temporary audit failure")
	}
	return nil
}

func TestLifecycleOutboxIsTransactionalAndRetries(t *testing.T) {
	db := openTestDB(t)
	createdAt := time.Now().UTC()
	task := models.Task{
		TaskID: "task-lifecycle-outbox", SessionID: "session-1",
		InteractionID: "int-1", DecisionID: "dec-1", RootInteractionID: "root-1", ParentInteractionID: "parent-1",
		InitiatorAgentID: "agent-a", TargetAgentID: "agent-b", Status: "pending", CreatedAt: createdAt, UpdatedAt: createdAt,
	}
	if err := db.TaskStore().Create(context.Background(), task); err != nil {
		t.Fatalf("create task: %v", err)
	}
	if _, _, err := db.TaskStore().UpdateStatus(context.Background(), task.TaskID, "pending", "accepted", nil, ""); err != nil {
		t.Fatalf("accept task: %v", err)
	}

	now := time.Now().UTC()
	outbox := db.LifecycleOutboxStore()
	auditor := &retryingLifecycleAuditor{}
	dispatcher := NewLifecycleOutboxDispatcher(outbox, auditor, time.Second)
	if err := dispatcher.RunOnce(context.Background(), now); err != nil {
		t.Fatalf("first dispatch: %v", err)
	}
	if due, err := outbox.ClaimDue(context.Background(), now, time.Minute, 10); err != nil || len(due) != 0 {
		t.Fatalf("outbox should wait for retry: due=%+v err=%v", due, err)
	}
	if err := dispatcher.RunOnce(context.Background(), now.Add(time.Second)); err != nil {
		t.Fatalf("retry dispatch: %v", err)
	}
	if due, err := outbox.ClaimDue(context.Background(), now.Add(time.Hour), time.Minute, 10); err != nil || len(due) != 0 {
		t.Fatalf("outbox should be delivered: due=%+v err=%v", due, err)
	}
	if auditor.calls != 2 || len(auditor.tasks) != 2 {
		t.Fatalf("audit calls = %d, tasks=%d", auditor.calls, len(auditor.tasks))
	}
	got := auditor.tasks[1]
	if got.InteractionID != "int-1" || got.DecisionID != "dec-1" || got.RootInteractionID != "root-1" || got.ParentInteractionID != "parent-1" {
		t.Fatalf("outbox lost linkage: %+v", got)
	}
}

func TestLifecycleOutboxFailureRollsBackTaskTransition(t *testing.T) {
	db := openTestDB(t)
	now := time.Now().UTC()
	task := models.Task{
		TaskID: "task-outbox-rollback", SessionID: "session-1", InteractionID: "int-1", DecisionID: "dec-1",
		InitiatorAgentID: "agent-a", TargetAgentID: "agent-b", Status: "pending", CreatedAt: now, UpdatedAt: now,
	}
	if err := db.TaskStore().Create(context.Background(), task); err != nil {
		t.Fatalf("create task: %v", err)
	}
	if _, err := db.ExecContext(context.Background(), "DROP TABLE lifecycle_outbox"); err != nil {
		t.Fatalf("drop outbox: %v", err)
	}
	if _, _, err := db.TaskStore().UpdateStatus(context.Background(), task.TaskID, "pending", "accepted", nil, ""); err == nil {
		t.Fatal("expected outbox persistence failure")
	}
	got, err := db.TaskStore().Get(context.Background(), task.TaskID)
	if err != nil || got.Status != "pending" {
		t.Fatalf("transition was not rolled back: task=%+v err=%v", got, err)
	}
}

func TestLifecycleOutboxClaimIsExclusiveAcrossInstances(t *testing.T) {
	path := filepath.Join(t.TempDir(), "a2a.db")
	dbA, err := Open(context.Background(), path)
	if err != nil {
		t.Fatalf("open store A: %v", err)
	}
	defer dbA.Close()
	dbB, err := Open(context.Background(), path)
	if err != nil {
		t.Fatalf("open store B: %v", err)
	}
	defer dbB.Close()

	now := time.Now().UTC()
	task := models.Task{
		TaskID: "task-outbox-exclusive", SessionID: "session-1", InteractionID: "int-1", DecisionID: "dec-1",
		InitiatorAgentID: "agent-a", TargetAgentID: "agent-b", Status: "pending", CreatedAt: now, UpdatedAt: now,
	}
	if err := dbA.TaskStore().Create(context.Background(), task); err != nil {
		t.Fatalf("create task: %v", err)
	}
	if _, _, err := dbA.TaskStore().UpdateStatus(context.Background(), task.TaskID, "pending", "accepted", nil, ""); err != nil {
		t.Fatalf("accept task: %v", err)
	}

	storeA := dbA.LifecycleOutboxStore()
	storeB := dbB.LifecycleOutboxStore()

	claimedA, err := storeA.ClaimDue(context.Background(), now.Add(time.Second), time.Minute, 10)
	if err != nil {
		t.Fatalf("claim A: %v", err)
	}
	if len(claimedA) != 1 {
		t.Fatalf("instance A claimed %d items, want 1", len(claimedA))
	}

	claimedB, err := storeB.ClaimDue(context.Background(), now.Add(time.Second), time.Minute, 10)
	if err != nil {
		t.Fatalf("claim B: %v", err)
	}
	if len(claimedB) != 0 {
		t.Fatalf("instance B double-claimed %d items, want 0: %+v", len(claimedB), claimedB)
	}

	if err := storeA.MarkDelivered(context.Background(), claimedA[0].ID, claimedA[0].ClaimToken, now.Add(time.Second)); err != nil {
		t.Fatalf("mark delivered: %v", err)
	}
	claimedB, err = storeB.ClaimDue(context.Background(), now.Add(2*time.Second), time.Minute, 10)
	if err != nil {
		t.Fatalf("claim B after deliver: %v", err)
	}
	if len(claimedB) != 0 {
		t.Fatalf("delivered item was re-claimed: %+v", claimedB)
	}
}

func TestTaskCreateAndEventRollbackTogether(t *testing.T) {
	db := openTestDB(t)
	if _, err := db.ExecContext(context.Background(), "DROP TABLE events"); err != nil {
		t.Fatalf("drop events table: %v", err)
	}
	task := models.Task{
		TaskID:           "task-create-rollback",
		SessionID:        "session-1",
		InitiatorAgentID: "agent-a",
		TargetAgentID:    "agent-b",
		Status:           "pending",
		CreatedAt:        time.Now().UTC(),
		UpdatedAt:        time.Now().UTC(),
	}
	if _, err := db.TaskStore().CreateWithEvent(context.Background(), task); err == nil {
		t.Fatal("expected created event persistence failure")
	}
	if _, err := db.TaskStore().Get(context.Background(), task.TaskID); err == nil {
		t.Fatal("task must be rolled back when created event persistence fails")
	}
}

func TestTaskStatusUpdatePersistsTransitionEvent(t *testing.T) {
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

	if _, _, err := ts.UpdateStatus(context.Background(), task.TaskID, "pending", "accepted", nil, ""); err != nil {
		t.Fatalf("accept task: %v", err)
	}
	if _, _, err := ts.UpdateStatus(context.Background(), task.TaskID, "accepted", "running", nil, ""); err != nil {
		t.Fatalf("start task: %v", err)
	}
	outcome := json.RawMessage(`{"result":"ok"}`)
	updated, event, err := ts.UpdateStatus(context.Background(), task.TaskID, "running", "completed", outcome, "")
	if err != nil {
		t.Fatalf("complete task: %v", err)
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
	if event.EventType != "task_completed" || event.TaskID != task.TaskID {
		t.Fatalf("unexpected transition event: %+v", event)
	}
	events, err := db.EventStore().ListAfter(context.Background(), task.TaskID, "")
	if err != nil {
		t.Fatalf("list transition events: %v", err)
	}
	if len(events) != 3 || events[2].EventID != event.EventID {
		t.Fatalf("persisted events = %+v, want final event %q", events, event.EventID)
	}
}

func TestTaskStoreRejectsInvalidTransitions(t *testing.T) {
	db := openTestDB(t)
	ts := db.TaskStore()
	task := models.Task{
		TaskID: "task-invalid-transition", SessionID: "session-1",
		InitiatorAgentID: "agent-a", TargetAgentID: "agent-b", Status: "pending",
		CreatedAt: time.Now().UTC(), UpdatedAt: time.Now().UTC(),
	}
	if err := ts.Create(context.Background(), task); err != nil {
		t.Fatalf("create task: %v", err)
	}

	for _, status := range []string{"running", "completed"} {
		if _, _, err := ts.UpdateStatus(context.Background(), task.TaskID, "pending", status, nil, ""); err != ErrInvalidTransition {
			t.Errorf("pending->%s error = %v, want ErrInvalidTransition", status, err)
		}
	}
}

func TestTaskStatusAndEventRollbackTogether(t *testing.T) {
	db := openTestDB(t)
	task := models.Task{
		TaskID:           "task-rollback",
		SessionID:        "session-1",
		InitiatorAgentID: "agent-a",
		TargetAgentID:    "agent-b",
		Status:           "pending",
		CreatedAt:        time.Now().UTC(),
		UpdatedAt:        time.Now().UTC(),
	}
	if err := db.TaskStore().Create(context.Background(), task); err != nil {
		t.Fatalf("create task: %v", err)
	}
	if _, err := db.ExecContext(context.Background(), "DROP TABLE events"); err != nil {
		t.Fatalf("drop events table: %v", err)
	}

	if _, _, err := db.TaskStore().UpdateStatus(context.Background(), task.TaskID, "pending", "accepted", nil, ""); err == nil {
		t.Fatal("expected transition event persistence failure")
	}
	got, err := db.TaskStore().Get(context.Background(), task.TaskID)
	if err != nil {
		t.Fatalf("get task: %v", err)
	}
	if got.Status != "pending" {
		t.Fatalf("status = %q, want pending after rollback", got.Status)
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

func TestEventSequencesArePerTaskAndConcurrentSafe(t *testing.T) {
	db := openTestDB(t)
	ctx := context.Background()
	for _, taskID := range []string{"sequence-a", "sequence-b"} {
		if err := db.TaskStore().Create(ctx, models.Task{TaskID: taskID, SessionID: "s", InitiatorAgentID: "a", TargetAgentID: "b", Status: "pending", CreatedAt: time.Now().UTC(), UpdatedAt: time.Now().UTC()}); err != nil {
			t.Fatal(err)
		}
	}
	const count = 40
	var wg sync.WaitGroup
	errs := make(chan error, count*2)
	for i := 0; i < count; i++ {
		for _, taskID := range []string{"sequence-a", "sequence-b"} {
			wg.Add(1)
			go func(i int, taskID string) {
				defer wg.Done()
				errs <- db.EventStore().Append(ctx, models.TaskEvent{EventID: fmt.Sprintf("%s-%d", taskID, i), TaskID: taskID, EventType: "test", Payload: []byte(`{}`), PublishedAt: time.Now().UTC()})
			}(i, taskID)
		}
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		if err != nil {
			t.Fatal(err)
		}
	}
	for _, taskID := range []string{"sequence-a", "sequence-b"} {
		events, err := db.EventStore().ListAfter(ctx, taskID, "")
		if err != nil {
			t.Fatal(err)
		}
		if len(events) != count {
			t.Fatalf("%s events=%d, want %d", taskID, len(events), count)
		}
		for i, event := range events {
			if event.Sequence != int64(i+1) || event.SchemaVersion != models.TaskEventSchemaVersion {
				t.Fatalf("%s event %d sequence/schema=%d/%d", taskID, i, event.Sequence, event.SchemaVersion)
			}
		}
	}
}

func TestEventCursorContract(t *testing.T) {
	db := openTestDB(t)
	ctx := context.Background()
	for _, taskID := range []string{"cursor-a", "cursor-b"} {
		if err := db.TaskStore().Create(ctx, models.Task{TaskID: taskID, SessionID: "s", InitiatorAgentID: "a", TargetAgentID: "b", Status: "pending", CreatedAt: time.Now().UTC(), UpdatedAt: time.Now().UTC()}); err != nil {
			t.Fatal(err)
		}
	}
	for i := 1; i <= 3; i++ {
		if err := db.EventStore().Append(ctx, models.TaskEvent{EventID: fmt.Sprintf("cursor-a-%d", i), TaskID: "cursor-a", EventType: "test", Payload: []byte(`{}`)}); err != nil {
			t.Fatal(err)
		}
	}
	events, err := db.EventStore().ListAfter(ctx, "cursor-a", "")
	if err != nil || len(events) != 3 {
		t.Fatalf("events=%d err=%v", len(events), err)
	}
	if events[0].Cursor == "" || strings.Contains(events[0].Cursor, events[0].EventID) {
		t.Fatalf("cursor is not opaque: %q", events[0].Cursor)
	}
	after, err := db.EventStore().ListAfter(ctx, "cursor-a", events[0].Cursor)
	if err != nil || len(after) != 2 || after[0].Sequence != 2 {
		t.Fatalf("valid cursor result=%+v err=%v", after, err)
	}
	legacy, err := db.EventStore().ListAfter(ctx, "cursor-a", events[0].EventID)
	if err != nil || len(legacy) != 2 {
		t.Fatalf("legacy event_id cursor result=%+v err=%v", legacy, err)
	}
	if _, err := db.EventStore().ListAfter(ctx, "cursor-a", "not-a-cursor"); !errors.Is(err, ErrEventCursorInvalid) {
		t.Fatalf("invalid cursor error=%v", err)
	}
	if _, err := db.EventStore().ListAfter(ctx, "cursor-b", events[0].Cursor); !errors.Is(err, ErrEventCursorInvalid) {
		t.Fatalf("cross-task cursor error=%v", err)
	}
	future, err := EncodeEventCursor("cursor-a", 4, models.TaskEventSchemaVersion)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.EventStore().ListAfter(ctx, "cursor-a", future); !errors.Is(err, ErrEventCursorFuture) {
		t.Fatalf("future cursor error=%v", err)
	}
	if _, err := db.EventStore().Compact(ctx, "cursor-a", 2); err != nil {
		t.Fatal(err)
	}
	if _, err := db.EventStore().ListAfter(ctx, "cursor-a", events[0].Cursor); !errors.Is(err, ErrEventCursorExpired) {
		t.Fatalf("expired cursor error=%v", err)
	}
	if _, err := db.EventStore().ListAfter(ctx, "cursor-a", events[0].EventID); !errors.Is(err, ErrEventCursorInvalid) {
		t.Fatalf("deleted legacy cursor error=%v", err)
	}
}

func TestEventCompactionRetainsTerminalAuditAtomically(t *testing.T) {
	db := openTestDB(t)
	ctx := context.Background()
	task := models.Task{TaskID: "compact-terminal", SessionID: "s", InitiatorAgentID: "a", TargetAgentID: "b", Status: "pending", CreatedAt: time.Now().UTC(), UpdatedAt: time.Now().UTC()}
	if _, err := db.TaskStore().CreateWithEvent(ctx, task); err != nil {
		t.Fatal(err)
	}
	if _, _, err := db.TaskStore().UpdateStatus(ctx, task.TaskID, "pending", "accepted", nil, ""); err != nil {
		t.Fatal(err)
	}
	if _, _, err := db.TaskStore().UpdateStatus(ctx, task.TaskID, "accepted", "running", nil, ""); err != nil {
		t.Fatal(err)
	}
	outcome := json.RawMessage(`{"ok":true}`)
	if _, _, err := db.TaskStore().UpdateStatus(ctx, task.TaskID, "running", "completed", outcome, ""); err != nil {
		t.Fatal(err)
	}
	audit, err := db.EventStore().Compact(ctx, task.TaskID, 4)
	if err != nil {
		t.Fatal(err)
	}
	if audit.TerminalStatus != "completed" || audit.TerminalSequence != 4 || audit.TerminalEventType != "task_completed" || audit.TerminalEventAt.IsZero() || len(audit.TerminalEventPayload) == 0 {
		t.Fatalf("terminal audit=%+v", audit)
	}
	var lower, eventCount, auditCount int64
	if err := db.QueryRowContext(ctx, `SELECT lower_sequence FROM event_retention WHERE task_id=?`, task.TaskID).Scan(&lower); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRowContext(ctx, `SELECT COUNT(*) FROM events WHERE task_id=?`, task.TaskID).Scan(&eventCount); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRowContext(ctx, `SELECT COUNT(*) FROM terminal_event_audit WHERE task_id=?`, task.TaskID).Scan(&auditCount); err != nil {
		t.Fatal(err)
	}
	if lower != 4 || eventCount != 1 || auditCount != 1 {
		t.Fatalf("lower/events/audits=%d/%d/%d", lower, eventCount, auditCount)
	}

	rollbackTask := models.Task{TaskID: "compact-rollback", SessionID: "s", InitiatorAgentID: "a", TargetAgentID: "b", Status: "completed", CreatedAt: time.Now().UTC(), UpdatedAt: time.Now().UTC()}
	if err := db.TaskStore().Create(ctx, rollbackTask); err != nil {
		t.Fatal(err)
	}
	if err := db.EventStore().Append(ctx, models.TaskEvent{EventID: "rollback-terminal", TaskID: rollbackTask.TaskID, EventType: "task_completed", Payload: []byte(`{}`)}); err != nil {
		t.Fatal(err)
	}
	if _, err := db.ExecContext(ctx, `DROP TABLE terminal_event_audit`); err != nil {
		t.Fatal(err)
	}
	if _, err := db.EventStore().Compact(ctx, rollbackTask.TaskID, 2); err == nil {
		t.Fatal("expected audit write failure")
	}
	if err := db.QueryRowContext(ctx, `SELECT lower_sequence FROM event_retention WHERE task_id=?`, rollbackTask.TaskID).Scan(&lower); err != nil || lower != 1 {
		t.Fatalf("watermark changed after rollback: %d err=%v", lower, err)
	}
	if err := db.QueryRowContext(ctx, `SELECT COUNT(*) FROM events WHERE task_id=?`, rollbackTask.TaskID).Scan(&eventCount); err != nil || eventCount != 1 {
		t.Fatalf("events changed after rollback: %d err=%v", eventCount, err)
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

func seedRunningTask(t *testing.T, ts TaskStore, taskID string) {
	t.Helper()
	now := time.Now().UTC()
	task := models.Task{
		TaskID: taskID, SessionID: "session-1",
		InitiatorAgentID: "agent-a", TargetAgentID: "agent-b", Status: "pending",
		CreatedAt: now, UpdatedAt: now,
	}
	if err := ts.Create(context.Background(), task); err != nil {
		t.Fatalf("create task: %v", err)
	}
	if _, _, err := ts.UpdateStatus(context.Background(), taskID, "pending", "accepted", nil, ""); err != nil {
		t.Fatalf("accept task: %v", err)
	}
	if _, _, err := ts.UpdateStatus(context.Background(), taskID, "accepted", "running", nil, ""); err != nil {
		t.Fatalf("run task: %v", err)
	}
}

func TestChildBudgetReservationConcurrentAndSettlement(t *testing.T) {
	db := openTestDB(t)
	ts := db.TaskStore()
	now := time.Now().UTC()
	parent := models.Task{TaskID: "budget-parent", SessionID: "s", InitiatorAgentID: "a", TargetAgentID: "b", Status: "running", Budget: models.DelegationBudget{TokenCount: 100, PaymentAmount: 10, Currency: "USD"}, CreatedAt: now, UpdatedAt: now}
	if err := ts.Create(context.Background(), parent); err != nil {
		t.Fatalf("create parent: %v", err)
	}

	var wg sync.WaitGroup
	results := make(chan string, 3)
	for i := 0; i < 3; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			child := models.Task{TaskID: fmt.Sprintf("budget-child-%d", i), ParentTaskID: parent.TaskID, SessionID: "s", InitiatorAgentID: "b", TargetAgentID: "c", Status: "pending", Budget: models.DelegationBudget{TokenCount: 60, PaymentAmount: 6, Currency: "USD"}, CreatedAt: now, UpdatedAt: now}
			if _, err := ts.CreateChildWithBudget(context.Background(), child); err == nil {
				results <- child.TaskID
			}
		}(i)
	}
	wg.Wait()
	close(results)
	var created []string
	for id := range results {
		created = append(created, id)
	}
	if len(created) != 1 {
		t.Fatalf("created children = %d, want 1", len(created))
	}
	reserved, _ := ts.Get(context.Background(), parent.TaskID)
	if reserved.ReservedBudget.TokenCount != 60 || reserved.ConsumedBudget.TokenCount != 0 {
		t.Fatalf("unexpected reserved parent budget: %+v", reserved)
	}
	if _, _, err := ts.UpdateStatusWithConsumption(context.Background(), created[0], "pending", "failed", nil, "dispatch_failed", models.DelegationBudget{}); err != nil {
		t.Fatalf("refund failed child: %v", err)
	}
	refunded, _ := ts.Get(context.Background(), parent.TaskID)
	if refunded.ReservedBudget.TokenCount != 0 || refunded.ConsumedBudget.TokenCount != 0 {
		t.Fatalf("failed child was not refunded: %+v", refunded)
	}
}

func TestChildBudgetExplicitConsumptionAndOutcomeUnknown(t *testing.T) {
	db := openTestDB(t)
	ts := db.TaskStore()
	now := time.Now().UTC()
	parent := models.Task{TaskID: "settle-parent", SessionID: "s", InitiatorAgentID: "a", TargetAgentID: "b", Status: "running", Budget: models.DelegationBudget{TokenCount: 100, PaymentAmount: 10, Currency: "USD"}, CreatedAt: now, UpdatedAt: now}
	if err := ts.Create(context.Background(), parent); err != nil {
		t.Fatal(err)
	}
	child := models.Task{TaskID: "settle-child", ParentTaskID: parent.TaskID, SessionID: "s", InitiatorAgentID: "b", TargetAgentID: "c", Status: "pending", Budget: models.DelegationBudget{TokenCount: 60, PaymentAmount: 6, Currency: "USD"}, CreatedAt: now, UpdatedAt: now}
	if _, err := ts.CreateChildWithBudget(context.Background(), child); err != nil {
		t.Fatal(err)
	}
	if _, _, err := ts.UpdateStatus(context.Background(), child.TaskID, "pending", "outcome_unknown", nil, ""); err != nil {
		t.Fatal(err)
	}
	unknown, _ := ts.Get(context.Background(), parent.TaskID)
	if unknown.ReservedBudget.TokenCount != 60 || unknown.ConsumedBudget.TokenCount != 0 {
		t.Fatalf("outcome_unknown changed parent budget: %+v", unknown)
	}
	consumed := models.DelegationBudget{TokenCount: 25, PaymentAmount: 2.5, Currency: "USD"}
	if _, _, err := ts.UpdateStatusWithConsumption(context.Background(), child.TaskID, "outcome_unknown", "completed", nil, "", consumed); err != nil {
		t.Fatal(err)
	}
	settled, _ := ts.Get(context.Background(), parent.TaskID)
	if settled.ReservedBudget.TokenCount != 0 || settled.ReservedBudget.PaymentAmount != 0 || settled.ConsumedBudget.TokenCount != 25 || settled.ConsumedBudget.PaymentAmount != 2.5 {
		t.Fatalf("explicit consumption not settled: %+v", settled)
	}
	if _, _, err := ts.UpdateStatusWithConsumption(context.Background(), child.TaskID, "outcome_unknown", "completed", nil, "", consumed); err != ErrStatusConflict {
		t.Fatalf("duplicate settlement error = %v, want conflict", err)
	}
	afterDuplicate, _ := ts.Get(context.Background(), parent.TaskID)
	if afterDuplicate.ConsumedBudget.TokenCount != 25 || afterDuplicate.ReservedBudget.TokenCount != 0 {
		t.Fatalf("duplicate settlement changed parent budget: %+v", afterDuplicate)
	}
}

func TestExecutionLeaseSetOnRunning(t *testing.T) {
	db := openTestDB(t)
	seedRunningTask(t, db.TaskStore(), "task-lease-001")

	var owner string
	var expiresAt int64
	if err := db.QueryRowContext(context.Background(), `SELECT exec_owner, exec_lease_expires_at FROM tasks WHERE task_id = ?`, "task-lease-001").Scan(&owner, &expiresAt); err != nil {
		t.Fatalf("query lease: %v", err)
	}
	if owner == "" {
		t.Error("exec_owner should be set when a task enters running")
	}
	if expiresAt <= 0 {
		t.Error("exec_lease_expires_at should be positive when a task enters running")
	}
}

func TestRenewExecutionLease(t *testing.T) {
	db := openTestDB(t)
	ts := db.TaskStore()
	seedRunningTask(t, ts, "task-lease-renew")

	const before int64 = 1
	if _, err := db.ExecContext(context.Background(), `UPDATE tasks SET exec_lease_expires_at = ? WHERE task_id = ?`, before, "task-lease-renew"); err != nil {
		t.Fatalf("set lease before renewal: %v", err)
	}
	if err := ts.RenewExecutionLease(context.Background(), "task-lease-renew"); err != nil {
		t.Fatalf("renew lease: %v", err)
	}
	var after int64
	if err := db.QueryRowContext(context.Background(), `SELECT exec_lease_expires_at FROM tasks WHERE task_id = ?`, "task-lease-renew").Scan(&after); err != nil {
		t.Fatalf("query lease after: %v", err)
	}
	if after <= before {
		t.Errorf("exec_lease_expires_at = %d, want > %d", after, before)
	}
}

func TestRenewExecutionLeaseReportsLostLease(t *testing.T) {
	db := openTestDB(t)
	ts := db.TaskStore()
	seedRunningTask(t, ts, "task-lease-owner")

	now := time.Now().UTC()
	nonRunning := models.Task{TaskID: "task-lease-not-running", SessionID: "session-1", InitiatorAgentID: "agent-a", TargetAgentID: "agent-b", Status: "pending", CreatedAt: now, UpdatedAt: now}
	if err := ts.Create(context.Background(), nonRunning); err != nil {
		t.Fatalf("create non-running task: %v", err)
	}
	if _, err := db.ExecContext(context.Background(), `UPDATE tasks SET exec_owner = ? WHERE task_id = ?`, "different-owner", "task-lease-owner"); err != nil {
		t.Fatalf("replace lease owner: %v", err)
	}

	for _, taskID := range []string{"task-lease-missing", nonRunning.TaskID, "task-lease-owner"} {
		if err := ts.RenewExecutionLease(context.Background(), taskID); !errors.Is(err, ErrExecutionLeaseLost) {
			t.Errorf("renew %s error = %v, want ErrExecutionLeaseLost", taskID, err)
		}
	}
}

func TestRecoverExpiredRunning(t *testing.T) {
	db := openTestDB(t)
	ts := db.TaskStore()
	seedRunningTask(t, ts, "task-recover")

	expired := time.Now().Add(-time.Minute).UnixNano()
	if _, err := db.ExecContext(context.Background(), `UPDATE tasks SET exec_lease_expires_at = ? WHERE task_id = ?`, expired, "task-recover"); err != nil {
		t.Fatalf("expire lease: %v", err)
	}

	recovered, err := ts.RecoverExpiredRunning(context.Background(), time.Now().UTC(), 10)
	if err != nil {
		t.Fatalf("recover: %v", err)
	}
	if len(recovered) != 1 {
		t.Fatalf("recovered = %d, want 1", len(recovered))
	}
	if recovered[0].TaskID != "task-recover" || recovered[0].Status != "outcome_unknown" {
		t.Fatalf("unexpected recovered task: %+v", recovered[0])
	}

	events, err := db.EventStore().ListAfter(context.Background(), "task-recover", "")
	if err != nil {
		t.Fatalf("list events: %v", err)
	}
	if len(events) == 0 || events[len(events)-1].EventType != "task_outcome_unknown" {
		t.Fatalf("recovery event missing: %+v", events)
	}
}

func TestRecoverExpiredRunningIsExclusiveAcrossInstances(t *testing.T) {
	path := filepath.Join(t.TempDir(), "a2a.db")
	dbA, err := Open(context.Background(), path)
	if err != nil {
		t.Fatalf("open store A: %v", err)
	}
	defer dbA.Close()
	dbB, err := Open(context.Background(), path)
	if err != nil {
		t.Fatalf("open store B: %v", err)
	}
	defer dbB.Close()

	seedRunningTask(t, dbA.TaskStore(), "task-recover-exclusive")
	expired := time.Now().Add(-time.Minute).UnixNano()
	if _, err := dbA.ExecContext(context.Background(), `UPDATE tasks SET exec_lease_expires_at = ? WHERE task_id = ?`, expired, "task-recover-exclusive"); err != nil {
		t.Fatalf("expire lease: %v", err)
	}

	var wg sync.WaitGroup
	var mu sync.Mutex
	total := 0
	errs := make(chan error, 2)
	for _, ts := range []TaskStore{dbA.TaskStore(), dbB.TaskStore()} {
		wg.Add(1)
		go func(ts TaskStore) {
			defer wg.Done()
			recovered, err := ts.RecoverExpiredRunning(context.Background(), time.Now().UTC(), 10)
			if err != nil {
				errs <- err
				return
			}
			mu.Lock()
			total += len(recovered)
			mu.Unlock()
		}(ts)
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		if err != nil {
			t.Fatalf("recover across instances: %v", err)
		}
	}
	if total != 1 {
		t.Fatalf("recovered total = %d, want exactly 1", total)
	}
}

func TestExecutionQueueEventIDsUniqueAcrossStores(t *testing.T) {
	t.Parallel()

	const (
		storeCount  = 32
		idsPerStore = 256
	)
	now := time.Unix(1_700_000_000, 123_000_000).UTC()
	ids := make(chan string, storeCount*idsPerStore)
	var wg sync.WaitGroup
	for range storeCount {
		queue := &executionQueueStore{owner: "shared-owner"}
		wg.Add(1)
		go func() {
			defer wg.Done()
			for range idsPerStore {
				ids <- newEventID(queue.owner, "shared-task", now)
			}
		}()
	}
	wg.Wait()
	close(ids)

	seen := make(map[string]struct{}, storeCount*idsPerStore)
	for id := range ids {
		if _, exists := seen[id]; exists {
			t.Fatalf("duplicate event ID: %q", id)
		}
		seen[id] = struct{}{}
	}
	if len(seen) != storeCount*idsPerStore {
		t.Fatalf("unique event IDs = %d, want %d", len(seen), storeCount*idsPerStore)
	}
}
