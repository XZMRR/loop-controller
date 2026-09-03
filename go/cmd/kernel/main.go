// Command kernel runs the Loop Controller Go interaction governance kernel.
package main

import (
	"context"
	"flag"
	"log"
	"net/http"
	"os"
	"time"

	"github.com/loop-controller/go/internal/api"
	"github.com/loop-controller/go/internal/delegation"
	"github.com/loop-controller/go/internal/discovery"
)

func main() {
	addr := flag.String("addr", ":8080", "listen address")
	secret := flag.String("secret", "", "HMAC token secret (falls back to GO_KERNEL_TOKEN_SECRET)")
	dbPath := flag.String("db", "", "SQLite database path (falls back to LC_A2A_DB_PATH, default ./data/a2a.db)")
	discoveryFile := flag.String("discovery-file", "", "optional JSON file with Agent Cards to auto-register")
	interactionURL := flag.String(
		"interaction-url",
		"",
		"Python IIGE base URL (falls back to LC_INTERACTION_URL, then LC_R2_URL)",
	)
	interactionToken := flag.String(
		"interaction-token",
		"",
		"IIGE Bearer token (falls back to LC_INTERACTION_TOKEN)",
	)
	flag.Parse()

	path := *dbPath
	if path == "" {
		path = os.Getenv("LC_A2A_DB_PATH")
	}

	interactionBaseURL := *interactionURL
	if interactionBaseURL == "" {
		interactionBaseURL = os.Getenv("LC_INTERACTION_URL")
	}
	if interactionBaseURL == "" {
		interactionBaseURL = os.Getenv("LC_R2_URL")
	}
	interactionBearerToken := *interactionToken
	if interactionBearerToken == "" {
		interactionBearerToken = os.Getenv("LC_INTERACTION_TOKEN")
	}

	tokenSecret := *secret
	if tokenSecret == "" {
		tokenSecret = os.Getenv("GO_KERNEL_TOKEN_SECRET")
	}
	if tokenSecret == "" {
		tokenSecret = "change-me-in-production"
		log.Println("warning: using default HMAC secret; set -secret or GO_KERNEL_TOKEN_SECRET in production")
	}

	var providers []discovery.AgentDiscoveryProvider
	if *discoveryFile != "" {
		providers = append(providers, discovery.NewStaticProvider(*discoveryFile))
	}

	srv, err := api.NewServer([]byte(tokenSecret), path, providers...)
	if err != nil {
		log.Fatalf("failed to create server: %v", err)
	}
	defer srv.Close()

	if interactionBaseURL != "" {
		srv.SetR2Authorizer(&delegation.HTTPR2Authorizer{
			BaseURL:     interactionBaseURL,
			BearerToken: interactionBearerToken,
			Client:      &http.Client{Timeout: 10 * time.Second},
		})
	}

	if *discoveryFile != "" {
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		if err := srv.SyncDiscovery(ctx); err != nil {
			log.Printf("discovery sync failed: %v", err)
		}
	}

	mux := http.NewServeMux()
	srv.RegisterRoutes(mux)

	log.Printf("Loop Controller A2A kernel listening on %s", *addr)
	if err := http.ListenAndServe(*addr, mux); err != nil {
		log.Fatalf("server failed: %v", err)
	}
}
