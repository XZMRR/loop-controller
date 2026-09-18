package assignmentworker

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"time"

	"github.com/loop-controller/go/internal/execution"
	"github.com/loop-controller/go/internal/models"
	"github.com/loop-controller/go/internal/observability"
	"github.com/loop-controller/go/internal/store"
)

type Loader func(context.Context, models.Task) (execution.Request, error)

type Config struct {
	PollInterval      time.Duration
	Lease             time.Duration
	RenewInterval     time.Duration
	SettlementBackoff time.Duration
	SettlementGrace   time.Duration
	ClaimLimit        int
	MaxConcurrent     int
	Owner             string
	OnError           func(error)
}

type Worker struct {
	config      Config
	assignments store.AssignmentStore
	queue       store.ExecutionQueueStore
	tasks       store.TaskStore
	executor    execution.TargetExecutor
	loader      Loader
	scheduling  *store.SchedulingStore
	ctx         context.Context
	cancel      context.CancelFunc
	wg          sync.WaitGroup
	mu          sync.Mutex
	handles     map[string]execution.Handle
	state       observability.State
	slots       chan struct{}
	claimMu     sync.Mutex
}

func New(config Config, assignments store.AssignmentStore, queue store.ExecutionQueueStore, tasks store.TaskStore, executor execution.TargetExecutor, loader Loader, scheduling ...*store.SchedulingStore) (*Worker, error) {
	if config.PollInterval <= 0 {
		config.PollInterval = 250 * time.Millisecond
	}
	if config.Lease <= 0 {
		config.Lease = time.Minute
	}
	if config.RenewInterval <= 0 {
		config.RenewInterval = config.Lease / 3
	}
	if config.SettlementBackoff <= 0 {
		config.SettlementBackoff = 25 * time.Millisecond
	}
	if config.SettlementGrace <= 0 {
		config.SettlementGrace = 250 * time.Millisecond
	}
	if config.ClaimLimit <= 0 {
		config.ClaimLimit = 16
	}
	if config.MaxConcurrent <= 0 {
		config.MaxConcurrent = config.ClaimLimit
	}
	if config.Owner == "" {
		return nil, errors.New("assignment worker owner is required")
	}
	if config.RenewInterval >= config.Lease/2 {
		return nil, errors.New("assignment worker renew interval must be less than half the lease")
	}
	if assignments == nil || queue == nil || tasks == nil || executor == nil || loader == nil {
		return nil, errors.New("assignment worker dependencies are required")
	}
	ctx, cancel := context.WithCancel(context.Background())
	var scheduler *store.SchedulingStore
	if len(scheduling) > 0 {
		scheduler = scheduling[0]
	}
	return &Worker{config: config, assignments: assignments, queue: queue, tasks: tasks, executor: executor, loader: loader, scheduling: scheduler, ctx: ctx, cancel: cancel, handles: make(map[string]execution.Handle), slots: make(chan struct{}, config.MaxConcurrent)}, nil
}

func (w *Worker) Run(ctx context.Context) error {
	w.state.Start()
	defer w.state.Stop()
	ticker := time.NewTicker(w.config.PollInterval)
	defer ticker.Stop()
	for {
		if err := w.RunOnce(ctx); err != nil && ctx.Err() == nil {
			return err
		}
		select {
		case <-ctx.Done():
			w.Close()
			return ctx.Err()
		case <-w.ctx.Done():
			return nil
		case <-ticker.C:
		}
	}
}

func (w *Worker) RunOnce(ctx context.Context) error {
	w.claimMu.Lock()
	defer w.claimMu.Unlock()
	now := time.Now().UTC()
	recovered, err := w.assignments.RecoverExpiredActive(ctx, models.AssignmentKindTargetExecution, now, w.config.ClaimLimit)
	w.state.Recovery(now, err)
	if err != nil {
		observability.Default.Add("assignment_recovery_errors_total", 1, string(models.AssignmentKindTargetExecution))
		w.state.Error(now)
		return err
	}
	observability.Default.Add("assignment_lease_expired_total", float64(recovered), string(models.AssignmentKindTargetExecution))
	observability.Default.Set("assignment_recovery_last_success_unixtime", float64(now.Unix()), string(models.AssignmentKindTargetExecution))
	dead, err := w.assignments.ExpireDueDeadLetters(ctx, models.AssignmentKindTargetExecution, now, w.config.ClaimLimit)
	if err != nil {
		w.state.Error(now)
		return err
	}
	observability.Default.Add("assignment_dead_letter_total", float64(dead), string(models.AssignmentKindTargetExecution), "expired")
	limit := min(w.config.ClaimLimit, cap(w.slots)-len(w.slots))
	if limit == 0 {
		w.state.Success(now)
		return nil
	}
	items, err := w.assignments.ClaimDue(ctx, store.ClaimDueParams{Kind: models.AssignmentKindTargetExecution, Now: now, Lease: w.config.Lease, Limit: limit, Owner: w.config.Owner})
	if err != nil {
		observability.Default.Add("assignment_claim_total", 1, string(models.AssignmentKindTargetExecution), "error")
		w.state.Error(now)
		return err
	}
	observability.Default.Add("assignment_claim_total", float64(len(items)), string(models.AssignmentKindTargetExecution), "success")
	w.state.Success(now)
	for _, item := range items {
		item := item
		w.slots <- struct{}{}
		w.wg.Add(1)
		go func() {
			defer w.wg.Done()
			defer func() { <-w.slots }()
			w.execute(item)
		}()
	}
	return nil
}

func (w *Worker) execute(a models.TaskAssignment) {
	ctx, cancel := context.WithCancel(w.ctx)
	defer cancel()
	t, err := w.tasks.Get(ctx, a.TaskID)
	if err != nil {
		w.report(fmt.Errorf("load assignment task %s: %w", a.AssignmentID, err))
		return
	}
	req, err := w.loader(ctx, t)
	if err != nil {
		w.report(fmt.Errorf("load execution request for assignment %s: %w", a.AssignmentID, err))
		w.finish(a, execution.Result{Status: "failed", ErrorCode: "execution_request_invalid"}, models.FailureClassPreDispatchPermanent)
		return
	}
	req.AssignmentID = a.AssignmentID
	req.DeliveryID = a.DeliveryID
	req.AssignmentAttempt = a.Attempt
	req.AssignmentFence = a.ExecutionFence
	a, err = w.assignments.MarkDispatched(ctx, transition(a, w.config.Owner))
	if err != nil {
		w.report(fmt.Errorf("mark assignment %s dispatched: %w", a.AssignmentID, err))
		return
	}
	handle, err := w.executor.Start(ctx, req)
	if err != nil {
		var dispatchErr *execution.DispatchError
		if errors.As(err, &dispatchErr) && dispatchErr.FailureClass == models.FailureClassPreDispatchTransient && dispatchErr.Disposition == models.DispatchDispositionConfirmedNotSent {
			if w.failover(ctx, a, dispatchErr.FailureClass, dispatchErr.Disposition) {
				return
			}
			w.retry(a, dispatchErr.FailureClass, dispatchErr.Disposition, "execution_start_failed")
			return
		}
		failure, disposition := models.FailureClassSentUnacknowledged, models.DispatchDispositionUnknown
		if errors.As(err, &dispatchErr) {
			failure, disposition = dispatchErr.FailureClass, dispatchErr.Disposition
		}
		mayBeSent := disposition != models.DispatchDispositionConfirmedNotSent
		w.finish(a, execution.Result{Status: "failed", ErrorCode: "execution_start_failed", FailureClass: failure, DispatchDisposition: disposition, MayBeSent: mayBeSent}, failure)
		return
	}
	w.mu.Lock()
	w.handles[a.TaskID] = handle
	w.mu.Unlock()
	defer func() {
		w.mu.Lock()
		if w.handles[a.TaskID] == handle {
			delete(w.handles, a.TaskID)
		}
		w.mu.Unlock()
	}()
	executing, err := w.assignments.MarkExecuting(ctx, transition(a, w.config.Owner))
	if err != nil {
		cancelledBeforeSend := handle.Cancel()
		if errors.Is(err, store.ErrAssignmentClaimLost) || errors.Is(err, store.ErrAssignmentTerminal) {
			return
		}
		result := execution.Result{Status: "failed", ErrorCode: "mark_executing_failed"}
		failure := models.FailureClassPreDispatchTransient
		if errors.Is(err, store.ErrAssignmentInvalidTransition) || errors.Is(err, store.ErrAssignmentConflict) {
			failure = models.FailureClassPreDispatchPermanent
		}
		if !cancelledBeforeSend {
			result.MayBeSent = true
			failure = models.FailureClassSentUnacknowledged
		}
		w.finish(a, result, failure)
		return
	}
	a = executing
	ticker := time.NewTicker(w.config.RenewInterval)
	defer ticker.Stop()
	for {
		select {
		case result, ok := <-handle.Done():
			if !ok {
				result = execution.Result{Status: "failed", ErrorCode: "executor_no_result", FailureClass: models.FailureClassSentUnacknowledged, DispatchDisposition: models.DispatchDispositionUnknown, MayBeSent: true}
			}
			failure := result.FailureClass
			if failure == "" {
				if result.ErrorCode == "execution_receipt_invalid" {
					failure = models.FailureClassReceiptInvalid
				} else if result.ErrorCode == execution.RequiredCapabilityUnavailableError || result.ErrorCode == "executor_peer_identity_invalid" {
					failure = models.FailureClassSecurityViolation
				} else if result.MayBeSent {
					failure = models.FailureClassSentUnacknowledged
				} else if result.Status == "failed" {
					failure = models.FailureClassRemoteRejected
				}
			}
			if failure != models.FailureClassReceiptInvalid && failure != models.FailureClassSecurityViolation && (result.DispatchDisposition == models.DispatchDispositionSentUnacknowledged || result.DispatchDisposition == models.DispatchDispositionUnknown && result.Status == "failed") {
				result.MayBeSent = true
			}
			if failure == models.FailureClassPreDispatchTransient && result.DispatchDisposition == models.DispatchDispositionConfirmedNotSent {
				if w.failover(ctx, a, failure, result.DispatchDisposition) {
					return
				}
				w.retry(a, failure, result.DispatchDisposition, result.ErrorCode)
				return
			}
			w.finish(a, result, failure)
			return
		case <-ticker.C:
			a, err = w.renew(ctx, a)
			if err != nil {
				handle.Cancel()
				w.report(fmt.Errorf("renew assignment %s: %w", a.AssignmentID, err))
				return
			}
		case <-ctx.Done():
			handle.Cancel()
			return
		}
	}
}

func (w *Worker) failover(ctx context.Context, a models.TaskAssignment, failure models.FailureClass, disposition models.DispatchDisposition) bool {
	if w.scheduling == nil {
		return false
	}
	_, err := w.scheduling.FailoverAndReserve(ctx, store.FailoverAndReserveParams{
		AssignmentTransition: transition(a, w.config.Owner),
		TenantID:             a.TenantID,
		Request:              models.SchedulingRequest{RequestID: "failover-" + a.AssignmentID, TenantID: a.TenantID, TaskID: a.TaskID},
		FailureClass:         failure,
		DispatchDisposition:  string(disposition),
		CapacityUnits:        1,
		Now:                  time.Now().UTC(),
	})
	if err == nil {
		observability.Default.Add("failover_total", 1, string(models.AssignmentKindTargetExecution), "success")
		return true
	}
	observability.Default.Add("failover_total", 1, string(models.AssignmentKindTargetExecution), "rejected")
	if !errors.Is(err, store.ErrSchedulingNoCandidate) && !errors.Is(err, store.ErrSchedulingCapacity) {
		w.report(fmt.Errorf("fail over assignment %s: %w", a.AssignmentID, err))
	}
	return false
}

func (w *Worker) retry(a models.TaskAssignment, failure models.FailureClass, disposition models.DispatchDisposition, errorCode string) {
	decision, err := w.assignments.ApplyRetryDecision(w.ctx, store.RetryDecision{
		AssignmentTransition: transition(a, w.config.Owner),
		FailureClass:         failure,
		ErrorCode:            errorCode,
		ConfirmedNotSent:     disposition == models.DispatchDispositionConfirmedNotSent,
		JitterValue:          0.5,
	})
	if err != nil {
		w.report(fmt.Errorf("retry assignment %s: %w", a.AssignmentID, err))
		return
	}
	if decision.State == models.AssignmentStateDeadLetter {
		observability.Default.Add("assignment_dead_letter_total", 1, string(models.AssignmentKindTargetExecution), "retry_exhausted")
	} else {
		observability.Default.Add("retry_scheduled_total", 1, string(models.AssignmentKindTargetExecution))
	}
}

func (w *Worker) finish(a models.TaskAssignment, result execution.Result, failure models.FailureClass) {
	ctx := w.ctx
	cancel := func() {}
	if ctx.Err() != nil {
		ctx, cancel = context.WithTimeout(context.Background(), w.config.SettlementGrace)
	}
	defer cancel()
	if err := w.settle(ctx, a, result, failure); err != nil {
		w.report(fmt.Errorf("settle assignment %s: %w", a.AssignmentID, err))
	}
}

func (w *Worker) settle(ctx context.Context, a models.TaskAssignment, result execution.Result, failure models.FailureClass) error {
	status := result.Status
	if result.MayBeSent {
		status = "outcome_unknown"
	}
	if status != "completed" && status != "failed" && status != "outcome_unknown" {
		status = "failed"
	}
	p := store.AssignmentResult{Status: status, FailureClass: failure, Outcome: result.Outcome, ErrorCode: result.ErrorCode, ExecutionReceipt: result.ExecutionReceipt}
	settlementDeadline := time.Now().UTC().Add(w.config.Lease)
	for {
		now := time.Now().UTC()
		if !settlementDeadline.After(now) || a.LeaseExpiresAt == nil || !a.LeaseExpiresAt.After(now) {
			return store.ErrAssignmentLeaseExpired
		}
		p.AssignmentTransition = transition(a, w.config.Owner)
		_, settled, err := w.queue.CommitExecutionResult(ctx, p)
		if err == nil {
			_ = settled
			if status == "outcome_unknown" {
				observability.Default.Add("assignment_outcome_unknown_total", 1, string(models.AssignmentKindTargetExecution))
			}
			return nil
		}
		if settlementFinal(err) {
			if errors.Is(err, store.ErrAssignmentClaimLost) || errors.Is(err, store.ErrAssignmentLeaseExpired) {
				observability.Default.Add("assignment_fence_rejected_total", 1, string(models.AssignmentKindTargetExecution), "result")
			}
			return err
		}

		current, getErr := w.assignments.Get(ctx, a.AssignmentID)
		if getErr == nil {
			if current.State == models.AssignmentStateSettled {
				p.Revision = current.Revision
				p.Now = time.Now().UTC()
				_, _, replayErr := w.queue.CommitExecutionResult(ctx, p)
				return replayErr
			}
			if current.Attempt != a.Attempt || current.ExecutionFence != a.ExecutionFence || current.LeaseOwner != w.config.Owner || current.ClaimToken != a.ClaimToken {
				return store.ErrAssignmentClaimLost
			}
			a = current
			a, getErr = w.renew(ctx, a)
		}
		if getErr != nil && settlementFinal(getErr) {
			return getErr
		}
		if err := waitContext(ctx, w.config.SettlementBackoff); err != nil {
			return err
		}
	}
}

func (w *Worker) renew(ctx context.Context, a models.TaskAssignment) (models.TaskAssignment, error) {
	return w.assignments.Renew(ctx, store.AssignmentClaim{Kind: models.AssignmentKindTargetExecution, AssignmentID: a.AssignmentID, Revision: a.Revision, Attempt: a.Attempt, Fence: a.ExecutionFence, Owner: w.config.Owner, ClaimToken: a.ClaimToken, Now: time.Now().UTC(), Lease: w.config.Lease})
}

func settlementFinal(err error) bool {
	return errors.Is(err, store.ErrAssignmentNotFound) || errors.Is(err, store.ErrAssignmentClaimLost) || errors.Is(err, store.ErrAssignmentLeaseExpired) || errors.Is(err, store.ErrAssignmentTerminal) || errors.Is(err, store.ErrAssignmentInvalidTransition) || errors.Is(err, store.ErrAssignmentResultConflict)
}

func waitContext(ctx context.Context, d time.Duration) error {
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-timer.C:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

func (w *Worker) report(err error) {
	if w.config.OnError != nil {
		w.config.OnError(err)
	}
}

func transition(a models.TaskAssignment, owner string) store.AssignmentTransition {
	return store.AssignmentTransition{Kind: models.AssignmentKindTargetExecution, AssignmentID: a.AssignmentID, Revision: a.Revision, Attempt: a.Attempt, Fence: a.ExecutionFence, Owner: owner, ClaimToken: a.ClaimToken, Now: time.Now().UTC()}
}

func (w *Worker) Cancel(taskID string) bool {
	w.mu.Lock()
	handle := w.handles[taskID]
	w.mu.Unlock()
	if handle == nil {
		return false
	}
	return handle.Cancel()
}

func (w *Worker) Close()                                { w.cancel(); w.state.Stop() }
func (w *Worker) Snapshot() observability.StateSnapshot { return w.state.Snapshot() }
func (w *Worker) Wait(ctx context.Context) error {
	done := make(chan struct{})
	go func() { w.wg.Wait(); close(done) }()
	select {
	case <-done:
		return nil
	case <-ctx.Done():
		return fmt.Errorf("wait assignment worker: %w", ctx.Err())
	}
}
