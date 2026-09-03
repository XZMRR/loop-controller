// Package token provides a minimal JWT-like HMAC token issuer for delegation.
package token

import (
	"bytes"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
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
	TokenID          string `json:"jti"`
	IssuedAt         int64  `json:"iat"`
	Issuer           string `json:"iss"`
	Audience         string `json:"aud"`
	ArgumentsSHA256  string `json:"arguments_sha256"`
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
	now := time.Now().UTC()
	claims.IssuedAt = now.Unix()
	claims.ExpiresAt = now.Add(ttl).Unix()
	if claims.TokenID == "" {
		claims.TokenID = randomID()
	}
	if claims.Issuer == "" {
		claims.Issuer = "loop-controller-go-kernel"
	}
	if claims.Audience == "" {
		claims.Audience = claims.TargetAgentID
	}
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
	headerBytes, err := base64.RawURLEncoding.DecodeString(parts[0])
	if err != nil || string(headerBytes) != `{"alg":"HS256","typ":"LC"}` {
		return DelegationClaims{}, errors.New("invalid token header")
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
	if claims.ExpiresAt <= time.Now().UTC().Unix() {
		return DelegationClaims{}, errors.New("token expired")
	}
	if claims.TokenID == "" || claims.IssuedAt == 0 || claims.Issuer == "" || claims.Audience == "" {
		return DelegationClaims{}, errors.New("required token claims missing")
	}
	return claims, nil
}

// HashArguments returns the SHA-256 digest used to bind a token to arguments.
// Valid JSON is decoded and re-encoded deterministically; non-JSON bytes are
// hashed unchanged.
func HashArguments(arguments []byte) string {
	decoder := json.NewDecoder(bytes.NewReader(arguments))
	decoder.UseNumber()
	var value any
	if err := decoder.Decode(&value); err == nil {
		var trailing any
		if err := decoder.Decode(&trailing); errors.Is(err, io.EOF) {
			if canonical, err := json.Marshal(value); err == nil {
				arguments = canonical
			}
		}
	}
	sum := sha256.Sum256(arguments)
	return hex.EncodeToString(sum[:])
}

func randomID() string {
	var value [16]byte
	if _, err := rand.Read(value[:]); err != nil {
		return fmt.Sprintf("token-%d", time.Now().UTC().UnixNano())
	}
	return hex.EncodeToString(value[:])
}

func sign(input string, secret []byte) string {
	mac := hmac.New(sha256.New, secret)
	mac.Write([]byte(input))
	return base64.RawURLEncoding.EncodeToString(mac.Sum(nil))
}
