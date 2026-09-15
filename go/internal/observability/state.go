package observability

import (
	"sync"
	"time"
)

type StateSnapshot struct {
	Started             bool       `json:"started"`
	Running             bool       `json:"running"`
	LastSuccessAt       *time.Time `json:"last_success_at,omitempty"`
	LastErrorAt         *time.Time `json:"last_error_at,omitempty"`
	LastError           string     `json:"last_error,omitempty"`
	LastRecoveryAt      *time.Time `json:"last_recovery_at,omitempty"`
	LastRecoveryErrorAt *time.Time `json:"last_recovery_error_at,omitempty"`
}

type State struct {
	mu sync.RWMutex
	s  StateSnapshot
}

func (s *State) Start() { s.mu.Lock(); s.s.Started, s.s.Running = true, true; s.mu.Unlock() }
func (s *State) Stop()  { s.mu.Lock(); s.s.Running = false; s.mu.Unlock() }
func (s *State) Success(now time.Time) {
	s.mu.Lock()
	n := now.UTC()
	s.s.LastSuccessAt = &n
	s.s.LastError = ""
	s.mu.Unlock()
}
func (s *State) Error(now time.Time) {
	s.mu.Lock()
	n := now.UTC()
	s.s.LastErrorAt = &n
	s.s.LastError = "operation_failed"
	s.mu.Unlock()
}
func (s *State) Recovery(now time.Time, err error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	n := now.UTC()
	if err == nil {
		s.s.LastRecoveryAt = &n
		return
	}
	s.s.LastRecoveryErrorAt = &n
}
func (s *State) Snapshot() StateSnapshot { s.mu.RLock(); defer s.mu.RUnlock(); return s.s }

type StateProvider interface{ Snapshot() StateSnapshot }
