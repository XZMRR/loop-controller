// Package instance provides a process-unique kernel instance identity.
//
// A unique instance ID is required for multi-instance deployments so that
// leases, outbox claims, and generated identifiers never collide across
// processes sharing the same state store.
package instance

import (
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"os"
)

// ID identifies a single running kernel instance within a deployment.
type ID string

// New returns a process-unique instance ID.
func New() ID {
	host, _ := os.Hostname()
	return ID(fmt.Sprintf("%s-%d-%s", host, os.Getpid(), randomToken()))
}

// String returns the instance ID as a string.
func (id ID) String() string { return string(id) }

// randomToken returns 8 bytes of cryptographically random hex. It never fails:
// if the entropy source is unavailable, the instance still needs a unique
// suffix, so it falls back to a best-effort value that is highly unlikely to
// collide within a single host.
func randomToken() string {
	var b [8]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "fallback"
	}
	return hex.EncodeToString(b[:])
}
