package main

import "testing"

func TestDispatchEntrypointsEnabled(t *testing.T) {
	if !dispatchEntrypointsEnabled(false) {
		t.Fatal("production mode must enable HTTP entrypoint dispatch")
	}
	if dispatchEntrypointsEnabled(true) {
		t.Fatal("development mode must disable HTTP entrypoint dispatch")
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
