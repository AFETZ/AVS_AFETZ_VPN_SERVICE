package mobile

import (
	"bytes"
	"encoding/binary"
	"errors"
	"github.com/pierrec/lz4/v4"
	"io"
)

func ipv4Packet(p []byte) ([]byte, int, error) {
	if len(p) < 20 || p[0]>>4 != 4 {
		return nil, 0, errors.New("not IPv4")
	}
	h := int(p[0]&15) * 4
	n := int(binary.BigEndian.Uint16(p[2:4]))
	if h < 20 || n < h || n > len(p) {
		return nil, 0, errors.New("invalid packet length")
	}
	return p[:n], h, nil
}

func checksum(p []byte) uint16 {
	var sum uint32
	for len(p) > 1 {
		sum += uint32(binary.BigEndian.Uint16(p))
		p = p[2:]
	}
	if len(p) > 0 {
		sum += uint32(p[0]) << 8
	}
	for sum>>16 != 0 {
		sum = (sum & 0xffff) + (sum >> 16)
	}
	return ^uint16(sum)
}

func replyHeader(original []byte, size int, protocol byte) []byte {
	p := make([]byte, size)
	p[0] = 0x45
	p[8] = 64
	p[9] = protocol
	binary.BigEndian.PutUint16(p[2:4], uint16(size))
	copy(p[12:16], original[16:20])
	copy(p[16:20], original[12:16])
	binary.BigEndian.PutUint16(p[10:12], checksum(p[:20]))
	return p
}

func dnsTruncated(p []byte, h int) []byte {
	if len(p) < h+20 || binary.BigEndian.Uint16(p[h+2:h+4]) != 53 {
		return nil
	}
	u := int(binary.BigEndian.Uint16(p[h+4 : h+6]))
	if u < 20 || h+u > len(p) {
		return nil
	}
	d := p[h+8 : h+u]
	if d[2]&0xf8 != 0 || binary.BigEndian.Uint16(d[4:6]) != 1 {
		return nil
	}
	// Echo exactly one uncompressed question; omit EDNS/additional records.
	end := 12
	for {
		if end >= len(d) {
			return nil
		}
		n := int(d[end])
		end++
		if n == 0 {
			break
		}
		if n > 63 || end+n > len(d) {
			return nil
		}
		end += n
	}
	end += 4
	if end > len(d) {
		return nil
	}
	response := append([]byte(nil), d[:end]...)
	response[2] = 0x82 | (d[2] & 1)
	response[3] = 0x80 | (d[3] & 0x10)
	for i := 6; i < 12; i++ {
		response[i] = 0
	}
	q := replyHeader(p, 28+len(response), 17)
	copy(q[20:22], p[h+2:h+4])
	copy(q[22:24], p[h:h+2])
	binary.BigEndian.PutUint16(q[24:26], uint16(8+len(response)))
	copy(q[28:], response) // A zero UDP checksum is valid for IPv4.
	return q
}

func unreachable(p []byte, h int) []byte {
	n := h + 8
	if n > len(p) {
		n = len(p)
	}
	q := replyHeader(p, 28+n, 1)
	q[20] = 3
	q[21] = 3
	copy(q[28:], p[:n])
	binary.BigEndian.PutUint16(q[22:24], checksum(q[20:]))
	return q
}

func compress(p []byte) []byte {
	plain := append([]byte{0}, p...)
	if len(p) <= 200 {
		return plain
	}
	var b bytes.Buffer
	b.WriteByte(0x1f)
	w := lz4.NewWriter(&b)
	_ = w.Apply(lz4.BlockSizeOption(lz4.Block64Kb))
	_, _ = w.Write(p)
	_ = w.Close()
	if b.Len() >= len(plain) {
		return plain
	}
	return b.Bytes()
}

func decompress(p []byte) ([]byte, error) {
	if len(p) < 2 {
		return nil, errors.New("empty frame")
	}
	if p[0] == 0 {
		return p[1:], nil
	}
	if p[0] != 0x1f {
		return nil, errors.New("unknown frame")
	}
	b, e := io.ReadAll(io.LimitReader(lz4.NewReader(bytes.NewReader(p[1:])), 65536))
	if len(b) > 65535 {
		return nil, errors.New("frame too large")
	}
	return b, e
}
