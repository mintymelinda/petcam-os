#!/usr/bin/env python3
"""Install PetCam into a pi-gen base image, compress it, and validate Imager metadata."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from jsonschema import validate

ROOT = Path(__file__).resolve().parents[1]
QEMU = Path("/usr/bin/qemu-arm-static")
BIND_MOUNTS = ("dev", "dev/pts", "proc", "sys")


def run(*args: str, cwd: Path | None = None, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        args, cwd=cwd, env=env, check=True, text=True, stdout=subprocess.PIPE
    )
    return result.stdout.strip()


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def run_target(rootfs: Path, script_path: str) -> None:
    run(
        "chroot", str(rootfs), "/usr/bin/qemu-arm-static",
        "/bin/bash", "-e", script_path,
    )


def assemble(rootfs: Path) -> None:
    build_dir = rootfs / "root/petcam-build"
    build_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(QEMU, rootfs / "usr/bin/qemu-arm-static")
    shutil.copy2(ROOT / "build/apt-packages.txt", build_dir / "apt-packages.txt")

    package_script = build_dir / "install-packages.sh"
    package_script.write_text(
        "#!/bin/bash\n"
        "set -euo pipefail\n"
        "export DEBIAN_FRONTEND=noninteractive\n"
        "apt-get update\n"
        "xargs -r apt-get install -y --no-install-recommends < /root/petcam-build/apt-packages.txt\n",
        encoding="utf-8",
    )
    package_script.chmod(0o755)
    run_target(rootfs, "/root/petcam-build/install-packages.sh")

    ustreamer_script = build_dir / "build-ustreamer.sh"
    shutil.copy2(
        ROOT / "stage-petcam-base/02-build-ustreamer/00-run-chroot.sh",
        ustreamer_script,
    )
    run_target(rootfs, "/root/petcam-build/build-ustreamer.sh")

    install_script = ROOT / "stage-petcam/00-install-files/00-run.sh"
    environment = os.environ.copy()
    environment["ROOTFS_DIR"] = str(rootfs)
    subprocess.run(
        ["bash", str(install_script)],
        cwd=install_script.parent,
        env=environment,
        check=True,
    )

    configure_script = build_dir / "configure-petcam.sh"
    shutil.copy2(
        ROOT / "stage-petcam/02-configure-petcam/00-run-chroot.sh",
        configure_script,
    )
    run_target(rootfs, "/root/petcam-build/configure-petcam.sh")
    (rootfs / "usr/bin/qemu-arm-static").unlink(missing_ok=True)
    shutil.rmtree(build_dir)


def main() -> int:
    base_image = Path(required("BASE_IMAGE_PATH")).resolve()
    output_image = Path(required("OUTPUT_IMAGE_PATH")).resolve()
    image_url = required("IMAGE_URL")
    version = required("RELEASE_VERSION")
    schema_path = Path(required("IMAGE_SCHEMA_PATH")).resolve()

    if not base_image.is_file() or base_image.suffix != ".img":
        raise SystemExit(f"Expected an uncompressed pi-gen .img file: {base_image}")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise SystemExit(f"Invalid release version: {version}")
    if not image_url.startswith("https://"):
        raise SystemExit("IMAGE_URL must use HTTPS")
    if not QEMU.is_file() or not schema_path.is_file():
        raise SystemExit("The QEMU helper and Raspberry Pi Imager schema are required.")

    package_manifest = (ROOT / "build/apt-packages.txt").read_text(encoding="utf-8").splitlines()
    stage_manifest = (
        ROOT / "stage-petcam-base/01-install-packages/00-packages"
    ).read_text(encoding="utf-8").splitlines()
    if package_manifest != stage_manifest:
        raise SystemExit("build/apt-packages.txt and the PetCam package stage must match.")

    output_image.parent.mkdir(parents=True, exist_ok=True)
    rootfs = ROOT / ".build/rootfs"
    rootfs.mkdir(parents=True, exist_ok=True)
    loop_device = ""
    mounted: list[Path] = []
    try:
        loop_device = run("losetup", "--find", "--show", "--partscan", str(base_image))
        run("udevadm", "settle", "--timeout=15")
        root_partition = Path(f"{loop_device}p2")
        if not root_partition.exists():
            raise SystemExit(f"Root partition was not created: {root_partition}")
        run("mount", str(root_partition), str(rootfs))
        for name in BIND_MOUNTS:
            target = rootfs / name
            run("mount", "--bind", str(Path("/") / name), str(target))
            mounted.append(target)
        assemble(rootfs)
    finally:
        for target in reversed(mounted):
            subprocess.run(["umount", str(target)], check=False)
        if rootfs.is_mount():
            subprocess.run(["umount", str(rootfs)], check=False)
        if loop_device:
            subprocess.run(["losetup", "-d", loop_device], check=False)

    with output_image.open("wb") as output:
        subprocess.run(
            ["xz", "-T0", "-6", "--force", "--keep", "--stdout", str(base_image)],
            check=True,
            stdout=output,
        )

    subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/create_imager_os_list.py"),
            str(output_image),
            "--image-url",
            image_url,
            "--output",
            str(ROOT / "petcam-os-os-list.json"),
        ],
        check=True,
    )
    catalogue = json.loads((ROOT / "petcam-os-os-list.json").read_text(encoding="utf-8"))
    validate(instance=catalogue, schema=json.loads(schema_path.read_text(encoding="utf-8")))
    entry = catalogue["os_list"][0]
    if entry.get("architecture") != "armhf" or entry.get("init_format") != "cloudinit-rpi":
        raise SystemExit("Generated Imager metadata does not preserve the armhf/cloud-init contract.")
    (ROOT / "build/output/build-info.txt").write_text(
        f"PetCam OS release: {version}\n"
        "pi-gen target: armhf\n"
        "Build host: ubuntu-24.04-arm\n",
        encoding="utf-8",
    )
    print(f"PetCam image assembled: {output_image} ({output_image.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
