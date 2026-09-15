package store

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"database/sql"
	"encoding/base64"
	"encoding/binary"
	"encoding/hex"
	"errors"
	"fmt"
	"strings"
	"sync/atomic"
	"time"

	"github.com/loop-controller/go/internal/models"
)

var (
	ErrEventCursorInvalid = errors.New("event_cursor_invalid")
	ErrEventCursorExpired = errors.New("event_cursor_expired")
	ErrEventCursorFuture  = errors.New("event_cursor_future")
)

type CursorError struct {
	Kind error
}

func (e *CursorError) Error() string { return e.Kind.Error() }
func (e *CursorError) Unwrap() error { return e.Kind }

type TerminalEventAudit struct {
	TaskID               string
	TerminalStatus       string
	Outcome              []byte
	ErrorCode            string
	BudgetTokenCount     int64
	BudgetPaymentAmount  float64
	BudgetCurrency       string
	TerminalSequence     int64
	TerminalEventType    string
	TerminalEventPayload []byte
	TerminalEventAt      time.Time
	RecordedAt           time.Time
}

// EventStore persists and queries task events for SSE delivery.
type EventStore interface {
	Append(ctx context.Context, ev models.TaskEvent) error
	ListPending(ctx context.Context, taskID string) ([]models.TaskEvent, error)
	ListAfter(ctx context.Context, taskID, cursor string) ([]models.TaskEvent, error)
	Cursor(ctx context.Context, ev models.TaskEvent) (string, error)
	Compact(ctx context.Context, taskID string, retainFrom int64) (TerminalEventAudit, error)
	MarkPublished(ctx context.Context, eventIDs []string) error
}

var eventIDSequence atomic.Uint64

func newEventID(owner, task string, now time.Time) string {
	var random [16]byte
	if _, err := rand.Read(random[:]); err == nil {
		return fmt.Sprintf("ev-%s-%s-%d-%s", owner, task, now.UnixNano(), hex.EncodeToString(random[:]))
	}
	return fmt.Sprintf("ev-%s-%s-%d-%d", owner, task, now.UnixNano(), eventIDSequence.Add(1))
}

type eventStore struct {
	db *sql.DB
}

type eventExecer interface {
	ExecContext(context.Context, string, ...any) (sql.Result, error)
	QueryRowContext(context.Context, string, ...any) *sql.Row
}

func appendEvent(ctx context.Context, db eventExecer, ev *models.TaskEvent) error {
	if ev.EventID == "" {
		return fmt.Errorf("event_id is required")
	}
	if ev.TaskID == "" {
		return fmt.Errorf("task_id is required")
	}
	if ev.SchemaVersion == 0 {
		ev.SchemaVersion = models.TaskEventSchemaVersion
	}
	if ev.PublishedAt.IsZero() {
		ev.PublishedAt = time.Now().UTC()
	}
	if _, err := db.ExecContext(ctx, `INSERT OR IGNORE INTO event_retention(task_id,lower_sequence,next_sequence) VALUES(?,1,1)`, ev.TaskID); err != nil {
		return fmt.Errorf("initialize event sequence: %w", err)
	}
	if err := db.QueryRowContext(ctx, `UPDATE event_retention SET next_sequence=next_sequence+1 WHERE task_id=? RETURNING next_sequence-1`, ev.TaskID).Scan(&ev.Sequence); err != nil {
		return fmt.Errorf("allocate event sequence: %w", err)
	}
	published := 0
	if ev.Published {
		published = 1
	}
	_, err := db.ExecContext(ctx, `INSERT INTO events(event_id,task_id,event_type,payload_json,published_at,published,sequence,schema_version) VALUES(?,?,?,?,?,?,?,?)`, ev.EventID, ev.TaskID, ev.EventType, string(ev.Payload), ev.PublishedAt.UTC().Format(time.RFC3339Nano), published, ev.Sequence, ev.SchemaVersion)
	if err != nil {
		return fmt.Errorf("insert event: %w", err)
	}
	return nil
}

func (s *eventStore) Append(ctx context.Context, ev models.TaskEvent) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("begin event append: %w", err)
	}
	defer tx.Rollback()
	if err := appendEvent(ctx, tx, &ev); err != nil {
		return err
	}
	if err := tx.Commit(); err != nil {
		return fmt.Errorf("commit event append: %w", err)
	}
	return nil
}

const eventColumns = `event_id,task_id,event_type,payload_json,published_at,published,sequence,schema_version`

func (s *eventStore) ListPending(ctx context.Context, taskID string) ([]models.TaskEvent, error) {
	rows, err := s.db.QueryContext(ctx, `SELECT `+eventColumns+` FROM events WHERE task_id=? AND published=0 ORDER BY sequence`, taskID)
	if err != nil {
		return nil, fmt.Errorf("list pending events: %w", err)
	}
	defer rows.Close()
	return scanEvents(rows)
}

func (s *eventStore) ListAfter(ctx context.Context, taskID, cursor string) ([]models.TaskEvent, error) {
	sequence, err := s.validateCursor(ctx, taskID, cursor)
	if err != nil {
		return nil, err
	}
	rows, err := s.db.QueryContext(ctx, `SELECT `+eventColumns+` FROM events WHERE task_id=? AND sequence>? ORDER BY sequence`, taskID, sequence)
	if err != nil {
		return nil, fmt.Errorf("list task events: %w", err)
	}
	defer rows.Close()
	events, err := scanEvents(rows)
	if err != nil {
		return nil, err
	}
	for i := range events {
		events[i].Cursor, err = EncodeEventCursor(taskID, events[i].Sequence, events[i].SchemaVersion)
		if err != nil {
			return nil, err
		}
	}
	return events, nil
}

type EventCursor struct {
	Sequence      int64
	SchemaVersion int
	digest        [16]byte
}

func cursorDigest(taskID string, sequence int64, schemaVersion int) [16]byte {
	var numbers [12]byte
	binary.BigEndian.PutUint64(numbers[:8], uint64(sequence))
	binary.BigEndian.PutUint32(numbers[8:], uint32(schemaVersion))
	h := sha256.New()
	h.Write([]byte("loop-controller:event-cursor:v1\x00"))
	h.Write([]byte(taskID))
	h.Write([]byte{0})
	h.Write(numbers[:])
	var out [16]byte
	copy(out[:], h.Sum(nil))
	return out
}

func EncodeEventCursor(taskID string, sequence int64, schemaVersion int) (string, error) {
	if taskID == "" || sequence <= 0 || schemaVersion <= 0 {
		return "", &CursorError{Kind: ErrEventCursorInvalid}
	}
	payload := make([]byte, 29)
	payload[0] = 1
	binary.BigEndian.PutUint64(payload[1:9], uint64(sequence))
	binary.BigEndian.PutUint32(payload[9:13], uint32(schemaVersion))
	digest := cursorDigest(taskID, sequence, schemaVersion)
	copy(payload[13:], digest[:])
	return base64.RawURLEncoding.EncodeToString(payload), nil
}

func DecodeEventCursor(encoded string) (EventCursor, error) {
	decoded, err := base64.RawURLEncoding.DecodeString(encoded)
	if err != nil || len(decoded) != 29 || decoded[0] != 1 {
		return EventCursor{}, &CursorError{Kind: ErrEventCursorInvalid}
	}
	cursor := EventCursor{
		Sequence:      int64(binary.BigEndian.Uint64(decoded[1:9])),
		SchemaVersion: int(binary.BigEndian.Uint32(decoded[9:13])),
	}
	copy(cursor.digest[:], decoded[13:])
	if cursor.Sequence <= 0 || cursor.SchemaVersion <= 0 {
		return EventCursor{}, &CursorError{Kind: ErrEventCursorInvalid}
	}
	return cursor, nil
}

func (s *eventStore) Cursor(ctx context.Context, ev models.TaskEvent) (string, error) {
	if ev.TaskID == "" || ev.Sequence <= 0 {
		return "", &CursorError{Kind: ErrEventCursorInvalid}
	}
	var schemaVersion int
	if err := s.db.QueryRowContext(ctx, `SELECT schema_version FROM events WHERE task_id=? AND sequence=?`, ev.TaskID, ev.Sequence).Scan(&schemaVersion); err != nil {
		return "", &CursorError{Kind: ErrEventCursorInvalid}
	}
	return EncodeEventCursor(ev.TaskID, ev.Sequence, schemaVersion)
}

func (s *eventStore) validateCursor(ctx context.Context, taskID, encoded string) (int64, error) {
	if encoded == "" {
		return 0, nil
	}
	var lower, next int64
	if err := s.db.QueryRowContext(ctx, `SELECT lower_sequence,next_sequence FROM event_retention WHERE task_id=?`, taskID).Scan(&lower, &next); err != nil {
		return 0, &CursorError{Kind: ErrEventCursorInvalid}
	}
	cursor, decodeErr := DecodeEventCursor(encoded)
	if decodeErr == nil {
		digest := cursorDigest(taskID, cursor.Sequence, cursor.SchemaVersion)
		if !equalBytes(cursor.digest[:], digest[:]) {
			return 0, &CursorError{Kind: ErrEventCursorInvalid}
		}
		if cursor.Sequence >= next {
			return 0, &CursorError{Kind: ErrEventCursorFuture}
		}
		if cursor.Sequence < lower {
			return 0, &CursorError{Kind: ErrEventCursorExpired}
		}
		var schemaVersion int
		if err := s.db.QueryRowContext(ctx, `SELECT schema_version FROM events WHERE task_id=? AND sequence=?`, taskID, cursor.Sequence).Scan(&schemaVersion); err != nil {
			return 0, &CursorError{Kind: ErrEventCursorInvalid}
		}
		if schemaVersion != cursor.SchemaVersion {
			return 0, &CursorError{Kind: ErrEventCursorInvalid}
		}
		return cursor.Sequence, nil
	}
	// Controlled compatibility with legacy event_id cursors. It is accepted only
	// while that exact event remains retained for this task.
	var sequence int64
	if err := s.db.QueryRowContext(ctx, `SELECT sequence FROM events WHERE task_id=? AND event_id=?`, taskID, encoded).Scan(&sequence); err != nil {
		return 0, &CursorError{Kind: ErrEventCursorInvalid}
	}
	return sequence, nil
}

func equalBytes(a, b []byte) bool {
	if len(a) != len(b) {
		return false
	}
	var diff byte
	for i := range a {
		diff |= a[i] ^ b[i]
	}
	return diff == 0
}

func (s *eventStore) Compact(ctx context.Context, taskID string, retainFrom int64) (TerminalEventAudit, error) {
	if retainFrom < 1 {
		return TerminalEventAudit{}, fmt.Errorf("retainFrom must be positive")
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return TerminalEventAudit{}, err
	}
	defer tx.Rollback()
	var lower, next int64
	if err := tx.QueryRowContext(ctx, `SELECT lower_sequence,next_sequence FROM event_retention WHERE task_id=?`, taskID).Scan(&lower, &next); err != nil {
		return TerminalEventAudit{}, err
	}
	if retainFrom > next {
		return TerminalEventAudit{}, &CursorError{Kind: ErrEventCursorFuture}
	}
	var audit TerminalEventAudit
	var outcome, errorCode sql.NullString
	if err := tx.QueryRowContext(ctx, `SELECT task_id,status,outcome,error_code,budget_token_count,budget_payment_amount,budget_currency FROM tasks WHERE task_id=?`, taskID).Scan(&audit.TaskID, &audit.TerminalStatus, &outcome, &errorCode, &audit.BudgetTokenCount, &audit.BudgetPaymentAmount, &audit.BudgetCurrency); err != nil {
		return TerminalEventAudit{}, err
	}
	if audit.TerminalStatus == "completed" || audit.TerminalStatus == "failed" || audit.TerminalStatus == "cancelled" {
		audit.Outcome = []byte(outcome.String)
		audit.ErrorCode = errorCode.String
		audit.TerminalSequence = next - 1
		var eventPayload, eventAt string
		if err := tx.QueryRowContext(ctx, `SELECT event_type,payload_json,published_at FROM events WHERE task_id=? AND sequence=?`, taskID, audit.TerminalSequence).Scan(&audit.TerminalEventType, &eventPayload, &eventAt); err != nil {
			return TerminalEventAudit{}, err
		}
		audit.TerminalEventPayload = []byte(eventPayload)
		if audit.TerminalEventAt, err = time.Parse(time.RFC3339Nano, eventAt); err != nil {
			return TerminalEventAudit{}, err
		}
		audit.RecordedAt = time.Now().UTC()
		if _, err := tx.ExecContext(ctx, `INSERT INTO terminal_event_audit(task_id,terminal_status,outcome_json,error_code,budget_token_count,budget_payment_amount,budget_currency,terminal_sequence,terminal_event_type,terminal_event_payload,terminal_event_at,recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET terminal_status=excluded.terminal_status,outcome_json=excluded.outcome_json,error_code=excluded.error_code,budget_token_count=excluded.budget_token_count,budget_payment_amount=excluded.budget_payment_amount,budget_currency=excluded.budget_currency,terminal_sequence=excluded.terminal_sequence,terminal_event_type=excluded.terminal_event_type,terminal_event_payload=excluded.terminal_event_payload,terminal_event_at=excluded.terminal_event_at,recorded_at=excluded.recorded_at`, taskID, audit.TerminalStatus, outcome, audit.ErrorCode, audit.BudgetTokenCount, audit.BudgetPaymentAmount, audit.BudgetCurrency, audit.TerminalSequence, audit.TerminalEventType, eventPayload, eventAt, audit.RecordedAt.Format(time.RFC3339Nano)); err != nil {
			return TerminalEventAudit{}, err
		}
	}
	if _, err := tx.ExecContext(ctx, `DELETE FROM events WHERE task_id=? AND sequence<?`, taskID, retainFrom); err != nil {
		return TerminalEventAudit{}, err
	}
	if retainFrom > lower {
		if _, err := tx.ExecContext(ctx, `UPDATE event_retention SET lower_sequence=?,compacted_at=? WHERE task_id=?`, retainFrom, time.Now().UTC().Format(time.RFC3339Nano), taskID); err != nil {
			return TerminalEventAudit{}, err
		}
	}
	if err := tx.Commit(); err != nil {
		return TerminalEventAudit{}, err
	}
	return audit, nil
}

func (s *eventStore) MarkPublished(ctx context.Context, eventIDs []string) error {
	if len(eventIDs) == 0 {
		return nil
	}
	placeholders := make([]string, len(eventIDs))
	args := make([]any, len(eventIDs))
	for i, id := range eventIDs {
		placeholders[i], args[i] = "?", id
	}
	_, err := s.db.ExecContext(ctx, `UPDATE events SET published=1 WHERE event_id IN (`+strings.Join(placeholders, ",")+`)`, args...)
	if err != nil {
		return fmt.Errorf("mark events published: %w", err)
	}
	return nil
}

func scanEvents(rows *sql.Rows) ([]models.TaskEvent, error) {
	var out []models.TaskEvent
	for rows.Next() {
		var ev models.TaskEvent
		var ts, payload string
		var published int
		if err := rows.Scan(&ev.EventID, &ev.TaskID, &ev.EventType, &payload, &ts, &published, &ev.Sequence, &ev.SchemaVersion); err != nil {
			return nil, fmt.Errorf("scan event: %w", err)
		}
		t, err := time.Parse(time.RFC3339Nano, ts)
		if err != nil {
			return nil, fmt.Errorf("parse event timestamp: %w", err)
		}
		ev.ProtocolVersion = models.CurrentProtocolVersion
		ev.PublishedAt, ev.Payload, ev.Published = t, []byte(payload), published == 1
		out = append(out, ev)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate events: %w", err)
	}
	return out, nil
}
