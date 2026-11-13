#!/usr/bin/env python3
"""TSL2591 ambient-light monitor over Linux I2C."""
from __future__ import annotations
import fcntl, json, os, signal, sys, time
from pathlib import Path
CONFIG=Path("/etc/petcam/light-sensor.conf")
STATUS=Path("/var/lib/petcam/light-sensor.json")
I2C_SLAVE=0x0703
COMMAND,ENABLE,CONTROL,ID,CHANNEL0,CHANNEL1=0xA0,0x00,0x01,0x12,0x14,0x16
STOP=False
DEFAULTS={"ENABLED":"false","I2C_BUS":"1","I2C_ADDRESS":"0x29","BRIGHT_THRESHOLD_LUX":"100","DARK_THRESHOLD_LUX":"30","POLL_SECONDS":"10"}
class SensorError(RuntimeError): pass
def validate(values):
    if str(values["ENABLED"]).lower() not in {"true","false"}: raise SensorError("ENABLED must be true or false.")
    try:
        bus=int(str(values["I2C_BUS"])); address=int(str(values["I2C_ADDRESS"]),0)
        bright=float(str(values["BRIGHT_THRESHOLD_LUX"])); dark=float(str(values["DARK_THRESHOLD_LUX"])); poll=int(str(values["POLL_SECONDS"]))
    except ValueError as error: raise SensorError("Light sensor settings contain an invalid number.") from error
    if not 0<=bus<=255 or not 0x03<=address<=0x77: raise SensorError("I2C bus or 7-bit device address is out of range.")
    if not 0<=dark<bright<=100000: raise SensorError("Thresholds must satisfy 0 <= dark < bright <= 100000 lux.")
    if not 2<=poll<=300: raise SensorError("POLL_SECONDS must be between 2 and 300.")
    return {"ENABLED":str(values["ENABLED"]).lower()=="true","I2C_BUS":bus,"I2C_ADDRESS":address,"BRIGHT_THRESHOLD_LUX":bright,"DARK_THRESHOLD_LUX":dark,"POLL_SECONDS":poll}
def load_config(path=CONFIG):
    values=dict(DEFAULTS)
    try: lines=path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError: return validate(values)
    for number,raw in enumerate(lines,1):
        line=raw.strip()
        if not line or line.startswith("#"): continue
        if "=" not in line: raise SensorError(f"Invalid setting on line {number}.")
        key,value=(part.strip() for part in line.split("=",1))
        if key not in values: raise SensorError(f"Unknown light sensor setting: {key}.")
        values[key]=value
    return validate(values)
def save_config(updates,path=CONFIG):
    if not isinstance(updates,dict): raise SensorError("Settings must be an object.")
    unknown=set(updates)-set(DEFAULTS)
    if unknown: raise SensorError("Unknown setting(s): "+", ".join(sorted(unknown)))
    current=load_config(path)
    values={**DEFAULTS,**{k:str(v).lower() if k=="ENABLED" else str(v) for k,v in current.items()},**{k:str(v).lower() if k=="ENABLED" else str(v) for k,v in updates.items()}}
    result=validate(values)
    output={"ENABLED":str(result["ENABLED"]).lower(),"I2C_BUS":str(result["I2C_BUS"]),"I2C_ADDRESS":hex(result["I2C_ADDRESS"]),"BRIGHT_THRESHOLD_LUX":str(result["BRIGHT_THRESHOLD_LUX"]),"DARK_THRESHOLD_LUX":str(result["DARK_THRESHOLD_LUX"]),"POLL_SECONDS":str(result["POLL_SECONDS"])}
    path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_name(path.name+".tmp")
    tmp.write_text("# PetCam TSL2591 settings.\n"+"".join(f"{k}={v}\n" for k,v in output.items()),encoding="utf-8")
    os.chmod(tmp,0o640); os.replace(tmp,path)
    return load_config(path)
def read_status(path=STATUS):
    try:
        value=json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value,dict) else {"state":"unknown","available":False}
    except (OSError,ValueError): return {"state":"unknown","available":False}
class TSL2591:
    def __init__(self,bus=1,address=0x29,device_root=Path("/dev")): self.path=device_root/f"i2c-{bus}"; self.address=address; self.fd=None
    def __enter__(self):
        try:
            self.fd=os.open(self.path,os.O_RDWR|os.O_CLOEXEC); fcntl.ioctl(self.fd,I2C_SLAVE,self.address)
            chip=self._read8(ID)
            if chip!=0x50: raise SensorError(f"No TSL2591 detected at 0x{self.address:02x} (chip ID 0x{chip:02x}).")
            self._write8(ENABLE,0x03); self._write8(CONTROL,0x00); time.sleep(.12); return self
        except Exception: self.close(); raise
    def _read8(self,reg):
        os.write(self.fd,bytes([COMMAND|reg])); data=os.read(self.fd,1)
        if len(data)!=1: raise SensorError("Short TSL2591 read.")
        return data[0]
    def _read16(self,reg):
        os.write(self.fd,bytes([COMMAND|reg])); data=os.read(self.fd,2)
        if len(data)!=2: raise SensorError("Short TSL2591 read.")
        return data[0]|(data[1]<<8)
    def _write8(self,reg,value): os.write(self.fd,bytes([COMMAND|reg,value]))
    def read_lux(self):
        ch0=self._read16(CHANNEL0); ch1=self._read16(CHANNEL1)
        if ch0>=0xffff or ch1>=0xffff: raise SensorError("TSL2591 reading saturated.")
        cpl=100.0/408.0
        return round(max(0.0,(ch0-1.64*ch1)/cpl,(.59*ch0-.86*ch1)/cpl),2)
    def close(self):
        if self.fd is not None:
            try: self._write8(ENABLE,0)
            except OSError: pass
            os.close(self.fd); self.fd=None
    def __exit__(self,*_): self.close()
def classify(lux,previous,bright,dark):
    if lux>=bright: return "bright"
    if lux<=dark: return "dark"
    return previous if previous in {"bright","dark"} else "unknown"
def _write_status(value):
    STATUS.parent.mkdir(parents=True,exist_ok=True); tmp=STATUS.with_name(STATUS.name+".tmp")
    tmp.write_text(json.dumps(value,separators=(",",":"))+"\n",encoding="utf-8")
    os.chmod(tmp,0o640); os.replace(tmp,STATUS)
def run(stop=lambda:STOP,sleep=time.sleep,sensor_factory=TSL2591,config_path=CONFIG):
    while not stop():
        try:
            config=load_config(config_path)
            if not config["ENABLED"]: _write_status({"state":"disabled","available":False}); sleep(5); continue
            with sensor_factory(config["I2C_BUS"],config["I2C_ADDRESS"]) as sensor:
                previous=read_status().get("state","unknown")
                while not stop():
                    config=load_config(config_path)
                    if not config["ENABLED"]: _write_status({"state":"disabled","available":False}); break
                    lux=sensor.read_lux(); state=classify(lux,previous,config["BRIGHT_THRESHOLD_LUX"],config["DARK_THRESHOLD_LUX"])
                    _write_status({"state":state,"available":True,"illuminance_lux":lux,"bright_threshold_lux":config["BRIGHT_THRESHOLD_LUX"],"dark_threshold_lux":config["DARK_THRESHOLD_LUX"],"updated_at":time.time()})
                    previous=state; sleep(config["POLL_SECONDS"])
        except (OSError,SensorError,ValueError) as error:
            _write_status({"state":"unknown","available":False,"error":str(error),"updated_at":time.time()})
            print(f"PetCam light sensor unavailable: {error}",file=sys.stderr,flush=True); sleep(10)
    return 0
def _stop_handler(_signum,_frame):
    global STOP
    STOP=True
if __name__=="__main__":
    signal.signal(signal.SIGTERM,_stop_handler); signal.signal(signal.SIGINT,_stop_handler); raise SystemExit(run())
