package assignmentworker

import (
	"context"
	"errors"
	"path/filepath"
	"sync/atomic"
	"testing"
	"time"

	"github.com/loop-controller/go/internal/execution"
	"github.com/loop-controller/go/internal/models"
	"github.com/loop-controller/go/internal/store"
)

type fakeHandle struct {
	done         chan execution.Result
	cancelled    atomic.Bool
	cancelResult bool
}

func (h *fakeHandle) Done() <-chan execution.Result { return h.done }
func (h *fakeHandle) Cancel() bool                  { h.cancelled.Store(true); return h.cancelResult }

type flakyQueue struct {
	store.ExecutionQueueStore
	calls atomic.Int32
}

type markExecutingStore struct {
	store.AssignmentStore
	err error
}

func (s *markExecutingStore) MarkExecuting(context.Context, store.AssignmentTransition) (models.TaskAssignment, error) {
	return models.TaskAssignment{}, s.err
}

func (q *flakyQueue) CommitExecutionResult(ctx context.Context, result store.AssignmentResult) (models.Task, models.TaskAssignment, error) {
	if q.calls.Add(1) == 1 {
		return models.Task{}, models.TaskAssignment{}, errors.New("temporary database error")
	}
	return q.ExecutionQueueStore.CommitExecutionResult(ctx, result)
}

type fakeExecutor struct {
	started  chan execution.Request
	handle   *fakeHandle
	startErr error
	start    func(execution.Request) (execution.Handle, error)
}

func (e *fakeExecutor) Start(_ context.Context, r execution.Request) (execution.Handle, error) {
	if e.start != nil {
		return e.start(r)
	}
	e.started <- r
	return e.handle, e.startErr
}

func TestWorkerExecutesAndCommitsResult(t *testing.T) {
	ctx := context.Background()
	db, err := store.Open(ctx, filepath.Join(t.TempDir(), "worker.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	now := time.Now().UTC()
	task := models.Task{TaskID: "task", SessionID: "session", InitiatorAgentID: "a", TargetAgentID: "b", TenantID: "tenant", Status: "accepted", CreatedAt: now, UpdatedAt: now}
	if err = db.TaskStore().Create(ctx, task); err != nil {
		t.Fatal(err)
	}
	_, _, err = db.ExecutionQueueStore().EnqueueAcceptedTask(ctx, store.EnqueueAcceptedTaskParams{Kind: models.AssignmentKindTargetExecution, AssignmentID: "assignment", DeliveryID: "delivery", TaskID: "task", TenantID: "tenant", TargetAgentID: "b"})
	if err != nil {
		t.Fatal(err)
	}
	h := &fakeHandle{done: make(chan execution.Result, 1), cancelResult: true}
	ex := &fakeExecutor{started: make(chan execution.Request, 1), handle: h}
	w, err := New(Config{Owner: "worker", Lease: time.Second, RenewInterval: 100 * time.Millisecond}, db.AssignmentStore(), db.ExecutionQueueStore(), db.TaskStore(), ex, func(_ context.Context, task models.Task) (execution.Request, error) {
		return execution.Request{TaskID: task.TaskID}, nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if err = w.RunOnce(ctx); err != nil {
		t.Fatal(err)
	}
	select {
	case req := <-ex.started:
		if req.TaskID != "task" || req.AssignmentID != "assignment" || req.DeliveryID != "delivery" || req.AssignmentAttempt != 1 || req.AssignmentFence != 1 {
			t.Fatalf("execution request=%+v", req)
		}
	case <-time.After(time.Second):
		t.Fatal("not started")
	}
	h.done <- execution.Result{Status: "completed", Outcome: []byte(`{"ok":true}`), ExecutionReceipt: []byte(`{"id":1}`)}
	wait, cancel := context.WithTimeout(ctx, time.Second)
	defer cancel()
	if err = w.Wait(wait); err != nil {
		t.Fatal(err)
	}
	got, _ := db.TaskStore().Get(ctx, "task")
	if got.Status != "completed" {
		t.Fatalf("task=%+v", got)
	}
	a, _ := db.AssignmentStore().Get(ctx, "assignment")
	if a.State != models.AssignmentStateSettled || string(a.ExecutionReceipt) != `{"id":1}` {
		t.Fatalf("assignment=%+v", a)
	}
}

func TestWorkerMaxConcurrentAcrossRunOnce(t *testing.T) {
	ctx := context.Background()
	db, err := store.Open(ctx, filepath.Join(t.TempDir(), "worker-limit.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	for _, id := range []string{"one", "two", "three"} {
		now := time.Now().UTC()
		if err = db.TaskStore().Create(ctx, models.Task{TaskID: id, SessionID: "s", InitiatorAgentID: "a", TargetAgentID: "b", TenantID: "tenant", Status: "accepted", CreatedAt: now, UpdatedAt: now}); err != nil {
			t.Fatal(err)
		}
		if _, _, err = db.ExecutionQueueStore().EnqueueAcceptedTask(ctx, store.EnqueueAcceptedTaskParams{Kind: models.AssignmentKindTargetExecution, AssignmentID: "assignment-" + id, DeliveryID: "delivery-" + id, TaskID: id, TenantID: "tenant", TargetAgentID: "b"}); err != nil {
			t.Fatal(err)
		}
	}
	handles := make(chan *fakeHandle, 3)
	ex := &fakeExecutor{start: func(r execution.Request) (execution.Handle, error) {
		h := &fakeHandle{done: make(chan execution.Result, 1), cancelResult: true}
		handles <- h
		return h, nil
	}}
	w, err := New(Config{Owner: "worker", Lease: time.Second, RenewInterval: 100 * time.Millisecond, ClaimLimit: 3, MaxConcurrent: 1}, db.AssignmentStore(), db.ExecutionQueueStore(), db.TaskStore(), ex, func(_ context.Context, task models.Task) (execution.Request, error) {
		return execution.Request{TaskID: task.TaskID}, nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if err = w.RunOnce(ctx); err != nil {
		t.Fatal(err)
	}
	first := <-handles
	for i := 0; i < 3; i++ {
		if err = w.RunOnce(ctx); err != nil {
			t.Fatal(err)
		}
	}
	select {
	case <-handles:
		t.Fatal("execution started over limit")
	case <-time.After(50 * time.Millisecond):
	}
	first.done <- execution.Result{Status: "completed"}
	wait, cancel := context.WithTimeout(ctx, time.Second)
	defer cancel()
	if err = w.Wait(wait); err != nil {
		t.Fatal(err)
	}
	if err = w.RunOnce(ctx); err != nil {
		t.Fatal(err)
	}
	select {
	case <-handles:
	case <-time.After(time.Second):
		t.Fatal("slot was not released")
	}
	w.Close()
	_ = w.Wait(wait)
}

func TestWorkerRetriesTemporarySettlementError(t *testing.T) {
	ctx := context.Background()
	db, _ := store.Open(ctx, filepath.Join(t.TempDir(), "worker.db"))
	defer db.Close()
	now := time.Now().UTC()
	_ = db.TaskStore().Create(ctx, models.Task{TaskID: "task", SessionID: "s", InitiatorAgentID: "a", TargetAgentID: "b", TenantID: "tenant", Status: "accepted", CreatedAt: now, UpdatedAt: now})
	_, _, _ = db.ExecutionQueueStore().EnqueueAcceptedTask(ctx, store.EnqueueAcceptedTaskParams{Kind: models.AssignmentKindTargetExecution, AssignmentID: "assignment", DeliveryID: "delivery", TaskID: "task", TenantID: "tenant", TargetAgentID: "b"})
	h := &fakeHandle{done: make(chan execution.Result, 1), cancelResult: true}
	ex := &fakeExecutor{started: make(chan execution.Request, 1), handle: h}
	queue := &flakyQueue{ExecutionQueueStore: db.ExecutionQueueStore()}
	w, _ := New(Config{Owner: "worker", Lease: time.Second, RenewInterval: 100 * time.Millisecond, SettlementBackoff: time.Millisecond}, db.AssignmentStore(), queue, db.TaskStore(), ex, func(context.Context, models.Task) (execution.Request, error) { return execution.Request{}, nil })
	_ = w.RunOnce(ctx)
	<-ex.started
	h.done <- execution.Result{Status: "completed"}
	wait, cancel := context.WithTimeout(ctx, time.Second)
	defer cancel()
	if err := w.Wait(wait); err != nil {
		t.Fatal(err)
	}
	assignment, _ := db.AssignmentStore().Get(ctx, "assignment")
	if assignment.State != models.AssignmentStateSettled || queue.calls.Load() < 2 {
		t.Fatalf("assignment=%+v commit calls=%d", assignment, queue.calls.Load())
	}
}

func TestWorkerMarkExecutingFailureUnconfirmedCancelSettlesUnknown(t *testing.T) {
	ctx := context.Background()
	db, _ := store.Open(ctx, filepath.Join(t.TempDir(), "worker.db"))
	defer db.Close()
	now := time.Now().UTC()
	_ = db.TaskStore().Create(ctx, models.Task{TaskID: "task", SessionID: "s", InitiatorAgentID: "a", TargetAgentID: "b", TenantID: "tenant", Status: "accepted", CreatedAt: now, UpdatedAt: now})
	_, _, _ = db.ExecutionQueueStore().EnqueueAcceptedTask(ctx, store.EnqueueAcceptedTaskParams{Kind: models.AssignmentKindTargetExecution, AssignmentID: "assignment", DeliveryID: "delivery", TaskID: "task", TenantID: "tenant", TargetAgentID: "b"})
	h := &fakeHandle{done: make(chan execution.Result, 1), cancelResult: false}
	ex := &fakeExecutor{started: make(chan execution.Request, 1), handle: h}
	assignments := &markExecutingStore{AssignmentStore: db.AssignmentStore(), err: errors.New("temporary database error")}
	w, _ := New(Config{Owner: "worker", Lease: time.Second, RenewInterval: 100 * time.Millisecond}, assignments, db.ExecutionQueueStore(), db.TaskStore(), ex, func(context.Context, models.Task) (execution.Request, error) { return execution.Request{}, nil })
	_ = w.RunOnce(ctx)
	<-ex.started
	wait, cancel := context.WithTimeout(ctx, time.Second)
	defer cancel()
	if err := w.Wait(wait); err != nil {
		t.Fatal(err)
	}
	task, _ := db.TaskStore().Get(ctx, "task")
	assignment, _ := db.AssignmentStore().Get(ctx, "assignment")
	if task.Status != "outcome_unknown" || assignment.FailureClass != models.FailureClassSentUnacknowledged || !h.cancelled.Load() {
		t.Fatalf("task=%+v assignment=%+v cancelled=%v", task, assignment, h.cancelled.Load())
	}
}

func TestWorkerAsyncConfirmedNotSentRetriesFromExecuting(t *testing.T) {
	ctx := context.Background()
	db, _ := store.Open(ctx, filepath.Join(t.TempDir(), "worker.db"))
	defer db.Close()
	now := time.Now().UTC()
	_ = db.TaskStore().Create(ctx, models.Task{TaskID: "task", SessionID: "s", InitiatorAgentID: "a", TargetAgentID: "b", TenantID: "tenant", Status: "accepted", CreatedAt: now, UpdatedAt: now})
	_, _, _ = db.ExecutionQueueStore().EnqueueAcceptedTask(ctx, store.EnqueueAcceptedTaskParams{Kind: models.AssignmentKindTargetExecution, AssignmentID: "assignment", DeliveryID: "delivery", TaskID: "task", TenantID: "tenant", TargetAgentID: "b"})
	h := &fakeHandle{done: make(chan execution.Result, 1), cancelResult: true}
	ex := &fakeExecutor{started: make(chan execution.Request, 1), handle: h}
	workerErrors := make(chan error, 1)
	w, _ := New(Config{Owner: "worker", Lease: time.Second, RenewInterval: 100 * time.Millisecond, OnError: func(err error) { workerErrors <- err }}, db.AssignmentStore(), db.ExecutionQueueStore(), db.TaskStore(), ex, func(context.Context, models.Task) (execution.Request, error) { return execution.Request{}, nil })
	_ = w.RunOnce(ctx)
	<-ex.started
	h.done <- execution.Result{Status: "failed", ErrorCode: "executor_unreachable", FailureClass: models.FailureClassPreDispatchTransient, DispatchDisposition: models.DispatchDispositionConfirmedNotSent}
	wait, cancel := context.WithTimeout(ctx, time.Second)
	defer cancel()
	if err := w.Wait(wait); err != nil {
		t.Fatal(err)
	}
	a, _ := db.AssignmentStore().Get(ctx, "assignment")
	if a.State != models.AssignmentStateRetryWait || a.FailureClass != models.FailureClassPreDispatchTransient {
		select {
		case err := <-workerErrors:
			t.Fatalf("assignment=%+v worker error=%v", a, err)
		default:
			t.Fatalf("assignment=%+v", a)
		}
	}
}

func TestWorkerSecurityFailureIsDeterministic(t *testing.T) {
	ctx := context.Background()
	db, _ := store.Open(ctx, filepath.Join(t.TempDir(), "worker.db"))
	defer db.Close()
	now := time.Now().UTC()
	_ = db.TaskStore().Create(ctx, models.Task{TaskID: "task", SessionID: "s", InitiatorAgentID: "a", TargetAgentID: "b", TenantID: "tenant", Status: "accepted", CreatedAt: now, UpdatedAt: now})
	_, _, _ = db.ExecutionQueueStore().EnqueueAcceptedTask(ctx, store.EnqueueAcceptedTaskParams{Kind: models.AssignmentKindTargetExecution, AssignmentID: "assignment", DeliveryID: "delivery", TaskID: "task", TenantID: "tenant", TargetAgentID: "b"})
	h := &fakeHandle{done: make(chan execution.Result, 1), cancelResult: true}
	ex := &fakeExecutor{started: make(chan execution.Request, 1), handle: h}
	w, _ := New(Config{Owner: "worker", Lease: time.Second, RenewInterval: 100 * time.Millisecond}, db.AssignmentStore(), db.ExecutionQueueStore(), db.TaskStore(), ex, func(context.Context, models.Task) (execution.Request, error) { return execution.Request{}, nil })
	_ = w.RunOnce(ctx)
	<-ex.started
	h.done <- execution.Result{Status: "failed", ErrorCode: execution.RequiredCapabilityUnavailableError, FailureClass: models.FailureClassSecurityViolation, DispatchDisposition: models.DispatchDispositionSentUnacknowledged}
	wait, cancel := context.WithTimeout(ctx, time.Second)
	defer cancel()
	if err := w.Wait(wait); err != nil {
		t.Fatal(err)
	}
	task, _ := db.TaskStore().Get(ctx, "task")
	a, _ := db.AssignmentStore().Get(ctx, "assignment")
	if task.Status != "failed" || a.State != models.AssignmentStateSettled || a.FailureClass != models.FailureClassSecurityViolation {
		t.Fatalf("task=%+v assignment=%+v", task, a)
	}
}

func TestWorkerStartErrorWithoutProofIsOutcomeUnknown(t *testing.T) {
	ctx := context.Background()
	db, _ := store.Open(ctx, filepath.Join(t.TempDir(), "worker.db"))
	defer db.Close()
	now := time.Now().UTC()
	_ = db.TaskStore().Create(ctx, models.Task{TaskID: "task", SessionID: "s", InitiatorAgentID: "a", TargetAgentID: "b", TenantID: "tenant", Status: "accepted", CreatedAt: now, UpdatedAt: now})
	_, _, _ = db.ExecutionQueueStore().EnqueueAcceptedTask(ctx, store.EnqueueAcceptedTaskParams{Kind: models.AssignmentKindTargetExecution, AssignmentID: "assignment", DeliveryID: "delivery", TaskID: "task", TenantID: "tenant", TargetAgentID: "b"})
	ex := &fakeExecutor{started: make(chan execution.Request, 1), startErr: errors.New("opaque executor failure")}
	w, _ := New(Config{Owner: "worker", Lease: time.Second, RenewInterval: 100 * time.Millisecond}, db.AssignmentStore(), db.ExecutionQueueStore(), db.TaskStore(), ex, func(context.Context, models.Task) (execution.Request, error) { return execution.Request{}, nil })
	_ = w.RunOnce(ctx)
	<-ex.started
	wait, cancel := context.WithTimeout(ctx, time.Second)
	defer cancel()
	if err := w.Wait(wait); err != nil {
		t.Fatal(err)
	}
	task, _ := db.TaskStore().Get(ctx, "task")
	a, _ := db.AssignmentStore().Get(ctx, "assignment")
	if task.Status != "outcome_unknown" || a.FailureClass != models.FailureClassSentUnacknowledged {
		t.Fatalf("task=%+v assignment=%+v", task, a)
	}
}

func TestWorkerCloseStopsRunningExecution(t *testing.T) {
	ctx := context.Background()
	db, _ := store.Open(ctx, filepath.Join(t.TempDir(), "worker.db"))
	defer db.Close()
	now := time.Now().UTC()
	_ = db.TaskStore().Create(ctx, models.Task{TaskID: "task", SessionID: "s", InitiatorAgentID: "a", TargetAgentID: "b", TenantID: "tenant", Status: "accepted", CreatedAt: now, UpdatedAt: now})
	_, _, _ = db.ExecutionQueueStore().EnqueueAcceptedTask(ctx, store.EnqueueAcceptedTaskParams{Kind: models.AssignmentKindTargetExecution, AssignmentID: "assignment", DeliveryID: "delivery", TaskID: "task", TenantID: "tenant", TargetAgentID: "b"})
	h := &fakeHandle{done: make(chan execution.Result), cancelResult: true}
	ex := &fakeExecutor{started: make(chan execution.Request, 1), handle: h}
	w, _ := New(Config{Owner: "worker", Lease: time.Second, RenewInterval: 100 * time.Millisecond}, db.AssignmentStore(), db.ExecutionQueueStore(), db.TaskStore(), ex, func(context.Context, models.Task) (execution.Request, error) { return execution.Request{}, nil })
	_ = w.RunOnce(ctx)
	<-ex.started
	w.Close()
	wait, cancel := context.WithTimeout(ctx, time.Second)
	defer cancel()
	if err := w.Wait(wait); err != nil {
		t.Fatal(err)
	}
	if !h.cancelled.Load() {
		t.Fatal("execution handle was not cancelled")
	}
}

func TestWorkerMayBeSentIsOutcomeUnknown(t *testing.T) {
	ctx := context.Background()
	db, _ := store.Open(ctx, filepath.Join(t.TempDir(), "worker.db"))
	defer db.Close()
	now := time.Now().UTC()
	_ = db.TaskStore().Create(ctx, models.Task{TaskID: "task", SessionID: "s", InitiatorAgentID: "a", TargetAgentID: "b", TenantID: "tenant", Status: "accepted", CreatedAt: now, UpdatedAt: now})
	_, _, _ = db.ExecutionQueueStore().EnqueueAcceptedTask(ctx, store.EnqueueAcceptedTaskParams{Kind: models.AssignmentKindTargetExecution, AssignmentID: "assignment", DeliveryID: "delivery", TaskID: "task", TenantID: "tenant", TargetAgentID: "b"})
	h := &fakeHandle{done: make(chan execution.Result, 1)}
	ex := &fakeExecutor{started: make(chan execution.Request, 1), handle: h}
	w, _ := New(Config{Owner: "worker", Lease: time.Second, RenewInterval: 100 * time.Millisecond}, db.AssignmentStore(), db.ExecutionQueueStore(), db.TaskStore(), ex, func(context.Context, models.Task) (execution.Request, error) { return execution.Request{}, nil })
	_ = w.RunOnce(ctx)
	<-ex.started
	h.done <- execution.Result{Status: "failed", MayBeSent: true, ErrorCode: "uncertain"}
	wait, cancel := context.WithTimeout(ctx, time.Second)
	defer cancel()
	_ = w.Wait(wait)
	got, _ := db.TaskStore().Get(ctx, "task")
	if got.Status != "outcome_unknown" {
		t.Fatalf("task=%+v", got)
	}
}
