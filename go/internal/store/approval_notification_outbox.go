package store

import (
	"bytes"
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/loop-controller/go/internal/models"
)

type ApprovalNotification struct {
	DeliveryID          string                  `json:"delivery_id"`
	ApprovalID          string                  `json:"approval_id"`
	RequestID           string                  `json:"request_id"`
	DecisionID          string                  `json:"decision_id"`
	SourceAgentID       string                  `json:"source_agent_id"`
	TargetAgentID       string                  `json:"target_agent_id"`
	AllowedTools        []string                `json:"allowed_tools"`
	AllowedCapabilities []string                `json:"allowed_capabilities"`
	AllowRedelegation   bool                    `json:"allow_redelegation"`
	Budget              models.DelegationBudget `json:"budget"`
	Deadline            *time.Time              `json:"deadline,omitempty"`
	ExpiresAt           time.Time               `json:"expires_at"`
	Status              string                  `json:"status"`
	Event               string                  `json:"event"`
}

type ApprovalNotificationItem struct {
	ID                int64
	DeliveryID        string
	DestinationURL    string
	DestinationDigest string
	Notification      ApprovalNotification
	Attempts          int
	ClaimToken        string
	DecodeError       error
}

type ApprovalNotificationOutboxStore interface {
	ClaimDue(context.Context, time.Time, time.Duration, int) ([]ApprovalNotificationItem, error)
	MarkDelivered(context.Context, int64, string, time.Time) error
	MarkFailed(context.Context, int64, string, time.Time, string) error
}

type approvalNotificationOutboxStore struct {
	db    *sql.DB
	owner string
}

func (db *DB) ApprovalNotificationOutboxStore() ApprovalNotificationOutboxStore {
	return &approvalNotificationOutboxStore{db: db.DB, owner: db.instanceID}
}

func insertApprovalNotificationOutbox(ctx context.Context, executor interface {
	ExecContext(context.Context, string, ...any) (sql.Result, error)
}, approval models.DelegationApproval, event string, at time.Time, destination string) error {
	normalized, digest, err := NormalizeWebhookURL(destination)
	if err != nil {
		return err
	}
	deliveryID := fmt.Sprintf("approval-webhook:%s:%s:%s", digest, approval.ApprovalID, event)
	payload, err := json.Marshal(ApprovalNotification{
		DeliveryID: deliveryID, ApprovalID: approval.ApprovalID, RequestID: approval.RequestID,
		DecisionID: approval.DecisionID, SourceAgentID: approval.InitiatorAgentID, TargetAgentID: approval.TargetAgentID,
		AllowedTools: approval.AllowedTools, AllowedCapabilities: approval.AllowedCapabilities,
		AllowRedelegation: approval.AllowRedelegation, Budget: approval.Budget, Deadline: approval.TaskDeadline,
		ExpiresAt: approval.ExpiresAt, Status: approval.Status, Event: event,
	})
	if err != nil {
		return err
	}
	_, err = executor.ExecContext(ctx, `INSERT INTO approval_notification_outbox(delivery_id,approval_id,event,destination_url,destination_digest,payload_json,created_at,next_attempt_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(approval_id,event) DO NOTHING`, deliveryID, approval.ApprovalID, event, normalized, digest, string(payload), at.Format(time.RFC3339Nano), at.Format(time.RFC3339Nano))
	return err
}

func NormalizeWebhookURL(raw string) (string, string, error) {
	u, err := url.Parse(strings.TrimSpace(raw))
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" || u.User != nil {
		return "", "", errors.New("invalid approval webhook URL")
	}
	u.Scheme = strings.ToLower(u.Scheme)
	host := strings.ToLower(u.Hostname())
	port := u.Port()
	if (u.Scheme == "http" && port == "80") || (u.Scheme == "https" && port == "443") {
		port = ""
	}
	if port != "" {
		host = net.JoinHostPort(host, port)
	} else if strings.Contains(host, ":") {
		host = "[" + host + "]"
	}
	u.Host = host
	u.Fragment = ""
	u.RawQuery = u.Query().Encode()
	if u.Path == "" {
		u.Path = "/"
	}
	normalized := u.String()
	sum := sha256.Sum256([]byte(normalized))
	return normalized, hex.EncodeToString(sum[:]), nil
}

type ApprovalNotifier interface {
	DestinationURL() string
	NotifyApproval(context.Context, string, ApprovalNotification) error
}

type ApprovalNotificationDispatcher struct {
	store       ApprovalNotificationOutboxStore
	notifier    ApprovalNotifier
	poll, lease time.Duration
}

func NewApprovalNotificationDispatcher(store ApprovalNotificationOutboxStore, notifier ApprovalNotifier, poll time.Duration) *ApprovalNotificationDispatcher {
	if poll <= 0 {
		poll = time.Second
	}
	lease := 30 * time.Second
	if webhook, ok := notifier.(*HTTPApprovalWebhook); ok && webhook.Client != nil && webhook.Client.Timeout >= lease {
		lease = webhook.Client.Timeout + time.Second
	}
	return &ApprovalNotificationDispatcher{store: store, notifier: notifier, poll: poll, lease: lease}
}

func (d *ApprovalNotificationDispatcher) Run(ctx context.Context) {
	_ = d.RunOnce(ctx, time.Now().UTC())
	ticker := time.NewTicker(d.poll)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case now := <-ticker.C:
			_ = d.RunOnce(ctx, now.UTC())
		}
	}
}

func (d *ApprovalNotificationDispatcher) RunOnce(ctx context.Context, now time.Time) error {
	items, err := d.store.ClaimDue(ctx, now, d.lease, 1)
	if err != nil {
		return err
	}
	for _, item := range items {
		if item.DecodeError != nil {
			return d.store.MarkFailed(ctx, item.ID, item.ClaimToken, now.Add(retryDelay(item.Attempts+1)), "invalid payload: "+item.DecodeError.Error())
		}
		if err := d.notifier.NotifyApproval(ctx, item.DestinationURL, item.Notification); err != nil {
			return d.store.MarkFailed(ctx, item.ID, item.ClaimToken, now.Add(retryDelay(item.Attempts+1)), err.Error())
		}
		return d.store.MarkDelivered(ctx, item.ID, item.ClaimToken, time.Now().UTC())
	}
	return nil
}

func (s *approvalNotificationOutboxStore) ClaimDue(ctx context.Context, now time.Time, lease time.Duration, limit int) ([]ApprovalNotificationItem, error) {
	if lease <= 0 {
		lease = 30 * time.Second
	}
	if limit <= 0 {
		limit = 100
	}
	claim := fmt.Sprintf("%s:%d", s.owner, now.UnixNano())
	rows, err := s.db.QueryContext(ctx, `UPDATE approval_notification_outbox SET claimed_by=?,claim_token=?,claim_expires_at=? WHERE outbox_id IN (SELECT outbox_id FROM approval_notification_outbox WHERE delivered_at IS NULL AND next_attempt_at<=? AND (claim_token='' OR claim_expires_at<?) ORDER BY outbox_id LIMIT ?) RETURNING outbox_id,delivery_id,destination_url,destination_digest,payload_json,attempts,claim_token`, s.owner, claim, now.Add(lease).UnixNano(), now.Format(time.RFC3339Nano), now.UnixNano(), limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var items []ApprovalNotificationItem
	for rows.Next() {
		var item ApprovalNotificationItem
		var payload string
		if err := rows.Scan(&item.ID, &item.DeliveryID, &item.DestinationURL, &item.DestinationDigest, &payload, &item.Attempts, &item.ClaimToken); err != nil {
			return nil, err
		}
		item.DecodeError = json.Unmarshal([]byte(payload), &item.Notification)
		items = append(items, item)
	}
	return items, rows.Err()
}

func (s *approvalNotificationOutboxStore) MarkDelivered(ctx context.Context, id int64, claim string, at time.Time) error {
	return s.ack(ctx, `UPDATE approval_notification_outbox SET delivered_at=?,attempts=attempts+1,last_error='',claimed_by='',claim_token='',claim_expires_at=0 WHERE outbox_id=? AND claimed_by=? AND claim_token=? AND delivered_at IS NULL`, at.Format(time.RFC3339Nano), id, s.owner, claim)
}

func (s *approvalNotificationOutboxStore) MarkFailed(ctx context.Context, id int64, claim string, next time.Time, last string) error {
	return s.ack(ctx, `UPDATE approval_notification_outbox SET attempts=attempts+1,next_attempt_at=?,last_error=?,claimed_by='',claim_token='',claim_expires_at=0 WHERE outbox_id=? AND claimed_by=? AND claim_token=? AND delivered_at IS NULL`, next.Format(time.RFC3339Nano), last, id, s.owner, claim)
}

func (s *approvalNotificationOutboxStore) ack(ctx context.Context, query string, args ...any) error {
	res, err := s.db.ExecContext(ctx, query, args...)
	if err != nil {
		return err
	}
	if n, _ := res.RowsAffected(); n != 1 {
		return ErrDispatchClaimLost
	}
	return nil
}

type HTTPApprovalWebhook struct {
	URL                 string
	AuthorizationHeader string
	Client              *http.Client
}

func (w *HTTPApprovalWebhook) DestinationURL() string { return w.URL }

func (w *HTTPApprovalWebhook) NotifyApproval(ctx context.Context, destination string, notification ApprovalNotification) error {
	payload, err := json.Marshal(notification)
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, destination, bytes.NewReader(payload))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	configured, _, configuredErr := NormalizeWebhookURL(w.URL)
	bound, _, boundErr := NormalizeWebhookURL(destination)
	if w.AuthorizationHeader != "" && configuredErr == nil && boundErr == nil && configured == bound {
		req.Header.Set("Authorization", w.AuthorizationHeader)
	}
	client := w.Client
	if client == nil {
		client = &http.Client{Timeout: 10 * time.Second}
	}
	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("approval webhook returned status %d", resp.StatusCode)
	}
	return nil
}
