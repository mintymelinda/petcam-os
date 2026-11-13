# PetCam application update engine

PetCam includes a root-owned, one-shot updater for **PetCam application releases only**. It does not upgrade Raspberry Pi OS, the kernel, firmware, bootloader, or boot partition.

## Trust model

Newly built images pin the project's Ed25519 public key at `/etc/petcam/update-signing.pub` and configure the stable GitHub Releases manifest endpoint:

```
https://github.com/mintymelinda/petcam-os/releases/latest/download/petcam-update-manifest.json
```

The detached signature is downloaded from the same URL with `.sig` appended. The endpoint resolves to the latest GitHub Release and is independent of Raspberry Pi Imager's catalogue.

The matching private key is held in the GitHub Actions secret `PETCAM_UPDATE_SIGNING_PRIVATE_KEY`. The tagged-release workflow fails closed if the secret is missing or if its derived public key does not match the public key pinned in the image. The signing private key must never be committed to the repository, copied to a Pi, or included in an image.

## Files and permissions

- `/usr/local/bin/petcam-update`: stable updater entry point, root-owned.
- `/etc/petcam/update.conf`: root-owned, mode 0600; contains the manifest URL and trusted public-key path.
- `/etc/petcam/update-signing.pub`: PEM-encoded Ed25519 public key pinned in the image.
- `/etc/systemd/system/petcam-update.service`: root-owned one-shot service; not enabled for automatic execution.
- `/var/lib/petcam/update/staging/`: root-owned download and extraction workspace.
- `/opt/petcam/releases/<version>/`: versioned application releases.
- `/opt/petcam/current`: atomically replaced symlink selecting the active release.
- `/etc/petcam/petcam.conf`: persistent camera configuration; not replaced by application updates.

The updater service is restricted to write to `/opt/petcam`, `/var/lib/petcam/update`, `/etc/systemd/system`, and `/etc/petcam`. The two `/etc` paths are required because a verified application release installs its managed Home Assistant, timelapse, and motion service units and initializes the Home Assistant configuration when it is first introduced. Existing Home Assistant settings are preserved. The web manager does not run as root and does not receive general root command access.

## Release publication contract

The manifest is UTF-8 JSON. Its detached Ed25519 signature covers the exact manifest bytes.

Example:

```json
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
```

The archive is generated from the exact `/opt/petcam/releases/<version>` directory in the just-built image by `tools/create_update_release.py`. It contains the required application modules and service units, including the optional Home Assistant MQTT publisher, timelapse capture, and motion detection features, plus the bundled µStreamer binary. These optional-feature files are explicit required entries rather than incidental extras, so a missing staged file fails release packaging. The manifest authenticates the application identity, version, architecture, archive URL, exact byte size, and SHA-256 digest.

The updater enforces compressed and expanded size limits and a maximum entry count. It accepts only regular files and directories, rejects links and special files, and does not preserve unsafe archive ownership or permission bits. The release workflow uploads the archive, manifest, and detached signature alongside the flashable image to the same GitHub Release.

## Existing installations

Images built before the update key and stable manifest URL were provisioned require a one-time bootstrap. This provisions the public trust key and manifest URL only; it does not require reflashing and does not require the private key.

Use the public key published in the repository at `stage-petcam/00-install-files/files/etc/petcam/update-signing.pub` and configure:

```sh
sudo install -d -o root -g root -m 0755 /etc/petcam
sudo install -o root -g root -m 0644 /path/to/update-signing.pub /etc/petcam/update-signing.pub
sudo tee /etc/petcam/update.conf >/dev/null <<'CONF'
MANIFEST_URL=https://github.com/mintymelinda/petcam-os/releases/latest/download/petcam-update-manifest.json
PUBLIC_KEY=/etc/petcam/update-signing.pub
CONF
sudo chown root:root /etc/petcam/update.conf
sudo chmod 0600 /etc/petcam/update.conf
sudo /usr/local/bin/petcam-update check
```

Never copy the signing private key to the device.

## Operations

Check signed release metadata without installing:

```sh
sudo /usr/local/bin/petcam-update check
```

Install a verified release through the restricted one-shot service:

```sh
sudo systemctl start petcam-update.service
sudo journalctl -u petcam-update.service
```

Installation is staged before activation. The updater atomically switches `/opt/petcam/current`, reloads systemd, restarts the stream and web services, and checks that the web manager is active and responds locally. A missing or disconnected camera is reported as a warning; camera availability is hardware state and does not itself fail the application update health check.

If the web manager health check fails, the updater restores the previous active release and restarts the services. The previous release is retained; old releases are not automatically deleted. This is application rollback, not operating-system or bootloader rollback.

## Web manager controls

The manager's **Software updates** panel supports manual signed-release checks, installed and available version details, release notes and archive size, explicit install confirmation, progress/status polling, and recent updater journal output.

A browser-local preference can trigger a check when that browser opens the PetCam manager. This is not a background schedule and does not install updates automatically. The manager remains unprivileged; its sudoers allowlist grants only fixed operations to check signed metadata, start the dedicated updater service, and read its recent journal.

## Limitations

- Tagged release publication requires the repository Actions secret `PETCAM_UPDATE_SIGNING_PRIVATE_KEY` and verifies it against the public key embedded in the image.
- Older installations need the one-time public-key and manifest configuration bootstrap.
- Updates cover the PetCam application only, not Raspberry Pi OS, kernel, firmware, or bootloader.
- Checks and installs are user-initiated; periodic background checks and automatic installation are not enabled.
