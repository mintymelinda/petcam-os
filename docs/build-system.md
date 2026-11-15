# PetCam OS image build

The image build has two independent layers.

1. **Base operating system:** GitHub Actions runs `usimd/pi-gen-action` on
   `ubuntu-24.04-arm`. The upstream pi-gen revision is pinned in
   `.github/workflows/build-image.yml`, and only the standard `stage0`,
   `stage1`, and `stage2` stages run.
2. **PetCam product assembly:** the action exports an uncompressed base image.
   Docker Compose starts the `image-assembly` service, built from
   `build/Dockerfile`. Its entry point mounts the image root partition, installs
   the declared packages, builds uStreamer, installs the PetCam application,
   configures its services, then compresses and validates the finished image.

PetCam-specific assembly therefore runs after pi-gen and does not patch or
otherwise depend on pi-gen's internal scripts, Dockerfile, work directory, or
cache layout.

## Architecture

The GitHub runner is ARM64, but the published image remains **32-bit armhf**.
This is intentional: the original Raspberry Pi Zero family requires a 32-bit
userspace. The assembly container includes `qemu-user-static` to execute
armhf commands in the target root filesystem when required. Do not change the
pi-gen target to its 64-bit branch without introducing and validating a
separate 64-bit image/update channel.

## Dependency manifests

- `build/apt-packages.txt` declares Debian packages installed into the target.
  It is checked against the package-stage manifest so the image and build
  declaration cannot silently drift.
- `build/requirements.txt` declares Python dependencies for the containerized
  assembly and Raspberry Pi Imager schema validation.
- `build/Dockerfile` owns host-side assembly tools and Python dependencies.
- `build/compose.yaml` declares the post-image assembly container and its
  required image, device, and udev mounts.

The assembly container needs privileged access to loop devices and mount
operations. It is intended for the disposable GitHub-hosted build runner; do
not run it against untrusted images or on a machine containing sensitive
mounted filesystems.

## Caching

BuildKit caches the Dockerfile layers used to prepare the assembly container,
using the GitHub Actions cache backend. There is deliberately no pi-gen work
directory cache, prepared-rootfs cache, or shared APT archive cache. Each
image build starts from a clean upstream base image and applies the product
assembly steps in order.

## Release safety

The signing private key remains in the GitHub Actions runner environment and
is never passed into the assembly container. The release job still verifies
that the private key matches the public key embedded in the image, signs the
application updater archive and manifest, verifies the required release
assets, and only then publishes the draft release.
