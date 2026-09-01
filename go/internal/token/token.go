// Package token provides a minimal JWT-like HMAC token issuer for delegation.
package token

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"
)

// DelegationClaims holds the claims embedded in a delegation token.
type DelegationClaims struct {
	RequestID        string `json:"request_id"`
	InitiatorAgentID string `json:"initiator_agent_id"`
	TargetAgentID    string `json:"target_agent_id"`
	ToolName         string `json:"tool_name"`
	TaskID           string `json:"task_id"`
	ExpiresAt        int64  `json:"exp"`
}

// HMACIssuer issues and validates HMAC-SHA256 tokens.
type HMACIssuer struct {
	secret []byte
}

// NewHMACIssuer creates an issuer with the given secret.
func NewHMACIssuer(secret []byte) *HMACIssuer {
	return &HMACIssuer{secret: secret}
}

// Issue creates a token for the given claims. The token expires after ttl.
func (i *HMACIssuer) Issue(claims DelegationClaims, ttl time.Duration) (string, error) {
	if len(i.secret) == 0 {
		return "", errors.New("secret is empty")
	}
	claims.ExpiresAt = time.Now().UTC().Add(ttl).Unix()
	payload, err := json.Marshal(claims)
	if err != nil {
		return "", err
	}
	header := base64.RawURLEncoding.EncodeToString([]byte(`{"alg":"HS256","typ":"LC"}`))
	body := base64.RawURLEncoding.EncodeToString(payload)
	sig := sign(header+"."+body, i.secret)
	return header + "." + body + "." + sig, nil
}

// Validate parses and verifies a token.
func (i *HMACIssuer) Validate(token string) (DelegationClaims, error) {
	parts := strings.Split(token, ".")
	if len(parts) != 3 {
		return DelegationClaims{}, errors.New("invalid token format")
	}
	expectedSig := sign(parts[0]+"."+parts[1], i.secret)
	if !hmac.Equal([]byte(expectedSig), []byte(parts[2])) {
		return DelegationClaims{}, errors.New("invalid token signature")
	}
	payload, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		return DelegationClaims{}, fmt.Errorf("decode payload: %w", err)
	}
	var claims DelegationClaims
	if err := json.Unmarshal(payload, &claims); err != nil {
		return DelegationClaims{}, fmt.Errorf("parse claims: %w", err)
	}
	if claims.ExpiresAt < time.Now().UTC().Unix() {
		return DelegationClaims{}, errors.New("token expired")
	}
	return claims, nil
}

func sign(input string, secret []byte) string {
	mac := hmac.New(sha256.New, secret)
	mac.Write([]byte(input))
	return base64.RawURLEncoding.EncodeToString(mac.Sum(nil))
}
