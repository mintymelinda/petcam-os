#!/usr/bin/env python3
"""Serve Raspberry Pi CSI camera MJPEG frames over a small HTTP endpoint."""
import argparse
import json
import logging
import signal
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


BOUNDARY = b"petcamframe"


class CSIFrameSource:
    def __init__(self, camera, width, height, fps, quality):
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
        self.reader = threading.Thread(target=self._capture, name="csi-capture", daemon=True)

    def start(self):
        self.reader.start()

    def _capture(self):
        try:
            self.process = subprocess.Popen(
                self.command, stdout=subprocess.PIPE, stderr=None, bufsize=0
            )
            buffer = bytearray()
            while True:
                chunk = self.process.stdout.read(65536)
                if not chunk:
                    break
                buffer.extend(chunk)
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
                    jpeg = bytes(buffer[start:end + 2])
                    del buffer[:end + 2]
                    with self.condition:
                        self.frame = jpeg
                        self.frame_number += 1
                        self.last_frame_at = time.monotonic()
                        self.condition.notify_all()
            code = self.process.wait()
            self.error = f"rpicam-vid exited with status {code}"
        except Exception as error:
            self.error = str(error)
            logging.exception("CSI camera capture failed")
        finally:
            with self.condition:
                self.condition.notify_all()

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
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()


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
    args = parser.parse_args()
    width, height = (int(value) for value in args.resolution.lower().split("x", 1))
    if width < 1 or height < 1 or args.fps < 1 or not 1 <= args.quality <= 100:
        parser.error("resolution, FPS, and quality must be positive and quality must be 1..100")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    source = CSIFrameSource(args.camera, width, height, args.fps, args.quality)
    PetCamHandler.source = source
    source.start()
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
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        source.stop()


if __name__ == "__main__":
    main()
