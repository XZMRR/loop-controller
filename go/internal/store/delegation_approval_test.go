package store

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/loop-controller/go/internal/models"
)

func TestApprovalConsumeAtomicallyCreatesTaskBudgetAndDispatch(t *testing.T) {
	db := openTestDB(t)
	ctx := context.Background()
	now := time.Now().UTC()
	parent := models.Task{ProtocolVersion: models.CurrentProtocolVersion, TaskID: "parent-approval", SessionID: "s", InteractionID: "pi", DecisionID: "pd", Budget: models.DelegationBudget{TokenCount: 10, Currency: "USD"}, InitiatorAgentID: "a", TargetAgentID: "b", Status: "running", CreatedAt: now, UpdatedAt: now}
	if err := db.TaskStore().Create(ctx, parent); err != nil {
		t.Fatal(err)
	}
	a := models.DelegationApproval{ProtocolVersion: models.CurrentProtocolVersion, ApprovalID: "approval-1", RequestID: "request-1", DecisionID: "decision-1", RequestHash: "hash", InitiatorAgentID: "b", TargetAgentID: "c", SessionID: "s", ParentTaskID: parent.TaskID, EffectiveArgs: json.RawMessage(`{"x":1}`), AllowedTools: []string{"echo"}, Budget: models.DelegationBudget{TokenCount: 4, Currency: "USD"}, ExpiresAt: now.Add(time.Hour), Status: "approved", CreatedAt: now, UpdatedAt: now, Version: 1}
	if _, _, err := db.DelegationApprovalStore().Create(ctx, a); err != nil {
		t.Fatal(err)
	}
	child := models.Task{ProtocolVersion: models.CurrentProtocolVersion, TaskID: "child-approval", SessionID: "s", InteractionID: "i", DecisionID: "d2", ParentTaskID: parent.TaskID, Budget: a.Budget, InitiatorAgentID: "b", TargetAgentID: "c", Status: "pending", CreatedAt: now, UpdatedAt: now, DelegationToken: "token"}
	dispatch := models.EntrypointTaskRequest{DeliveryID: "delegation-dispatch-" + child.TaskID, RequestID: a.RequestID, TaskID: child.TaskID}
	consumed, err := db.DelegationApprovalStore().Consume(ctx, a.ApprovalID, a.Version, child, models.AgentEntrypoint{Type: "http", URL: "http://target"}, dispatch)
	if err != nil {
		t.Fatal(err)
	}
	if consumed.Status != "consumed" || consumed.TaskID != child.TaskID {
		t.Fatalf("unexpected approval: %+v", consumed)
	}
	storedParent, _ := db.TaskStore().Get(ctx, parent.TaskID)
	if storedParent.ReservedBudget.TokenCount != 4 {
		t.Fatalf("reserved=%d", storedParent.ReservedBudget.TokenCount)
	}
	items, err := db.DelegationDispatchOutboxStore().ClaimDue(ctx, now.Add(time.Second), time.Minute, 10)
	if err != nil || len(items) != 0 {
		t.Fatalf("legacy dispatcher claimed assignment outbox: %+v err=%v", items, err)
	}
	assignment, err := db.AssignmentStore().Get(ctx, "outbound-assignment-"+child.TaskID)
	if err != nil {
		t.Fatal(err)
	}
	item, err := db.DelegationDispatchOutboxStore().LoadOutboundDelegation(ctx, assignment.AssignmentID)
	if err != nil || item.Request.DeliveryID != dispatch.DeliveryID {
		t.Fatalf("assignment outbox=%+v err=%v", item, err)
	}
	again, err := db.DelegationApprovalStore().Consume(ctx, a.ApprovalID, a.Version, child, models.AgentEntrypoint{}, dispatch)
	if err != nil || again.TaskID != child.TaskID {
		t.Fatalf("idempotent consume: %+v %v", again, err)
	}
}

func TestApprovalAuditOutboxExcludesEffectiveArguments(t *testing.T) {
	db := openTestDB(t)
	ctx := context.Background()
	now := time.Now().UTC()
	a := models.DelegationApproval{ApprovalID: "approval-audit", RequestID: "request-audit", DecisionID: "decision-audit", RequestHash: "digest", InitiatorAgentID: "a", TargetAgentID: "b", EffectiveArgs: json.RawMessage(`{"password":"secret"}`), ExpiresAt: now.Add(time.Hour), Status: "pending", CreatedAt: now, UpdatedAt: now, Version: 1}
	if _, _, err := db.DelegationApprovalStore().Create(ctx, a); err != nil {
		t.Fatal(err)
	}
	items, err := db.ApprovalAuditOutboxStore().ClaimDue(ctx, now.Add(time.Second), time.Minute, 10)
	if err != nil || len(items) != 1 {
		t.Fatalf("audit items=%+v err=%v", items, err)
	}
	encoded, _ := json.Marshal(items[0].Approval)
	if string(encoded) == "" || json.Valid(encoded) == false {
		t.Fatalf("invalid audit payload: %s", encoded)
	}
	if bytes.Contains(encoded, []byte("secret")) || bytes.Contains(encoded, []byte("password")) || len(items[0].Approval.EffectiveArgs) != 0 {
		t.Fatalf("approval audit leaked effective arguments: %s", encoded)
	}
}

func TestApprovalTransitionCASExpiryAndReplay(t *testing.T) {
	db := openTestDB(t)
	ctx := context.Background()
	now := time.Now().UTC()
	store := db.DelegationApprovalStore()
	seed := func(id string, expires time.Time) models.DelegationApproval {
		a := models.DelegationApproval{ApprovalID: id, RequestID: "request-" + id, DecisionID: "decision-" + id, RequestHash: "hash-" + id, InitiatorAgentID: "a", TargetAgentID: "b", EffectiveArgs: json.RawMessage(`{}`), ExpiresAt: expires, Status: "pending", CreatedAt: now, UpdatedAt: now, Version: 1}
		if _, _, err := store.Create(ctx, a); err != nil {
			t.Fatal(err)
		}
		return a
	}
	a := seed("cas", now.Add(time.Hour))
	approved, err := store.Transition(ctx, a.ApprovalID, a.Version, "pending", "approved", "reviewer", "ok", "action-1")
	if err != nil || approved.ApproverID != "reviewer" {
		t.Fatalf("approve=%+v err=%v", approved, err)
	}
	replayed, err := store.Transition(ctx, a.ApprovalID, a.Version, "pending", "approved", "reviewer", "ok", "action-1")
	if err != nil || replayed.Status != "approved" {
		t.Fatalf("replay=%+v err=%v", replayed, err)
	}
	if _, err := store.Transition(ctx, a.ApprovalID, a.Version, "pending", "approved", "reviewer", "changed", "action-1"); !errors.Is(err, ErrApprovalConflict) {
		t.Fatalf("changed replay err=%v", err)
	}
	if _, err := store.Transition(ctx, a.ApprovalID, approved.Version, "approved", "cancelled", "a", "", ""); !errors.Is(err, ErrApprovalConflict) {
		t.Fatalf("missing replay key err=%v", err)
	}
	expired := seed("expired", now.Add(-time.Second))
	count, err := store.ExpireDue(ctx, now, 10)
	if err != nil || count != 1 {
		t.Fatalf("expire count=%d err=%v", count, err)
	}
	got, _ := store.Get(ctx, expired.ApprovalID)
	if got.Status != "expired" {
		t.Fatalf("status=%s", got.Status)
	}
}

func TestApprovalConsumeRejectsDifferentTaskReplay(t *testing.T) {
	db := openTestDB(t)
	ctx := context.Background()
	now := time.Now().UTC()
	a := models.DelegationApproval{ApprovalID: "approval-task-replay", RequestID: "request-task-replay", DecisionID: "decision-task-replay", RequestHash: "h", InitiatorAgentID: "a", TargetAgentID: "b", EffectiveArgs: json.RawMessage(`{}`), ExpiresAt: now.Add(time.Hour), Status: "approved", CreatedAt: now, UpdatedAt: now, Version: 1}
	store := db.DelegationApprovalStore()
	if _, _, err := store.Create(ctx, a); err != nil {
		t.Fatal(err)
	}
	first := models.Task{ProtocolVersion: models.CurrentProtocolVersion, TaskID: "task-first", InteractionID: "i", DecisionID: "d", InitiatorAgentID: "a", TargetAgentID: "b", Status: "pending", CreatedAt: now, UpdatedAt: now}
	dispatch := models.EntrypointTaskRequest{DeliveryID: "delegation-dispatch:" + a.ApprovalID, TaskID: first.TaskID}
	if _, err := store.Consume(ctx, a.ApprovalID, 1, first, models.AgentEntrypoint{}, dispatch); err != nil {
		t.Fatal(err)
	}
	second := first
	second.TaskID = "task-second"
	if _, err := store.Consume(ctx, a.ApprovalID, 1, second, models.AgentEntrypoint{}, dispatch); !errors.Is(err, ErrApprovalConflict) {
		t.Fatalf("different task replay err=%v", err)
	}
}

func TestApprovalOutboxesAreAtomicAndIndependent(t *testing.T) {
	db := openTestDB(t)
	ctx := context.Background()
	now := time.Now().UTC()
	db.SetApprovalNotificationsEnabled(true)
	a := models.DelegationApproval{ApprovalID: "approval-dual", RequestID: "request-dual", DecisionID: "decision-dual", RequestHash: "h", InitiatorAgentID: "a", TargetAgentID: "b", EffectiveArgs: json.RawMessage(`{"secret":"plain"}`), ExpiresAt: now.Add(time.Hour), Status: "pending", CreatedAt: now, UpdatedAt: now, Version: 1}
	if _, _, err := db.DelegationApprovalStore().Create(ctx, a); err != nil {
		t.Fatal(err)
	}
	audit, err := db.ApprovalAuditOutboxStore().ClaimDue(ctx, now.Add(time.Second), time.Minute, 10)
	if err != nil || len(audit) != 1 {
		t.Fatalf("audit=%+v err=%v", audit, err)
	}
	notifications, err := db.ApprovalNotificationOutboxStore().ClaimDue(ctx, now.Add(time.Second), time.Minute, 10)
	if err != nil || len(notifications) != 1 {
		t.Fatalf("notifications=%+v err=%v", notifications, err)
	}
	_, digest, err := NormalizeWebhookURL("http://approval-webhook.invalid/")
	if err != nil {
		t.Fatal(err)
	}
	if audit[0].EventID == notifications[0].DeliveryID || notifications[0].DeliveryID != "approval-webhook:"+digest+":approval-dual:created" {
		t.Fatalf("destination IDs audit=%q webhook=%q", audit[0].EventID, notifications[0].DeliveryID)
	}
	encoded, _ := json.Marshal(notifications[0].Notification)
	if bytes.Contains(encoded, []byte("plain")) || bytes.Contains(encoded, []byte("secret")) {
		t.Fatalf("notification leaked arguments: %s", encoded)
	}
	if err := db.ApprovalAuditOutboxStore().MarkDelivered(ctx, audit[0].ID, audit[0].ClaimToken, now); err != nil {
		t.Fatal(err)
	}
	if err := db.ApprovalNotificationOutboxStore().MarkFailed(ctx, notifications[0].ID, notifications[0].ClaimToken, now.Add(time.Second), "retry"); err != nil {
		t.Fatal(err)
	}
	var auditDelivered, notificationDelivered any
	if err := db.QueryRowContext(ctx, `SELECT delivered_at FROM approval_audit_outbox WHERE approval_id=?`, a.ApprovalID).Scan(&auditDelivered); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRowContext(ctx, `SELECT delivered_at FROM approval_notification_outbox WHERE approval_id=?`, a.ApprovalID).Scan(&notificationDelivered); err != nil {
		t.Fatal(err)
	}
	if auditDelivered == nil || notificationDelivered != nil {
		t.Fatalf("progress was shared: audit=%v notification=%v", auditDelivered, notificationDelivered)
	}
}

func TestApprovalNotificationDisabledAndAtomicRollback(t *testing.T) {
	db := openTestDB(t)
	ctx := context.Background()
	now := time.Now().UTC()
	seed := func(id string) models.DelegationApproval {
		return models.DelegationApproval{ApprovalID: id, RequestID: "request-" + id, DecisionID: "decision-" + id, RequestHash: "h", InitiatorAgentID: "a", TargetAgentID: "b", ExpiresAt: now.Add(time.Hour), Status: "pending", CreatedAt: now, UpdatedAt: now, Version: 1}
	}
	if _, _, err := db.DelegationApprovalStore().Create(ctx, seed("disabled")); err != nil {
		t.Fatal(err)
	}
	var count int
	if err := db.QueryRowContext(ctx, `SELECT COUNT(*) FROM approval_notification_outbox`).Scan(&count); err != nil || count != 0 {
		t.Fatalf("disabled notification count=%d err=%v", count, err)
	}
	db.SetApprovalNotificationsEnabled(true)
	if _, err := db.ExecContext(ctx, `CREATE TRIGGER reject_notification BEFORE INSERT ON approval_notification_outbox BEGIN SELECT RAISE(ABORT, 'notification rejected'); END`); err != nil {
		t.Fatal(err)
	}
	if _, _, err := db.DelegationApprovalStore().Create(ctx, seed("rollback")); err == nil {
		t.Fatal("expected notification insert failure")
	}
	if err := db.QueryRowContext(ctx, `SELECT COUNT(*) FROM delegation_approvals WHERE approval_id='rollback'`).Scan(&count); err != nil || count != 0 {
		t.Fatalf("approval was not rolled back: count=%d err=%v", count, err)
	}
	if err := db.QueryRowContext(ctx, `SELECT COUNT(*) FROM approval_audit_outbox WHERE approval_id='rollback'`).Scan(&count); err != nil || count != 0 {
		t.Fatalf("audit was not rolled back: count=%d err=%v", count, err)
	}
}

func TestApprovalExpiredEnqueuesBothDestinations(t *testing.T) {
	db := openTestDB(t)
	ctx := context.Background()
	now := time.Now().UTC()
	db.SetApprovalNotificationsEnabled(true)
	a := models.DelegationApproval{ApprovalID: "dual-expired", RequestID: "request-dual-expired", DecisionID: "decision-dual-expired", RequestHash: "h", InitiatorAgentID: "a", TargetAgentID: "b", ExpiresAt: now.Add(-time.Second), Status: "pending", CreatedAt: now.Add(-time.Hour), UpdatedAt: now.Add(-time.Hour), Version: 1}
	if _, _, err := db.DelegationApprovalStore().Create(ctx, a); err != nil {
		t.Fatal(err)
	}
	if n, err := db.DelegationApprovalStore().ExpireDue(ctx, now, 10); err != nil || n != 1 {
		t.Fatalf("expired=%d err=%v", n, err)
	}
	for _, table := range []string{"approval_audit_outbox", "approval_notification_outbox"} {
		var count int
		if err := db.QueryRowContext(ctx, `SELECT COUNT(*) FROM `+table+` WHERE approval_id=? AND event='expired'`, a.ApprovalID).Scan(&count); err != nil || count != 1 {
			t.Fatalf("%s expired count=%d err=%v", table, count, err)
		}
	}
}

func TestApprovalNotificationFencingRetryAndRestart(t *testing.T) {
	path := t.TempDir() + "/approval.db"
	ctx := context.Background()
	now := time.Now().UTC()
	db, err := Open(ctx, path)
	if err != nil {
		t.Fatal(err)
	}
	db.SetApprovalNotificationsEnabled(true)
	a := models.DelegationApproval{ApprovalID: "restart", RequestID: "request-restart", DecisionID: "decision-restart", RequestHash: "h", InitiatorAgentID: "a", TargetAgentID: "b", ExpiresAt: now.Add(time.Hour), Status: "pending", CreatedAt: now, UpdatedAt: now, Version: 1}
	if _, _, err := db.DelegationApprovalStore().Create(ctx, a); err != nil {
		t.Fatal(err)
	}
	outbox := db.ApprovalNotificationOutboxStore()
	first, _ := outbox.ClaimDue(ctx, now.Add(time.Second), time.Millisecond, 1)
	second, _ := outbox.ClaimDue(ctx, now.Add(2*time.Second), time.Minute, 1)
	if len(first) != 1 || len(second) != 1 || first[0].ClaimToken == second[0].ClaimToken {
		t.Fatalf("claims=%+v %+v", first, second)
	}
	if err := outbox.MarkDelivered(ctx, first[0].ID, first[0].ClaimToken, now); !errors.Is(err, ErrDispatchClaimLost) {
		t.Fatalf("stale ack=%v", err)
	}
	if err := outbox.MarkFailed(ctx, second[0].ID, second[0].ClaimToken, now.Add(3*time.Second), "temporary"); err != nil {
		t.Fatal(err)
	}
	if err := db.Close(); err != nil {
		t.Fatal(err)
	}
	db, err = Open(ctx, path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	restarted, err := db.ApprovalNotificationOutboxStore().ClaimDue(ctx, now.Add(4*time.Second), time.Minute, 1)
	if err != nil || len(restarted) != 1 || restarted[0].Attempts != 1 {
		t.Fatalf("restart items=%+v err=%v", restarted, err)
	}
}

func TestApprovalNotificationBindsDestinationAndDeliveryID(t *testing.T) {
	db := openTestDB(t)
	ctx := context.Background()
	now := time.Now().UTC()
	var callsA, callsB atomic.Int32
	serverA := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) { callsA.Add(1) }))
	defer serverA.Close()
	serverB := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) { callsB.Add(1) }))
	defer serverB.Close()

	db.SetApprovalNotificationDestination(serverA.URL)
	a := models.DelegationApproval{ApprovalID: "bound-destination", RequestID: "request-bound", DecisionID: "decision-bound", RequestHash: "h", InitiatorAgentID: "a", TargetAgentID: "b", ExpiresAt: now.Add(time.Hour), Status: "pending", CreatedAt: now, UpdatedAt: now, Version: 1}
	if _, _, err := db.DelegationApprovalStore().Create(ctx, a); err != nil {
		t.Fatal(err)
	}
	dispatcher := NewApprovalNotificationDispatcher(db.ApprovalNotificationOutboxStore(), &HTTPApprovalWebhook{URL: serverB.URL, Client: &http.Client{Timeout: time.Second}}, time.Second)
	if err := dispatcher.RunOnce(ctx, now.Add(time.Second)); err != nil {
		t.Fatal(err)
	}
	if callsA.Load() != 1 || callsB.Load() != 0 {
		t.Fatalf("old queued destination leaked: A=%d B=%d", callsA.Load(), callsB.Load())
	}
	_, digest, err := NormalizeWebhookURL(serverA.URL)
	if err != nil {
		t.Fatal(err)
	}
	var deliveryID, storedDigest string
	if err := db.QueryRowContext(ctx, `SELECT delivery_id,destination_digest FROM approval_notification_outbox WHERE approval_id=?`, a.ApprovalID).Scan(&deliveryID, &storedDigest); err != nil {
		t.Fatal(err)
	}
	if storedDigest != digest || !strings.Contains(deliveryID, digest) {
		t.Fatalf("delivery ID %q does not bind digest %q", deliveryID, digest)
	}
}

func TestApprovalOutboxBadPayloadDoesNotBlockNextRow(t *testing.T) {
	db := openTestDB(t)
	ctx := context.Background()
	now := time.Now().UTC()
	db.SetApprovalNotificationDestination("http://approval-webhook.invalid/")
	for _, id := range []string{"bad", "good"} {
		a := models.DelegationApproval{ApprovalID: id, RequestID: "request-" + id, DecisionID: "decision-" + id, RequestHash: "h", InitiatorAgentID: "a", TargetAgentID: "b", ExpiresAt: now.Add(time.Hour), Status: "pending", CreatedAt: now, UpdatedAt: now, Version: 1}
		if _, _, err := db.DelegationApprovalStore().Create(ctx, a); err != nil {
			t.Fatal(err)
		}
	}
	for _, table := range []string{"approval_notification_outbox", "approval_audit_outbox"} {
		if _, err := db.ExecContext(ctx, `UPDATE `+table+` SET payload_json='{' WHERE approval_id='bad'`); err != nil {
			t.Fatal(err)
		}
	}
	notifier := &recordingApprovalNotifier{destination: "http://approval-webhook.invalid/"}
	notificationDispatcher := NewApprovalNotificationDispatcher(db.ApprovalNotificationOutboxStore(), notifier, time.Second)
	if err := notificationDispatcher.RunOnce(ctx, now.Add(time.Second)); err != nil {
		t.Fatal(err)
	}
	if err := notificationDispatcher.RunOnce(ctx, now.Add(time.Second)); err != nil {
		t.Fatal(err)
	}
	if notifier.calls != 1 {
		t.Fatalf("good notification calls=%d", notifier.calls)
	}
	auditor := &recordingApprovalAuditor{}
	auditDispatcher := NewApprovalAuditOutboxDispatcher(db.ApprovalAuditOutboxStore(), auditor, time.Second)
	if err := auditDispatcher.RunOnce(ctx, now.Add(time.Second)); err != nil {
		t.Fatal(err)
	}
	if err := auditDispatcher.RunOnce(ctx, now.Add(time.Second)); err != nil {
		t.Fatal(err)
	}
	if auditor.calls != 1 {
		t.Fatalf("good audit calls=%d", auditor.calls)
	}
	for _, table := range []string{"approval_notification_outbox", "approval_audit_outbox"} {
		var attempts int
		var lastError string
		if err := db.QueryRowContext(ctx, `SELECT attempts,last_error FROM `+table+` WHERE approval_id='bad'`).Scan(&attempts, &lastError); err != nil {
			t.Fatal(err)
		}
		if attempts != 1 || !strings.Contains(lastError, "invalid payload") {
			t.Fatalf("%s bad payload state attempts=%d error=%q", table, attempts, lastError)
		}
	}
}

type recordingApprovalNotifier struct {
	destination string
	calls       int
}

func (n *recordingApprovalNotifier) DestinationURL() string { return n.destination }
func (n *recordingApprovalNotifier) NotifyApproval(context.Context, string, ApprovalNotification) error {
	n.calls++
	return nil
}

type recordingApprovalAuditor struct{ calls int }

func (a *recordingApprovalAuditor) RecordApprovalLifecycle(context.Context, models.DelegationApproval, string, string) error {
	a.calls++
	return nil
}

func TestDispatchOutboxClaimTokenFencesStaleAck(t *testing.T) {
	db := openTestDB(t)
	ctx := context.Background()
	now := time.Now().UTC()
	// Seed through the atomic consumer to preserve foreign-key and invariant coverage.
	a := models.DelegationApproval{ApprovalID: "approval-fence", RequestID: "request-fence", DecisionID: "decision-fence", RequestHash: "h", InitiatorAgentID: "a", TargetAgentID: "b", EffectiveArgs: json.RawMessage(`{}`), ExpiresAt: now.Add(time.Hour), Status: "approved", CreatedAt: now, UpdatedAt: now, Version: 1}
	if _, _, err := db.DelegationApprovalStore().Create(ctx, a); err != nil {
		t.Fatal(err)
	}
	task := models.Task{ProtocolVersion: models.CurrentProtocolVersion, TaskID: "task-fence", InteractionID: "i", DecisionID: "d", InitiatorAgentID: "a", TargetAgentID: "b", Status: "pending", CreatedAt: now, UpdatedAt: now}
	if _, err := db.DelegationApprovalStore().Consume(ctx, a.ApprovalID, 1, task, models.AgentEntrypoint{Type: "http", URL: "http://target"}, models.EntrypointTaskRequest{DeliveryID: "delegation-dispatch:approval-fence", TaskID: task.TaskID}); err != nil {
		t.Fatal(err)
	}
	if _, err := db.ExecContext(ctx, `UPDATE delegation_dispatch_outbox SET assignment_id=NULL WHERE task_id=?`, task.TaskID); err != nil {
		t.Fatal(err)
	}
	outbox := db.DelegationDispatchOutboxStore()
	first, _ := outbox.ClaimDue(ctx, now.Add(time.Second), time.Millisecond, 1)
	second, _ := outbox.ClaimDue(ctx, now.Add(2*time.Second), time.Minute, 1)
	if len(first) != 1 || len(second) != 1 || first[0].ClaimToken == second[0].ClaimToken {
		t.Fatalf("claims: %+v %+v", first, second)
	}
	if err := outbox.MarkDelivered(ctx, first[0].DeliveryID, first[0].ClaimToken, now); !errors.Is(err, ErrDispatchClaimLost) {
		t.Fatalf("stale ack=%v", err)
	}
	if err := outbox.MarkDelivered(ctx, second[0].DeliveryID, second[0].ClaimToken, now); err != nil {
		t.Fatal(err)
	}
}
