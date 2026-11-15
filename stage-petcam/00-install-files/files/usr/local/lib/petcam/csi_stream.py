#!/usr/bin/env python3
"""Serve Raspberry Pi CSI camera MJPEG frames over a small HTTP endpoint."""
import argparse
import json
import logging
import os
import signal
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


BOUNDARY = b"petcamframe"


def extract_jpeg_frames(buffer):
    frames = []
    while True:
        start = buffer.find(b"\xff\xd8")
        if start < 0:
            if len(buffer) > 1:
                del buffer[:-1]
            break
        end = buffer.find(b"\xff\xd9", start + 2)
        if end < 0:
            if start:
                del buffer[:start]
            break
        frames.append(bytes(buffer[start:end + 2]))
        del buffer[:end + 2]
    return frames


class CSIFrameSource:
    def __init__(self, camera, width, height, fps, quality):
        self.camera = camera
        self.width = width
        self.height = height
        self.fps = fps
        self.command = [
            "rpicam-vid", "--camera", str(camera), "--timeout", "0",
            "--nopreview", "--codec", "mjpeg", "--width", str(width),
            "--height", str(height), "--framerate", str(fps),
            "--quality", str(quality), "--output", "-",
        ]
        self.condition = threading.Condition()
        self.frame = None
        self.frame_number = 0
        self.started_at = time.monotonic()
        self.last_frame_at = None
        self.error = None
        self.process = None
        self.stop_event = threading.Event()
        self.reader = threading.Thread(target=self._capture, name="csi-capture", daemon=True)

    def start(self):
        self.reader.start()

    def _capture(self):
        try:
            self._capture_rpicam()
        except Exception as error:
            self.error = str(error)
            logging.exception("CSI camera capture failed")
        finally:
            with self.condition:
                self.condition.notify_all()

    def _capture_rpicam(self):
        self.process = subprocess.Popen(
            self.command, stdout=subprocess.PIPE, stderr=None, bufsize=0
        )
        buffer = bytearray()
        while not self.stop_event.is_set():
            chunk = self.process.stdout.read(65536)
            if not chunk:
                break
            buffer.extend(chunk)
            for jpeg in extract_jpeg_frames(buffer):
                with self.condition:
                    self.frame = jpeg
                    self.frame_number += 1
                    self.last_frame_at = time.monotonic()
                    self.condition.notify_all()
        code = self.process.wait()
        if not self.stop_event.is_set():
            self.error = f"rpicam-vid exited with status {code}"

    def wait_for_first_frame(self, timeout=20):
        with self.condition:
            self.condition.wait_for(lambda: self.frame is not None or self.error is not None, timeout=timeout)
            return self.frame is not None

    def snapshot(self):
        with self.condition:
            age = None if self.last_frame_at is None else round(time.monotonic() - self.last_frame_at, 3)
            elapsed = max(time.monotonic() - self.started_at, 0.001)
            return {
                "backend": "csi",
                "frames_received": self.frame_number,
                "captured_fps_average": round(self.frame_number / elapsed, 2),
                "last_frame_age_seconds": age,
                "capture_process_running": self.process is not None and self.process.poll() is None,
                "error": self.error,
            }

    def stop(self):
        self.stop_event.set()
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        if self.reader.is_alive() and threading.current_thread() is not self.reader:
            self.reader.join(timeout=5)



CAMERA_STATUS_PATH = Path("/var/lib/petcam/camera-status.json")


class H264TCPBridge:
    """Expose the single hardware-encoded CSI byte stream to the local RTSP server."""

    def __init__(self, host="127.0.0.1", port=8556):
        self.host = host
        self.port = port
        self.listener = None
        self.clients = set()
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = None

    def start(self):
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind((self.host, self.port))
        self.listener.listen(4)
        self.listener.settimeout(0.5)
        self.thread = threading.Thread(target=self._accept, name="csi-h264-bridge", daemon=True)
        self.thread.start()

    def _accept(self):
        while not self.stop_event.is_set():
            try:
                client, _address = self.listener.accept()
                client.settimeout(0.25)
                with self.lock:
                    self.clients.add(client)
            except socket.timeout:
                continue
            except OSError:
                break

    def publish(self, chunk):
        stale = []
        with self.lock:
            for client in self.clients:
                try:
                    client.sendall(chunk)
                except OSError:
                    stale.append(client)
            for client in stale:
                self.clients.discard(client)
                try:
                    client.close()
                except OSError:
                    pass

    def stop(self):
        self.stop_event.set()
        if self.listener:
            self.listener.close()
        with self.lock:
            clients = list(self.clients)
            self.clients.clear()
        for client in clients:
            try:
                client.close()
            except OSError:
                pass
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2)


def run_hardware_h264(args, width, height):
    """Run rpicam-vid only after the V4L2 hardware encoder has been positively identified."""
    probe = subprocess.run(
        ["/usr/local/bin/petcam-camera", "h264-encoder"],
        capture_output=True, text=True, check=False, timeout=8,
    )
    try:
        capability = json.loads(probe.stdout)
    except (ValueError, TypeError):
        capability = {}
    if probe.returncode != 0 or capability.get("available") is not True:
        raise SystemExit("CSI H.264 requested but no Raspberry Pi hardware H.264 encoder was confirmed; refusing software encoding.")

    bridge = H264TCPBridge(args.host, args.tcp_port)
    bridge.start()
    command = [
        "rpicam-vid", "--camera", str(args.camera), "--timeout", "0",
        "--nopreview", "--codec", "h264", "--inline",
        "--width", str(width), "--height", str(height),
        "--framerate", str(args.fps), "--output", "-",
    ]
    process = None
    marker = {
        "pid": os.getpid(), "native_h264": True, "backend": "csi",
        "device": capability.get("device"), "encoder": capability.get("encoder"),
        "resolution": f"{width}x{height}", "fps": args.fps,
        "camera": args.camera, "tcp_port": args.tcp_port,
    }
    CAMERA_STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    CAMERA_STATUS_PATH.write_text(json.dumps(marker) + "\n", encoding="utf-8")
    stop_event = threading.Event()

    def shutdown(_signum, _frame):
        stop_event.set()
        if process and process.poll() is None:
            process.terminate()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        logging.info("Starting CSI camera %s at %sx%s %s FPS using confirmed hardware H.264 encoder %s",
                     args.camera, width, height, args.fps, capability.get("encoder"))
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=None, bufsize=0)
        while not stop_event.is_set():
            chunk = process.stdout.read(65536)
            if not chunk:
                break
            bridge.publish(chunk)
        code = process.wait()
        if not stop_event.is_set() and code != 0:
            raise SystemExit(f"rpicam-vid hardware H.264 capture exited with status {code}")
    finally:
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        bridge.stop()
        try:
            current = json.loads(CAMERA_STATUS_PATH.read_text(encoding="utf-8"))
            if current.get("pid") == os.getpid():
                CAMERA_STATUS_PATH.unlink()
        except (OSError, ValueError):
            pass


class PetCamHandler(BaseHTTPRequestHandler):
    source = None

    def do_GET(self):
        if self.path == "/status":
            payload = json.dumps(self.source.snapshot()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path == "/":
            page = (
                b"<!doctype html><html><head><title>PetCam CSI</title></head>"
                b"<body style='margin:0;background:#111;color:#eee;font:16px sans-serif'>"
                b"<img src='/stream' style='display:block;max-width:100%;height:auto;margin:auto'>"
                b"</body></html>"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)
            return
        if self.path != "/stream":
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Age", "0")
        self.send_header("Cache-Control", "no-cache, private")
        self.send_header("Pragma", "no-cache")
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=petcamframe")
        self.end_headers()
        last_sent = 0
        try:
            while True:
                with self.source.condition:
                    self.source.condition.wait_for(
                        lambda: self.source.frame_number > last_sent or self.source.error is not None,
                        timeout=5,
                    )
                    if self.source.frame_number <= last_sent:
                        if self.source.error:
                            break
                        continue
                    frame = self.source.frame
                    last_sent = self.source.frame_number
                self.wfile.write(b"--" + BOUNDARY + b"\r\n")
                self.wfile.write(b"Content-Type: image/jpeg\r\n")
                self.wfile.write(f"Content-Length: {len(frame)}\r\n\r\n".encode())
                self.wfile.write(frame + b"\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass

    def log_message(self, fmt, *args):
        logging.info("%s - %s", self.client_address[0], fmt % args)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--camera", type=int, default=0)
    parser.add_argument("--resolution", required=True)
    parser.add_argument("--fps", required=True, type=float)
    parser.add_argument("--quality", type=int, default=70)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--h264", action="store_true", help="use hardware H.264 only; requires a confirmed V4L2 encoder")
    parser.add_argument("--tcp-port", type=int, default=8556)
    args = parser.parse_args()
    width, height = (int(value) for value in args.resolution.lower().split("x", 1))
    if width < 1 or height < 1 or args.fps < 1 or not 1 <= args.quality <= 100:
        parser.error("resolution, FPS, and quality must be positive and quality must be 1..100")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.h264:
        run_hardware_h264(args, width, height)
        return
    source = CSIFrameSource(args.camera, width, height, args.fps, args.quality)
    PetCamHandler.source = source
    source.start()
    if not source.wait_for_first_frame(timeout=20):
        source.stop()
        raise SystemExit(f"CSI camera did not produce a frame: {source.error or 'capture startup timed out'}")
    server = ThreadingHTTPServer((args.host, args.port), PetCamHandler)
    server.daemon_threads = True

    def shutdown(_signum, _frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    logging.info(
        "Starting CSI camera %s at %sx%s %s FPS on port %s",
        args.camera, width, height, args.fps, args.port,
    )
    monitor = threading.Thread(
        target=lambda: (source.reader.join(), server.shutdown()),
        name="csi-capture-watchdog",
        daemon=True,
    )
    monitor.start()
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        source.stop()


if __name__ == "__main__":
    main()
