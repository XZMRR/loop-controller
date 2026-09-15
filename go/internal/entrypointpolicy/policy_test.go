package entrypointpolicy

import (
	"context"
	"net"
	"net/http"
	"net/netip"
	"strings"
	"sync"
	"testing"
)

type resolverFunc func(context.Context, string, string) ([]netip.Addr, error)

func (f resolverFunc) LookupNetIP(ctx context.Context, network, host string) ([]netip.Addr, error) {
	return f(ctx, network, host)
}

type recordingDialer struct {
	mu        sync.Mutex
	addresses []string
}

func (d *recordingDialer) DialContext(context.Context, string, string) (net.Conn, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.addresses = append(d.addresses, "called")
	return nil, net.ErrClosed
}

func TestStrictRejectsUnsafeEntrypoints(t *testing.T) {
	resolver := resolverFunc(func(_ context.Context, _, host string) ([]netip.Addr, error) {
		addresses := map[string][]netip.Addr{
			"private.test": {netip.MustParseAddr("10.0.0.1")},
			"ula.test":     {netip.MustParseAddr("fd00::1")},
			"link.test":    {netip.MustParseAddr("169.254.169.254")},
			"mapped.test":  {netip.MustParseAddr("::ffff:127.0.0.1")},
			"mixed.test":   {netip.MustParseAddr("8.8.8.8"), netip.MustParseAddr("127.0.0.1")},
		}
		return addresses[host], nil
	})
	policy := Policy{Resolver: resolver}
	for _, raw := range []string{
		"http://8.8.8.8", "https://user:pass@8.8.8.8", "https://8.8.8.8/#fragment",
		"https://127.0.0.1", "https://10.0.0.1", "https://169.254.169.254",
		"https://[::1]", "https://private.test", "https://ula.test", "https://link.test",
		"https://mapped.test", "https://mixed.test", "https://8.8.8.8:8443", "https://%31%32%37.0.0.1",
	} {
		t.Run(raw, func(t *testing.T) {
			if _, err := policy.Validate(context.Background(), raw); err == nil {
				t.Fatalf("accepted unsafe URL %q", raw)
			}
		})
	}
}

func TestDevelopmentMustBeExplicit(t *testing.T) {
	if _, err := Strict().Check("http://127.0.0.1:8080"); err == nil {
		t.Fatal("strict accepted HTTP")
	}
	if _, err := Development().Check("http://127.0.0.1:8080"); err != nil {
		t.Fatalf("explicit development rejected HTTP: %v", err)
	}
}

func TestValidatedClientPinsResolvedAddressAndRejectsRedirect(t *testing.T) {
	policy := Policy{Resolver: resolverFunc(func(context.Context, string, string) ([]netip.Addr, error) {
		return []netip.Addr{netip.MustParseAddr("8.8.8.8")}, nil
	})}
	approved, err := policy.Validate(context.Background(), "https://agent.example/path")
	if err != nil {
		t.Fatal(err)
	}
	dialer := &recordingDialer{}
	policy.Dialer = dialer
	client, err := policy.Client(nil, approved)
	if err != nil {
		t.Fatal(err)
	}
	if err := client.CheckRedirect(nil, nil); err == nil {
		t.Fatal("redirect was allowed")
	}
	transport := client.Transport.(*http.Transport)
	_, _ = transport.DialContext(context.Background(), "tcp", "agent.example:443")
	if len(dialer.addresses) != 1 {
		t.Fatal("pinned dialer was not used")
	}
	if approved.URL.Hostname() != "agent.example" || len(approved.IPs) != 1 || !strings.Contains(approved.IPs[0].String(), "8.8.8.8") {
		t.Fatalf("approval not pinned: %+v", approved)
	}
}
