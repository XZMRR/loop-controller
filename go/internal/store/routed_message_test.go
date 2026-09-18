package store

import (
	"context"
	"testing"
	"time"

	"github.com/loop-controller/go/internal/models"
)

func TestRoutedMessageStoreSaveAndListByAgent(t *testing.T) {
	db := openTestDB(t)
	rms := db.RoutedMessageStore()
	ctx := context.Background()

	msg := models.Message{
		MessageID:       "msg-1",
		FromAgentID:     "agent-a",
		ToAgentID:       "agent-b",
		Role:            "user",
		Parts:           []models.Part{{Type: "text", Text: "hello"}},
		Timestamp:       time.Now().UTC(),
		ProtocolVersion: "0.53.0",
	}
	if err := rms.Save(ctx, msg); err != nil {
		t.Fatalf("save: %v", err)
	}

	for _, agentID := range []string{"agent-a", "agent-b"} {
		got, err := rms.ListByAgent(ctx, agentID)
		if err != nil {
			t.Fatalf("list by agent %s: %v", agentID, err)
		}
		if len(got) != 1 || got[0].MessageID != "msg-1" {
			t.Fatalf("unexpected messages for %s: %+v", agentID, got)
		}
		if len(got[0].Parts) != 1 || got[0].Parts[0].Text != "hello" {
			t.Fatalf("parts roundtrip mismatch: %+v", got[0].Parts)
		}
	}

	got, err := rms.ListByAgent(ctx, "agent-c")
	if err != nil {
		t.Fatalf("list by unrelated agent: %v", err)
	}
	if len(got) != 0 {
		t.Fatalf("expected no messages for agent-c, got %+v", got)
	}
}
