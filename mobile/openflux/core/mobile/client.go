// Package mobile connects native VPN packet interfaces to an OpenFlux exit.
// Yandex wire protocol derived from p1neappleXpress/OpenFlux a8a8937 (GPL-3.0-or-later).
package mobile

import (
	"context"
	"errors"
	"net/url"
	"runtime/debug"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

type Client struct {
	ctx       context.Context
	cancel    context.CancelFunc
	outgoing  chan []byte
	incoming  chan []byte
	connected atomic.Bool
	received  atomic.Int64
	sent      atomic.Int64
	wg        sync.WaitGroup
	stopOnce  sync.Once
}

// ValidateURL accepts only a shared Yandex document, never arbitrary endpoints.
func ValidateURL(value string) bool {
	if len(value) > 2048 || strings.IndexFunc(value, func(r rune) bool { return r <= 32 }) >= 0 {
		return false
	}
	u, err := url.Parse(value)
	if err != nil || u.Scheme != "https" || u.User != nil || u.Path == "" || u.Path == "/" || (u.Port() != "" && u.Port() != "443") {
		return false
	}
	switch u.Hostname() {
	case "disk.yandex.ru", "disk.yandex.com", "docs.yandex.ru", "docs.yandex.com", "disk.360.yandex.ru":
		return true
	}
	return false
}

// Start returns immediately; the transport reconnects until Stop is called.
// Native callers must keep the transport's own sockets outside their VPN.
func Start(documentURL string) (*Client, error) {
	if !ValidateURL(documentURL) {
		return nil, errors.New("invalid Yandex document URL")
	}
	debug.SetMemoryLimit(32 << 20)
	ctx, cancel := context.WithCancel(context.Background())
	c := &Client{ctx: ctx, cancel: cancel, outgoing: make(chan []byte, 256), incoming: make(chan []byte, 256)}
	c.wg.Add(1)
	go func() { defer c.wg.Done(); c.run(documentURL) }()
	return c, nil
}

func (c *Client) Connected() bool      { return c.connected.Load() }
func (c *Client) ReceivedBytes() int64 { return c.received.Load() }
func (c *Client) SentBytes() int64     { return c.sent.Load() }

// Stop cancels dial/reconnect/read operations and unblocks ReadPacket.
func (c *Client) Stop() { c.stopOnce.Do(c.cancel); c.wg.Wait() }

func (c *Client) ReadPacket() ([]byte, error) {
	select {
	case <-c.ctx.Done():
		return nil, errors.New("stopped")
	case p := <-c.incoming:
		return p, nil
	}
}

func (c *Client) deliver(p []byte) {
	select {
	case <-c.ctx.Done():
	case c.incoming <- p:
	default:
	}
}

// SendPacket accepts one complete IPv4 packet from the OS's TUN interface.
// UDP DNS receives a truncated response so the system resolver retries over TCP.
// Other UDP receives an unreachable response to encourage TCP fallback (e.g. QUIC).
func (c *Client) SendPacket(packet []byte) error {
	if c.ctx.Err() != nil {
		return errors.New("stopped")
	}
	p, ihl, err := ipv4Packet(packet)
	if err != nil {
		return nil
	} // IPv6 and malformed packets are never sent outside the VPN.
	if p[12] != 10 || p[13] != 10 || p[14] != 10 || p[15] != 2 {
		return nil
	}
	if p[6]&0x3f != 0 || p[7] != 0 {
		return nil
	} // MTU 1280; fragmented packets unsupported.
	if p[9] == 17 {
		if reply := dnsTruncated(p, ihl); reply != nil {
			c.deliver(reply)
		} else {
			c.deliver(unreachable(p, ihl))
		}
		return nil
	}
	if p[9] != 6 || len(p) < ihl+20 {
		return nil
	}
	if !c.connected.Load() {
		return nil
	} // OS TCP retries after the transport reconnects.
	copyPacket := append([]byte(nil), p...)
	select {
	case <-c.ctx.Done():
		return errors.New("stopped")
	case c.outgoing <- copyPacket:
		c.sent.Add(int64(len(p)))
	default:
	}
	return nil
}

func (c *Client) receiveWire(data []byte) {
	p, err := decompress(data)
	if err != nil {
		return
	}
	p, _, err = ipv4Packet(p)
	if err != nil || p[9] != 6 || p[16] != 10 || p[17] != 10 || p[18] != 10 || p[19] != 2 {
		return
	}
	c.received.Add(int64(len(p)))
	c.deliver(append([]byte(nil), p...))
}

func (c *Client) run(documentURL string) {
	delay := 2 * time.Second
	for c.ctx.Err() == nil {
		started := time.Now()
		c.session(documentURL)
		c.connected.Store(false)
		// Never replay packets from a previous transport connection.
		for len(c.outgoing) > 0 {
			<-c.outgoing
		}
		if time.Since(started) > 30*time.Second {
			delay = 2 * time.Second
		}
		timer := time.NewTimer(delay)
		select {
		case <-c.ctx.Done():
			timer.Stop()
			return
		case <-timer.C:
		}
		if delay < 15*time.Second {
			delay *= 2
			if delay > 15*time.Second {
				delay = 15 * time.Second
			}
		}
	}
}
