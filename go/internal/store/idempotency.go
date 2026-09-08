package store

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"fmt"
	"time"
)

// IdempotencyStore records request/response mappings to avoid duplicate work.
type IdempotencyStore interface {
	TryBegin(ctx context.Context, key, scope, requestHash string) (Result, error)
	Complete(ctx context.Context, key, scope string, responseStatus int, responseBody []byte) error
	Retrieve(ctx context.Context, key, scope string) (Result, error)
}

// Result is the current idempotency record state.
type Result struct {
	Key            string
	Scope          string
	CreatedAt      time.Time
	CompletedAt    *time.Time
	RequestHash    string
	ResponseBody   []byte
	ResponseStatus int
	Locked         bool
}

// ErrKeyLocked is returned when another request is currently processing the
// same idempotency key.
var ErrKeyLocked = fmt.Errorf("idempotency key is locked")

// ErrScopeMismatch is returned when the same key is reused with a different scope.
var ErrScopeMismatch = fmt.Errorf("idempotency key scope mismatch")

// HashKey returns a stable SHA-256 hash for an idempotency key and scope.
func HashKey(key, scope string) string {
	h := sha256.New()
	fmt.Fprintf(h, "%s\x00%s", scope, key)
	return hex.EncodeToString(h.Sum(nil))
}

type idempotencyStore struct {
	db *sql.DB
}

func (s *idempotencyStore) TryBegin(ctx context.Context, key, scope, requestHash string) (Result, error) {
	keyHash := HashKey(key, scope)
	now := time.Now().UTC()

	tx, err := s.db.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelSerializable})
	if err != nil {
		return Result{}, fmt.Errorf("begin idempotency tx: %w", err)
	}
	defer tx.Rollback()

	var existing Result
	var createdAt, completedAt sql.NullString
	var responseBody string
	var locked int
	err = tx.QueryRowContext(ctx, `
		SELECT key_hash, scope, created_at, completed_at, request_hash, response_body, response_status, locked
		FROM idempotency_keys
		WHERE key_hash = ?
	`, keyHash).Scan(&existing.Key, &existing.Scope, &createdAt, &completedAt, &existing.RequestHash, &responseBody, &existing.ResponseStatus, &locked)

	switch {
	case err == sql.ErrNoRows:
		if _, err := tx.ExecContext(ctx, `
			INSERT INTO idempotency_keys (key_hash, scope, created_at, request_hash, response_body, response_status, locked)
			VALUES (?, ?, ?, ?, ?, ?, 1)
		`, keyHash, scope, now.Format(time.RFC3339), requestHash, "", 0); err != nil {
			return Result{}, fmt.Errorf("insert idempotency key: %w", err)
		}
		if err := tx.Commit(); err != nil {
			return Result{}, fmt.Errorf("commit idempotency begin: %w", err)
		}
		return Result{
			Key:         keyHash,
			Scope:       scope,
			CreatedAt:   now,
			RequestHash: requestHash,
			Locked:      true,
		}, nil

	case err != nil:
		return Result{}, fmt.Errorf("select idempotency key: %w", err)
	}

	existing.ResponseBody = []byte(responseBody)
	existing.Locked = locked == 1
	existing.CreatedAt, _ = time.Parse(time.RFC3339, createdAt.String)
	if completedAt.Valid {
		c, _ := time.Parse(time.RFC3339, completedAt.String)
		existing.CompletedAt = &c
	}

	if existing.Scope != scope {
		return Result{}, ErrScopeMismatch
	}
	if existing.Locked {
		return Result{}, ErrKeyLocked
	}
	if existing.RequestHash != requestHash {
		return Result{}, fmt.Errorf("idempotency key reused with different request")
	}

	if err := tx.Commit(); err != nil {
		return Result{}, fmt.Errorf("commit idempotency read: %w", err)
	}
	return existing, nil
}

func (s *idempotencyStore) Complete(ctx context.Context, key, scope string, responseStatus int, responseBody []byte) error {
	keyHash := HashKey(key, scope)
	now := time.Now().UTC()
	_, err := s.db.ExecContext(ctx, `
		UPDATE idempotency_keys
		SET completed_at = ?, response_status = ?, response_body = ?, locked = 0
		WHERE key_hash = ?
	`, now.Format(time.RFC3339), responseStatus, string(responseBody), keyHash)
	if err != nil {
		return fmt.Errorf("complete idempotency key: %w", err)
	}
	return nil
}

func (s *idempotencyStore) Retrieve(ctx context.Context, key, scope string) (Result, error) {
	keyHash := HashKey(key, scope)
	var r Result
	var createdAt, completedAt sql.NullString
	var responseBody string
	var locked int
	err := s.db.QueryRowContext(ctx, `
		SELECT key_hash, scope, created_at, completed_at, request_hash, response_body, response_status, locked
		FROM idempotency_keys
		WHERE key_hash = ?
	`, keyHash).Scan(&r.Key, &r.Scope, &createdAt, &completedAt, &r.RequestHash, &responseBody, &r.ResponseStatus, &locked)
	if err != nil {
		if err == sql.ErrNoRows {
			return Result{}, err
		}
		return Result{}, fmt.Errorf("retrieve idempotency key: %w", err)
	}
	r.ResponseBody = []byte(responseBody)
	r.Locked = locked == 1
	r.CreatedAt, _ = time.Parse(time.RFC3339, createdAt.String)
	if completedAt.Valid {
		c, _ := time.Parse(time.RFC3339, completedAt.String)
		r.CompletedAt = &c
	}
	return r, nil
}
