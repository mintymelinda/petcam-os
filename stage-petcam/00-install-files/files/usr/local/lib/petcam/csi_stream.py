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
        self.picam2 = None
        self.native_h264 = False
        self.native_h264_error = None
        self.rtmp_width = None
        self.rtmp_height = None
        self.stop_event = threading.Event()
        try:
            with open("/etc/petcam/rtmp.json", encoding="utf-8") as config_file:
                rtmp_config = json.load(config_file)
            if rtmp_config.get("enabled") is True:
                self.rtmp_width = int(rtmp_config["width"])
                self.rtmp_height = int(rtmp_config["height"])
        except (OSError, ValueError, KeyError, TypeError):
            pass
        self.reader = threading.Thread(target=self._capture, name="csi-capture", daemon=True)

    def start(self):
        self.reader.start()

    def _capture(self):
        try:
            if self._rtmp_enabled():
                try:
                    self._capture_picamera2()
                    return
                except Exception as error:
                    self.native_h264_error = str(error)
                    self.native_h264 = False
                    logging.exception("Picamera2 dual-output capture unavailable; falling back to rpicam-vid MJPEG")
                    self._close_picamera2()
            self._capture_rpicam()
        except Exception as error:
            self.error = str(error)
            logging.exception("CSI camera capture failed")
        finally:
            with self.condition:
                self.condition.notify_all()

    def _rtmp_enabled(self):
        try:
            with open("/etc/petcam/rtmp.json", encoding="utf-8") as config_file:
                config = json.load(config_file)
            return config.get("enabled") is True
        except (OSError, ValueError, AttributeError):
            return False

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

    def _capture_picamera2(self):
        from picamera2 import Picamera2
        from picamera2.encoders import H264Encoder, MJPEGEncoder
        from picamera2.outputs import FfmpegOutput, Output

        with open("/etc/petcam/rtmp.json", encoding="utf-8") as config_file:
            rtmp = json.load(config_file)
        output_width = int(rtmp["width"])
        output_height = int(rtmp["height"])
        output_fps = int(rtmp["fps"])
        capture_fps = float(self.fps)
        fps_ratio = capture_fps / output_fps
        frame_skip = max(1, round(fps_ratio))
        if output_width > self.width or output_height > self.height:
            raise RuntimeError("Native H.264 output cannot exceed the selected CSI capture resolution.")
        if output_fps > capture_fps or abs(fps_ratio - frame_skip) > 0.05:
            raise RuntimeError(
                "Native H.264 requires RTMP FPS to be an integer divisor of the selected CSI capture FPS."
            )

        source = self

        class MJPEGFrameOutput(Output):
            def outputframe(self, frame, keyframe=True, timestamp=None, packet=None, audio=False):
                if audio:
                    return
                with source.condition:
                    source.frame = bytes(frame)
                    source.frame_number += 1
                    source.last_frame_at = time.monotonic()
                    source.condition.notify_all()

        camera = Picamera2(self.camera)
        self.picam2 = camera
        camera_config = camera.create_video_configuration(
            main={"size": (self.width, self.height), "format": "YUV420"},
            lores={"size": (output_width, output_height), "format": "YUV420"},
            controls={"FrameRate": capture_fps},
            buffer_count=6,
        )
        camera.configure(camera_config)
        mjpeg_encoder = MJPEGEncoder()
        h264_encoder = H264Encoder(
            bitrate=int(rtmp["bitrate_kbps"]) * 1000,
            repeat=True,
            iperiod=output_fps * 2,
            framerate=output_fps,
        )
        h264_encoder.frame_skip_count = frame_skip
        h264_output = FfmpegOutput(
            "-f mpegts udp://127.0.0.1:10000?pkt_size=1316"
        )
        camera.start_encoder(mjpeg_encoder, MJPEGFrameOutput(), name="main")
        camera.start_encoder(h264_encoder, h264_output, name="lores")
        camera.start()
        self.native_h264 = True
        logging.info(
            "Picamera2 dual-output capture active: MJPEG %sx%s at %s FPS; H.264 %sx%s at %s FPS",
            self.width, self.height, capture_fps, output_width, output_height, output_fps,
        )
        try:
            while not self.stop_event.wait(0.5):
                if camera.started is False:
                    raise RuntimeError("Picamera2 camera stopped unexpectedly")
        finally:
            self._close_picamera2()

    def _close_picamera2(self):
        camera = self.picam2
        self.picam2 = None
        if camera is None:
            return
        try:
            camera.stop_encoder()
        except Exception:
            pass
        try:
            camera.stop()
        except Exception:
            pass
        try:
            camera.close()
        except Exception:
            pass

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
                "capture_process_running": (
                    self.process is not None and self.process.poll() is None
                ) or (self.picam2 is not None and self.picam2.started),
                "native_h264": self.native_h264,
                "native_h264_error": self.native_h264_error,
                "native_h264_resolution": (
                    f"{self.rtmp_width}x{self.rtmp_height}" if self.native_h264 else None
                ),
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
        self._close_picamera2()


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
