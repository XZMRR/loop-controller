package store

import (
	"context"
	"database/sql"
	"testing"

	"github.com/loop-controller/go/internal/models"
)

func TestAgentStoreUpsertGetListDelete(t *testing.T) {
	db := openTestDB(t)
	as := db.AgentStore()
	ctx := context.Background()

	card := models.AgentCard{
		AgentID:      "agent-a",
		Name:         "Agent A",
		Description:  "desc",
		Entrypoint:   models.AgentEntrypoint{Type: "http", URL: "http://localhost:8080"},
		Capabilities: []string{"delegate_execution"},
		TrustDomain:  "example.com",
		Version:      "0.53.0",
	}
	if err := as.Upsert(ctx, card); err != nil {
		t.Fatalf("upsert: %v", err)
	}

	got, err := as.Get(ctx, "agent-a")
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	if got.Name != "Agent A" || got.Entrypoint.URL != "http://localhost:8080" {
		t.Fatalf("unexpected agent: %+v", got)
	}
	if len(got.Capabilities) != 1 || got.Capabilities[0] != "delegate_execution" {
		t.Fatalf("capabilities roundtrip mismatch: %+v", got.Capabilities)
	}

	// Upsert must be idempotent and replace existing fields.
	card.Name = "Agent A Updated"
	if err := as.Upsert(ctx, card); err != nil {
		t.Fatalf("upsert update: %v", err)
	}
	got, err = as.Get(ctx, "agent-a")
	if err != nil {
		t.Fatalf("get after update: %v", err)
	}
	if got.Name != "Agent A Updated" {
		t.Fatalf("expected updated name, got %q", got.Name)
	}

	list, err := as.List(ctx)
	if err != nil {
		t.Fatalf("list: %v", err)
	}
	if len(list) != 1 || list[0].AgentID != "agent-a" {
		t.Fatalf("unexpected list: %+v", list)
	}

	if err := as.Delete(ctx, "agent-a"); err != nil {
		t.Fatalf("delete: %v", err)
	}
	if _, err := as.Get(ctx, "agent-a"); err != sql.ErrNoRows {
		t.Fatalf("expected sql.ErrNoRows after delete, got %v", err)
	}
	if err := as.Delete(ctx, "agent-a"); err != sql.ErrNoRows {
		t.Fatalf("expected sql.ErrNoRows deleting missing agent, got %v", err)
	}
}
