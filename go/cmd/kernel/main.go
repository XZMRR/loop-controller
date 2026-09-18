// Command kernel runs the Loop Controller Go interaction governance kernel.
package main

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"flag"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/loop-controller/go/internal/api"
	"github.com/loop-controller/go/internal/assignmentworker"
	"github.com/loop-controller/go/internal/dag"
	"github.com/loop-controller/go/internal/delegation"
	"github.com/loop-controller/go/internal/discovery"
	"github.com/loop-controller/go/internal/execution"
	"github.com/loop-controller/go/internal/store"
)

const defaultTokenSecret = "change-me-in-production"

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

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
	controlTenant := flag.String("control-tenant", "", "tenant_id bound to the control token (falls back to LC_A2A_CONTROL_TENANT)")
	serverCert := flag.String("server-cert", "", "control API server certificate PEM (falls back to LC_A2A_SERVER_CERT)")
	serverKey := flag.String("server-key", "", "control API server private key PEM (falls back to LC_A2A_SERVER_KEY)")
	clientCA := flag.String("client-ca", "", "control API trusted client CA PEM (falls back to LC_A2A_CLIENT_CA)")
	expectedClientURI := flag.String("expected-client-uri", "", "expected client certificate URI SAN workload ID (falls back to LC_A2A_EXPECTED_CLIENT_URI)")
	allowHTTP := flag.Bool("allow-http", envBool("LC_A2A_ALLOW_HTTP"), "explicitly allow HTTP in development mode")
	approvalToken := flag.String("approval-token", "", "independent approval Bearer token (falls back to LC_A2A_APPROVAL_TOKEN)")
	approverPrincipal := flag.String("approver-principal", "", "principal bound to the approval token (falls back to LC_A2A_APPROVER_PRINCIPAL)")
	approverTenant := flag.String("approver-tenant", "", "tenant_id scope for approver (falls back to LC_A2A_APPROVER_TENANT)")
	approverGlobal := flag.Bool("approver-global", envBool("LC_A2A_APPROVER_GLOBAL"), "explicitly allow approver access across tenants")
	approvalWebhookURL := flag.String("approval-webhook-url", "", "approval webhook URL (falls back to LC_A2A_APPROVAL_WEBHOOK_URL)")
	approvalWebhookAuth := flag.String("approval-webhook-authorization", "", "approval webhook Authorization header (falls back to LC_A2A_APPROVAL_WEBHOOK_AUTHORIZATION)")
	executorURL := flag.String(
		"executor-url",
		"",
		"target tool executor base URL (falls back to LC_EXECUTOR_URL, then interaction URL)",
	)
	executorToken := flag.String("executor-token", "", "target Python R2 credential (falls back only to LC_EXECUTOR_TOKEN)")
	executorStrict := flag.Bool("executor-strict", envBool("LC_EXECUTOR_STRICT"), "require strict HTTPS/mTLS target execution")
	executorCA := flag.String("executor-ca", "", "executor CA PEM path (falls back to LC_EXECUTOR_CA)")
	executorCert := flag.String("executor-cert", "", "executor client certificate PEM path (falls back to LC_EXECUTOR_CERT)")
	executorKey := flag.String("executor-key", "", "executor client private key PEM path (falls back to LC_EXECUTOR_KEY)")
	executorAttester := flag.String("executor-expected-attester", "", "trusted receipt attester workload ID (falls back to LC_EXECUTOR_EXPECTED_ATTESTER)")
	executorReceiptExecutor := flag.String("executor-expected-receipt-executor", "", "trusted receipt executor identity (falls back to LC_EXECUTOR_EXPECTED_RECEIPT_EXECUTOR)")
	executorReceiptBackend := flag.String("executor-expected-receipt-backend", "", "trusted receipt backend identity (falls back to LC_EXECUTOR_EXPECTED_RECEIPT_BACKEND)")
	executorPeerPin := flag.String("executor-peer-cert-sha256", "", "SHA-256 pin of executor leaf certificate DER (falls back to LC_EXECUTOR_PEER_CERT_SHA256)")
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
	readyMaxDepth := flag.Int64("ready-max-queue-depth", envInt64("LC_READY_MAX_QUEUE_DEPTH", 10000), "maximum queue depth for readiness")
	readyMaxLag := flag.Duration("ready-max-queue-lag", envDuration("LC_READY_MAX_QUEUE_LAG", 5*time.Minute), "maximum oldest queue lag for readiness")
	readyRecoveryStale := flag.Duration("ready-max-recovery-staleness", envDuration("LC_READY_MAX_RECOVERY_STALENESS", 2*time.Minute), "maximum recovery staleness for readiness")
	readyDiscoveryStale := flag.Duration("ready-max-discovery-staleness", envDuration("LC_READY_MAX_DISCOVERY_STALENESS", 5*time.Minute), "maximum discovery staleness for readiness")
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
	expectedAttester := firstNonEmpty(*executorAttester, os.Getenv("LC_EXECUTOR_EXPECTED_ATTESTER"))
	expectedReceiptExecutor := firstNonEmpty(*executorReceiptExecutor, os.Getenv("LC_EXECUTOR_EXPECTED_RECEIPT_EXECUTOR"))
	expectedReceiptBackend := firstNonEmpty(*executorReceiptBackend, os.Getenv("LC_EXECUTOR_EXPECTED_RECEIPT_BACKEND"))
	expectedPeerPin := firstNonEmpty(*executorPeerPin, os.Getenv("LC_EXECUTOR_PEER_CERT_SHA256"))
	if *executorStrict && (executorBaseURL == "" || executorBearerToken == "" || expectedAttester == "" || expectedReceiptExecutor == "" || expectedReceiptBackend == "" || expectedPeerPin == "") {
		log.Print("strict executor requires independent URL/token, trusted receipt attester/executor/backend, and TLS peer certificate pin")
		return
	}

	tokenSecret, err := resolveTokenSecret(*secret, os.Getenv("GO_KERNEL_TOKEN_SECRET"), *development)
	if err != nil {
		log.Print(err)
		return
	}
	if *development && tokenSecret == defaultTokenSecret {
		log.Println("warning: development mode is using the insecure default HMAC secret")
	}

	var providers []discovery.AgentDiscoveryProvider
	if *discoveryFile != "" {
		providers = append(providers, discovery.NewStaticProvider(*discoveryFile))
	}

	controlBearerToken, controlInitiatorID, err := resolveControlAuth(
		*controlToken, os.Getenv("LC_A2A_CONTROL_TOKEN"),
		*controlInitiator, os.Getenv("LC_A2A_CONTROL_INITIATOR"), *development,
	)
	if err != nil {
		log.Print(err)
		return
	}
	controlTenantID := firstNonEmpty(*controlTenant, os.Getenv("LC_A2A_CONTROL_TENANT"))
	if !*development && controlTenantID == "" {
		log.Print("A2A control tenant binding is required outside development mode; set LC_A2A_CONTROL_TENANT")
		return
	}
	approvalBearerToken, approverID, err := resolveApprovalAuth(*approvalToken, os.Getenv("LC_A2A_APPROVAL_TOKEN"), *approverPrincipal, os.Getenv("LC_A2A_APPROVER_PRINCIPAL"))
	if err != nil {
		log.Print(err)
		return
	}
	approverTenantID := firstNonEmpty(*approverTenant, os.Getenv("LC_A2A_APPROVER_TENANT"))
	if approvalBearerToken != "" && approverTenantID == "" && !*approverGlobal && !*development {
		log.Print("approval authentication requires tenant scope or explicit global access outside development mode")
		return
	}
	controlTLS, err := buildControlTLSConfig(
		*development, *allowHTTP,
		firstNonEmpty(*serverCert, os.Getenv("LC_A2A_SERVER_CERT")),
		firstNonEmpty(*serverKey, os.Getenv("LC_A2A_SERVER_KEY")),
		firstNonEmpty(*clientCA, os.Getenv("LC_A2A_CLIENT_CA")),
		firstNonEmpty(*expectedClientURI, os.Getenv("LC_A2A_EXPECTED_CLIENT_URI")),
	)
	if err != nil {
		log.Print(err)
		return
	}
	webhookURL := strings.TrimSpace(*approvalWebhookURL)
	if webhookURL == "" {
		webhookURL = strings.TrimSpace(os.Getenv("LC_A2A_APPROVAL_WEBHOOK_URL"))
	}
	webhookAuth := strings.TrimSpace(*approvalWebhookAuth)
	if webhookAuth == "" {
		webhookAuth = strings.TrimSpace(os.Getenv("LC_A2A_APPROVAL_WEBHOOK_AUTHORIZATION"))
	}
	if webhookURL != "" {
		if _, _, err := store.NormalizeWebhookURL(webhookURL); err != nil {
			log.Print(err)
			return
		}
	}

	var executorClient *http.Client
	if executorBaseURL != "" {
		executorClient, err = buildExecutorClient(*executorStrict, firstNonEmpty(*executorCA, os.Getenv("LC_EXECUTOR_CA")), firstNonEmpty(*executorCert, os.Getenv("LC_EXECUTOR_CERT")), firstNonEmpty(*executorKey, os.Getenv("LC_EXECUTOR_KEY")))
		if err != nil {
			log.Print(err)
			return
		}
	}

	srv, err := api.NewServer([]byte(tokenSecret), path, providers...)
	if err != nil {
		log.Printf("failed to create server: %v", err)
		return
	}
	defer srv.Close()
	srv.SetControlAuthForTenant(controlBearerToken, controlInitiatorID, controlTenantID)
	srv.SetApprovalAuthForTenant(approvalBearerToken, approverID, approverTenantID, *approverGlobal || *development)
	if webhookURL != "" {
		srv.SetApprovalNotifier(&store.HTTPApprovalWebhook{URL: webhookURL, AuthorizationHeader: webhookAuth, Client: &http.Client{Timeout: 10 * time.Second}})
	}
	var entrypointClient delegation.EntrypointClient
	if dispatchEntrypointsEnabled(*development) {
		entrypointClient = &delegation.HTTPEntrypointClient{Client: &http.Client{Timeout: 10 * time.Second}}
		srv.SetEntrypointClient(entrypointClient)
	}
	var targetExecutor execution.TargetExecutor
	if executorBaseURL != "" {
		targetExecutor = &execution.HTTPExecutor{
			BaseURL: executorBaseURL, BearerToken: executorBearerToken, Client: executorClient, Strict: *executorStrict,
			ExpectedAttesterWorkloadID: expectedAttester, ExpectedReceiptExecutor: expectedReceiptExecutor,
			ExpectedReceiptBackend: expectedReceiptBackend, ExpectedPeerCertificateSHA256: expectedPeerPin,
		}
		srv.SetTargetExecutor(targetExecutor)
	}
	srv.SetTargetTaskAutomation(*autoAcceptTarget, *autoStartTarget)
	schedulerEnabled := envBool("LC_SCHEDULER_ENABLED")
	dagEnabled := envBoolDefault("LC_DAG_CONTROLLER_ENABLED", false)
	assignmentWorkerEnabled := envBool("LC_ASSIGNMENT_WORKER_ENABLED")
	if err := validateRuntimeFeatures(schedulerEnabled, dagEnabled, assignmentWorkerEnabled); err != nil {
		log.Print(err)
		return
	}
	if schedulerEnabled {
		srv.EnableScheduling()
	}
	if dagEnabled {
		srv.EnableDAG()
	}
	var assignmentWorker *assignmentworker.Worker
	var assignmentWorkerCancel context.CancelFunc
	if targetExecutor != nil && envBool("LC_ASSIGNMENT_WORKER_ENABLED") {
		assignmentWorker, err = assignmentworker.New(assignmentworker.Config{Owner: srv.Store().InstanceID()}, srv.Store().AssignmentStore(), srv.Store().ExecutionQueueStore(), srv.Store().TaskStore(), targetExecutor, srv.ExecutionLoader(), srv.SchedulingStore())
		if err != nil {
			log.Print(err)
			return
		}
		srv.EnableDurableAssignments(assignmentWorker)
		workerCtx, cancel := context.WithCancel(context.Background())
		assignmentWorkerCancel = cancel
		go func() {
			if runErr := assignmentWorker.Run(workerCtx); runErr != nil && !errors.Is(runErr, context.Canceled) {
				log.Printf("assignment worker failed: %v", runErr)
			}
		}()
	}
	var dagController *dag.Controller
	var dagControllerCancel context.CancelFunc
	if dagEnabled {
		dagController, err = dag.New(dag.Config{Owner: srv.Store().InstanceID(), OnError: func(err error) { log.Printf("dag controller failed: %v", err) }}, srv.Store().DAGStore())
		if err != nil {
			log.Print(err)
			return
		}
		controllerCtx, cancel := context.WithCancel(context.Background())
		dagControllerCancel = cancel
		go func() {
			if runErr := dagController.Run(controllerCtx); runErr != nil && !errors.Is(runErr, context.Canceled) {
				log.Printf("dag controller stopped: %v", runErr)
			}
		}()
	}
	var outboundWorker *assignmentworker.OutboundWorker
	var outboundWorkerCancel context.CancelFunc
	if envBool("LC_OUTBOUND_ASSIGNMENT_WORKER_ENABLED") {
		if entrypointClient == nil {
			log.Print("LC_OUTBOUND_ASSIGNMENT_WORKER_ENABLED requires an entrypoint client")
			return
		}
		var workerErr error
		outboundWorker, workerErr = assignmentworker.NewOutbound(assignmentworker.OutboundConfig{Owner: srv.Store().InstanceID(), OnError: func(err error) { log.Printf("outbound assignment dispatch failed: %v", err) }}, srv.Store().AssignmentStore(), srv.Store().DelegationDispatchOutboxStore(), entrypointClient, srv.SchedulingStore())
		if workerErr != nil {
			log.Print(workerErr)
			return
		}
		srv.EnableDurableOutboundDelegation()
		workerCtx, cancel := context.WithCancel(context.Background())
		outboundWorkerCancel = cancel
		go func() {
			if runErr := outboundWorker.Run(workerCtx); runErr != nil && !errors.Is(runErr, context.Canceled) {
				log.Printf("outbound assignment worker failed: %v", runErr)
			}
		}()
	}

	srv.SetWorkerStates(assignmentWorker, outboundWorker)
	srv.SetReadinessConfig(api.ReadinessConfig{
		Strict: !*development, MaxQueueDepth: *readyMaxDepth, MaxQueueOldestLag: *readyMaxLag,
		MaxRecoveryStaleness: *readyRecoveryStale, MaxDiscoveryStaleness: *readyDiscoveryStale,
		RequireAssignmentWorker: assignmentWorkerEnabled, RequireOutboundWorker: outboundWorker != nil,
		RequireSchedulableTarget:     schedulerEnabled || assignmentWorkerEnabled,
		RequiredSecurityCapabilities: []string{"scheduler_fencing_v1", "idempotency_v1", "workload_identity_v1"},
	})

	if interactionBaseURL != "" {
		authorizer := &delegation.HTTPR2Authorizer{
			BaseURL:     interactionBaseURL,
			BearerToken: interactionBearerToken,
			Client:      &http.Client{Timeout: 10 * time.Second},
		}
		if *executorStrict {
			authorizer.LifecycleBaseURL = executorBaseURL
			authorizer.LifecycleBearerToken = executorBearerToken
			authorizer.LifecycleClient = executorClient
			authorizer.StrictLifecycle = true
		}
		srv.SetR2Authorizer(authorizer)
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

	peerURI := firstNonEmpty(*expectedClientURI, os.Getenv("LC_A2A_EXPECTED_CLIENT_URI"))
	httpServer := &http.Server{Addr: *addr, Handler: requirePeerIdentity(peerURI, mux), TLSConfig: controlTLS}
	errCh := make(chan error, 1)
	go func() {
		if controlTLS != nil {
			errCh <- httpServer.ListenAndServeTLS(
				firstNonEmpty(*serverCert, os.Getenv("LC_A2A_SERVER_CERT")),
				firstNonEmpty(*serverKey, os.Getenv("LC_A2A_SERVER_KEY")),
			)
			return
		}
		errCh <- httpServer.ListenAndServe()
	}()
	log.Printf("Loop Controller A2A kernel listening on %s", *addr)
	select {
	case <-ctx.Done():
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		if dagControllerCancel != nil {
			dagControllerCancel()
			dagController.Close()
			if err := dagController.Wait(shutdownCtx); err != nil {
				log.Printf("dag controller shutdown failed: %v", err)
			}
		}
		if assignmentWorkerCancel != nil {
			assignmentWorkerCancel()
			assignmentWorker.Close()
			if err := assignmentWorker.Wait(shutdownCtx); err != nil {
				log.Printf("assignment worker shutdown failed: %v", err)
			}
		}
		if outboundWorkerCancel != nil {
			outboundWorkerCancel()
			outboundWorker.Close()
			if err := outboundWorker.Wait(shutdownCtx); err != nil {
				log.Printf("outbound assignment worker shutdown failed: %v", err)
			}
		}
		if err := httpServer.Shutdown(shutdownCtx); err != nil {
			log.Printf("server shutdown failed: %v", err)
		}
		if err := srv.Close(); err != nil {
			log.Printf("kernel shutdown failed: %v", err)
		}
		if err := <-errCh; err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Printf("server failed: %v", err)
		}
	case err := <-errCh:
		if err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Printf("server failed: %v", err)
		}
	}
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			return strings.TrimSpace(value)
		}
	}
	return ""
}

func buildControlTLSConfig(development, allowHTTP bool, certPath, keyPath, clientCAPath, expectedURI string) (*tls.Config, error) {
	if certPath == "" && keyPath == "" && clientCAPath == "" {
		if development && allowHTTP {
			return nil, nil
		}
		return nil, errors.New("control API HTTPS/mTLS is required; development HTTP requires explicit -allow-http")
	}
	if certPath == "" || keyPath == "" || clientCAPath == "" {
		return nil, errors.New("control API server certificate, key, and client CA must be configured together")
	}
	caPEM, err := os.ReadFile(clientCAPath)
	if err != nil {
		return nil, errors.New("control API client CA is not readable")
	}
	clientRoots := x509.NewCertPool()
	if !clientRoots.AppendCertsFromPEM(caPEM) {
		return nil, errors.New("control API client CA contains no certificates")
	}
	if _, err := tls.LoadX509KeyPair(certPath, keyPath); err != nil {
		return nil, errors.New("control API server certificate/key is invalid")
	}
	if !development && strings.TrimSpace(expectedURI) == "" {
		return nil, errors.New("expected client URI SAN workload ID is required outside development mode")
	}
	return &tls.Config{
		MinVersion: tls.VersionTLS12, ClientAuth: tls.RequireAndVerifyClientCert, ClientCAs: clientRoots,
	}, nil
}

func requirePeerIdentity(expectedURI string, next http.Handler) http.Handler {
	expected := strings.TrimSpace(expectedURI)
	if expected == "" {
		return next
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.TLS == nil || len(r.TLS.VerifiedChains) == 0 || len(r.TLS.PeerCertificates) == 0 {
			writePeerIdentityError(w, "client certificate identity is required", "client_identity_required")
			return
		}
		for _, uri := range r.TLS.PeerCertificates[0].URIs {
			if uri.String() == expected {
				next.ServeHTTP(w, r)
				return
			}
		}
		writePeerIdentityError(w, "client certificate identity mismatch", "client_identity_mismatch")
	})
}

func writePeerIdentityError(w http.ResponseWriter, message, code string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusForbidden)
	_, _ = w.Write([]byte(`{"protocol_version":"0.54.0","error":"` + message + `","code":"` + code + `"}`))
}

func buildExecutorClient(strict bool, caPath, certPath, keyPath string) (*http.Client, error) {
	if !strict {
		return &http.Client{Timeout: 30 * time.Second}, nil
	}
	if caPath == "" || certPath == "" || keyPath == "" {
		return nil, errors.New("strict executor requires LC_EXECUTOR_CA, LC_EXECUTOR_CERT, and LC_EXECUTOR_KEY")
	}
	caPEM, err := os.ReadFile(caPath)
	if err != nil {
		return nil, err
	}
	roots := x509.NewCertPool()
	if !roots.AppendCertsFromPEM(caPEM) {
		return nil, errors.New("executor CA contains no certificates")
	}
	certificate, err := tls.LoadX509KeyPair(certPath, keyPath)
	if err != nil {
		return nil, err
	}
	return &http.Client{Timeout: 30 * time.Second, Transport: &http.Transport{TLSClientConfig: &tls.Config{MinVersion: tls.VersionTLS12, RootCAs: roots, Certificates: []tls.Certificate{certificate}}}}, nil
}

func dispatchEntrypointsEnabled(development bool) bool {
	return !development
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

func validateRuntimeFeatures(schedulerEnabled, dagEnabled, assignmentWorkerEnabled bool) error {
	if (schedulerEnabled || dagEnabled) && !assignmentWorkerEnabled {
		return errors.New("LC_SCHEDULER_ENABLED and LC_DAG_CONTROLLER_ENABLED require LC_ASSIGNMENT_WORKER_ENABLED")
	}
	if dagEnabled && !schedulerEnabled {
		return errors.New("LC_DAG_CONTROLLER_ENABLED requires LC_SCHEDULER_ENABLED")
	}
	return nil
}

func envInt64(name string, fallback int64) int64 {
	value, err := strconv.ParseInt(strings.TrimSpace(os.Getenv(name)), 10, 64)
	if err != nil || value < 0 {
		return fallback
	}
	return value
}

func envDuration(name string, fallback time.Duration) time.Duration {
	value, err := time.ParseDuration(strings.TrimSpace(os.Getenv(name)))
	if err != nil || value <= 0 {
		return fallback
	}
	return value
}

func envBoolDefault(name string, fallback bool) bool {
	if strings.TrimSpace(os.Getenv(name)) == "" {
		return fallback
	}
	return envBool(name)
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
