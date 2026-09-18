package dag

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"time"

	"github.com/loop-controller/go/internal/store"
)

type Config struct {
	Owner               string
	PollInterval, Lease time.Duration
	BatchSize           int
	OnError             func(error)
}
type Controller struct {
	config Config
	store  *store.DAGStore
	cancel context.CancelFunc
	wg     sync.WaitGroup
}

func New(config Config, s *store.DAGStore) (*Controller, error) {
	if s == nil || config.Owner == "" {
		return nil, errors.New("dag controller requires owner and store")
	}
	if config.PollInterval <= 0 {
		config.PollInterval = 100 * time.Millisecond
	}
	if config.Lease <= 0 {
		config.Lease = 30 * time.Second
	}
	if config.BatchSize <= 0 {
		config.BatchSize = 32
	}
	return &Controller{config: config, store: s}, nil
}
func (c *Controller) Run(ctx context.Context) error {
	ctx, c.cancel = context.WithCancel(ctx)
	c.wg.Add(1)
	defer c.wg.Done()
	ticker := time.NewTicker(c.config.PollInterval)
	defer ticker.Stop()
	for {
		if err := c.RunOnce(ctx); err != nil && c.config.OnError != nil {
			c.config.OnError(err)
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-ticker.C:
		}
	}
}
func (c *Controller) RunOnce(ctx context.Context) error {
	items, err := c.store.ClaimWakeups(ctx, c.config.Owner, time.Now().UTC(), c.config.Lease, c.config.BatchSize)
	if err != nil {
		return err
	}
	for _, w := range items {
		n, e := c.store.GetNode(ctx, w.DAGID, w.NodeID)
		if e == nil {
			_, e = c.store.AdmitReadyNode(ctx, n.TenantID, w.DAGID, w.NodeID, n.Revision, time.Now().UTC())
		}
		if e == nil {
			e = c.store.AckWakeup(ctx, w)
		}
		if e != nil && c.config.OnError != nil {
			c.config.OnError(fmt.Errorf("admit DAG %s node %s: %w", w.DAGID, w.NodeID, e))
		}
	}
	return nil
}
func (c *Controller) Close() {
	if c.cancel != nil {
		c.cancel()
	}
}
func (c *Controller) Wait(ctx context.Context) error {
	done := make(chan struct{})
	go func() { c.wg.Wait(); close(done) }()
	select {
	case <-done:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}
