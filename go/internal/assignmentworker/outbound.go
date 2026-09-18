package assignmentworker

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"time"

	"github.com/loop-controller/go/internal/delegation"
	"github.com/loop-controller/go/internal/models"
	"github.com/loop-controller/go/internal/observability"
	"github.com/loop-controller/go/internal/store"
)

type OutboundConfig struct {
	PollInterval      time.Duration
	Lease             time.Duration
	RenewInterval     time.Duration
	RetryBackoff      time.Duration
	JitterValue       func() float64
	SettlementBackoff time.Duration
	ClaimLimit        int
	MaxConcurrent     int
	MaxAttempts       int
	Owner             string
	OnError           func(error)
}

type OutboundWorker struct {
	config      OutboundConfig
	assignments store.AssignmentStore
	outbox      store.DelegationDispatchOutboxStore
	client      delegation.EntrypointClient
	scheduling  *store.SchedulingStore
	ctx         context.Context
	cancel      context.CancelFunc
	wg          sync.WaitGroup
	mu          sync.Mutex
	active      map[string]context.CancelFunc
	state       observability.State
	slots       chan struct{}
	claimMu     sync.Mutex
}

func NewOutbound(config OutboundConfig, assignments store.AssignmentStore, outbox store.DelegationDispatchOutboxStore, client delegation.EntrypointClient, scheduling ...*store.SchedulingStore) (*OutboundWorker, error) {
	if config.Owner == "" || assignments == nil || outbox == nil || client == nil {
		return nil, errors.New("outbound assignment worker dependencies and owner are required")
	}
	if config.PollInterval <= 0 {
		config.PollInterval = 250 * time.Millisecond
	}
	if config.Lease <= 0 {
		config.Lease = time.Minute
	}
	if config.RenewInterval <= 0 {
		config.RenewInterval = config.Lease / 3
	}
	if config.RenewInterval >= config.Lease/2 {
		return nil, errors.New("outbound assignment worker renew interval must be less than half the lease")
	}
	if config.RetryBackoff <= 0 {
		config.RetryBackoff = time.Second
	}
	if config.JitterValue == nil {
		config.JitterValue = func() float64 { return 0.5 }
	}
	if config.SettlementBackoff <= 0 {
		config.SettlementBackoff = 25 * time.Millisecond
	}
	if config.ClaimLimit <= 0 {
		config.ClaimLimit = 16
	}
	if config.MaxConcurrent <= 0 {
		config.MaxConcurrent = config.ClaimLimit
	}
	if config.MaxAttempts <= 0 {
		config.MaxAttempts = 5
	}
	ctx, cancel := context.WithCancel(context.Background())
	var scheduler *store.SchedulingStore
	if len(scheduling) > 0 {
		scheduler = scheduling[0]
	}
	return &OutboundWorker{config: config, assignments: assignments, outbox: outbox, client: client, scheduling: scheduler, ctx: ctx, cancel: cancel, active: make(map[string]context.CancelFunc), slots: make(chan struct{}, config.MaxConcurrent)}, nil
}

func (w *OutboundWorker) Run(ctx context.Context) error {
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

func (w *OutboundWorker) RunOnce(ctx context.Context) error {
	w.claimMu.Lock()
	defer w.claimMu.Unlock()
	now := time.Now().UTC()
	recovered, err := w.assignments.RecoverExpiredActive(ctx, models.AssignmentKindOutboundDelegation, now, w.config.ClaimLimit)
	w.state.Recovery(now, err)
	if err != nil {
		observability.Default.Add("assignment_recovery_errors_total", 1, string(models.AssignmentKindOutboundDelegation))
		w.state.Error(now)
		return err
	}
	observability.Default.Add("assignment_lease_expired_total", float64(recovered), string(models.AssignmentKindOutboundDelegation))
	observability.Default.Set("assignment_recovery_last_success_unixtime", float64(now.Unix()), string(models.AssignmentKindOutboundDelegation))
	dead, err := w.assignments.ExpireDueDeadLetters(ctx, models.AssignmentKindOutboundDelegation, now, w.config.ClaimLimit)
	if err != nil {
		w.state.Error(now)
		return err
	}
	observability.Default.Add("assignment_dead_letter_total", float64(dead), string(models.AssignmentKindOutboundDelegation), "expired")
	limit := min(w.config.ClaimLimit, cap(w.slots)-len(w.slots))
	if limit == 0 {
		w.state.Success(now)
		return nil
	}
	items, err := w.assignments.ClaimDue(ctx, store.ClaimDueParams{Kind: models.AssignmentKindOutboundDelegation, Now: now, Lease: w.config.Lease, Limit: limit, Owner: w.config.Owner})
	if err != nil {
		observability.Default.Add("assignment_claim_total", 1, string(models.AssignmentKindOutboundDelegation), "error")
		w.state.Error(now)
		return err
	}
	observability.Default.Add("assignment_claim_total", float64(len(items)), string(models.AssignmentKindOutboundDelegation), "success")
	w.state.Success(now)
	for _, a := range items {
		w.start(a)
	}
	return nil
}

func (w *OutboundWorker) start(a models.TaskAssignment) {
	w.mu.Lock()
	if _, exists := w.active[a.AssignmentID]; exists || w.ctx.Err() != nil {
		w.mu.Unlock()
		return
	}
	ctx, cancel := context.WithCancel(w.ctx)
	w.active[a.AssignmentID] = cancel
	w.slots <- struct{}{}
	w.wg.Add(1)
	w.mu.Unlock()
	go func() {
		defer w.wg.Done()
		defer func() { <-w.slots }()
		defer cancel()
		defer func() {
			w.mu.Lock()
			delete(w.active, a.AssignmentID)
			w.mu.Unlock()
		}()
		if err := w.dispatch(ctx, a); err != nil && !errors.Is(err, context.Canceled) {
			w.report(fmt.Errorf("dispatch outbound assignment %s: %w", a.AssignmentID, err))
		}
	}()
}

func (w *OutboundWorker) dispatch(ctx context.Context, a models.TaskAssignment) error {
	item, err := w.outbox.LoadOutboundDelegation(ctx, a.AssignmentID)
	if err != nil {
		return fmt.Errorf("load outbound payload: %w", err)
	}
	item.Request.AssignmentID, item.Request.AssignmentAttempt, item.Request.AssignmentFence = a.AssignmentID, a.Attempt, a.ExecutionFence
	a, err = w.assignments.MarkDispatched(ctx, outboundTransition(a, w.config.Owner))
	if err != nil {
		return err
	}
	done := make(chan error, 1)
	w.wg.Add(1)
	go func() {
		defer w.wg.Done()
		done <- w.client.Dispatch(ctx, item.Entrypoint, item.Request)
	}()
	ticker := time.NewTicker(w.config.RenewInterval)
	defer ticker.Stop()
	for {
		select {
		case dispatchErr := <-done:
			if dispatchErr == nil {
				return w.commit(ctx, a, store.AssignmentResult{Status: "delivered"})
			}
			var structured *delegation.DispatchError
			if !errors.As(dispatchErr, &structured) {
				structured = &delegation.DispatchError{Err: dispatchErr, FailureClass: models.FailureClassSentUnacknowledged, Disposition: models.DispatchDispositionUnknown, MayBeSent: true}
			}
			if structured.FailureClass == models.FailureClassPreDispatchTransient && structured.Disposition == models.DispatchDispositionConfirmedNotSent {
				if w.scheduling != nil {
					_, failoverErr := w.scheduling.FailoverAndReserve(ctx, store.FailoverAndReserveParams{AssignmentTransition: outboundTransition(a, w.config.Owner), TenantID: a.TenantID, Request: models.SchedulingRequest{RequestID: "failover-" + a.AssignmentID, TenantID: a.TenantID, TaskID: a.TaskID}, FailureClass: structured.FailureClass, DispatchDisposition: string(structured.Disposition), CapacityUnits: 1, Now: time.Now().UTC()})
					if failoverErr == nil {
						observability.Default.Add("failover_total", 1, string(models.AssignmentKindOutboundDelegation), "success")
						return nil
					}
					observability.Default.Add("failover_total", 1, string(models.AssignmentKindOutboundDelegation), "rejected")
					if !errors.Is(failoverErr, store.ErrSchedulingNoCandidate) && !errors.Is(failoverErr, store.ErrSchedulingCapacity) {
						return failoverErr
					}
				}
				return w.retry(ctx, a, structured)
			}
			status := "failed"
			if structured.MayBeSent || structured.FailureClass == models.FailureClassSentUnacknowledged {
				status = "outcome_unknown"
			}
			return w.commit(ctx, a, store.AssignmentResult{Status: status, FailureClass: structured.FailureClass, ErrorCode: "outbound_dispatch_failed"})
		case <-ticker.C:
			a, err = w.renew(ctx, a)
			if err != nil {
				return err
			}
		case <-ctx.Done():
			return ctx.Err()
		}
	}
}

func (w *OutboundWorker) commit(ctx context.Context, a models.TaskAssignment, result store.AssignmentResult) error {
	deadline := time.Now().UTC().Add(w.config.Lease)
	for {
		now := time.Now().UTC()
		if a.LeaseExpiresAt == nil || !a.LeaseExpiresAt.After(now) || !deadline.After(now) {
			return store.ErrAssignmentLeaseExpired
		}
		result.AssignmentTransition = outboundTransition(a, w.config.Owner)
		_, _, err := w.outbox.CommitOutboundDispatchResult(ctx, result)
		if err == nil {
			return nil
		}
		if settlementFinal(err) {
			return err
		}
		current, getErr := w.assignments.Get(ctx, a.AssignmentID)
		if getErr == nil {
			if current.State == models.AssignmentStateSettled {
				result.Revision = current.Revision
				result.Now = time.Now().UTC()
				_, _, replayErr := w.outbox.CommitOutboundDispatchResult(ctx, result)
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

func (w *OutboundWorker) retry(ctx context.Context, a models.TaskAssignment, dispatchErr *delegation.DispatchError) error {
	deadline := time.Now().UTC().Add(w.config.Lease)
	for {
		now := time.Now().UTC()
		if a.LeaseExpiresAt == nil || !a.LeaseExpiresAt.After(now) || !deadline.After(now) {
			return store.ErrAssignmentLeaseExpired
		}
		decision, err := w.assignments.ApplyRetryDecision(ctx, store.RetryDecision{AssignmentTransition: outboundTransition(a, w.config.Owner), FailureClass: dispatchErr.FailureClass, ErrorCode: "outbound_dispatch_transient", ConfirmedNotSent: dispatchErr.Disposition == models.DispatchDispositionConfirmedNotSent, RetryAfter: dispatchErr.RetryAfter, JitterValue: w.config.JitterValue()})
		if err == nil {
			if decision.State == models.AssignmentStateDeadLetter {
				observability.Default.Add("assignment_dead_letter_total", 1, string(models.AssignmentKindOutboundDelegation), "retry_exhausted")
			} else {
				observability.Default.Add("retry_scheduled_total", 1, string(models.AssignmentKindOutboundDelegation))
			}
			return nil
		}
		if settlementFinal(err) {
			return err
		}
		current, getErr := w.assignments.Get(ctx, a.AssignmentID)
		if getErr != nil {
			return fmt.Errorf("retry assignment after commit error: %w", err)
		}
		if current.State == models.AssignmentStateRetryWait && current.Attempt == a.Attempt && current.ExecutionFence == a.ExecutionFence {
			return nil
		}
		if current.Attempt != a.Attempt || current.ExecutionFence != a.ExecutionFence || current.LeaseOwner != w.config.Owner || current.ClaimToken != a.ClaimToken {
			return store.ErrAssignmentClaimLost
		}
		a, getErr = w.renew(ctx, current)
		if getErr != nil {
			return getErr
		}
		if err := waitContext(ctx, w.config.SettlementBackoff); err != nil {
			return err
		}
	}
}

func (w *OutboundWorker) renew(ctx context.Context, a models.TaskAssignment) (models.TaskAssignment, error) {
	return w.assignments.Renew(ctx, store.AssignmentClaim{Kind: models.AssignmentKindOutboundDelegation, AssignmentID: a.AssignmentID, Revision: a.Revision, Attempt: a.Attempt, Fence: a.ExecutionFence, Owner: w.config.Owner, ClaimToken: a.ClaimToken, Now: time.Now().UTC(), Lease: w.config.Lease})
}

func outboundTransition(a models.TaskAssignment, owner string) store.AssignmentTransition {
	return store.AssignmentTransition{Kind: models.AssignmentKindOutboundDelegation, AssignmentID: a.AssignmentID, Revision: a.Revision, Attempt: a.Attempt, Fence: a.ExecutionFence, Owner: owner, ClaimToken: a.ClaimToken, Now: time.Now().UTC()}
}

func (w *OutboundWorker) report(err error) {
	if w.config.OnError != nil {
		w.config.OnError(err)
	}
}

func (w *OutboundWorker) Close() {
	w.cancel()
	w.state.Stop()
	w.mu.Lock()
	for _, cancel := range w.active {
		cancel()
	}
	w.mu.Unlock()
}

func (w *OutboundWorker) Snapshot() observability.StateSnapshot { return w.state.Snapshot() }
func (w *OutboundWorker) Wait(ctx context.Context) error {
	done := make(chan struct{})
	go func() { w.wg.Wait(); close(done) }()
	select {
	case <-done:
		return nil
	case <-ctx.Done():
		return fmt.Errorf("wait outbound assignment worker: %w", ctx.Err())
	}
}
