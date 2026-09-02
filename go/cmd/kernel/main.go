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
	r2URL := flag.String("r2-url", "", "Python R2 base URL (falls back to LC_R2_URL)")
	flag.Parse()

	path := *dbPath
	if path == "" {
		path = os.Getenv("LC_A2A_DB_PATH")
	}

	r2BaseURL := *r2URL
	if r2BaseURL == "" {
		r2BaseURL = os.Getenv("LC_R2_URL")
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

	if r2BaseURL != "" {
		srv.SetR2Authorizer(&delegation.HTTPR2Authorizer{
			BaseURL: r2BaseURL,
			Client:  &http.Client{Timeout: 10 * time.Second},
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
