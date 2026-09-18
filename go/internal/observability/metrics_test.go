package observability

import (
	"bytes"
	"strings"
	"sync"
	"testing"
)

func TestRegistryConcurrentLowCardinalityExposition(t *testing.T) {
	r := NewRegistry()
	var wg sync.WaitGroup
	for i := 0; i < 32; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := 0; j < 100; j++ {
				r.Add("assignment_claim_total", 1, "target_execution", "success")
				r.Set("sse_active_subscribers", float64(j))
			}
		}()
	}
	wg.Wait()
	var out bytes.Buffer
	if err := r.WritePrometheus(&out); err != nil {
		t.Fatal(err)
	}
	text := out.String()
	if !strings.Contains(text, `assignment_claim_total{kind="target_execution",result="success"} 3200`) {
		t.Fatalf("unexpected metrics: %s", text)
	}
	for _, forbidden := range []string{"task_id", "user_id", "agent_id", "tenant_id", "secret-canary"} {
		if strings.Contains(text, forbidden) {
			t.Errorf("forbidden label %s", forbidden)
		}
	}
}
