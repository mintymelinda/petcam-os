# Home Assistant MQTT integration

PetCam includes an optional MQTT 3.1.1 publisher that uses Home Assistant MQTT discovery. It is implemented with Python's standard library, so it adds no package dependency. The publisher runs as a separate systemd service and is **not enabled by default**. Camera capture and the web manager do not depend on the MQTT broker.

## Requirements

- Home Assistant with the MQTT integration configured.
- An MQTT broker reachable from the PetCam device.
- MQTT credentials if the broker requires authentication.

Enable MQTT discovery in Home Assistant's MQTT integration. The default discovery prefix is `homeassistant`.

On an existing installation, first install the current signed PetCam application update. The updater installs the signed service unit and seeds the default configuration if it is missing; it preserves an existing Home Assistant configuration and credentials.

## Configure PetCam

Edit `/etc/petcam/home-assistant.conf`:

```ini
MQTT_ENABLED=true
MQTT_HOST=homeassistant.local
MQTT_PORT=1883
MQTT_USERNAME=petcam
MQTT_PASSWORD=replace-with-your-broker-password
MQTT_TLS=false
MQTT_CLIENT_ID=
MQTT_DISCOVERY_PREFIX=homeassistant
MQTT_TOPIC_PREFIX=petcam
MQTT_PUBLISH_STREAM_URL=false
MQTT_STREAM_URL=
```

Use `MQTT_TLS=true` with a TLS-enabled broker listener (commonly port 8883). TLS certificate verification is enabled. Leave username and password empty if the broker allows anonymous clients. A password requires a username.

The file is installed as `root:petcam` with mode `0640`. Keep it that way: it contains the broker credentials. Do not put credentials in the topic prefix, client ID, or stream URL. After changing settings on an already-running publisher, apply them with `sudo systemctl restart petcam-home-assistant.service`.

After saving the configuration, enable and start the publisher:

```sh
sudo systemctl enable --now petcam-home-assistant.service
sudo journalctl -u petcam-home-assistant.service -f
```

Home Assistant discovers these entities automatically:

| Entity | Meaning |
| --- | --- |
| Camera connected | Last camera-presence result from PetCam's health monitor |
| Stream ready | Capture backend and public OBS stream both produced frames |
| Ambient light | TSL2591 bright/dark state using configurable lux thresholds |
| Illuminance | Current TSL2591 reading in lux (lx) |
| Health state | PetCam health-monitor state |
| Active backend | Last detected USB or CSI backend |
| Active mode | Configured resolution, frame rate, and format |
| Health reason | Current health-monitor explanation |

The publisher reports device identity using a non-reversible hash of the device machine ID (or hostname fallback). It publishes retained discovery and state messages. MQTT broker availability uses a retained Last Will (`offline`) so Home Assistant marks the device unavailable if the publisher or network disappears unexpectedly. A separate retained PetCam-health availability topic means MQTT connectivity alone does not make an unhealthy PetCam appear healthy. A disconnected camera remains visible as a disconnected camera instead of making every entity unavailable.

The publisher retries broker failures with bounded backoff. It runs independently of the capture and web-manager services, so a broker outage cannot block or restart camera streaming.

## TSL2591 ambient light sensor

PetCam supports the TSL2591FN over the Raspberry Pi I²C bus. Wire VIN to 3.3V, GND to GND, SDA to GPIO 2 (physical pin 3), and SCL to GPIO 3 (physical pin 5). Enable I²C in Raspberry Pi OS (for example, \`sudo raspi-config\` → Interface Options → I2C), then reboot. The sensor's default I²C address is \`0x29\`.

In the web manager, open Camera Settings → Ambient light sensor, enable it, and set the bright and dark thresholds in lux. Bright is asserted at or above the bright threshold; dark is asserted at or below the dark threshold. Between thresholds, the previous state is retained to prevent rapid toggling. Defaults are 100 lx (bright) and 30 lx (dark). Home Assistant discovers an Ambient light binary sensor and an Illuminance sensor automatically through MQTT. The feature remains optional; a disabled or disconnected sensor reports unknown/unavailable.

The service reads the sensor through Linux \`/dev/i2c-1\` and uses Python's standard library. The service account receives the \`i2c\` supplementary group through the installed I²C utilities package.

## OBS stream URL privacy

The OBS stream URL is **not published by default**. To explicitly publish it as an additional diagnostic sensor, set:

```ini
MQTT_PUBLISH_STREAM_URL=true
# Optional override; otherwise PetCam advertises http://<hostname>.local:8080/stream
MQTT_STREAM_URL=http://petcam.local:8080/stream
```

Only absolute HTTP(S) URLs without embedded credentials, query strings, or fragments are accepted. The URL is sent to the MQTT broker and becomes visible to Home Assistant users with access to that broker. Do not enable this option if the broker is shared with untrusted clients or the stream should remain private.

## Disable or remove

Stop and disable the optional service:

```sh
sudo systemctl disable --now petcam-home-assistant.service
```

To remove retained Home Assistant discovery entities as well, run this once while the broker is reachable and the configuration still contains its connection settings:

```sh
sudo /usr/bin/python3 /opt/petcam/current/lib/home_assistant.py --remove
```

The MQTT integration is read-only. It does not expose camera selection, restart, or configuration commands over MQTT. Those operations remain behind the web manager's authenticated session and CSRF protections.

## Topics

With the defaults, topics are namespaced below `petcam/<device-id>/`:

- `broker`: MQTT publisher online/offline state (including the broker's Last Will).
- `health_availability`: actual PetCam service and health-monitor availability.
- `state/<entity>`: retained Home Assistant entity state.

Home Assistant discovery configuration is retained below `homeassistant/<component>/<device-id>/<entity>/config`.