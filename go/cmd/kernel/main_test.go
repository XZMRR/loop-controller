package main

import (
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"testing"
)

func TestDispatchEntrypointsEnabled(t *testing.T) {
	if !dispatchEntrypointsEnabled(false) {
		t.Fatal("production mode must enable HTTP entrypoint dispatch")
	}
	if dispatchEntrypointsEnabled(true) {
		t.Fatal("development mode must disable HTTP entrypoint dispatch")
	}
}

func TestDAGControllerDefaultAndDependencies(t *testing.T) {
	const name = "LC_DAG_CONTROLLER_ENABLED"
	t.Setenv(name, "")
	if envBoolDefault(name, false) {
		t.Fatal("DAG controller must default off")
	}
	if err := validateRuntimeFeatures(false, false, false); err != nil {
		t.Fatalf("default configuration must not require scheduler/worker: %v", err)
	}
	if err := validateRuntimeFeatures(false, true, true); err == nil {
		t.Fatal("DAG requires scheduler")
	}
	if err := validateRuntimeFeatures(true, true, false); err == nil {
		t.Fatal("DAG/scheduler require assignment worker")
	}
	if err := validateRuntimeFeatures(true, true, true); err != nil {
		t.Fatalf("explicit dependencies should be valid: %v", err)
	}
}

func TestEnvBool(t *testing.T) {
	const name = "LC_TEST_BOOL"
	defer os.Unsetenv(name)
	for _, value := range []string{"1", "true", "YES", "on"} {
		if err := os.Setenv(name, value); err != nil {
			t.Fatal(err)
		}
		if !envBool(name) {
			t.Errorf("envBool(%q) = false", value)
		}
	}
	if err := os.Setenv(name, "false"); err != nil {
		t.Fatal(err)
	}
	if envBool(name) {
		t.Fatal("envBool(false) = true")
	}
}

func TestResolveControlAuth(t *testing.T) {
	tests := []struct {
		name                                     string
		flagToken, envToken, flagOwner, envOwner string
		wantToken, wantOwner                     string
		development                              bool
		wantErr                                  bool
	}{
		{name: "production requires auth", wantErr: true},
		{name: "development disabled", development: true},
		{name: "environment", envToken: "token", envOwner: "agent-a", wantToken: "token", wantOwner: "agent-a"},
		{name: "flags override environment", flagToken: "flag-token", envToken: "env-token", flagOwner: "agent-a", envOwner: "agent-b", wantToken: "flag-token", wantOwner: "agent-a"},
		{name: "token without initiator", flagToken: "token", wantErr: true},
		{name: "initiator without token", flagOwner: "agent-a", wantErr: true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			token, owner, err := resolveControlAuth(tt.flagToken, tt.envToken, tt.flagOwner, tt.envOwner, tt.development)
			if (err != nil) != tt.wantErr {
				t.Fatalf("error = %v, wantErr %v", err, tt.wantErr)
			}
			if token != tt.wantToken || owner != tt.wantOwner {
				t.Fatalf("got (%q, %q), want (%q, %q)", token, owner, tt.wantToken, tt.wantOwner)
			}
		})
	}
}

func TestRequirePeerIdentityUsesErrorEnvelope(t *testing.T) {
	called := false
	handler := requirePeerIdentity("spiffe://example.test/control", http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		called = true
	}))

	for _, tc := range []struct {
		name string
		tls  *tls.ConnectionState
		code string
	}{
		{name: "missing", code: "client_identity_required"},
		{name: "mismatch", tls: &tls.ConnectionState{
			VerifiedChains:   [][]*x509.Certificate{{{}}},
			PeerCertificates: []*x509.Certificate{{URIs: []*url.URL{{Scheme: "spiffe", Host: "example.test", Path: "/other"}}}},
		}, code: "client_identity_mismatch"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			req := httptest.NewRequest(http.MethodGet, "/health", nil)
			req.TLS = tc.tls
			res := httptest.NewRecorder()
			handler.ServeHTTP(res, req)
			if res.Code != http.StatusForbidden || res.Header().Get("Content-Type") != "application/json" {
				t.Fatalf("status/content-type = %d/%q", res.Code, res.Header().Get("Content-Type"))
			}
			var envelope struct {
				ProtocolVersion string `json:"protocol_version"`
				Error           string `json:"error"`
				Code            string `json:"code"`
			}
			if err := json.Unmarshal(res.Body.Bytes(), &envelope); err != nil || envelope.ProtocolVersion != "0.54.0" || envelope.Error == "" || envelope.Code != tc.code {
				t.Fatalf("error envelope = %+v, err=%v", envelope, err)
			}
		})
	}
	if called {
		t.Fatal("protected handler called for invalid peer identity")
	}
}

func TestResolveTokenSecret(t *testing.T) {
	tests := []struct {
		name        string
		flagSecret  string
		envSecret   string
		development bool
		want        string
		wantErr     bool
	}{
		{name: "production requires secret", wantErr: true},
		{name: "production rejects default", envSecret: defaultTokenSecret, wantErr: true},
		{name: "production uses environment", envSecret: "production-secret", want: "production-secret"},
		{name: "flag takes precedence", flagSecret: "flag-secret", envSecret: "environment-secret", want: "flag-secret"},
		{name: "development permits default", development: true, want: defaultTokenSecret},
		{name: "development accepts configured secret", envSecret: "development-secret", development: true, want: "development-secret"},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, err := resolveTokenSecret(tt.flagSecret, tt.envSecret, tt.development)
			if (err != nil) != tt.wantErr {
				t.Fatalf("resolveTokenSecret() error = %v, wantErr %v", err, tt.wantErr)
			}
			if got != tt.want {
				t.Errorf("resolveTokenSecret() = %q, want %q", got, tt.want)
			}
		})
	}
}
