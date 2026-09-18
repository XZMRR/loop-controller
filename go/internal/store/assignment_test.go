package store

import (
	"context"
	"errors"
	"math"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/loop-controller/go/internal/models"
)

func assignmentFixture(t *testing.T) (*DB, models.TaskAssignment, time.Time) {
	t.Helper()
	ctx := context.Background()
	db, err := Open(ctx, filepath.Join(t.TempDir(), "assignment.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	now := time.Date(2026, 9, 12, 10, 0, 0, 123, time.UTC)
	task := models.Task{TaskID: "task-a", SessionID: "session", InitiatorAgentID: "a", TargetAgentID: "b", TenantID: "tenant-a", Status: "pending", CreatedAt: now, UpdatedAt: now}
	if err := db.TaskStore().Create(ctx, task); err != nil {
		t.Fatal(err)
	}
	a := models.TaskAssignment{Kind: models.AssignmentKindTargetExecution, AssignmentID: "assignment-a", TenantID: "tenant-a", TaskID: task.TaskID, AgentID: "agent-a", DeliveryID: "delivery-a", IdempotencyKey: "key-a", CreatedAt: now}
	return db, a, now
}

func TestAssignmentCreateGetReopenAndIdempotency(t *testing.T) {
	ctx := context.Background()
	db, a, now := assignmentFixture(t)
	path := db.Path()
	store := db.AssignmentStore()
	created, err := store.Create(ctx, a)
	if err != nil {
		t.Fatal(err)
	}
	if created.State != models.AssignmentStateQueued || created.Revision != 1 || created.Attempt != 0 || created.ExecutionFence != 0 || created.LeaseOwner != "" || created.ClaimToken != "" {
		t.Fatalf("unexpected create: %+v", created)
	}
	replayed, err := store.Create(ctx, a)
	if err != nil || replayed.AssignmentID != created.AssignmentID {
		t.Fatalf("idempotent create: %+v %v", replayed, err)
	}
	differentKey := a
	differentKey.IdempotencyKey = "key-b"
	if _, err = store.Create(ctx, differentKey); !errors.Is(err, ErrAssignmentExists) {
		t.Fatalf("same assignment with different idempotency key: %v", err)
	}
	conflict := a
	conflict.AssignmentID = "other"
	conflict.DeliveryID = "other-delivery"
	conflict.AgentID = "other-agent"
	if _, err = store.Create(ctx, conflict); !errors.Is(err, ErrAssignmentConflict) {
		t.Fatalf("got %v", err)
	}
	if err = db.Close(); err != nil {
		t.Fatal(err)
	}
	db, err = Open(ctx, path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	got, err := db.AssignmentStore().Get(ctx, a.AssignmentID)
	if err != nil || !got.CreatedAt.Equal(now) {
		t.Fatalf("reopen get: %+v %v", got, err)
	}
}

func TestAssignmentRetryPolicyPersistsAndBackoffIsDeterministic(t *testing.T) {
	ctx := context.Background()
	db, a, now := assignmentFixture(t)
	deadline := now.Add(time.Hour)
	a.Deadline = &deadline
	a.RetryPolicy = &models.RetryPolicy{
		MaxAttempts:             4,
		InitialBackoff:          2 * time.Second,
		MaxBackoff:              10 * time.Second,
		BackoffMultiplier:       2,
		Jitter:                  0.25,
		RetryableFailureClasses: []models.FailureClass{models.FailureClassPreDispatchTransient},
		Deadline:                &deadline,
	}
	created, err := db.AssignmentStore().Create(ctx, a)
	if err != nil {
		t.Fatal(err)
	}
	if created.RetryPolicy == nil || created.RetryPolicy.MaxAttempts != 4 || !created.RetryPolicy.Deadline.Equal(deadline) {
		t.Fatalf("policy=%+v", created.RetryPolicy)
	}
	got, err := db.AssignmentStore().Get(ctx, a.AssignmentID)
	if err != nil || got.RetryPolicy == nil || got.RetryPolicy.InitialBackoff != 2*time.Second {
		t.Fatalf("persisted policy=%+v err=%v", got.RetryPolicy, err)
	}
	if delay := RetryBackoff(*got.RetryPolicy, 3, 0); delay != 6*time.Second {
		t.Fatalf("low jitter delay=%s", delay)
	}
	if delay := RetryBackoff(*got.RetryPolicy, 3, 1); delay != 10*time.Second {
		t.Fatalf("high jitter/cap delay=%s", delay)
	}
	capped := *got.RetryPolicy
	capped.BackoffMultiplier = math.MaxFloat64
	if delay := RetryBackoff(capped, 2, 0.5); delay != capped.MaxBackoff {
		t.Fatalf("capped delay=%s", delay)
	}
}

func TestAssignmentCreateValidatesTaskTenantAndUniqueKeys(t *testing.T) {
	ctx := context.Background()
	db, a, _ := assignmentFixture(t)
	s := db.AssignmentStore()
	wrong := a
	wrong.AssignmentID = "wrong"
	wrong.DeliveryID = "wrong"
	wrong.IdempotencyKey = ""
	wrong.TenantID = "tenant-b"
	if _, err := s.Create(ctx, wrong); !errors.Is(err, ErrAssignmentConflict) {
		t.Fatalf("tenant error=%v", err)
	}
	missing := a
	missing.AssignmentID = "missing"
	missing.TaskID = "missing"
	missing.DeliveryID = "missing"
	missing.IdempotencyKey = ""
	if _, err := s.Create(ctx, missing); !errors.Is(err, ErrAssignmentNotFound) {
		t.Fatalf("missing error=%v", err)
	}
	if _, err := s.Create(ctx, a); err != nil {
		t.Fatal(err)
	}
	duplicate := a
	duplicate.AssignmentID = "duplicate"
	duplicate.IdempotencyKey = ""
	duplicate.DeliveryID = a.DeliveryID
	if _, err := s.Create(ctx, duplicate); err == nil {
		t.Fatal("expected delivery conflict")
	}
}

func TestAssignmentClaimTakeoverRenewTransitionsAndResult(t *testing.T) {
	ctx := context.Background()
	db, a, now := assignmentFixture(t)
	s := db.AssignmentStore()
	if _, err := s.Create(ctx, a); err != nil {
		t.Fatal(err)
	}
	claims, err := s.ClaimDue(ctx, ClaimDueParams{Kind: models.AssignmentKindTargetExecution, Now: now, Lease: time.Minute, Limit: 1, Owner: "owner-a"})
	if err != nil || len(claims) != 1 {
		t.Fatalf("claim=%v err=%v", claims, err)
	}
	first := claims[0]
	if first.Attempt != 1 || first.ExecutionFence != 1 || first.Revision != 2 || len(first.ClaimToken) != 32 {
		t.Fatalf("first=%+v", first)
	}
	renewed, err := s.Renew(ctx, AssignmentClaim{Kind: models.AssignmentKindTargetExecution, AssignmentID: a.AssignmentID, Revision: first.Revision, Attempt: 1, Fence: 1, Owner: "owner-a", ClaimToken: first.ClaimToken, Now: now.Add(time.Second), Lease: 2 * time.Minute})
	if err != nil {
		t.Fatal(err)
	}
	bad := AssignmentClaim{Kind: models.AssignmentKindTargetExecution, AssignmentID: a.AssignmentID, Revision: renewed.Revision, Attempt: 1, Fence: 1, Owner: "wrong", ClaimToken: first.ClaimToken, Now: now.Add(2 * time.Second), Lease: time.Minute}
	if _, err = s.Renew(ctx, bad); !errors.Is(err, ErrAssignmentClaimLost) {
		t.Fatalf("bad owner=%v", err)
	}
	dispatched, err := s.MarkDispatched(ctx, AssignmentTransition{Kind: models.AssignmentKindTargetExecution, AssignmentID: a.AssignmentID, Revision: renewed.Revision, Attempt: 1, Fence: 1, Owner: "owner-a", ClaimToken: first.ClaimToken, Now: now.Add(3 * time.Second)})
	if err != nil {
		t.Fatal(err)
	}
	dispatched, err = s.Renew(ctx, AssignmentClaim{Kind: models.AssignmentKindTargetExecution, AssignmentID: a.AssignmentID, Revision: dispatched.Revision, Attempt: 1, Fence: 1, Owner: "owner-a", ClaimToken: first.ClaimToken, Now: now.Add(4 * time.Second), Lease: 3 * time.Minute})
	if err != nil || dispatched.State != models.AssignmentStateDispatched {
		t.Fatalf("renew dispatched=%+v %v", dispatched, err)
	}
	if claims, err = s.ClaimDue(ctx, ClaimDueParams{Kind: models.AssignmentKindTargetExecution, Now: now.Add(10 * time.Minute), Lease: time.Minute, Limit: 1, Owner: "owner-b"}); err != nil || len(claims) != 0 {
		t.Fatalf("dispatched claimed: %v %v", claims, err)
	}
	executing, err := s.MarkExecuting(ctx, AssignmentTransition{Kind: models.AssignmentKindTargetExecution, AssignmentID: a.AssignmentID, Revision: dispatched.Revision, Attempt: 1, Fence: 1, Owner: "owner-a", ClaimToken: first.ClaimToken, Now: now.Add(5 * time.Second)})
	if err != nil {
		t.Fatal(err)
	}
	executing, err = s.Renew(ctx, AssignmentClaim{Kind: models.AssignmentKindTargetExecution, AssignmentID: a.AssignmentID, Revision: executing.Revision, Attempt: 1, Fence: 1, Owner: "owner-a", ClaimToken: first.ClaimToken, Now: now.Add(6 * time.Second), Lease: 4 * time.Minute})
	if err != nil || executing.State != models.AssignmentStateExecuting {
		t.Fatalf("renew executing=%+v %v", executing, err)
	}
	result := AssignmentResult{AssignmentTransition: AssignmentTransition{Kind: models.AssignmentKindTargetExecution, AssignmentID: a.AssignmentID, Revision: executing.Revision, Attempt: 1, Fence: 1, Owner: "owner-a", ClaimToken: first.ClaimToken, Now: now.Add(7 * time.Second)}, Status: "completed", FailureClass: models.FailureClassRemoteTimeout, Outcome: []byte(`{"ok":true}`), ConsumedBudget: models.DelegationBudget{TokenCount: 2, Currency: "TOK"}, ExecutionReceipt: []byte(`{"receipt":1}`)}
	settled, err := s.CommitResult(ctx, result)
	if err != nil || settled.State != models.AssignmentStateSettled || settled.LeaseOwner != "" || settled.FailureClass != models.FailureClassRemoteTimeout {
		t.Fatalf("settle=%+v %v", settled, err)
	}
	result.Revision = settled.Revision
	if _, err = s.CommitResult(ctx, result); err != nil {
		t.Fatalf("same result replay=%v", err)
	}
	result.Status = "failed"
	if _, err = s.CommitResult(ctx, result); !errors.Is(err, ErrAssignmentResultConflict) {
		t.Fatalf("different result=%v", err)
	}
	attempts, err := s.ListAttempts(ctx, a.AssignmentID)
	if err != nil || len(attempts) != 1 || attempts[0].State != models.AssignmentStateSettled || attempts[0].FailureClass != models.FailureClassRemoteTimeout {
		t.Fatalf("attempts=%+v %v", attempts, err)
	}
}

func TestAssignmentRetryDecisionSafetyAndDeadLetterReplay(t *testing.T) {
	ctx := context.Background()
	db, a, now := assignmentFixture(t)
	a.RetryPolicy = &models.RetryPolicy{MaxAttempts: 2, InitialBackoff: time.Second, MaxBackoff: time.Minute, BackoffMultiplier: 2, RetryableFailureClasses: []models.FailureClass{models.FailureClassPreDispatchTransient}}
	s := db.AssignmentStore()
	if _, err := s.Create(ctx, a); err != nil {
		t.Fatal(err)
	}
	claims, err := s.ClaimDue(ctx, ClaimDueParams{Kind: a.Kind, Now: now, Lease: time.Minute, Limit: 1, Owner: "worker"})
	if err != nil || len(claims) != 1 {
		t.Fatalf("claim=%+v err=%v", claims, err)
	}
	claim := claims[0]
	base := RetryDecision{AssignmentTransition: AssignmentTransition{Kind: a.Kind, AssignmentID: a.AssignmentID, Revision: claim.Revision, Attempt: claim.Attempt, Fence: claim.ExecutionFence, Owner: "worker", ClaimToken: claim.ClaimToken, Now: now.Add(time.Second)}, FailureClass: models.FailureClassPreDispatchTransient, ConfirmedNotSent: true, JitterValue: 0.5}
	for _, failure := range []models.FailureClass{models.FailureClassSentUnacknowledged, models.FailureClassReceiptInvalid, models.FailureClassSecurityViolation} {
		blocked := base
		blocked.FailureClass = failure
		if _, err = s.ApplyRetryDecision(ctx, blocked); !errors.Is(err, ErrAssignmentRetryForbidden) {
			t.Fatalf("failure %s error=%v", failure, err)
		}
	}
	unconfirmed := base
	unconfirmed.ConfirmedNotSent = false
	if _, err = s.ApplyRetryDecision(ctx, unconfirmed); !errors.Is(err, ErrAssignmentRetryForbidden) {
		t.Fatalf("unconfirmed error=%v", err)
	}
	retry, err := s.ApplyRetryDecision(ctx, base)
	if err != nil || retry.State != models.AssignmentStateRetryWait || retry.NotBefore == nil || !retry.NotBefore.Equal(now.Add(2*time.Second)) {
		t.Fatalf("retry=%+v err=%v", retry, err)
	}
	claims, err = s.ClaimDue(ctx, ClaimDueParams{Kind: a.Kind, Now: now.Add(2 * time.Second), Lease: time.Minute, Limit: 1, Owner: "worker"})
	if err != nil || len(claims) != 1 {
		t.Fatalf("second claim=%+v err=%v", claims, err)
	}
	claim = claims[0]
	dead, err := s.ApplyRetryDecision(ctx, RetryDecision{AssignmentTransition: AssignmentTransition{Kind: a.Kind, AssignmentID: a.AssignmentID, Revision: claim.Revision, Attempt: claim.Attempt, Fence: claim.ExecutionFence, Owner: "worker", ClaimToken: claim.ClaimToken, Now: now.Add(3 * time.Second)}, FailureClass: models.FailureClassPreDispatchTransient, ConfirmedNotSent: true, JitterValue: 0.5})
	if err != nil || dead.State != models.AssignmentStateDeadLetter {
		t.Fatalf("dead=%+v err=%v", dead, err)
	}
	if _, err = s.ReplayDeadLetter(ctx, DeadLetterReplayParams{TenantID: a.TenantID, AssignmentID: a.AssignmentID, ExpectedRevision: dead.Revision, Now: now.Add(4 * time.Second)}); !errors.Is(err, ErrAssignmentDeadLetterReplay) {
		t.Fatalf("attempt cap replay error=%v", err)
	}
	listed, err := s.ListDeadLetters(ctx, a.TenantID, 10)
	if err != nil || len(listed) != 1 || listed[0].AssignmentID != a.AssignmentID {
		t.Fatalf("listed=%+v err=%v", listed, err)
	}
	other, err := s.ListDeadLetters(ctx, "other-tenant", 10)
	if err != nil || len(other) != 0 {
		t.Fatalf("cross tenant=%+v err=%v", other, err)
	}
}

func TestAssignmentDeadlineExpiredCannotClaim(t *testing.T) {
	ctx := context.Background()
	db, a, now := assignmentFixture(t)
	deadline := now
	a.Deadline = &deadline
	s := db.AssignmentStore()
	if _, err := s.Create(ctx, a); err != nil {
		t.Fatal(err)
	}
	claims, err := s.ClaimDue(ctx, ClaimDueParams{Kind: models.AssignmentKindTargetExecution, Now: now, Lease: time.Minute, Limit: 1, Owner: "owner"})
	if err != nil || len(claims) != 0 {
		t.Fatalf("expired deadline claimed: %v %v", claims, err)
	}
	if count, expireErr := s.ExpireDueDeadLetters(ctx, a.Kind, now, 10); expireErr != nil || count != 1 {
		t.Fatalf("expire count=%d err=%v", count, expireErr)
	}
	dead, err := s.Get(ctx, a.AssignmentID)
	if err != nil || dead.State != models.AssignmentStateDeadLetter || dead.FailureClass != models.FailureClassDeadlineExceeded {
		t.Fatalf("dead=%+v err=%v", dead, err)
	}
	task, err := db.TaskStore().Get(ctx, a.TaskID)
	if err != nil || task.Status != "failed" || task.ErrorCode != "dead_lettered" {
		t.Fatalf("task=%+v err=%v", task, err)
	}
}

func TestAssignmentDeadlineRecoveryConcurrentSettlesOnce(t *testing.T) {
	ctx := context.Background()
	db, a, now := assignmentFixture(t)
	deadline := now
	a.Deadline = &deadline
	if _, err := db.AssignmentStore().Create(ctx, a); err != nil {
		t.Fatal(err)
	}
	db2, err := Open(ctx, db.Path())
	if err != nil {
		t.Fatal(err)
	}
	defer db2.Close()
	start := make(chan struct{})
	counts := make(chan int64, 2)
	errs := make(chan error, 2)
	var wg sync.WaitGroup
	for _, s := range []AssignmentStore{db.AssignmentStore(), db2.AssignmentStore()} {
		wg.Add(1)
		go func(s AssignmentStore) {
			defer wg.Done()
			<-start
			count, err := s.ExpireDueDeadLetters(ctx, a.Kind, now, 10)
			counts <- count
			errs <- err
		}(s)
	}
	close(start)
	wg.Wait()
	close(counts)
	close(errs)
	var total int64
	for count := range counts {
		total += count
	}
	for err := range errs {
		if err != nil {
			t.Fatal(err)
		}
	}
	if total != 1 {
		t.Fatalf("recovered=%d", total)
	}
	var events int
	if err := db.QueryRow(`SELECT COUNT(*) FROM assignment_retry_events WHERE assignment_id=? AND event_type='dead_lettered'`, a.AssignmentID).Scan(&events); err != nil || events != 1 {
		t.Fatalf("events=%d err=%v", events, err)
	}
}

func TestAssignmentExpiredClaimTakeoverAndCancelFencesOldClaim(t *testing.T) {
	ctx := context.Background()
	db, a, now := assignmentFixture(t)
	s := db.AssignmentStore()
	if _, err := s.Create(ctx, a); err != nil {
		t.Fatal(err)
	}
	one, _ := s.ClaimDue(ctx, ClaimDueParams{Kind: models.AssignmentKindTargetExecution, Now: now, Lease: time.Minute, Limit: 1, Owner: "one"})
	two, err := s.ClaimDue(ctx, ClaimDueParams{Kind: models.AssignmentKindTargetExecution, Now: now.Add(2 * time.Minute), Lease: time.Minute, Limit: 1, Owner: "two"})
	if err != nil || len(two) != 1 {
		t.Fatalf("takeover=%v %v", two, err)
	}
	if two[0].Attempt != 2 || two[0].ExecutionFence != 2 {
		t.Fatalf("takeover=%+v", two[0])
	}
	attempts, _ := s.ListAttempts(ctx, a.AssignmentID)
	if attempts[0].State != models.AssignmentStateSuperseded {
		t.Fatalf("old attempt=%+v", attempts[0])
	}
	if _, err = s.RequestCancel(ctx, CancelAssignmentParams{Kind: models.AssignmentKindTargetExecution, AssignmentID: a.AssignmentID, ExpectedRevision: two[0].Revision, ExpectedAttempt: two[0].Attempt, ExpectedFence: 1, Now: now.Add(3 * time.Minute)}); !errors.Is(err, ErrAssignmentConflict) {
		t.Fatalf("stale fence cancel=%v", err)
	}
	cancelled, err := s.RequestCancel(ctx, CancelAssignmentParams{Kind: models.AssignmentKindTargetExecution, AssignmentID: a.AssignmentID, ExpectedRevision: two[0].Revision, ExpectedAttempt: two[0].Attempt, ExpectedFence: two[0].ExecutionFence, Now: now.Add(3 * time.Minute)})
	if err != nil || cancelled.ExecutionFence != 3 {
		t.Fatalf("cancel=%+v %v", cancelled, err)
	}
	if _, err = s.RequestCancel(ctx, CancelAssignmentParams{Kind: models.AssignmentKindTargetExecution, AssignmentID: a.AssignmentID, ExpectedRevision: two[0].Revision, ExpectedAttempt: two[0].Attempt, ExpectedFence: two[0].ExecutionFence, Now: now.Add(4 * time.Minute)}); err != nil {
		t.Fatalf("cancel replay=%v", err)
	}
	if _, err = s.Renew(ctx, AssignmentClaim{Kind: models.AssignmentKindTargetExecution, AssignmentID: a.AssignmentID, Revision: two[0].Revision, Attempt: 2, Fence: 2, Owner: "two", ClaimToken: two[0].ClaimToken, Now: now.Add(2*time.Minute + time.Second), Lease: time.Minute}); !errors.Is(err, ErrAssignmentTerminal) {
		t.Fatalf("old renew=%v", err)
	}
	_ = one
}

func TestAssignmentMultiInstanceClaimCrashTakeoverFencesOldWorker(t *testing.T) {
	ctx := context.Background()
	db1, a, now := assignmentFixture(t)
	if _, err := db1.AssignmentStore().Create(ctx, a); err != nil {
		t.Fatal(err)
	}
	db2, err := Open(ctx, db1.Path())
	if err != nil {
		t.Fatal(err)
	}
	defer db2.Close()
	oldClaims, err := db1.AssignmentStore().ClaimDue(ctx, ClaimDueParams{Kind: a.Kind, Now: now, Lease: time.Second, Limit: 1, Owner: "worker-old"})
	if err != nil || len(oldClaims) != 1 {
		t.Fatalf("old claim=%+v err=%v", oldClaims, err)
	}
	old := oldClaims[0]
	newClaims, err := db2.AssignmentStore().ClaimDue(ctx, ClaimDueParams{Kind: a.Kind, Now: now.Add(2 * time.Second), Lease: time.Second, Limit: 1, Owner: "worker-new"})
	if err != nil || len(newClaims) != 1 {
		t.Fatalf("new claim=%+v err=%v", newClaims, err)
	}
	if newClaims[0].Attempt <= old.Attempt || newClaims[0].ExecutionFence <= old.ExecutionFence {
		t.Fatalf("old=%+v new=%+v", old, newClaims[0])
	}
	_, _, err = db1.ExecutionQueueStore().CommitExecutionResult(ctx, AssignmentResult{AssignmentTransition: AssignmentTransition{Kind: old.Kind, AssignmentID: old.AssignmentID, Revision: old.Revision, Attempt: old.Attempt, Fence: old.ExecutionFence, Owner: old.LeaseOwner, ClaimToken: old.ClaimToken, Now: now.Add(2 * time.Second)}, Status: "completed"})
	if !errors.Is(err, ErrAssignmentClaimLost) {
		t.Fatalf("stale worker commit=%v", err)
	}
}

func TestAssignmentOutboundMayBeSentCrashBecomesUnknownWithoutReplay(t *testing.T) {
	ctx := context.Background()
	db1, a, now := assignmentFixture(t)
	a.Kind = models.AssignmentKindOutboundDelegation
	if _, err := db1.AssignmentStore().Create(ctx, a); err != nil {
		t.Fatal(err)
	}
	db2, err := Open(ctx, db1.Path())
	if err != nil {
		t.Fatal(err)
	}
	defer db2.Close()
	claims, err := db1.AssignmentStore().ClaimDue(ctx, ClaimDueParams{Kind: a.Kind, Now: now, Lease: time.Second, Limit: 1, Owner: "outbound-old"})
	if err != nil || len(claims) != 1 {
		t.Fatalf("claims=%+v err=%v", claims, err)
	}
	dispatched, err := db1.AssignmentStore().MarkDispatched(ctx, AssignmentTransition{Kind: a.Kind, AssignmentID: a.AssignmentID, Revision: claims[0].Revision, Attempt: claims[0].Attempt, Fence: claims[0].ExecutionFence, Owner: claims[0].LeaseOwner, ClaimToken: claims[0].ClaimToken, Now: now.Add(time.Millisecond)})
	if err != nil {
		t.Fatal(err)
	}
	if recovered, err := db2.AssignmentStore().RecoverExpiredActive(ctx, a.Kind, now.Add(2*time.Second), 1); err != nil || recovered != 1 {
		t.Fatalf("recovered=%d err=%v", recovered, err)
	}
	stored, _ := db2.AssignmentStore().Get(ctx, a.AssignmentID)
	task, _ := db2.TaskStore().Get(ctx, a.TaskID)
	if stored.State != models.AssignmentStateSettled || stored.ResultStatus != "outcome_unknown" || task.Status != "outcome_unknown" {
		t.Fatalf("assignment=%+v task=%+v", stored, task)
	}
	items, err := db2.AssignmentStore().ClaimDue(ctx, ClaimDueParams{Kind: a.Kind, Now: now.Add(3 * time.Second), Lease: time.Second, Limit: 1, Owner: "outbound-new"})
	if err != nil || len(items) != 0 {
		t.Fatalf("uncertain dispatch replayed: %+v err=%v", items, err)
	}
	_, _, err = db1.ExecutionQueueStore().CommitExecutionResult(ctx, AssignmentResult{AssignmentTransition: AssignmentTransition{Kind: a.Kind, AssignmentID: a.AssignmentID, Revision: dispatched.Revision, Attempt: dispatched.Attempt, Fence: dispatched.ExecutionFence, Owner: dispatched.LeaseOwner, ClaimToken: dispatched.ClaimToken, Now: now.Add(3 * time.Second)}, Status: "completed"})
	if !errors.Is(err, ErrAssignmentClaimLost) {
		t.Fatalf("stale outbound commit=%v", err)
	}
}

func TestAssignmentConcurrentClaimOnlyOneWinner(t *testing.T) {
	ctx := context.Background()
	db, a, now := assignmentFixture(t)
	path := db.Path()
	if _, err := db.AssignmentStore().Create(ctx, a); err != nil {
		t.Fatal(err)
	}
	db2, err := Open(ctx, path)
	if err != nil {
		t.Fatal(err)
	}
	defer db2.Close()
	start := make(chan struct{})
	var wg sync.WaitGroup
	wg.Add(2)
	counts := make(chan int, 2)
	errs := make(chan error, 2)
	for i, s := range []AssignmentStore{db.AssignmentStore(), db2.AssignmentStore()} {
		go func(i int, s AssignmentStore) {
			defer wg.Done()
			<-start
			items, err := s.ClaimDue(ctx, ClaimDueParams{Kind: models.AssignmentKindTargetExecution, Now: now, Lease: time.Minute, Limit: 1, Owner: string(rune('a' + i))})
			errs <- err
			counts <- len(items)
		}(i, s)
	}
	close(start)
	wg.Wait()
	close(counts)
	close(errs)
	total := 0
	for n := range counts {
		total += n
	}
	for err := range errs {
		if err != nil {
			t.Fatal(err)
		}
	}
	if total != 1 {
		t.Fatalf("winners=%d", total)
	}
}

func TestExecutionQueueEnqueueAndAtomicSettlement(t *testing.T) {
	ctx := context.Background()
	db, _, now := assignmentFixture(t)
	ts := db.TaskStore()
	if _, _, err := ts.UpdateStatus(ctx, "task-a", "pending", "accepted", nil, ""); err != nil {
		t.Fatal(err)
	}
	queue := db.ExecutionQueueStore()
	p := EnqueueAcceptedTaskParams{Kind: models.AssignmentKindTargetExecution, AssignmentID: "exec-task-a", DeliveryID: "delivery-task-a", IdempotencyKey: "start-task-a", TaskID: "task-a", TenantID: "tenant-a", TargetAgentID: "b", Now: now}
	running, assignment, err := queue.EnqueueAcceptedTask(ctx, p)
	if err != nil || running.Status != "running" || assignment.State != models.AssignmentStateQueued {
		t.Fatalf("enqueue task=%+v assignment=%+v err=%v", running, assignment, err)
	}
	if replayTask, replayAssignment, err := queue.EnqueueAcceptedTask(ctx, p); err != nil || replayTask.Status != "running" || replayAssignment.AssignmentID != assignment.AssignmentID {
		t.Fatalf("replay task=%+v assignment=%+v err=%v", replayTask, replayAssignment, err)
	}
	claims, err := db.AssignmentStore().ClaimDue(ctx, ClaimDueParams{Kind: models.AssignmentKindTargetExecution, Now: now.Add(time.Second), Lease: time.Minute, Limit: 1, Owner: "worker"})
	if err != nil || len(claims) != 1 {
		t.Fatalf("claims=%v err=%v", claims, err)
	}
	a, err := db.AssignmentStore().MarkDispatched(ctx, AssignmentTransition{Kind: models.AssignmentKindTargetExecution, AssignmentID: claims[0].AssignmentID, Revision: claims[0].Revision, Attempt: 1, Fence: 1, Owner: "worker", ClaimToken: claims[0].ClaimToken, Now: now.Add(2 * time.Second)})
	if err != nil {
		t.Fatal(err)
	}
	a, err = db.AssignmentStore().MarkExecuting(ctx, AssignmentTransition{AssignmentID: a.AssignmentID, Revision: a.Revision, Attempt: 1, Fence: 1, Owner: "worker", ClaimToken: a.ClaimToken, Now: now.Add(3 * time.Second), Kind: models.AssignmentKindTargetExecution})
	if err != nil {
		t.Fatal(err)
	}
	before, _ := db.EventStore().ListPending(ctx, "task-a")
	result := AssignmentResult{AssignmentTransition: AssignmentTransition{AssignmentID: a.AssignmentID, Revision: a.Revision, Attempt: 1, Fence: 1, Owner: "worker", ClaimToken: a.ClaimToken, Now: now.Add(4 * time.Second), Kind: models.AssignmentKindTargetExecution}, Status: "completed", Outcome: []byte(`{"ok":true}`)}
	completed, settled, err := queue.CommitExecutionResult(ctx, result)
	if err != nil || completed.Status != "completed" || settled.State != models.AssignmentStateSettled {
		t.Fatalf("commit task=%+v assignment=%+v err=%v", completed, settled, err)
	}
	result.Revision = settled.Revision
	if _, _, err = queue.CommitExecutionResult(ctx, result); err != nil {
		t.Fatalf("replay=%v", err)
	}
	after, _ := db.EventStore().ListPending(ctx, "task-a")
	if len(after) != len(before)+1 {
		t.Fatalf("events before=%d after=%d", len(before), len(after))
	}
}

func TestExecutionQueueOldFenceCannotSettleTask(t *testing.T) {
	ctx := context.Background()
	db, _, now := assignmentFixture(t)
	_, _, _ = db.TaskStore().UpdateStatus(ctx, "task-a", "pending", "accepted", nil, "")
	queue := db.ExecutionQueueStore()
	_, _, err := queue.EnqueueAcceptedTask(ctx, EnqueueAcceptedTaskParams{Kind: models.AssignmentKindTargetExecution, AssignmentID: "exec", DeliveryID: "delivery", TaskID: "task-a", TenantID: "tenant-a", TargetAgentID: "b", Now: now})
	if err != nil {
		t.Fatal(err)
	}
	claims, _ := db.AssignmentStore().ClaimDue(ctx, ClaimDueParams{Kind: models.AssignmentKindTargetExecution, Now: now.Add(time.Second), Lease: time.Second, Limit: 1, Owner: "old"})
	old := claims[0]
	newClaims, _ := db.AssignmentStore().ClaimDue(ctx, ClaimDueParams{Kind: models.AssignmentKindTargetExecution, Now: now.Add(3 * time.Second), Lease: time.Minute, Limit: 1, Owner: "new"})
	if len(newClaims) != 1 {
		t.Fatal("expected takeover")
	}
	_, _, err = queue.CommitExecutionResult(ctx, AssignmentResult{AssignmentTransition: AssignmentTransition{AssignmentID: old.AssignmentID, Revision: old.Revision, Attempt: old.Attempt, Fence: old.ExecutionFence, Owner: old.LeaseOwner, ClaimToken: old.ClaimToken, Now: now.Add(4 * time.Second), Kind: models.AssignmentKindTargetExecution}, Status: "outcome_unknown", ConsumedBudget: models.DelegationBudget{TokenCount: 99}})
	if !errors.Is(err, ErrAssignmentClaimLost) {
		t.Fatalf("old fence=%v", err)
	}
	task, _ := db.TaskStore().Get(ctx, "task-a")
	if task.Status != "running" {
		t.Fatalf("task=%+v", task)
	}
}

func TestAssignmentRenewExpiredAndSchemaCompatibility(t *testing.T) {
	ctx := context.Background()
	db, a, now := assignmentFixture(t)
	s := db.AssignmentStore()
	if _, err := s.Create(ctx, a); err != nil {
		t.Fatal(err)
	}
	one, _ := s.ClaimDue(ctx, ClaimDueParams{Kind: models.AssignmentKindTargetExecution, Now: now, Lease: time.Minute, Limit: 1, Owner: "one"})
	c := one[0]
	if _, err := s.Renew(ctx, AssignmentClaim{Kind: models.AssignmentKindTargetExecution, AssignmentID: a.AssignmentID, Revision: c.Revision, Attempt: c.Attempt, Fence: c.ExecutionFence, Owner: c.LeaseOwner, ClaimToken: c.ClaimToken, Now: now.Add(time.Second), Lease: 10 * time.Second}); !errors.Is(err, ErrAssignmentConflict) {
		t.Fatalf("shorten lease=%v", err)
	}
	unchanged, err := s.Get(ctx, a.AssignmentID)
	if err != nil || unchanged.Revision != c.Revision || !unchanged.LeaseExpiresAt.Equal(*c.LeaseExpiresAt) {
		t.Fatalf("shorten changed assignment=%+v %v", unchanged, err)
	}
	if _, err := s.Renew(ctx, AssignmentClaim{Kind: models.AssignmentKindTargetExecution, AssignmentID: a.AssignmentID, Revision: c.Revision, Attempt: c.Attempt, Fence: c.ExecutionFence, Owner: c.LeaseOwner, ClaimToken: c.ClaimToken, Now: now.Add(2 * time.Minute), Lease: time.Minute}); !errors.Is(err, ErrAssignmentLeaseExpired) {
		t.Fatalf("expired=%v", err)
	}
	var ddl string
	if err := db.QueryRowContext(ctx, `SELECT sql FROM sqlite_master WHERE type='table' AND name='tasks'`).Scan(&ddl); err != nil {
		t.Fatal(err)
	}
	if models.CurrentProtocolVersion != "0.54.0" {
		t.Fatalf("protocol=%s", models.CurrentProtocolVersion)
	}
	wantCheck := "status IN ('pending','accepted','running','completed','failed','cancelled','outcome_unknown')"
	if !strings.Contains(ddl, wantCheck) {
		t.Fatalf("tasks status CHECK changed: %s", ddl)
	}
}
