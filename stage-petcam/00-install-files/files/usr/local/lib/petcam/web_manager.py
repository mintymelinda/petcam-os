#!/usr/bin/env python3
"""Small, dependency-free PetCam camera management HTTP interface."""
import json
import os
import re
import ssl
import subprocess
import threading
import time
import hmac
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import ensure_config_lock, parse_config
from camera import PIXEL_BUDGETS, mode_supports_fps, model_tier, read_model
from network_diagnostics import collect_diagnostics, create_support_bundle
from camera_profiles import (
    USTREAMER_FORMATS, apply_primary_config, camera_identity, configure_profile, create_profile, list_profiles,
    remove_profile, resource_budget_report, stream_port,
)
from timelapse import load_config as load_timelapse_config, save_config as save_timelapse_config, state_snapshot as timelapse_snapshot
from motion_detection import load_config as load_motion_config, save_config as save_motion_config, state_snapshot as motion_snapshot, FRAMES as MOTION_FRAMES
from web_auth import SESSION_TTL, get_session, initialize_password, invalidate_session, is_configured, new_session, verify_password

CONFIG = Path("/etc/petcam/petcam.conf")
DEFAULT_STREAM_URL = "http://127.0.0.1:8081"


def backend_stream_url():
    try:
        return f"http://127.0.0.1:{parse_config(CONFIG)['PORT']}"
    except Exception:
        return DEFAULT_STREAM_URL
TLS_CERT = Path("/etc/petcam/web-manager.crt")
TLS_KEY = Path("/etc/petcam/web-manager.key")
HEALTH_FILE = Path("/var/lib/petcam/health.json")
_login_attempts = {}
_login_lock = threading.Lock()
PAGE = r"""<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PetCam</title>
<style>
:root{color-scheme:dark;font:16px system-ui,sans-serif;background:#17191d;color:#eee}body.setup-required>section:not(#first-run),body.setup-required>footer{display:none}#first-run[hidden]{display:none}
body{max-width:760px;margin:2rem auto;padding:0 1rem}h1{font-size:1.5rem}section{background:#22262c;border:1px solid #383e47;border-radius:10px;padding:1rem;margin:1rem 0}
button,select,input{font:inherit;padding:.55rem .7rem;border-radius:6px;border:1px solid #59616d;background:#303741;color:inherit}
button{cursor:pointer}button:disabled{opacity:.5}label{display:block;margin:.7rem 0 .25rem}select{width:100%}
#update-confirmation{border:1px solid #59616d;border-radius:8px;padding:1rem;margin-top:1rem;background:#303741}#update-confirmation[hidden]{display:none}#update-confirmation p{margin-top:0}
video,img{display:block;width:100%;max-height:55vh;object-fit:contain;background:#090a0b;border-radius:6px}.motion-event{display:flex;gap:.75rem;align-items:center;margin:.6rem 0}.motion-event img{width:160px;max-height:110px;object-fit:cover}
small,.muted{color:#aeb7c2}#message{min-height:1.5em;color:#b9d8ff}.row{display:flex;gap:.5rem;flex-wrap:wrap}
dialog{max-width:min(28rem,calc(100% - 2rem));width:100%;background:#22262c;color:inherit;border:1px solid #59616d;border-radius:10px;padding:1.25rem}dialog::backdrop{background:#000b}dialog form{display:grid;gap:.5rem}dialog input{width:100%;box-sizing:border-box}#login-message{min-height:1.4em;color:#ffb4a9}
</style>
<body><h1>PetCam <small>Camera</small></h1>
<section id="first-run" hidden><h2>Welcome to PetCam</h2><p>Create the administrator password for this camera. You can do this from your browser; SSH is not required.</p><form id="first-run-form"><label for="setup-password">Administrator password (12–1024 characters)</label><input id="setup-password" type="password" minlength="12" maxlength="1024" autocomplete="new-password" required><label for="setup-password-confirm">Confirm password</label><input id="setup-password-confirm" type="password" minlength="12" maxlength="1024" autocomplete="new-password" required><p id="setup-message" role="status" aria-live="polite"></p><button id="setup-submit" type="submit">Create administrator password</button></form></section>
<dialog id="login-dialog" aria-labelledby="login-title"><form id="login-form"><h2 id="login-title">Sign in to PetCam</h2><label for="login-password">Administrator password</label><input id="login-password" type="password" autocomplete="current-password" required><p id="login-message" role="alert" aria-live="polite"></p><button id="login-submit" type="submit">Sign in</button></form></dialog>
<section id="updates"><div class="row" style="justify-content:space-between;align-items:center"><h2>Software updates</h2><span id="update-state" class="muted">Not checked</span></div>
<p id="update-message" role="status" class="muted">Check for signed PetCam application releases. Operating system and firmware updates are managed separately.</p>
<div class="row"><button id="check-updates">Check for updates</button><button id="install-update" disabled>Install update</button></div>
<div id="update-confirmation" hidden aria-labelledby="update-confirmation-title"><p id="update-confirmation-title"><strong>Install this signed update?</strong></p><p class="muted">The camera stream and web interface will briefly restart while PetCam installs the verified application release.</p><div class="row"><button id="cancel-update-install" type="button">Cancel</button><button id="confirm-update-install" type="button">Install update</button></div></div>
<label style="display:flex;gap:.5rem;align-items:center"><input id="auto-check" type="checkbox"> Check automatically when opening this page</label>
<details><summary>Update details and diagnostics</summary><p id="update-details" class="muted">No update information yet.</p><pre id="update-logs" style="white-space:pre-wrap;overflow-wrap:anywhere"></pre></details>
</section>
<section><h2>Live preview</h2><img id="live-preview" src="/stream" alt="Live camera preview"><p id="capture-rate" class="muted">Checking actual capture rate…</p><p class="muted">The preview uses the same stream OBS receives.</p></section>
<section><h2>OBS browser source</h2><p class="muted">Add this URL as a Browser Source in OBS on a computer on the same network.</p><div class="row" style="align-items:center"><code id="browser-source-url" style="flex:1;overflow-wrap:anywhere">Loading…</code><button id="copy-browser-source">Copy URL</button></div><p id="copy-message" class="muted" role="status" aria-live="polite"></p></section>
<section><div class="row" style="justify-content:space-between;align-items:center"><h2>Camera selection</h2><button id="refresh">Refresh cameras</button></div>
<p id="message" role="status">Discovering cameras…</p>
<label for="camera">Camera</label><select id="camera"></select>
<label for="mode">Capture mode</label><select id="mode"></select>
<label for="stream-target">Configure stream</label><select id="stream-target"><option value="default">Primary stream (existing /stream)</option></select>
<div class="row" style="margin-top:1rem"><button id="apply">Apply selected camera and mode</button><button id="add-stream">Add independent stream</button></div>
<p class="muted">Only advertised modes are offered. Applying to the primary stream preserves the existing /stream URL. Independent streams receive their own configuration, service, and OBS URL.</p></section>
<section><div class="row" style="justify-content:space-between;align-items:center"><h2>Camera controls</h2><button id="refresh-camera-controls" type="button">Refresh controls</button></div>
<p id="camera-controls-status" class="muted" role="status">Controls for the active camera will appear here.</p>
<p class="muted">Controls are discovered from the connected camera. Some settings may be unavailable while automatic modes are active. Dynamic framerate can reduce the actual captured FPS when enabled.</p>
<div id="camera-controls-list" class="muted">No camera controls loaded.</div></section>
<section><div class="row" style="justify-content:space-between;align-items:center"><h2>Camera streams</h2><button id="refresh-streams">Refresh streams</button></div><div id="stream-profiles" class="muted">Loading streams…</div></section>
<section><div class="row" style="justify-content:space-between;align-items:center"><h2>Network and OBS diagnostics</h2><button id="refresh-diagnostics">Run checks</button></div><p id="diagnostic-summary" role="status">Diagnostics have not been run yet.</p><p id="diagnostic-details" class="muted"></p><div class="row"><button id="download-support-bundle">Download redacted support bundle</button></div></section>
<section><div class="row" style="justify-content:space-between;align-items:center"><h2>Stream health</h2><button id="refresh-health">Refresh</button></div><p id="health-state" role="status">Checking stream health…</p><p id="health-reason" class="muted"></p><small id="health-details" class="muted"></small></section>
<section><div class="row" style="justify-content:space-between;align-items:center"><h2>Timelapse</h2><button id="refresh-timelapse">Refresh status</button></div>
<p id="timelapse-status" role="status">Loading timelapse status…</p>
<label style="display:flex;gap:.5rem;align-items:center"><input id="timelapse-enabled" type="checkbox"> Enable periodic captures</label>
<label for="timelapse-interval">Capture interval (seconds)</label><input id="timelapse-interval" type="number" min="60" max="86400" value="300">
<label for="timelapse-start">Daily schedule start (local time)</label><input id="timelapse-start" type="time" value="00:00">
<label for="timelapse-end">Daily schedule end (local time)</label><input id="timelapse-end" type="time" value="23:59">
<label for="timelapse-retention">Retention (days)</label><input id="timelapse-retention" type="number" min="1" max="3650" value="30">
<label for="timelapse-storage">Maximum storage (MiB)</label><input id="timelapse-storage" type="number" min="64" max="102400" value="2048">
<label for="timelapse-free">Minimum free disk space (MiB)</label><input id="timelapse-free" type="number" min="64" max="102400" value="256">
<label for="timelapse-width">Maximum image width (0 = live resolution)</label><input id="timelapse-width" type="number" min="0" max="4096" value="0">
<label for="timelapse-quality">JPEG quality (0 = preserve source unless resizing; 1–95 = re-encode)</label><input id="timelapse-quality" type="number" min="0" max="95" value="85">
<label for="timelapse-path">Storage directory (inside /var/lib/petcam)</label><input id="timelapse-path" type="text" value="/var/lib/petcam/timelapse/frames">
<p class="muted">Frames reuse the active MJPEG stream, so timelapse does not open another camera process. Optional resizing/re-encoding is performed only after a frame is received.</p>
<div class="row"><button id="save-timelapse">Save timelapse settings</button></div></section>
<section><div class="row" style="justify-content:space-between;align-items:center"><h2>Motion detection</h2><button id="refresh-motion">Refresh status</button></div>
<p id="motion-status" role="status">Loading motion status…</p>
<label style="display:flex;gap:.5rem;align-items:center"><input id="motion-enabled" type="checkbox"> Enable motion snapshots</label>
<label for="motion-sensitivity">Sensitivity (higher detects smaller changes)</label><input id="motion-sensitivity" type="range" min="1" max="100" value="50">
<label for="motion-min-change">Minimum changed pixels (%)</label><input id="motion-min-change" type="number" min="0.1" max="20" step="0.1" value="2.5">
<label for="motion-sample">Sampling interval (seconds)</label><input id="motion-sample" type="number" min="1" max="30" value="2">
<label for="motion-confirm">Consecutive samples to confirm motion</label><input id="motion-confirm" type="number" min="1" max="5" value="2">
<label for="motion-cooldown">Minimum time between events (seconds)</label><input id="motion-cooldown" type="number" min="5" max="86400" value="60">
<label style="display:flex;gap:.5rem;align-items:center"><input id="motion-quiet-enabled" type="checkbox"> Suppress snapshots during quiet hours</label>
<label for="motion-quiet-start">Quiet hours start (local time)</label><input id="motion-quiet-start" type="time" value="22:00">
<label for="motion-quiet-end">Quiet hours end (local time)</label><input id="motion-quiet-end" type="time" value="07:00">
<label for="motion-retention">Event snapshot retention (days)</label><input id="motion-retention" type="number" min="1" max="3650" value="14">
<label for="motion-storage">Maximum event storage (MiB)</label><input id="motion-storage" type="number" min="64" max="102400" value="512">
<label for="motion-free">Minimum free disk space (MiB)</label><input id="motion-free" type="number" min="64" max="102400" value="256">
<p class="muted">Motion samples reuse the active camera MJPEG stream. Frames are reduced to 160×120 grayscale for comparison; no second camera process or continuous video recording is used. The worker runs with a strict CPU and memory limit. Disabled by default.</p>
<div class="row"><button id="save-motion">Save motion settings</button></div>
<h3>Recent motion events</h3><div id="motion-events" class="muted">No motion events recorded.</div></section>
<section><h2>Account security</h2><p class="muted">Sign out of this manager.</p><button id="logout">Sign out</button></section>
<footer style="text-align:center;padding:1rem 0 2rem"><small>Enjoying PetCam? <a href="https://www.paypal.me/kimmynightmare" target="_blank" rel="noopener noreferrer" style="color:#b9d8ff">Support development with PayPal</a></small></footer>
<script>
const $=id=>document.getElementById(id);let inventory=[],streams=[],activeConfig=null,captureStatus={},resourceBudget=null;
const browserSourceUrl="http://"+window.location.hostname+":8080/stream";
$("first-run-form").onsubmit=async event=>{event.preventDefault();const password=$("setup-password").value,confirmation=$("setup-password-confirm").value,message=$("setup-message"),button=$("setup-submit");if(password!==confirmation){message.textContent="The passwords do not match.";return}button.disabled=true;message.textContent="Creating administrator account…";try{const response=await fetch("/api/setup",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({password,password_confirmation:confirmation})});const data=await response.json();if(!response.ok)throw Error(data.error||"Initial setup failed");message.textContent="Administrator account created. Opening PetCam…";location.reload()}catch(error){message.textContent=error.message;button.disabled=false}};
$("browser-source-url").textContent=browserSourceUrl;
$("copy-browser-source").onclick=async()=>{const message=$("copy-message");try{if(navigator.clipboard&&window.isSecureContext){await navigator.clipboard.writeText(browserSourceUrl)}else{const field=document.createElement("textarea");field.value=browserSourceUrl;field.setAttribute("readonly","");field.style.position="fixed";field.style.opacity="0";document.body.append(field);field.select();const copied=document.execCommand("copy");field.remove();if(!copied)throw Error("Clipboard access is unavailable")}message.textContent="Browser source URL copied."}catch(e){message.textContent="Could not copy automatically. Select and copy the URL above."}};
let csrfToken=null;let loginPromise=null;async function login(){if(loginPromise)return loginPromise;loginPromise=(async()=>{const setupResponse=await fetch("/api/setup/status",{cache:"no-store"});const setupState=await setupResponse.json();if(!setupResponse.ok)throw Error(setupState.error||"Unable to check initial setup");if(!setupState.configured)throw Error("Create the administrator password in the welcome panel before signing in.");return await new Promise((resolve,reject)=>{const dialog=$("login-dialog"),form=$("login-form"),password=$("login-password"),message=$("login-message"),button=$("login-submit");let settled=false;const finish=(error,data)=>{if(settled)return;settled=true;password.value="";dialog.close();if(error)reject(error);else resolve(data)};form.onsubmit=async event=>{event.preventDefault();button.disabled=true;message.textContent="Signing in…";try{const response=await fetch("/api/login",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({password:password.value})});const data=await response.json();if(!response.ok)throw Error(data.error||"Sign-in failed");csrfToken=data.csrf_token;finish(null,data)}catch(error){message.textContent=error.message;password.value="";password.focus();button.disabled=false}};dialog.oncancel=event=>{event.preventDefault();finish(Error("Sign-in cancelled"))};dialog.showModal();password.focus()})})();try{return await loginPromise}finally{loginPromise=null}}async function request(path,options={}){const headers=new Headers(options.headers||{});if(options.method&&options.method.toUpperCase()!=="GET"&&!csrfToken){let session=await fetch("/api/session");if(session.status===401){await login()}else{const state=await session.json();if(!session.ok)throw Error(state.error||session.statusText);csrfToken=state.csrf_token}}if(options.method&&options.method.toUpperCase()!=="GET"&&csrfToken)headers.set("X-PetCam-CSRF",csrfToken);let r=await fetch(path,{...options,headers});if(r.status===401){await login();if(options.method&&options.method.toUpperCase()!=="GET"&&csrfToken)headers.set("X-PetCam-CSRF",csrfToken);r=await fetch(path,{...options,headers})}const d=await r.json();if(!r.ok)throw Error(d.error||r.statusText);return d}
function modesFor(camera){return camera.kind==="usb"?camera.modes.filter(m=>["MJPG","MJPEG","JPEG","YUYV","YVYU","UYVY","YUV420","YVU420","RGB565","RGB24","GREY"].includes(m.format)):camera.modes}
function projectedPixelRate(mode,target="default",adding=false){const candidate=mode.width*mode.height*mode.fps,budget=resourceBudget;if(!budget||!Number.isFinite(Number(budget.estimated_total_pixels_per_second)))return candidate;let total=Number(budget.estimated_total_pixels_per_second);if(adding)total+=candidate;else if(target==="default")total=total-Number(budget.primary_estimated_pixels_per_second||0)+candidate;else{const existing=streams.find(item=>item.id===target);total=total-Number(existing?.estimated_pixels_per_second||0)+candidate}return Math.max(candidate,total)}
function exceedsPerformanceBudget(camera,mode,target="default",adding=false){const budget=Number(resourceBudget?.pixel_budget_pixels_per_second||camera?.hardware_budget_pixels_per_second||0);return budget>0&&projectedPixelRate(mode,target,adding)>budget}
function confirmPerformanceOverride(camera,mode,target="default",adding=false){if(!exceedsPerformanceBudget(camera,mode,target,adding))return false;const estimate=projectedPixelRate(mode,target,adding),budget=Number(resourceBudget?.pixel_budget_pixels_per_second||camera.hardware_budget_pixels_per_second);return confirm("Performance warning: this change is estimated to put the device at "+estimate.toLocaleString()+" aggregate pixels/second, above the "+(resourceBudget?.hardware_tier||camera.hardware_tier)+" device guideline of "+budget.toLocaleString()+" pixels/second. It may cause low frame rates, dropped frames, or an unresponsive camera stream. Use this mode anyway?")}
function showSelectedModeWarning(){const c=inventory[+$("camera").value],raw=$("mode").value,target=$("stream-target").value;if(!c||!raw)return;const mode=JSON.parse(raw);if(exceedsPerformanceBudget(c,mode,target))$("message").textContent="Performance warning: this selection would exceed the estimated aggregate "+c.hardware_tier+" device budget. You can still apply it after confirming the warning.";else $("message").textContent="Selected mode is within the estimated device performance budget."}
function activeCameraMatches(camera,config){if(!config)return false;if(config.SOURCE==="usb")return camera.kind==="usb"&&(config.USB_PHYSICAL_PATH?camera.physical_path===config.USB_PHYSICAL_PATH:camera.device===config.DEVICE);if(config.SOURCE==="csi")return camera.kind==="csi"&&(config.CSI_DEVICE_PATH?camera.device_path===config.CSI_DEVICE_PATH:String(camera.camera)===String(config.CAMERA));return false}
function fillModes(preferred=null){const c=inventory[+$("camera").value];$("mode").replaceChildren();if(!c)return;let matched=false;for(const m of modesFor(c)){let fps=m.fps?.length?m.fps:[m.fps_range?.max||30];if(c.kind==="usb")fps=fps.filter(Number.isInteger);if(preferred&&preferred.width===m.width&&preferred.height===m.height&&(preferred.format||"CSI")===(m.format||"CSI")&&!fps.some(f=>Math.abs(f-preferred.fps)<0.1))fps=[...fps,preferred.fps];for(const f of fps){const mode={format:m.format||"CSI",width:m.width,height:m.height,fps:f};const o=document.createElement("option");o.value=JSON.stringify(mode);o.textContent=`${m.width}×${m.height} · ${f} fps · ${m.format||"CSI"}`+(exceedsPerformanceBudget(c,mode,$("stream-target").value,false)?" · ⚠ above device estimate":"");if(preferred&&mode.width===preferred.width&&mode.height===preferred.height&&mode.format===(preferred.format||"CSI")&&Math.abs(mode.fps-preferred.fps)<0.1){o.selected=true;matched=true}$("mode").append(o)}}if(preferred&&!matched){const mode={format:preferred.format||"CSI",width:preferred.width,height:preferred.height,fps:Number(preferred.fps)};const o=document.createElement("option");o.value=JSON.stringify(mode);o.textContent=`${mode.width}×${mode.height} · ${mode.fps} fps · ${mode.format} (saved setting; not currently advertised)`;o.selected=true;$("mode").prepend(o)}$("mode").onchange=showSelectedModeWarning;showSelectedModeWarning()}
function renderCaptureStatus(){const fps=Number(captureStatus.latest_captured_fps),configured=activeConfig?.FPS;if(Number.isFinite(fps)&&fps>0){$("capture-rate").textContent="Actual captured rate: "+fps.toFixed(1).replace(/\.0$/,"")+" FPS"+(configured&&configured!=="auto"?" · configured: "+configured+" FPS":"")+(activeConfig?.RESOLUTION&&activeConfig.RESOLUTION!=="auto"?" · "+activeConfig.RESOLUTION:"")}else $("capture-rate").textContent="Actual captured rate: not currently reported"}
function refreshLivePreview(){const preview=$("live-preview");preview.src="/stream?refresh="+Date.now()}
async function refresh(){ $("message").textContent="Scanning connected cameras…";try{const d=await request("/api/cameras");inventory=d.cameras;activeConfig=d.active_config;captureStatus=d.capture_status||{};resourceBudget=d.resource_budget||null;$("camera").replaceChildren();inventory.forEach((c,i)=>{const o=document.createElement("option");o.value=i;o.textContent=c.label;$("camera").append(o)});const activeIndex=inventory.findIndex(c=>activeCameraMatches(c,activeConfig));if(activeIndex>=0){$("camera").value=String(activeIndex);$("camera").options[activeIndex].textContent+=" · current"}if(!inventory.length)$("message").textContent="No supported cameras detected. Connect a USB or CSI camera and refresh.";else{const selected=activeIndex>=0?activeConfig:null;const selectedCamera=activeIndex>=0?inventory[activeIndex]:null;const currentResolution=selected&&selected.RESOLUTION!=="auto"?selected.RESOLUTION:captureStatus.active_resolution;const currentFps=selected&&selected.FPS!=="auto"?Number(selected.FPS):Number(captureStatus.latest_captured_fps);let currentFormat=selected&&selected.FORMAT!=="auto"?selected.FORMAT:(captureStatus.active_capture_format||"CSI");if(currentFormat==="MJPEG"||currentFormat==="JPEG")currentFormat="MJPG";if(selectedCamera?.kind==="csi")currentFormat="CSI";const preferred=currentResolution&&currentResolution!=="auto"&&Number.isFinite(currentFps)&&currentFps>0?{width:Number(currentResolution.split("x")[0]),height:Number(currentResolution.split("x")[1]),fps:currentFps,format:currentFormat}:null;$("message").textContent=inventory.length+" camera(s) found. Current saved camera settings are selected.";fillModes(preferred)}renderCaptureStatus();await refreshStreams();await refreshCameraControls()}catch(e){$("message").textContent=e.message}}

function cameraControlLabel(name){const labels={auto_exposure:"Auto exposure",exposure_time_absolute:"Exposure time",exposure_dynamic_framerate:"Dynamic framerate",white_balance_automatic:"Automatic white balance",white_balance_temperature:"White balance temperature",power_line_frequency:"Power-line frequency",backlight_compensation:"Backlight compensation",focus_automatic_continuous:"Continuous autofocus",focus_absolute:"Focus"};return labels[name]||name.replaceAll("_"," ").replace(/\b\w/g,letter=>letter.toUpperCase())}
async function refreshCameraControls(){
const root=$("camera-controls-list"),status=$("camera-controls-status");root.replaceChildren();status.textContent="Reading active camera controls…";
try{
const data=await request("/api/camera-controls");
if(!data.supported){status.textContent=data.message||"Camera controls are unavailable.";root.textContent="";return}
status.textContent="Controls for "+data.label+" · "+data.device;
if(!data.controls.length){root.textContent="This camera did not report any controls.";return}
for(const control of data.controls){
const row=document.createElement("div"),label=document.createElement("label"),details=document.createElement("small"),editor=document.createElement("div"),apply=document.createElement("button");
row.style.cssText="border-top:1px solid #383e47;padding:.65rem 0";
label.textContent=cameraControlLabel(control.name);details.className="muted";details.style.display="block";
details.textContent="Current: "+(control.value===null?"unknown":control.value)+(control.default!==null?" · Default: "+control.default:"")+(control.min!==null&&control.max!==null?" · Range: "+control.min+"–"+control.max:"")+(control.writable?"":" · Currently unavailable");
editor.className="row";editor.style.alignItems="center";
let input;
if(control.type==="bool"){input=document.createElement("input");input.type="checkbox";input.checked=Number(control.value)===1;input.disabled=!control.writable}
else if(control.type==="menu"||control.type==="intmenu"){input=document.createElement("select");for(const option of control.menu){const choice=document.createElement("option");choice.value=option.value;choice.textContent=option.label;input.append(choice)}input.value=String(control.value);input.disabled=!control.writable}
else if(control.type==="int"&&control.min!==null&&control.max!==null&&control.max-control.min<=10000){input=document.createElement("input");input.type="range";input.min=control.min;input.max=control.max;input.step=control.step;input.value=control.value??control.min;input.disabled=!control.writable;const output=document.createElement("output");output.textContent=input.value;input.oninput=()=>output.textContent=input.value;editor.append(input,output)}
else{input=document.createElement("input");input.type="number";if(control.min!==null)input.min=control.min;if(control.max!==null)input.max=control.max;input.step=control.step;input.value=control.value??"";input.disabled=!control.writable}
apply.type="button";apply.textContent="Apply";apply.disabled=!control.writable;apply.onclick=async()=>{apply.disabled=true;status.textContent="Updating "+cameraControlLabel(control.name)+"…";try{const value=control.type==="bool"?(input.checked?1:0):Number(input.value);await request("/api/camera-controls",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name:control.name,value})});await refreshCameraControls();status.textContent=cameraControlLabel(control.name)+" updated."}catch(error){status.textContent=error.message;apply.disabled=false}};
editor.append(apply);row.append(label,details,editor);
if(control.name==="exposure_dynamic_framerate"){const note=document.createElement("small");note.className="muted";note.textContent="When enabled, the camera may lower its delivered frame rate to accommodate exposure. Disable it if you need a stable frame-rate target.";row.append(note)}
root.append(row)
}
}catch(error){status.textContent="Camera controls unavailable: "+error.message}
}
$("refresh-camera-controls").onclick=refreshCameraControls;
$("refresh").onclick=refresh;$("camera").onchange=fillModes;$("stream-target").onchange=showSelectedModeWarning;
$("apply").onclick=async()=>{const c=inventory[+$("camera").value],m=$("mode").value,target=$("stream-target").value;if(!c||!m)return;const mode=JSON.parse(m),override=confirmPerformanceOverride(c,mode,target,false);if(exceedsPerformanceBudget(c,mode,target,false)&&!override)return;$("apply").disabled=true;$("message").textContent="Applying camera settings…";try{if(target==="default"){await request("/api/select",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({kind:c.kind,camera_id:c.id,device:c.device,physical_path:c.physical_path,device_path:c.device_path,camera:c.camera,mode,allow_performance_override:override})})}else{await request("/api/streams/configure",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({profile_id:target,camera_id:c.id,mode,allow_performance_override:override})})}if(target==="default"){await refresh();refreshLivePreview()}else{await refreshStreams()}$("message").textContent="Camera settings saved. The selected stream preview has been reconnected."}catch(e){$("message").textContent=e.message}finally{$("apply").disabled=false}};
$("add-stream").onclick=async()=>{const c=inventory[+$("camera").value],m=$("mode").value;if(!c||!m)return;const mode=JSON.parse(m),override=confirmPerformanceOverride(c,mode,"default",true);if(exceedsPerformanceBudget(c,mode,"default",true)&&!override)return;const b=$("add-stream");b.disabled=true;$("message").textContent="Starting independent camera stream…";try{const d=await request("/api/streams",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({camera_id:c.id,mode,allow_performance_override:override})});$("message").textContent="Independent stream started: "+d.profile.stream_path;await refreshStreams()}catch(e){$("message").textContent=e.message}finally{b.disabled=false}};
$("refresh-streams").onclick=refreshStreams;
$("logout").onclick=async()=>{try{await request("/api/logout",{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"});csrfToken=null;location.reload()}catch(e){alert(e.message)}};
function fillStreamTargets(){const selected=$("stream-target").value;$("stream-target").replaceChildren();const primary=document.createElement("option");primary.value="default";primary.textContent="Primary stream (/stream)";$("stream-target").append(primary);for(const p of streams){if(p.id==="default")continue;const o=document.createElement("option");o.value=p.id;o.textContent=p.label+" · "+p.state;$("stream-target").append(o)}if([...$("stream-target").options].some(o=>o.value===selected))$("stream-target").value=selected}
function renderStreams(){const root=$("stream-profiles");root.replaceChildren();for(const p of streams){const card=document.createElement("section"),title=document.createElement("h3"),state=document.createElement("p"),preview=document.createElement("img"),url=document.createElement("code"),row=document.createElement("div");title.textContent=p.label;state.textContent="Service: "+p.state+" · "+(p.ready?"frames available":"no frame detected")+(p.id==="default"&&Number(captureStatus.latest_captured_fps)>0?" · actual "+Number(captureStatus.latest_captured_fps).toFixed(1).replace(/\.0$/,"")+" FPS":"")+(p.stable_identity===false?" · identity may change if USB enumeration changes":"");preview.src=p.stream_path;preview.alt=p.label+" preview";preview.style.maxHeight="220px";url.textContent="OBS: http://"+window.location.hostname+":8080"+p.stream_path;row.className="row";row.append(url);card.append(title,state,preview,row);if(p.managed_independently){const remove=document.createElement("button");remove.textContent="Remove stream";remove.onclick=async()=>{remove.disabled=true;try{await request("/api/streams/remove",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({profile_id:p.id})});await refreshStreams()}catch(e){state.textContent=e.message;remove.disabled=false}};card.append(remove)}root.append(card)}}
async function refreshStreams(){try{const d=await request("/api/streams");streams=d.streams||[];fillStreamTargets();renderStreams()}catch(e){$("stream-profiles").textContent="Unable to load streams: "+e.message}}
async function refreshTimelapse(){try{const d=await request("/api/timelapse");const c=d.config,s=d.status;$("timelapse-enabled").checked=c.enabled;$("timelapse-interval").value=c.interval_seconds;$("timelapse-start").value=c.schedule_start;$("timelapse-end").value=c.schedule_end;$("timelapse-retention").value=c.retention_days;$("timelapse-storage").value=c.max_storage_mb;$("timelapse-free").value=c.min_free_mb;$("timelapse-width").value=c.max_width;$("timelapse-quality").value=c.jpeg_quality;$("timelapse-path").value=c.storage_path;$("timelapse-status").textContent="Status: "+s.state+" · "+s.frames+" frames · "+Math.round(s.storage_bytes/1048576)+" MiB"+(s.last_capture_at?" · Last capture: "+s.last_capture_at:"")+(s.last_error?" · "+s.last_error:"")}catch(e){$("timelapse-status").textContent="Timelapse status unavailable: "+e.message}}
$("refresh-timelapse").onclick=refreshTimelapse;
$("save-timelapse").onclick=async()=>{const b=$("save-timelapse");b.disabled=true;try{await request("/api/timelapse",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({enabled:$("timelapse-enabled").checked,interval_seconds:Number($("timelapse-interval").value),schedule_start:$("timelapse-start").value,schedule_end:$("timelapse-end").value,retention_days:Number($("timelapse-retention").value),max_storage_mb:Number($("timelapse-storage").value),min_free_mb:Number($("timelapse-free").value),max_width:Number($("timelapse-width").value),jpeg_quality:Number($("timelapse-quality").value),storage_path:$("timelapse-path").value})});await refreshTimelapse()}catch(e){$("timelapse-status").textContent=e.message}finally{b.disabled=false}};
refreshTimelapse();
async function refreshMotion(){
try{
const d=await request("/api/motion"),c=d.config,s=d.status;
$("motion-enabled").checked=c.enabled;$("motion-sensitivity").value=c.sensitivity;$("motion-min-change").value=c.minimum_changed_percent;$("motion-sample").value=c.sample_interval_seconds;$("motion-confirm").value=c.confirm_samples;$("motion-cooldown").value=c.min_event_interval_seconds;$("motion-quiet-enabled").checked=c.quiet_hours_enabled;$("motion-quiet-start").value=c.quiet_hours_start;$("motion-quiet-end").value=c.quiet_hours_end;$("motion-retention").value=c.retention_days;$("motion-storage").value=c.max_storage_mb;$("motion-free").value=c.min_free_mb;
$("motion-status").textContent="Status: "+s.state+" · "+s.event_count+" events · "+s.snapshots+" snapshots · "+Math.round(s.storage_bytes/1048576)+" MiB"+(s.last_event_at?" · Last event: "+s.last_event_at:"")+(s.cpu_percent!==undefined?" · Worker CPU: "+s.cpu_percent+"%":"")+(s.peak_rss_kib!==undefined?" · Peak RAM: "+Math.round(s.peak_rss_kib/1024)+" MiB":"")+(s.last_error?" · "+s.last_error:"");
const root=$("motion-events");root.replaceChildren();if(!s.events.length){root.textContent="No motion events recorded.";return}
for(const event of s.events){const row=document.createElement("div"),img=document.createElement("img"),details=document.createElement("div"),time=document.createElement("div"),change=document.createElement("small");row.className="motion-event";img.src="/api/motion/events/"+encodeURIComponent(event.file);img.alt="Motion snapshot "+event.timestamp;time.textContent=event.timestamp;change.textContent="Changed pixels: "+event.changed_percent+"%";details.append(time,change);row.append(img,details);root.append(row)}
}catch(e){$("motion-status").textContent="Motion status unavailable: "+e.message}
}
$("refresh-motion").onclick=refreshMotion;
$("save-motion").onclick=async()=>{const b=$("save-motion");b.disabled=true;try{await request("/api/motion",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({enabled:$("motion-enabled").checked,sensitivity:Number($("motion-sensitivity").value),minimum_changed_percent:Number($("motion-min-change").value),sample_interval_seconds:Number($("motion-sample").value),confirm_samples:Number($("motion-confirm").value),min_event_interval_seconds:Number($("motion-cooldown").value),quiet_hours_enabled:$("motion-quiet-enabled").checked,quiet_hours_start:$("motion-quiet-start").value,quiet_hours_end:$("motion-quiet-end").value,retention_days:Number($("motion-retention").value),max_storage_mb:Number($("motion-storage").value),min_free_mb:Number($("motion-free").value)})});await refreshMotion()}catch(e){$("motion-status").textContent=e.message}finally{b.disabled=false}};
refreshMotion();setInterval(refreshMotion,15000);

async function refreshDiagnostics(){try{const d=await request("/api/diagnostics");const layers=d.layers||{};const failed=Object.entries(layers).filter(([k,v])=>v&&(v.healthy===false||v.reachable===false||v.resolved===false||v.available===false));$("diagnostic-summary").textContent=failed.length?failed.map(([k])=>k.replaceAll("_"," ")+": attention needed").join(" · "):"Local camera, listener, and network checks look healthy.";const urls=(d.obs_browser_source_urls||[]).join("\n");$("diagnostic-details").textContent="OBS URL(s): "+urls+"\n"+(d.problems||[]).join("\n")+"\n"+d.remote_client_note}catch(e){$("diagnostic-summary").textContent="Diagnostics unavailable: "+e.message}}$("refresh-diagnostics").onclick=refreshDiagnostics;refreshDiagnostics();
$("download-support-bundle").onclick=async()=>{const b=$("download-support-bundle");b.disabled=true;try{let r=await fetch("/api/support-bundle");if(r.status===401){await login();r=await fetch("/api/support-bundle")}if(!r.ok)throw Error("Support bundle request failed ("+r.status+")");const blob=await r.blob(),url=URL.createObjectURL(blob),a=document.createElement("a");a.href=url;a.download="petcam-support.tar.gz";document.body.append(a);a.click();a.remove();URL.revokeObjectURL(url)}catch(e){$("diagnostic-summary").textContent="Support bundle failed: "+e.message}finally{b.disabled=false}};
async function refreshHealth(){try{const h=await request("/api/health");$("health-state").textContent="Status: "+h.state.replaceAll("_"," ");$("health-reason").textContent=h.reason||"No active issue reported."; $("health-details").textContent="Recoveries: "+(h.recovery_count||0)+" · Last checked: "+(h.last_checked_at||"not yet")+(h.last_recovery_at?" · Last recovery: "+h.last_recovery_at:"")}catch(e){$("health-state").textContent="Health status unavailable";$("health-reason").textContent=e.message}}$("refresh-health").onclick=refreshHealth;refresh();refreshHealth();setInterval(refreshHealth,30000);
const autoCheck=$("auto-check");autoCheck.checked=localStorage.getItem("petcam-auto-update-check")==="true";
autoCheck.onchange=()=>localStorage.setItem("petcam-auto-update-check",String(autoCheck.checked));
let updateAvailable=false,updateBusy=false;
async function updateStatus(){try{const s=await request("/api/update/status");const wasBusy=updateBusy;updateBusy=s.active;$("update-state").textContent=s.active?"Installing…":s.result==="failed"?"Last install failed":s.result==="success"?"Installed":"Idle";$("update-logs").textContent=s.logs||"";if(!s.active&&wasBusy){updateBusy=false;await checkUpdates()}$("install-update").disabled=!updateAvailable||s.active}catch(e){$("update-message").textContent=e.message}}
async function checkUpdates(){const b=$("check-updates");b.disabled=true;$("update-message").textContent="Checking signed release metadata…";try{const d=await request("/api/update/check");updateAvailable=d.update_available;$("update-state").textContent=updateAvailable?"Update available":"Up to date";$("update-message").textContent=updateAvailable?"A verified application release is available. Review the notes before installing.":"This device is running the latest published PetCam application."; const releaseList=(d.releases||[]).map(r=>r.version+" · "+Math.ceil(r.archive_size/1048576)+" MiB"+(r.update_available?" · update":" · installed/current")+(r.release_notes?"\\n"+r.release_notes:"")).join("\\n\\n");$("update-details").textContent="Installed: "+(d.installed_version||"unknown")+" · Newest valid: "+d.available_version+"\\n\\nRecent verified releases\\n"+(releaseList||"No valid releases found.");$("install-update").disabled=!updateAvailable||updateBusy}catch(e){$("update-state").textContent="Check failed";$("update-message").textContent=e.message}finally{b.disabled=false}}
$("check-updates").onclick=checkUpdates;
async function installUpdate(){if(!updateAvailable||updateBusy)return;$("update-confirmation").hidden=true;$("install-update").disabled=true;$("check-updates").disabled=true;$("update-message").textContent="Starting verified update…";try{await request("/api/update/install",{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"});updateBusy=true;$("update-state").textContent="Installing…";$("update-message").textContent="The update is downloading and staging. This page may briefly disconnect while services restart.";const poll=async()=>{await updateStatus();if(updateBusy)setTimeout(poll,2000)};setTimeout(poll,1500)}catch(e){$("update-message").textContent=e.message;$("install-update").disabled=false}finally{$("check-updates").disabled=false}}
$("install-update").onclick=()=>{if(!updateAvailable||updateBusy)return;$("update-confirmation").hidden=false;$("confirm-update-install").focus()};
$("cancel-update-install").onclick=()=>{$("update-confirmation").hidden=true;$("install-update").focus()};
$("confirm-update-install").onclick=installUpdate;
if(autoCheck.checked)checkUpdates();updateStatus();
(async()=>{try{const response=await fetch("/api/setup/status",{cache:"no-store"});const state=await response.json();if(!response.ok)throw Error(state.error||"Unable to check setup status");if(!state.configured){document.body.classList.add("setup-required");$("first-run").hidden=false}}catch(error){$("setup-message").textContent=error.message}})();
</script></body></html>"""


def run_camera(command):
    result = subprocess.run(["/usr/local/bin/petcam-camera", *command],
                            capture_output=True, text=True, timeout=30, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Camera discovery failed")
    return json.loads(result.stdout)


def camera_inventory():
    usb = run_camera(["discover"])
    tier = model_tier(read_model())
    performance_budget = PIXEL_BUDGETS[tier]
    result = []
    for item in usb.get("usb_devices", []):
        camera = {"kind": "usb", "device": item["device"],
                  "physical_path": item.get("physical_path", ""),
                  "label": f'USB · {item["device"]}', "modes": [mode for mode in item["modes"] if mode["format"] in USTREAMER_FORMATS]}
        identity = camera_identity(camera)
        result.append({
            **camera, "id": identity["id"], "stable_identity": identity["stable_identity"],
            "hardware_tier": tier, "hardware_budget_pixels_per_second": performance_budget,
        })
    for item in usb.get("csi_cameras", []):
        camera = {"kind": "csi", "camera": item["index"], "sensor": item["sensor"],
                  "device_path": item.get("device_path", ""), "label": f'CSI · {item["sensor"]} (camera {item["index"]})',
                  "modes": item["modes"]}
        identity = camera_identity(camera)
        result.append({
            **camera, "id": identity["id"], "stable_identity": identity["stable_identity"],
            "hardware_tier": tier, "hardware_budget_pixels_per_second": performance_budget,
        })
    return result


def active_camera_config():
    try:
        config = parse_config(CONFIG, check_hardware=False)
    except Exception:
        return None
    return {
        key: config[key]
        for key in ("SOURCE", "DEVICE", "USB_PHYSICAL_PATH", "CSI_DEVICE_PATH", "CAMERA", "FORMAT", "RESOLUTION", "FPS")
    }


def current_resource_budget():
    try:
        return resource_budget_report()
    except Exception:
        return None


def live_capture_status():
    try:
        result = subprocess.run(
            ["/usr/local/bin/petcam-status"],
            capture_output=True, text=True, timeout=15, check=False,
        )
        if result.returncode:
            return {}
        status = json.loads(result.stdout)
        if not isinstance(status, dict):
            return {}
        return {
            "latest_captured_fps": status.get("latest_captured_fps"),
            "active_resolution": status.get("active_resolution"),
            "active_capture_format": status.get("active_capture_format"),
        }
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError):
        return {}



V4L2_CONTROL_LINE = re.compile(
    r"^\s*(?P<name>[a-zA-Z0-9_]+)\s+0x[0-9a-fA-F]+\s+"
    r"\((?P<type>[^)]+)\)\s*:\s*(?P<metadata>.*)$"
)
V4L2_MENU_LINE = re.compile(r"^\s*(?P<value>-?\d+)\s*:\s*(?P<label>.+?)\s*$")


def parse_v4l2_controls(output):
    """Parse v4l2-ctl control metadata without assuming a particular camera model."""
    controls = []
    current = None
    for raw_line in output.splitlines():
        match = V4L2_CONTROL_LINE.match(raw_line)
        if match:
            metadata = match.group("metadata")
            values = {}
            for key in ("min", "max", "step", "default", "value"):
                value_match = re.search(rf"(?:^|\s){key}=(-?\d+)", metadata)
                if value_match:
                    values[key] = int(value_match.group(1))
            flags_match = re.search(r"(?:^|\s)flags=(.+)$", metadata)
            flags = [flag.strip() for flag in flags_match.group(1).split(",")] if flags_match else []
            control_type = match.group("type").lower()
            supported = control_type in ("bool", "int", "menu", "intmenu")
            current = {
                "name": match.group("name"),
                "type": control_type,
                "min": values.get("min"),
                "max": values.get("max"),
                "step": max(1, values.get("step", 1)),
                "default": values.get("default"),
                "value": values.get("value"),
                "flags": flags,
                "menu": [],
                "supported": supported,
                "writable": supported and not any(flag in flags for flag in ("inactive", "read-only", "grabbed")),
            }
            controls.append(current)
            continue
        if current:
            menu_match = V4L2_MENU_LINE.match(raw_line)
            if menu_match:
                current["menu"].append({
                    "value": int(menu_match.group("value")),
                    "label": menu_match.group("label"),
                })
    return controls


def active_usb_camera():
    """Resolve the configured primary USB camera through current discovery, never a client path."""
    config = active_camera_config()
    if not config or config.get("SOURCE") != "usb":
        return None
    physical_path = config.get("USB_PHYSICAL_PATH", "")
    device = config.get("DEVICE", "")
    for camera in camera_inventory():
        if camera.get("kind") != "usb":
            continue
        if physical_path and camera.get("physical_path") == physical_path:
            return camera
        if not physical_path and device and camera.get("device") == device:
            return camera
    return None


def read_camera_controls(camera=None):
    camera = camera or active_usb_camera()
    if not camera:
        return {
            "supported": False,
            "message": "Camera controls are available for the active USB camera. Select and apply a USB camera first.",
            "controls": [],
        }
    device = camera.get("device", "")
    if not device.startswith(("/dev/video", "/dev/v4l/by-id/", "/dev/v4l/by-path/")):
        raise ValueError("The active camera has an invalid V4L2 device path.")
    result = subprocess.run(
        ["/usr/bin/v4l2-ctl", "--device", device, "--list-ctrls-menus"],
        capture_output=True, text=True, timeout=10, check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Unable to read camera controls")
    return {
        "supported": True,
        "device": device,
        "label": camera.get("label", "Active USB camera"),
        "controls": parse_v4l2_controls(result.stdout),
    }


def set_camera_control(data):
    name = data.get("name")
    value = data.get("value")
    if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_]+", name):
        raise ValueError("Invalid camera control name.")
    camera = active_usb_camera()
    if not camera:
        raise ValueError("There is no active USB camera.")
    current = read_camera_controls(camera)
    control = next((item for item in current["controls"] if item["name"] == name), None)
    if not control:
        raise ValueError("That control is not supported by the active camera.")
    if not control["writable"]:
        raise ValueError("That camera control is currently inactive or read-only.")
    if control["type"] == "bool":
        if isinstance(value, bool):
            value = int(value)
        if not isinstance(value, int) or value not in (0, 1):
            raise ValueError("Boolean camera controls must be true or false.")
    else:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("Camera control values must be integers.")
        if control["min"] is not None and value < control["min"]:
            raise ValueError("Camera control value is below the supported minimum.")
        if control["max"] is not None and value > control["max"]:
            raise ValueError("Camera control value is above the supported maximum.")
        if control["min"] is not None and (value - control["min"]) % control["step"]:
            raise ValueError("Camera control value does not match the supported step.")
        if control["type"] in ("menu", "intmenu") and value not in {
            option["value"] for option in control["menu"]
        }:
            raise ValueError("Camera control value is not an advertised menu option.")
    result = subprocess.run(
        ["/usr/bin/v4l2-ctl", "--device", camera["device"], f"--set-ctrl={name}={value}"],
        capture_output=True, text=True, timeout=10, check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Unable to update camera control")
    return read_camera_controls(camera)

def save_selection(data):
    kind = data.get("kind")
    mode = data.get("mode") or {}
    width, height, fps = mode.get("width"), mode.get("height"), mode.get("fps")
    if kind not in ("usb", "csi") or not isinstance(width, int) or not isinstance(height, int):
        raise ValueError("Invalid camera selection")
    if not (1 <= width <= 10000 and 1 <= height <= 10000 and isinstance(fps, (int, float)) and 0 < fps <= 240):
        raise ValueError("Invalid capture mode")

    discovered = camera_inventory()
    camera_id = data.get("camera_id")
    if camera_id:
        match = next((item for item in discovered if item["kind"] == kind and item["id"] == camera_id), None)
    elif kind == "usb":
        physical_path = data.get("physical_path", "")
        device = data.get("device", "")
        match = next((
            item for item in discovered
            if item["kind"] == "usb" and (
                item.get("physical_path") == physical_path if physical_path else item["device"] == device
            )
        ), None)
    else:
        device_path = data.get("device_path", "")
        camera = data.get("camera")
        match = next((
            item for item in discovered
            if item["kind"] == "csi" and (
                item.get("device_path") == device_path if device_path else item["camera"] == camera
            )
        ), None)
    if not match:
        raise ValueError("Selected camera is no longer available.")

    if kind == "usb":
        if not float(fps).is_integer():
            raise ValueError("USB camera FPS must be a whole number")
        if not any(
            item["width"] == width and item["height"] == height
            and item["format"] == mode.get("format")
            and mode_supports_fps(item, float(fps))
            for item in match["modes"]
        ):
            raise ValueError("That USB mode is no longer advertised by the camera")
        device = match["device"]
        physical_path = match.get("physical_path", "")
        values = {
            "SOURCE": "usb",
            "DEVICE": device if device.startswith("/dev/v4l/by-id/") or not physical_path else "",
            "USB_PHYSICAL_PATH": physical_path,
            "CSI_DEVICE_PATH": "",
            "CAMERA": "auto",
            "FORMAT": mode["format"],
            "RESOLUTION": f"{width}x{height}",
            "FPS": str(int(fps)),
        }
    else:
        if not any(
            item["width"] == width and item["height"] == height and abs(item["fps"] - fps) < 0.1
            for item in match["modes"]
        ):
            raise ValueError("That CSI mode is no longer advertised by the camera")
        device_path = match.get("device_path", "")
        values = {
            "SOURCE": "csi",
            "DEVICE": "",
            "USB_PHYSICAL_PATH": "",
            "CSI_DEVICE_PATH": device_path,
            "CAMERA": "auto" if device_path else str(match["camera"]),
            "FORMAT": "auto",
            "RESOLUTION": f"{width}x{height}",
            "FPS": str(fps),
        }
    return apply_primary_config(
        values, match, allow_performance_override=data.get("allow_performance_override") is True,
    )


def proxy_stream(handler, port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/stream", timeout=5) as upstream:
            handler.send_response(upstream.status)
            handler.send_header("Content-Type", upstream.headers.get("Content-Type", "multipart/x-mixed-replace"))
            handler.send_header("Cache-Control", "no-store")
            handler.end_headers()
            while chunk := upstream.read(65536):
                handler.wfile.write(chunk)
                handler.wfile.flush()
    except (OSError, urllib.error.URLError, BrokenPipeError, TimeoutError):
        pass


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


def _origin_ok(handler):
    origin = urlparse(handler.headers.get("Origin", ""))
    return origin.scheme == "https" and origin.netloc.lower() == handler.headers.get("Host", "").lower()


def _cookie(handler):
    for part in handler.headers.get("Cookie", "").split(";"):
        key, sep, value = part.strip().partition("=")
        if sep and key == "petcam_session":
            return value
    return ""


def _body(handler, maximum=8192):
    try:
        length = int(handler.headers.get("Content-Length", "0"))
    except ValueError:
        raise ValueError("Invalid request size")
    if not 1 <= length <= maximum:
        raise ValueError("Invalid request size")
    return json.loads(handler.rfile.read(length))


class Handler(BaseHTTPRequestHandler):
    def send_json(self, status, payload, headers=None):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _session(self):
        return get_session(_cookie(self))

    def _require_auth(self):
        if self._session():
            return True
        self.send_json(401, {"error": "Authentication required"})
        return False

    def _require_csrf(self):
        session = self._session()
        supplied = self.headers.get("X-PetCam-CSRF", "")
        if not session or not _origin_ok(self) or not hmac.compare_digest(session["csrf"], supplied):
            self.send_json(403, {"error": "Request origin or CSRF token is not valid"})
            return False
        return True

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/setup/status":
            self.send_json(200, {"configured": is_configured()})
            return
        if path == "/":
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if not self._require_auth():
            return
        if path == "/api/session":
            self.send_json(200, {"csrf_token": self._session()["csrf"]})
            return
        if path == "/api/timelapse":
            try:
                config = load_timelapse_config()
                self.send_json(200, {"config": config, "status": timelapse_snapshot(config)})
            except Exception as error:
                self.send_json(503, {"error": str(error)})
            return
        if path == "/api/motion":
            try:
                config = load_motion_config()
                self.send_json(200, {"config": config, "status": motion_snapshot(config)})
            except Exception as error:
                self.send_json(503, {"error": str(error)})
            return
        if path.startswith("/api/motion/events/"):
            filename = path.rsplit("/", 1)[-1]
            if not re.fullmatch(r"[0-9]{8}T[0-9]{6}\.[0-9]{6}Z(?:-[0-9]{3})?\.jpg", filename):
                self.send_error(404)
                return
            image_path = MOTION_FRAMES / filename
            try:
                payload = image_path.read_bytes()
            except OSError:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "private, no-store")
            self.end_headers()
            self.wfile.write(payload)
            return
        if path == "/api/diagnostics":
            try:
                self.send_json(200, collect_diagnostics())
            except Exception as error:
                self.send_json(503, {"error": str(error)})
            return
        if path == "/api/support-bundle":
            try:
                body = create_support_bundle()
                self.send_response(200)
                self.send_header("Content-Type", "application/gzip")
                self.send_header("Content-Disposition", "attachment; filename=\"petcam-support.tar.gz\"")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except Exception as error:
                self.send_json(503, {"error": str(error)})
            return
        if path == "/api/health":
            try:
                health = json.loads(HEALTH_FILE.read_text(encoding="utf-8"))
                if not isinstance(health, dict):
                    raise ValueError("Invalid health status document")
                self.send_json(200, health)
            except (OSError, ValueError):
                self.send_json(200, {"state": "not_checked", "reason": "The periodic health monitor has not reported yet.", "recovery_count": 0})
            return
        if path == "/api/update/check":
            try:
                self.send_json(200, update_check())
            except Exception as error:
                self.send_json(503, {"error": str(error)})
            return
        if path == "/api/update/status":
            try:
                self.send_json(200, update_status())
            except Exception as error:
                self.send_json(503, {"error": str(error)})
            return
        if path == "/api/camera-controls":
            try:
                self.send_json(200, read_camera_controls())
            except Exception as error:
                self.send_json(503, {"error": str(error)})
            return
        if path == "/api/cameras":
            try:
                self.send_json(200, {"cameras": camera_inventory(), "active_config": active_camera_config(), "capture_status": live_capture_status(), "resource_budget": current_resource_budget()})
            except Exception as error:
                self.send_json(500, {"error": str(error)})
            return
        if path == "/api/streams":
            try:
                self.send_json(200, {"streams": list_profiles()})
            except Exception as error:
                self.send_json(503, {"error": str(error)})
            return
        if path == "/stream" or path.startswith("/stream/"):
            try:
                profile_id = "default" if path == "/stream" else path[len("/stream/"):]
                proxy_stream(self, stream_port(profile_id))
            except ValueError as error:
                self.send_json(404, {"error": str(error)})
            return
        self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/setup":
            if is_configured():
                self.send_json(409, {"error": "PetCam administrator setup has already been completed."})
                return
            if not _origin_ok(self):
                self.send_json(403, {"error": "Request origin is not allowed"})
                return
            try:
                data = _body(self, 4096)
                if data.get("password") != data.get("password_confirmation"):
                    self.send_json(400, {"error": "The passwords do not match."})
                    return
                initialize_password(data.get("password", ""))
                token, csrf = new_session()
                self.send_json(201, {"ok": True, "csrf_token": csrf}, {
                    "Set-Cookie": f"petcam_session={token}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age={SESSION_TTL}"
                })
            except ValueError as error:
                status = 409 if is_configured() else 400
                self.send_json(status, {"error": str(error)})
            except Exception as error:
                self.send_json(400, {"error": str(error)})
            return
        if path == "/api/login":
            if not _origin_ok(self):
                self.send_json(403, {"error": "Request origin is not allowed"})
                return
            try:
                data = _body(self, 4096)
                peer = self.client_address[0]
                now = time.time()
                with _login_lock:
                    attempts = [stamp for stamp in _login_attempts.get(peer, []) if stamp > now - 300]
                    if len(attempts) >= 5:
                        self.send_json(429, {"error": "Too many sign-in attempts. Wait five minutes and retry."})
                        return
                    attempts.append(now)
                    _login_attempts[peer] = attempts
                if not verify_password(data.get("password", "")):
                    self.send_json(401, {"error": "Invalid password"})
                    return
                with _login_lock:
                    _login_attempts.pop(peer, None)
                token, csrf = new_session()
                self.send_json(200, {"ok": True, "csrf_token": csrf}, {
                    "Set-Cookie": f"petcam_session={token}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age={SESSION_TTL}"
                })
            except Exception as error:
                self.send_json(400, {"error": str(error)})
            return
        if not self._require_auth():
            return
        if not self._require_csrf():
            return
        try:
            data = _body(self)
            if path == "/api/camera-controls":
                self.send_json(200, set_camera_control(data))
                return
            if path == "/api/timelapse":
                current = load_timelapse_config()
                updated = save_timelapse_config({**current, **data})
                self.send_json(200, {"ok": True, "config": updated})
                return
            if path == "/api/motion":
                current = load_motion_config()
                updated = save_motion_config({**current, **data})
                self.send_json(200, {"ok": True, "config": updated})
                return
            if path == "/api/logout":
                invalidate_session(_cookie(self))
                self.send_json(200, {"ok": True}, {
                    "Set-Cookie": "petcam_session=; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=0"
                })
                return
            if path == "/api/update/install":
                start_update()
                self.send_json(202, {"ok": True, "status": "started"})
                return
            if path == "/api/streams":
                camera = next((item for item in camera_inventory() if item["id"] == data.get("camera_id")), None)
                if not camera:
                    raise ValueError("Selected camera is no longer available.")
                profile = create_profile(
                    camera, data.get("mode") or {},
                    allow_performance_override=data.get("allow_performance_override") is True,
                )
                self.send_json(201, {"ok": True, "profile": profile})
                return
            if path == "/api/streams/configure":
                camera = next((item for item in camera_inventory() if item["id"] == data.get("camera_id")), None)
                if not camera:
                    raise ValueError("Selected camera is no longer available.")
                profile = configure_profile(
                    data.get("profile_id", ""), camera, data.get("mode") or {},
                    allow_performance_override=data.get("allow_performance_override") is True,
                )
                self.send_json(200, {"ok": True, "profile": profile})
                return
            if path == "/api/streams/remove":
                remove_profile(data.get("profile_id", ""))
                self.send_json(200, {"ok": True})
                return
            if path == "/api/select":
                save_selection(data)
                self.send_json(200, {"ok": True})
                return
            self.send_error(404)
        except Exception as error:
            self.send_json(400, {"error": str(error)})

    def log_message(self, fmt, *args):
        pass


class StreamHandler(BaseHTTPRequestHandler):
    """Unauthenticated HTTP endpoint reserved for OBS stream clients."""

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/stream":
            profile_id = "default"
        elif path.startswith("/stream/"):
            profile_id = path[len("/stream/"):]
        else:
            self.send_error(404)
            return
        try:
            proxy_stream(self, stream_port(profile_id))
        except ValueError:
            self.send_error(404)

    def log_message(self, fmt, *args):
        pass


def health_monitor_loop():
    from health_monitor import check_health
    last_summary = None
    while True:
        started = time.monotonic()
        try:
            report = check_health()
            summary = (report["state"], report.get("reason", ""), report.get("recovery_count", 0))
            if summary != last_summary:
                print(f"PetCam health: {summary[0]} - {summary[1]}", flush=True)
                last_summary = summary
        except Exception as error:
            print(f"PetCam health monitor failed: {error}", file=sys.stderr, flush=True)
        time.sleep(max(1, 30 - (time.monotonic() - started)))


def main():
    if not TLS_CERT.is_file() or not TLS_KEY.is_file():
        raise SystemExit("PetCam HTTPS certificate is missing.")
    ensure_config_lock()
    threading.Thread(target=health_monitor_loop, name="petcam-health-monitor", daemon=True).start()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(TLS_CERT, TLS_KEY)
    manager = ThreadingHTTPServer(("0.0.0.0", 8443), Handler)
    manager.socket = context.wrap_socket(manager.socket, server_side=True)
    stream = ThreadingHTTPServer(("0.0.0.0", 8080), StreamHandler)
    threads = [
        threading.Thread(target=manager.serve_forever, daemon=True),
        threading.Thread(target=stream.serve_forever, daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        while any(thread.is_alive() for thread in threads):
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        manager.shutdown()
        stream.shutdown()
        manager.server_close()
        stream.server_close()


if __name__ == "__main__":
    main()
