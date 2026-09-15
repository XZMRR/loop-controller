package entrypointpolicy

import (
	"context"
	"crypto/tls"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/netip"
	"net/url"
	"strconv"
	"strings"
	"time"
)

var ErrRejected = errors.New("entrypoint URL rejected")

type Resolver interface {
	LookupNetIP(context.Context, string, string) ([]netip.Addr, error)
}

type Dialer interface {
	DialContext(context.Context, string, string) (net.Conn, error)
}

type Policy struct {
	Development  bool
	AllowedPorts map[uint16]bool
	Resolver     Resolver
	Dialer       Dialer
}

type Approved struct {
	URL *url.URL
	IPs []netip.Addr
}

func Strict() Policy      { return Policy{} }
func Development() Policy { return Policy{Development: true} }

func (p Policy) Check(raw string) (*url.URL, error) {
	if raw == "" || strings.ContainsAny(raw, "\r\n\t") || strings.Contains(raw, "\\") {
		return nil, rejected("invalid URL")
	}
	u, err := url.Parse(raw)
	if err != nil || u.Host == "" || u.Hostname() == "" || u.Opaque != "" {
		return nil, rejected("invalid URL")
	}
	if u.User != nil || u.Fragment != "" {
		return nil, rejected("userinfo and fragment are forbidden")
	}
	if u.Scheme != "https" && !(p.Development && u.Scheme == "http") {
		return nil, rejected("HTTPS is required")
	}
	host := u.Hostname()
	if strings.Contains(host, "%") || strings.HasSuffix(host, ".") {
		return nil, rejected("non-canonical hostname")
	}
	port := uint16(443)
	if u.Scheme == "http" {
		port = 80
	}
	if text := u.Port(); text != "" {
		n, err := strconv.ParseUint(text, 10, 16)
		if err != nil || n == 0 {
			return nil, rejected("invalid port")
		}
		port = uint16(n)
		if !p.Development && port != 443 && !p.AllowedPorts[port] {
			return nil, rejected("port is not allowed")
		}
	}
	copyURL := *u
	copyURL.Host = net.JoinHostPort(host, strconv.Itoa(int(port)))
	return &copyURL, nil
}

func (p Policy) ValidateRegistration(ctx context.Context, raw string) error {
	if p.Development {
		_, err := p.Check(raw)
		return err
	}
	_, err := p.Validate(ctx, raw)
	return err
}

func (p Policy) Validate(ctx context.Context, raw string) (Approved, error) {
	u, err := p.Check(raw)
	if err != nil {
		return Approved{}, err
	}
	host := u.Hostname()
	var ips []netip.Addr
	if ip, err := netip.ParseAddr(host); err == nil {
		ips = []netip.Addr{ip}
	} else {
		resolver := p.Resolver
		if resolver == nil {
			resolver = net.DefaultResolver
		}
		ips, err = resolver.LookupNetIP(ctx, "ip", host)
		if err != nil {
			return Approved{}, fmt.Errorf("%w: hostname resolution failed", ErrRejected)
		}
		if len(ips) == 0 {
			return Approved{}, rejected("hostname has no addresses")
		}
	}
	for _, ip := range ips {
		if !p.Development && forbidden(ip) {
			return Approved{}, rejected("hostname resolves to a forbidden address")
		}
	}
	return Approved{URL: u, IPs: append([]netip.Addr(nil), ips...)}, nil
}

func (p Policy) Client(base *http.Client, approved Approved) (*http.Client, error) {
	var transport *http.Transport
	if base == nil || base.Transport == nil {
		transport = http.DefaultTransport.(*http.Transport).Clone()
	} else {
		original, ok := base.Transport.(*http.Transport)
		if !ok {
			if p.Development {
				clone := *base
				clone.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
				return &clone, nil
			}
			return nil, errors.New("entrypoint client requires *http.Transport")
		}
		transport = original.Clone()
	}
	host := approved.URL.Hostname()
	port := approved.URL.Port()
	dialer := p.Dialer
	if dialer == nil {
		dialer = &net.Dialer{Timeout: 10 * time.Second, KeepAlive: 30 * time.Second}
	}
	transport.DialContext = func(ctx context.Context, network, _ string) (net.Conn, error) {
		var last error
		for _, ip := range approved.IPs {
			conn, err := dialer.DialContext(ctx, network, net.JoinHostPort(ip.String(), port))
			if err == nil {
				return conn, nil
			}
			last = err
		}
		return nil, fmt.Errorf("entrypoint connection failed: %w", last)
	}
	if approved.URL.Scheme == "https" {
		if transport.TLSClientConfig == nil {
			transport.TLSClientConfig = &tls.Config{ServerName: host, MinVersion: tls.VersionTLS12}
		} else {
			transport.TLSClientConfig = transport.TLSClientConfig.Clone()
			transport.TLSClientConfig.ServerName = host
		}
	}
	result := &http.Client{Transport: transport, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	if base != nil {
		result.Timeout = base.Timeout
		result.Jar = base.Jar
	}
	return result, nil
}

func forbidden(ip netip.Addr) bool {
	if !ip.IsValid() || ip.Is4In6() {
		return true
	}
	ip = ip.Unmap()
	if ip.IsLoopback() || ip.IsUnspecified() || ip.IsMulticast() || ip.IsLinkLocalUnicast() || ip.IsPrivate() {
		return true
	}
	blocked := []netip.Prefix{
		netip.MustParsePrefix("100.64.0.0/10"), netip.MustParsePrefix("169.254.0.0/16"),
		netip.MustParsePrefix("192.0.0.0/24"), netip.MustParsePrefix("192.0.2.0/24"),
		netip.MustParsePrefix("198.18.0.0/15"), netip.MustParsePrefix("198.51.100.0/24"),
		netip.MustParsePrefix("203.0.113.0/24"), netip.MustParsePrefix("224.0.0.0/4"),
		netip.MustParsePrefix("240.0.0.0/4"), netip.MustParsePrefix("2001:db8::/32"),
		netip.MustParsePrefix("fc00::/7"), netip.MustParsePrefix("fe80::/10"),
	}
	for _, prefix := range blocked {
		if prefix.Contains(ip) {
			return true
		}
	}
	return false
}

func rejected(reason string) error { return fmt.Errorf("%w: %s", ErrRejected, reason) }
