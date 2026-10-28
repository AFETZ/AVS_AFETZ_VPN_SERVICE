package mobile

import (
	"crypto/rand"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"github.com/gorilla/websocket"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"sync"
	"time"
)

var configPattern = regexp.MustCompile(`(?s)<script[^>]*id="client-config"[^>]*>(.*?)</script>`)
var cursorPattern = regexp.MustCompile(`"cursor":"[^;]+;([^"]+)"`)
var changesPattern = regexp.MustCompile(`"excelAdditionalInfo":"([^"]+)"`)

type documentConfig struct {
	Office struct {
		Balancer string `json:"balancer_url"`
		Editor   struct {
			Token    string `json:"token"`
			Document struct {
				Key         string                 `json:"key"`
				Type        string                 `json:"fileType"`
				URL         string                 `json:"url"`
				Title       string                 `json:"title"`
				Permissions map[string]interface{} `json:"permissions"`
			} `json:"document"`
		} `json:"editor_config"`
	} `json:"officeActionData"`
}

func (c *Client) session(documentURL string) {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	defer transport.CloseIdleConnections()
	httpClient := &http.Client{Transport: transport, Timeout: 25 * time.Second}
	req, err := http.NewRequestWithContext(c.ctx, "GET", documentURL, nil)
	if err != nil {
		return
	}
	req.Header.Set("User-Agent", "Mozilla/5.0")
	resp, err := httpClient.Do(req)
	if err != nil {
		return
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
	resp.Body.Close()
	if err != nil || resp.StatusCode != 200 {
		return
	}
	match := configPattern.FindSubmatch(body)
	if len(match) != 2 {
		return
	}
	var cfg documentConfig
	if json.Unmarshal(match[1], &cfg) != nil {
		return
	}
	o := cfg.Office
	doc := o.Editor.Document
	if o.Editor.Token == "" || doc.Key == "" {
		return
	}
	base, err := url.Parse(o.Balancer)
	if err != nil || base.Scheme != "https" || base.User != nil || base.Host == "" {
		return
	}
	// Balancer details come from Yandex over authenticated TLS.
	host := base.Hostname()
	if !(strings.HasSuffix(host, ".yandex.ru") || strings.HasSuffix(host, ".yandex.net") || strings.HasSuffix(host, ".yandex.com")) {
		return
	}
	var cookies []string
	for _, v := range resp.Cookies() {
		cookies = append(cookies, v.Name+"="+v.Value)
	}
	headers := http.Header{"User-Agent": {"Mozilla/5.0"}, "Origin": {o.Balancer}, "Cookie": {strings.Join(cookies, "; ")}}
	wsURL := url.URL{Scheme: "wss", Host: base.Host, Path: "/2024.1.1-375/doc/" + doc.Key + "/c/", RawQuery: "EIO=4&transport=websocket"}
	dialer := websocket.Dialer{HandshakeTimeout: 15 * time.Second}
	conn, handshake, err := dialer.DialContext(c.ctx, wsURL.String(), headers)
	if err != nil {
		if handshake != nil {
			handshake.Body.Close()
		}
		return
	}
	defer conn.Close()
	conn.SetReadLimit(1 << 20)
	var writeMu sync.Mutex
	write := func(p []byte) error {
		writeMu.Lock()
		defer writeMu.Unlock()
		_ = conn.SetWriteDeadline(time.Now().Add(10 * time.Second))
		return conn.WriteMessage(websocket.TextMessage, p)
	}
	done := make(chan struct{})
	defer close(done)
	go func() {
		select {
		case <-c.ctx.Done():
			conn.Close()
		case <-done:
		}
	}()
	var random [8]byte
	if _, err = rand.Read(random[:]); err != nil {
		return
	}
	userID := hex.EncodeToString(random[:])
	token, _ := json.Marshal(map[string]string{"token": o.Editor.Token})
	if write(append([]byte("40"), token...)) != nil {
		return
	}
	open := map[string]interface{}{"c": "open", "id": doc.Key, "userid": userID, "format": doc.Type, "url": doc.URL, "title": doc.Title, "lcid": 25}
	auth := map[string]interface{}{"type": "auth", "docid": doc.Key, "token": "fghhfgsjdgfjs", "user": map[string]string{"id": userID}, "editorType": 0, "lastOtherSaveTime": -1, "permissions": doc.Permissions, "openCmd": open, "coEditingMode": "fast", "jwtOpen": o.Editor.Token}
	msg, _ := json.Marshal([]interface{}{"message", auth})
	if write(append([]byte("42"), msg...)) != nil {
		return
	}
	c.connected.Store(true)
	var writer sync.WaitGroup
	writer.Add(1)
	writerDone := make(chan struct{})
	defer func() { close(writerDone); conn.Close(); writer.Wait() }()
	go func() {
		defer writer.Done()
		tick := time.NewTicker(10 * time.Second)
		defer tick.Stop()
		for {
			select {
			case <-c.ctx.Done():
				return
			case <-writerDone:
				return
			case <-tick.C:
				if write([]byte(`42["message",{"type":"cursor","cursor":"18;---KA---"}]`)) != nil {
					conn.Close()
					return
				}
			case p := <-c.outgoing:
				payload := base64.StdEncoding.EncodeToString(compress(p))
				if write([]byte(fmt.Sprintf(`42["message",{"type":"cursor","cursor":"18;%s"}]`, payload))) != nil {
					conn.Close()
					return
				}
			}
		}
	}()
	for c.ctx.Err() == nil {
		_ = conn.SetReadDeadline(time.Now().Add(75 * time.Second))
		_, message, err := conn.ReadMessage()
		if err != nil {
			return
		}
		if string(message) == "2" {
			if write([]byte("3")) != nil {
				return
			}
			continue
		}
		if strings.Contains(string(message), "---KA---") {
			continue
		}
		m := cursorPattern.FindSubmatch(message)
		if len(m) != 2 {
			m = changesPattern.FindSubmatch(message)
		}
		if len(m) != 2 {
			continue
		}
		data, err := base64.StdEncoding.DecodeString(string(m[1]))
		if err == nil {
			c.receiveWire(data)
		}
	}
}
