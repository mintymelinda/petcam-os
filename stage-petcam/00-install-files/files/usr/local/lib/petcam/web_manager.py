#!/usr/bin/env python3
"""Small, dependency-free PetCam camera management HTTP interface."""
import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

CONFIG = Path("/etc/petcam/petcam.conf")
STREAM_URL = "http://127.0.0.1:8081"
PAGE = r"""<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PetCam</title>
<style>
:root{color-scheme:dark;font:16px system-ui,sans-serif;background:#17191d;color:#eee}
body{max-width:760px;margin:2rem auto;padding:0 1rem}h1{font-size:1.5rem}section{background:#22262c;border:1px solid #383e47;border-radius:10px;padding:1rem;margin:1rem 0}
button,select{font:inherit;padding:.55rem .7rem;border-radius:6px;border:1px solid #59616d;background:#303741;color:inherit}
button{cursor:pointer}button:disabled{opacity:.5}label{display:block;margin:.7rem 0 .25rem}select{width:100%}
video,img{display:block;width:100%;max-height:55vh;object-fit:contain;background:#090a0b;border-radius:6px}
small,.muted{color:#aeb7c2}#message{min-height:1.5em;color:#b9d8ff}.row{display:flex;gap:.5rem;flex-wrap:wrap}
</style>
<body><h1>PetCam <small>Camera</small></h1>
<section id="updates"><div class="row" style="justify-content:space-between;align-items:center"><h2>Software updates</h2><span id="update-state" class="muted">Not checked</span></div>
<p id="update-message" role="status" class="muted">Check for signed PetCam application releases. Operating system and firmware updates are managed separately.</p>
<div class="row"><button id="check-updates">Check for updates</button><button id="install-update" disabled>Install update</button></div>
<label style="display:flex;gap:.5rem;align-items:center"><input id="auto-check" type="checkbox"> Check automatically when opening this page</label>
<details><summary>Update details and diagnostics</summary><p id="update-details" class="muted">No update information yet.</p><pre id="update-logs" style="white-space:pre-wrap;overflow-wrap:anywhere"></pre></details>
</section>
<section><h2>Live preview</h2><img src="/stream" alt="Live camera preview"><p class="muted">The preview uses the same stream OBS receives.</p></section>
<section><div class="row" style="justify-content:space-between;align-items:center"><h2>Camera selection</h2><button id="refresh">Refresh cameras</button></div>
<p id="message" role="status">Discovering cameras…</p>
<label for="camera">Camera</label><select id="camera"></select>
<label for="mode">Capture mode</label><select id="mode"></select>
<div class="row" style="margin-top:1rem"><button id="apply">Use selected camera and mode</button></div>
<p class="muted">Only modes advertised by the camera are offered. Settings are saved across reboots. A camera change briefly restarts the stream.</p></section>
<footer style="text-align:center;padding:1rem 0 2rem"><small>Enjoying PetCam? <a href="https://www.paypal.me/kimmynightmare" target="_blank" rel="noopener noreferrer" style="color:#b9d8ff">Support development with PayPal</a></small></footer>
<script>
const $=id=>document.getElementById(id);let inventory=[];
async function request(path,options){const r=await fetch(path,options);const d=await r.json();if(!r.ok)throw Error(d.error||r.statusText);return d}
function modesFor(camera){return camera.kind==="usb"?camera.modes.filter(m=>["MJPG","MJPEG","JPEG","YUYV","YVYU","UYVY","YUV420","YVU420","RGB565","RGB24","GREY"].includes(m.format)):camera.modes}
function fillModes(){const c=inventory[+$("camera").value];$("mode").replaceChildren();if(!c)return;for(const m of modesFor(c)){const fps=m.fps?.length?m.fps:[m.fps_range?.max||30];for(const f of fps){const o=document.createElement("option");o.value=JSON.stringify({format:m.format||"CSI",width:m.width,height:m.height,fps:f});o.textContent=`${m.width}×${m.height} · ${f} fps · ${m.format||"CSI"}`; $("mode").append(o)}}}
async function refresh(){ $("message").textContent="Scanning connected cameras…";try{const d=await request("/api/cameras");inventory=d.cameras;$("camera").replaceChildren();inventory.forEach((c,i)=>{const o=document.createElement("option");o.value=i;o.textContent=c.label;$("camera").append(o)});if(!inventory.length)$("message").textContent="No supported cameras detected. Connect a USB or CSI camera and refresh.";else{$("message").textContent=`${inventory.length} camera(s) found. Select one to preview and configure.`;fillModes()}}catch(e){$("message").textContent=e.message}}
$("refresh").onclick=refresh;$("camera").onchange=fillModes;
$("apply").onclick=async()=>{const c=inventory[+$("camera").value],m=$("mode").value;if(!c||!m)return;$("apply").disabled=true;$("message").textContent="Applying camera settings…";try{await request("/api/select",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({kind:c.kind,device:c.device,camera:c.camera,mode:JSON.parse(m)})});$("message").textContent="Camera settings saved. Stream restarted.";setTimeout(refresh,1200)}catch(e){$("message").textContent=e.message}finally{$("apply").disabled=false}};
refresh();
const autoCheck=$("auto-check");autoCheck.checked=localStorage.getItem("petcam-auto-update-check")==="true";
autoCheck.onchange=()=>localStorage.setItem("petcam-auto-update-check",String(autoCheck.checked));
let updateAvailable=false,updateBusy=false;
async function updateStatus(){try{const s=await request("/api/update/status");updateBusy=s.active;$("update-state").textContent=s.active?"Installing…":s.result==="failed"?"Last install failed":s.result==="success"?"Installed":"Idle";$("update-logs").textContent=s.logs||"";if(!s.active&&updateBusy){updateBusy=false;await checkUpdates()}$("install-update").disabled=!updateAvailable||s.active}catch(e){$("update-message").textContent=e.message}}
async function checkUpdates(){const b=$("check-updates");b.disabled=true;$("update-message").textContent="Checking signed release metadata…";try{const d=await request("/api/update/check");updateAvailable=d.update_available;$("update-state").textContent=updateAvailable?"Update available":"Up to date";$("update-message").textContent=updateAvailable?"A verified application release is available. Review the notes before installing.":"This device is running the latest published PetCam application."; $("update-details").textContent="Installed: "+(d.installed_version||"unknown")+" · Available: "+d.available_version+" · Download: "+Math.ceil(d.archive_size/1048576)+" MiB\n\n"+(d.release_notes||"No release notes provided.");$("install-update").disabled=!updateAvailable||updateBusy}catch(e){$("update-state").textContent="Check failed";$("update-message").textContent=e.message}finally{b.disabled=false}}
$("check-updates").onclick=checkUpdates;
$("install-update").onclick=async()=>{if(!updateAvailable||updateBusy)return;if(!confirm("Install the signed PetCam application update now? The camera stream and web interface will briefly restart."))return;$("install-update").disabled=true;$("check-updates").disabled=true;$("update-message").textContent="Starting verified update…";try{await request("/api/update/install",{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"});updateBusy=true;$("update-state").textContent="Installing…";$("update-message").textContent="The update is downloading and staging. This page may briefly disconnect while services restart.";const poll=async()=>{await updateStatus();if(updateBusy)setTimeout(poll,2000)};setTimeout(poll,1500)}catch(e){$("update-message").textContent=e.message;$("install-update").disabled=false}finally{$("check-updates").disabled=false}};
if(autoCheck.checked)checkUpdates();updateStatus();
</script></body></html>"""


def run_camera(command):
    result = subprocess.run(["/usr/local/bin/petcam-camera", *command],
                            capture_output=True, text=True, timeout=30, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Camera discovery failed")
    return json.loads(result.stdout)


def camera_inventory():
    usb = run_camera(["discover"])
    result = []
    for item in usb.get("usb_devices", []):
        result.append({"kind": "usb", "device": item["device"],
                       "label": f'USB · {item["device"]}', "modes": item["modes"]})
    for item in usb.get("csi_cameras", []):
        result.append({"kind": "csi", "camera": item["index"],
                       "label": f'CSI · {item["sensor"]} (camera {item["index"]})',
                       "modes": item["modes"]})
    return result


def save_selection(data):
    kind = data.get("kind")
    mode = data.get("mode") or {}
    width, height, fps = mode.get("width"), mode.get("height"), mode.get("fps")
    if kind not in ("usb", "csi") or not isinstance(width, int) or not isinstance(height, int):
        raise ValueError("Invalid camera selection")
    if not (1 <= width <= 10000 and 1 <= height <= 10000 and isinstance(fps, (int, float)) and 0 < fps <= 240):
        raise ValueError("Invalid capture mode")
    if kind == "usb":
        device = data.get("device", "")
        if not re.fullmatch(r"/dev/(video[0-9]+|v4l/by-id/[A-Za-z0-9._:+-]+)", device):
            raise ValueError("Invalid USB device path")
        discovered = camera_inventory()
        match = next((c for c in discovered if c["kind"] == "usb" and c["device"] == device), None)
        if not match or not any(m["width"] == width and m["height"] == height and
                                (m["format"] == mode.get("format")) and
                                (not m["fps"] or any(abs(v-fps) < .1 for v in m["fps"]))
                                for m in match["modes"]):
            raise ValueError("That USB mode is no longer advertised by the camera")
        values = {"SOURCE": "usb", "DEVICE": device, "CAMERA": "auto",
                  "FORMAT": mode["format"], "RESOLUTION": f"{width}x{height}", "FPS": str(fps)}
    else:
        camera = data.get("camera")
        discovered = camera_inventory()
        match = next((c for c in discovered if c["kind"] == "csi" and c["camera"] == camera), None)
        if not match or not any(m["width"] == width and m["height"] == height and abs(m["fps"]-fps) < .1 for m in match["modes"]):
            raise ValueError("That CSI mode is no longer advertised by the camera")
        values = {"SOURCE": "csi", "DEVICE": "", "CAMERA": str(camera),
                  "FORMAT": "auto", "RESOLUTION": f"{width}x{height}", "FPS": str(fps)}
    existing = {}
    try:
        for line in CONFIG.read_text().splitlines():
            match = re.match(r"^([A-Z_]+)=(.*)$", line)
            if match:
                existing[match.group(1)] = match.group(2)
    except FileNotFoundError:
        pass
    existing.update(values)
    order = ("SOURCE", "DEVICE", "CAMERA", "FORMAT", "RESOLUTION", "FPS", "CSI_QUALITY", "PORT")
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG.with_suffix(".conf.tmp")
    tmp.write_text("# PetCam camera settings; managed by the web interface.\n" +
                   "".join(f"{key}={existing.get(key, default)}\n" for key, default in
                           (("SOURCE","auto"),("DEVICE",""),("CAMERA","auto"),("FORMAT","auto"),
                            ("RESOLUTION","auto"),("FPS","auto"),("CSI_QUALITY","70"),("PORT","8081"))))
    os.chmod(tmp, 0o640)
    os.replace(tmp, CONFIG)
    subprocess.run(["sudo", "/usr/bin/systemctl", "restart", "petcam-stream.service"],
                   check=True, timeout=30, capture_output=True, text=True)


def update_check():
    result = subprocess.run(["sudo", "-n", "/usr/local/bin/petcam-update", "check"],
                            capture_output=True, text=True, timeout=45, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Update check failed")
    return json.loads(result.stdout)


def update_status():
    result = subprocess.run(
        ["/usr/bin/systemctl", "show", "petcam-update.service",
         "--property=ActiveState", "--property=Result", "--property=ExecMainStatus", "--value"],
        capture_output=True, text=True, timeout=5, check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Unable to read update service status")
    values = result.stdout.splitlines()
    values += ["unknown"] * (3 - len(values))
    logs = subprocess.run(
        ["sudo", "-n", "/usr/bin/journalctl", "-u", "petcam-update.service",
         "-n", "20", "--no-pager", "-o", "cat"],
        capture_output=True, text=True, timeout=5, check=False,
    )
    return {"active": values[0] in ("active", "activating", "deactivating"),
            "result": "success" if values[1] == "success" else ("failed" if values[1] == "exit-code" else values[1]),
            "exit_code": values[2], "logs": logs.stdout[-6000:] if logs.returncode == 0 else ""}


def start_update():
    result = subprocess.run(
        ["sudo", "-n", "/usr/bin/systemctl", "start", "petcam-update.service"],
        capture_output=True, text=True, timeout=10, check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Unable to start update service")


class Handler(BaseHTTPRequestHandler):
    def send_json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "no-store")
        self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            body = PAGE.encode()
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "no-store")
            self.end_headers(); self.wfile.write(body); return
        if path == "/api/update/check":
            try: self.send_json(200, update_check())
            except Exception as error: self.send_json(503, {"error": str(error)})
            return
        if path == "/api/update/status":
            try: self.send_json(200, update_status())
            except Exception as error: self.send_json(503, {"error": str(error)})
            return
        if path == "/api/cameras":
            try: self.send_json(200, {"cameras": camera_inventory()})
            except Exception as error: self.send_json(500, {"error": str(error)})
            return
        if path == "/stream":
            try:
                with urllib.request.urlopen(STREAM_URL + "/stream", timeout=5) as upstream:
                    self.send_response(upstream.status)
                    self.send_header("Content-Type", upstream.headers.get("Content-Type", "multipart/x-mixed-replace"))
                    self.send_header("Cache-Control", "no-store"); self.end_headers()
                    while chunk := upstream.read(65536): self.wfile.write(chunk); self.wfile.flush()
            except (OSError, urllib.error.URLError, BrokenPipeError, TimeoutError): pass
            return
        self.send_error(404)

    def do_POST(self):
        if urlparse(self.path).path == "/api/update/install":
            origin = self.headers.get("Origin", "")
            if not origin or urlparse(origin).netloc != self.headers.get("Host"):
                self.send_json(403, {"error": "Request origin is not allowed"}); return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length > 1024: raise ValueError("Invalid request size")
                start_update()
                self.send_json(202, {"ok": True, "status": "started"})
            except Exception as error: self.send_json(503, {"error": str(error)})
            return
        if urlparse(self.path).path != "/api/select":
            self.send_error(404); return
        origin = self.headers.get("Origin", "")
        host = self.headers.get("Host", "").split(":")[0]
        if not origin or urlparse(origin).netloc != self.headers.get("Host"):
            self.send_json(403, {"error": "Request origin is not allowed"}); return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 1 <= length <= 8192: raise ValueError("Invalid request size")
            data = json.loads(self.rfile.read(length))
            save_selection(data)
            self.send_json(200, {"ok": True})
        except Exception as error:
            self.send_json(400, {"error": str(error)})

    def log_message(self, fmt, *args):
        pass


def main():
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()


if __name__ == "__main__":
    main()
