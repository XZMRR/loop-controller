// Command kernel runs the Loop Controller Go interaction governance kernel.
package main

import (
	"context"
	"errors"
	"flag"
	"log"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/loop-controller/go/internal/api"
	"github.com/loop-controller/go/internal/delegation"
	"github.com/loop-controller/go/internal/discovery"
	"github.com/loop-controller/go/internal/execution"
)

const defaultTokenSecret = "change-me-in-production"

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
	controlToken := flag.String("control-token", "", "A2A control-plane Bearer token (falls back to LC_A2A_CONTROL_TOKEN)")
	controlInitiator := flag.String("control-initiator", "", "initiator_agent_id bound to the control token (falls back to LC_A2A_CONTROL_INITIATOR)")
	agentControlTokens := make(tokenInitiatorPairs)
	flag.Var(
		&agentControlTokens,
		"agent-control-token",
		"extra control-plane token bound to an agent identity, repeatable as token=initiator_agent_id (env LC_A2A_AGENT_CONTROL_TOKENS, comma-separated)",
	)
	interactionAgentTokens := make(tokenInitiatorPairs)
	flag.Var(
		&interactionAgentTokens,
		"interaction-agent-token",
		"IIGE token bound to an agent identity, repeatable as token=initiator_agent_id (env LC_INTERACTION_AGENT_TOKENS, comma-separated)",
	)
	approvalToken := flag.String("approval-token", "", "independent approval Bearer token (falls back to LC_A2A_APPROVAL_TOKEN)")
	approverPrincipal := flag.String("approver-principal", "", "principal bound to the approval token (falls back to LC_A2A_APPROVER_PRINCIPAL)")
	executorURL := flag.String(
		"executor-url",
		"",
		"target tool executor base URL (falls back to LC_EXECUTOR_URL, then interaction URL)",
	)
	executorToken := flag.String(
		"executor-token",
		"",
		"target Python R2 Bearer token (falls back to LC_EXECUTOR_TOKEN, then interaction token)",
	)
	autoAcceptTarget := flag.Bool(
		"target-auto-accept",
		envBool("LC_TARGET_AUTO_ACCEPT"),
		"automatically accept target tasks after entrypoint delivery",
	)
	autoStartTarget := flag.Bool(
		"target-auto-start",
		envBool("LC_TARGET_AUTO_START"),
		"automatically accept and start target tasks after entrypoint delivery",
	)
	development := flag.Bool(
		"development",
		false,
		"enable insecure development defaults and disable HTTP entrypoint dispatch",
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
	executorBaseURL := *executorURL
	if executorBaseURL == "" {
		executorBaseURL = os.Getenv("LC_EXECUTOR_URL")
	}
	if executorBaseURL == "" {
		executorBaseURL = interactionBaseURL
	}
	executorBearerToken := *executorToken
	if executorBearerToken == "" {
		executorBearerToken = os.Getenv("LC_EXECUTOR_TOKEN")
	}
	if executorBearerToken == "" {
		executorBearerToken = interactionBearerToken
	}

	tokenSecret, err := resolveTokenSecret(*secret, os.Getenv("GO_KERNEL_TOKEN_SECRET"), *development)
	if err != nil {
		log.Fatal(err)
	}
	if *development && tokenSecret == defaultTokenSecret {
		log.Println("warning: development mode is using the insecure default HMAC secret")
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
	controlBearerToken, controlInitiatorID, err := resolveControlAuth(
		*controlToken, os.Getenv("LC_A2A_CONTROL_TOKEN"),
		*controlInitiator, os.Getenv("LC_A2A_CONTROL_INITIATOR"), *development,
	)
	if err != nil {
		log.Fatal(err)
	}
	srv.SetControlAuth(controlBearerToken, controlInitiatorID)
	srv.SetAgentControlTokens(mergeTokenPairs(agentControlTokens, parseTokenPairList(os.Getenv("LC_A2A_AGENT_CONTROL_TOKENS"))))
	approvalBearerToken, approverID, err := resolveApprovalAuth(*approvalToken, os.Getenv("LC_A2A_APPROVAL_TOKEN"), *approverPrincipal, os.Getenv("LC_A2A_APPROVER_PRINCIPAL"))
	if err != nil {
		log.Fatal(err)
	}
	srv.SetApprovalAuth(approvalBearerToken, approverID)
	if dispatchEntrypointsEnabled(*development) {
		srv.SetEntrypointClient(&delegation.HTTPEntrypointClient{
			Client: &http.Client{Timeout: 10 * time.Second},
		})
	}
	if executorBaseURL != "" {
		srv.SetTargetExecutor(&execution.HTTPExecutor{
			BaseURL:     executorBaseURL,
			BearerToken: executorBearerToken,
			Client:      &http.Client{Timeout: 30 * time.Second},
		})
	}
	srv.SetTargetTaskAutomation(*autoAcceptTarget, *autoStartTarget)

	if interactionBaseURL != "" {
		srv.SetR2Authorizer(&delegation.HTTPR2Authorizer{
			BaseURL:     interactionBaseURL,
			BearerToken: interactionBearerToken,
			Tokens:      invertTokenPairs(mergeTokenPairs(interactionAgentTokens, parseTokenPairList(os.Getenv("LC_INTERACTION_AGENT_TOKENS")))),
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

func dispatchEntrypointsEnabled(development bool) bool {
	return !development
}

// tokenInitiatorPairs collects repeatable token=initiator_agent_id flag values.
type tokenInitiatorPairs map[string]string

func (m tokenInitiatorPairs) String() string {
	parts := make([]string, 0, len(m))
	for token, initiator := range m {
		parts = append(parts, token+"="+initiator)
	}
	return strings.Join(parts, ",")
}

func (m tokenInitiatorPairs) Set(value string) error {
	token, initiator, ok := strings.Cut(strings.TrimSpace(value), "=")
	token = strings.TrimSpace(token)
	initiator = strings.TrimSpace(initiator)
	if !ok || token == "" || initiator == "" {
		return errors.New("expected token=initiator_agent_id")
	}
	m[token] = initiator
	return nil
}

// parseTokenPairList parses a comma-separated token=initiator_agent_id list
// (env var form) into tokenInitiatorPairs; malformed entries are skipped.
func parseTokenPairList(raw string) tokenInitiatorPairs {
	pairs := make(tokenInitiatorPairs)
	for _, entry := range strings.Split(raw, ",") {
		entry = strings.TrimSpace(entry)
		if entry == "" {
			continue
		}
		_ = pairs.Set(entry)
	}
	return pairs
}

func mergeTokenPairs(base, extra tokenInitiatorPairs) tokenInitiatorPairs {
	merged := make(tokenInitiatorPairs, len(base)+len(extra))
	for token, initiator := range base {
		merged[token] = initiator
	}
	for token, initiator := range extra {
		merged[token] = initiator
	}
	return merged
}

// invertTokenPairs swaps key/value so callers can look up by initiator.
func invertTokenPairs(pairs tokenInitiatorPairs) map[string]string {
	inverted := make(map[string]string, len(pairs))
	for token, initiator := range pairs {
		inverted[initiator] = token
	}
	return inverted
}

func resolveControlAuth(flagToken, envToken, flagInitiator, envInitiator string, development bool) (string, string, error) {
	controlToken := strings.TrimSpace(flagToken)
	if controlToken == "" {
		controlToken = strings.TrimSpace(envToken)
	}
	initiatorID := strings.TrimSpace(flagInitiator)
	if initiatorID == "" {
		initiatorID = strings.TrimSpace(envInitiator)
	}
	if (controlToken == "") != (initiatorID == "") {
		return "", "", errors.New("control token and initiator must be configured together")
	}
	if !development && controlToken == "" {
		return "", "", errors.New("A2A control authentication is required outside development mode; set LC_A2A_CONTROL_TOKEN and LC_A2A_CONTROL_INITIATOR")
	}
	return controlToken, initiatorID, nil
}

func resolveApprovalAuth(flagToken, envToken, flagPrincipal, envPrincipal string) (string, string, error) {
	token := strings.TrimSpace(flagToken)
	if token == "" {
		token = strings.TrimSpace(envToken)
	}
	principal := strings.TrimSpace(flagPrincipal)
	if principal == "" {
		principal = strings.TrimSpace(envPrincipal)
	}
	if (token == "") != (principal == "") {
		return "", "", errors.New("approval token and approver principal must be configured together")
	}
	return token, principal, nil
}

func envBool(name string) bool {
	switch strings.ToLower(strings.TrimSpace(os.Getenv(name))) {
	case "1", "true", "yes", "on":
		return true
	default:
		return false
	}
}

func resolveTokenSecret(flagSecret, envSecret string, development bool) (string, error) {
	secret := strings.TrimSpace(flagSecret)
	if secret == "" {
		secret = strings.TrimSpace(envSecret)
	}
	if secret == "" {
		if development {
			return defaultTokenSecret, nil
		}
		return "", errors.New("HMAC token secret is required outside development mode; set -secret or GO_KERNEL_TOKEN_SECRET")
	}
	if !development && secret == defaultTokenSecret {
		return "", errors.New("insecure default HMAC token secret is forbidden outside development mode")
	}
	return secret, nil
}
