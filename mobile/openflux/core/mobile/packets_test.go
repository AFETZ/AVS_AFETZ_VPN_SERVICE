package mobile

import (
	"bytes"
	"context"
	"encoding/binary"
	"testing"
)

func queryPacket() []byte {
	d := []byte{0x12, 0x34, 1, 0, 0, 1, 0, 0, 0, 0, 0, 0, 7, 'e', 'x', 'a', 'm', 'p', 'l', 'e', 3, 'c', 'o', 'm', 0, 0, 1, 0, 1}
	p := make([]byte, 28+len(d))
	p[0] = 0x45
	p[8] = 64
	p[9] = 17
	binary.BigEndian.PutUint16(p[2:4], uint16(len(p)))
	copy(p[12:16], []byte{10, 10, 10, 2})
	copy(p[16:20], []byte{1, 1, 1, 1})
	binary.BigEndian.PutUint16(p[20:22], 34567)
	binary.BigEndian.PutUint16(p[22:24], 53)
	binary.BigEndian.PutUint16(p[24:26], uint16(8+len(d)))
	copy(p[28:], d)
	binary.BigEndian.PutUint16(p[10:12], checksum(p[:20]))
	return p
}

func TestDNSFallbackPreservesQuestionAndAddresses(t *testing.T) {
	p := queryPacket()
	q := dnsTruncated(p, 20)
	if q == nil || checksum(q[:20]) != 0 {
		t.Fatal("invalid response checksum")
	}
	if !bytes.Equal(q[12:16], p[16:20]) || !bytes.Equal(q[16:20], p[12:16]) {
		t.Fatal("address reversal")
	}
	if !bytes.Equal(q[28:30], p[28:30]) || q[30]&0x82 != 0x82 || !bytes.Equal(q[40:], p[40:]) {
		t.Fatal("DNS ID, truncation, or question changed")
	}
	if binary.BigEndian.Uint16(q[20:22]) != 53 || binary.BigEndian.Uint16(q[22:24]) != 34567 {
		t.Fatal("ports")
	}
}

func TestMalformedDNSAndFragments(t *testing.T) {
	p := queryPacket()
	p[40] = 0xc0
	if dnsTruncated(p, 20) != nil {
		t.Fatal("compressed malformed question accepted")
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	c := &Client{ctx: ctx, incoming: make(chan []byte, 1)}
	p = queryPacket()
	p[6] = 0x20
	if err := c.SendPacket(p); err != nil || len(c.incoming) != 0 {
		t.Fatal("fragment accepted")
	}
	p = queryPacket()
	p[12] = 11
	c.SendPacket(p)
	if len(c.incoming) != 0 {
		t.Fatal("foreign source accepted")
	}
}

func TestCompressionInteroperabilityAndBounds(t *testing.T) {
	for _, p := range [][]byte{[]byte("small"), bytes.Repeat([]byte("tcp packet"), 400)} {
		q, e := decompress(compress(p))
		if e != nil || !bytes.Equal(p, q) {
			t.Fatal("roundtrip", e)
		}
	}
	if _, e := decompress(compress(bytes.Repeat([]byte{1}, 70000))); e == nil {
		t.Fatal("oversized frame accepted")
	}
}

func TestStopUnblocksReader(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	c := &Client{ctx: ctx, cancel: cancel, incoming: make(chan []byte)}
	c.Stop()
	c.Stop()
	if _, e := c.ReadPacket(); e == nil {
		t.Fatal("read did not stop")
	}
}

func TestURLValidation(t *testing.T) {
	for _, u := range []string{"", "https://disk.yandex.ru/", "https://disk.yandex.ru.evil.test/i/a", "https://user@disk.yandex.ru/i/a", "https://disk.yandex.ru:80/i/a", "https://disk.yandex.ru/i/a\n"} {
		if ValidateURL(u) {
			t.Fatal(u)
		}
	}
	if !ValidateURL("https://disk.yandex.ru/i/example") {
		t.Fatal("valid URL rejected")
	}
}
