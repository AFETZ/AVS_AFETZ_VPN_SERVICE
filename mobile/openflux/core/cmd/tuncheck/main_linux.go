// Integration adapter: a TUN interface lives in an isolated Linux network namespace;
// the OpenFlux transport runs in the original namespace, like an excluded mobile app.
package main

import (
	"afzvpn/openfluxmobile/mobile"
	"encoding/binary"
	"flag"
	"fmt"
	"log"
	"os"
	"os/signal"
	"runtime"
	"strings"
	"syscall"
	"unsafe"
)

func main() {
	ns := flag.String("namespace", "", "network namespace path")
	env := flag.String("env", "", "private env file")
	flag.Parse()
	runtime.LockOSThread()
	original, e := os.Open("/proc/self/ns/net")
	if e != nil {
		log.Fatal(e)
	}
	defer original.Close()
	namespace, e := os.Open(*ns)
	if e != nil {
		log.Fatal(e)
	}
	defer namespace.Close()
	if _, _, e := syscall.RawSyscall(308, namespace.Fd(), syscall.CLONE_NEWNET, 0); e != 0 {
		log.Fatal(e)
	} // Linux amd64 setns
	file, e := os.OpenFile("/dev/net/tun", os.O_RDWR, 0)
	if e != nil {
		log.Fatal(e)
	}
	defer file.Close()
	var req [40]byte
	copy(req[:], "afztest0")
	binary.NativeEndian.PutUint16(req[16:18], 0x1001)
	if _, _, e := syscall.Syscall(syscall.SYS_IOCTL, file.Fd(), 0x400454ca, uintptr(unsafe.Pointer(&req[0]))); e != 0 {
		log.Fatal(e)
	}
	if _, _, e := syscall.RawSyscall(308, original.Fd(), syscall.CLONE_NEWNET, 0); e != 0 {
		log.Fatal(e)
	}
	runtime.UnlockOSThread()
	data, e := os.ReadFile(*env)
	if e != nil {
		log.Fatal(e)
	}
	_, document, ok := strings.Cut(strings.TrimSpace(string(data)), "=")
	if !ok {
		log.Fatal("invalid env")
	}
	c, e := mobile.Start(document)
	if e != nil {
		log.Fatal(e)
	}
	defer c.Stop()
	fmt.Println("TUN_READY")
	go func() {
		b := make([]byte, 65535)
		for {
			n, e := file.Read(b)
			if e != nil {
				return
			}
			if e = c.SendPacket(b[:n]); e != nil {
				return
			}
		}
	}()
	go func() {
		for {
			p, e := c.ReadPacket()
			if e != nil {
				return
			}
			if _, e = file.Write(p); e != nil {
				return
			}
		}
	}()
	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
	<-stop
}
