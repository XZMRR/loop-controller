package observability

import (
	"fmt"
	"io"
	"sort"
	"strings"
	"sync"
)

type metric struct {
	help   string
	kind   string
	labels []string
	values map[string]float64
}

type Registry struct {
	mu      sync.RWMutex
	metrics map[string]*metric
}

func NewRegistry() *Registry {
	r := &Registry{metrics: make(map[string]*metric)}
	for _, d := range []struct {
		name, help, kind string
		labels           []string
	}{
		{"scheduler_candidates_total", "Scheduling candidates evaluated.", "counter", []string{"result"}},
		{"scheduler_no_candidate_total", "Scheduling attempts with no candidate.", "counter", []string{"reason"}},
		{"assignment_claim_total", "Assignment claims.", "counter", []string{"kind", "result"}},
		{"assignment_lease_expired_total", "Expired assignment leases recovered.", "counter", []string{"kind"}},
		{"assignment_fence_rejected_total", "Assignment operations rejected by fencing.", "counter", []string{"kind", "operation"}},
		{"assignment_outcome_unknown_total", "Assignments ending with unknown outcome.", "counter", []string{"kind"}},
		{"assignment_dead_letter_total", "Assignments moved to dead letter.", "counter", []string{"kind", "reason"}},
		{"retry_scheduled_total", "Assignment retries scheduled.", "counter", []string{"kind"}},
		{"failover_total", "Assignment failovers.", "counter", []string{"kind", "result"}},
		{"discovery_stale_agents", "Agents unavailable due to stale discovery.", "gauge", nil},
		{"sse_active_subscribers", "Active SSE subscribers.", "gauge", nil},
		{"sse_slow_consumer_disconnect_total", "SSE slow consumer disconnects.", "counter", []string{"reason"}},
		{"sse_cursor_expired_total", "Expired SSE cursors rejected.", "counter", nil},
		{"assignment_queue_depth", "Current assignment queue depth.", "gauge", []string{"kind"}},
		{"assignment_queue_oldest_lag_seconds", "Age of the oldest due assignment.", "gauge", []string{"kind"}},
		{"assignment_recovery_errors_total", "Expired lease recovery failures.", "counter", []string{"kind"}},
		{"assignment_recovery_last_success_unixtime", "Last successful expired lease recovery.", "gauge", []string{"kind"}},
		{"discovery_refresh_errors_total", "Discovery refresh failures.", "counter", nil},
		{"discovery_last_success_unixtime", "Last successful discovery refresh.", "gauge", nil},
	} {
		r.metrics[d.name] = &metric{help: d.help, kind: d.kind, labels: d.labels, values: make(map[string]float64)}
	}
	return r
}

func (r *Registry) Add(name string, value float64, labels ...string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	m := r.metrics[name]
	if m == nil || m.kind != "counter" || len(labels) != len(m.labels) {
		return
	}
	m.values[strings.Join(labels, "\xff")] += value
}

func (r *Registry) Set(name string, value float64, labels ...string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	m := r.metrics[name]
	if m == nil || m.kind != "gauge" || len(labels) != len(m.labels) {
		return
	}
	m.values[strings.Join(labels, "\xff")] = value
}

func (r *Registry) WritePrometheus(w io.Writer) error {
	r.mu.RLock()
	defer r.mu.RUnlock()
	names := make([]string, 0, len(r.metrics))
	for name := range r.metrics {
		names = append(names, name)
	}
	sort.Strings(names)
	for _, name := range names {
		m := r.metrics[name]
		if _, err := fmt.Fprintf(w, "# HELP %s %s\n# TYPE %s %s\n", name, m.help, name, m.kind); err != nil {
			return err
		}
		keys := make([]string, 0, len(m.values))
		for key := range m.values {
			keys = append(keys, key)
		}
		if len(keys) == 0 && len(m.labels) == 0 {
			keys = append(keys, "")
		}
		sort.Strings(keys)
		for _, key := range keys {
			if _, err := io.WriteString(w, name); err != nil {
				return err
			}
			if len(m.labels) > 0 {
				values := strings.Split(key, "\xff")
				if _, err := io.WriteString(w, "{"); err != nil {
					return err
				}
				for i, label := range m.labels {
					if i > 0 {
						_, _ = io.WriteString(w, ",")
					}
					_, _ = fmt.Fprintf(w, `%s=%q`, label, values[i])
				}
				_, _ = io.WriteString(w, "}")
			}
			if _, err := fmt.Fprintf(w, " %g\n", m.values[key]); err != nil {
				return err
			}
		}
	}
	return nil
}

var Default = NewRegistry()
