# PetCam application update engine

The image includes a root-owned, one-shot updater for **PetCam application releases only**. It does not upgrade Raspberry Pi OS, the kernel, firmware, bootloader, or boot partition.

## Current safety state

The updater is deliberately fail-closed. The image ships with an empty MANIFEST_URL and does not ship a release-signing public key. Until the project provisions its official Ed25519 public key and publishes signed release metadata, checks and installs fail with a clear configuration error. Do not generate or substitute a local key on deployed devices: all devices must trust the same project-controlled signing key.

The signing private key must never be committed to this repository or included in an image. It belongs in a protected release-signing environment.

## Files and permissions

- /usr/local/bin/petcam-update: stable updater entry point, root-owned.
- /etc/petcam/update.conf: root-owned, mode 0600; contains the manifest URL and trusted public-key path.
- /etc/petcam/update-signing.pub: PEM-encoded Ed25519 public key; provisioned by a future release-key change.
- /etc/systemd/system/petcam-update.service: root-owned, one-shot service; not enabled for automatic execution.
- /var/lib/petcam/update/staging/: root-owned download and extraction workspace.
- /opt/petcam/releases/<version>/: versioned releases.
- /opt/petcam/current: atomically replaced symlink selecting the active release.
- /etc/petcam/petcam.conf: persistent camera configuration; never changed by the updater.

The updater service can write only to /opt/petcam and /var/lib/petcam/update. It does not run inside the web manager and does not grant the web manager root access.

## Release publication contract

The configured manifest URL returns UTF-8 JSON. Its detached Ed25519 signature is downloaded from the same URL with .sig appended.

Example manifest:

~~~json
{
  "schema_version": 1,
  "application": "petcam-os",
  "version": "1.2.3",
  "architecture": "armhf",
  "archive_url": "https://github.com/mintymelinda/petcam-os/releases/download/v1.2.3/petcam-1.2.3-armhf.tar.gz",
  "archive_size": 12345678,
  "archive_sha256": "64 lowercase hexadecimal characters",
  "release_notes": "Bug fixes and camera compatibility improvements"
}
~~~

The signature covers the exact manifest bytes. The archive must be a gzip-compressed tar containing these files at its root:

- VERSION
- manifest.json (embedded application/version identity)
- lib/camera.py
- lib/csi_stream.py
- lib/web_manager.py
- lib/start-stream.sh
- vendor/bin/ustreamer

The signed manifest authenticates the archive URL, exact byte size, SHA-256 digest, version, application identity, and target architecture. The archive is limited to 300 MiB compressed, 512 MiB expanded, and 10,000 entries. Only regular files and directories are accepted; links and special files are rejected. Extraction does not preserve archive ownership or unsafe permission bits.

The publisher must build the release archive from the same versioned release tree used by the image build, include the version-pinned µStreamer binary, generate the manifest, then sign the exact manifest bytes with the protected Ed25519 release key.

## Operations

Once the official key and release endpoint are provisioned:

~~~sh
# Check the signed release metadata (no installation)
sudo /usr/local/bin/petcam-update check

# Install a verified release through the restricted one-shot service
sudo systemctl start petcam-update.service

# Inspect the last update result
sudo journalctl -u petcam-update.service
~~~

The update is staged before activation. Activation atomically switches /opt/petcam/current, reloads systemd, restarts the stream and web services, then checks that the web manager is active and responds locally. The stream service is also restarted, but a missing or disconnected camera is reported as a warning rather than treated as a failed application update. If the web health check fails, the updater restores the previous active release and restarts the services.

A camera being absent does not itself fail the update health check; camera availability is hardware state, not release integrity. The previous release is retained. The updater does not automatically delete old releases.

## Limitations

- No official signing key or published release endpoint is provisioned yet, so installation is intentionally disabled by default.
- There is no in-place migration for already-flashed images.
- This is application rollback, not operating-system or bootloader rollback.
- The web UI does not yet expose update controls; that is the next integration step.
- Automatic periodic checks and automatic installation are not enabled.
- Release publishing and signing automation must be implemented separately before this can be used by end users.


## Web manager integration

The camera management page includes a Software updates panel with manual signed-release checks, installed/available versions, release notes and archive size, an explicit install confirmation, progress/status polling, and recent updater journal output. A browser-local preference can trigger a check whenever that browser opens the PetCam page; it does not schedule background work or install updates automatically.

The web manager remains unprivileged. Its sudoers allowlist grants only three fixed operations: checking signed metadata, starting the dedicated one-shot update service, and reading that service's recent journal. It cannot invoke an arbitrary root command or run the updater's install command directly. The install endpoint validates the request origin, and the updater still performs all signature, digest, archive, and rollback checks.

The update panel reports application releases only. It does not offer operating-system, kernel, firmware, or bootloader updates.
