#!/usr/bin/env python3
"""Apply cache-aware build changes to the pinned pi-gen checkout."""

from pathlib import Path
import sys


def replace_once(path: Path, old: str, new: str, description: str) -> None:
    content = path.read_text(encoding="utf-8")
    if content.count(old) != 1:
        raise SystemExit(f"pi-gen {description} no longer matches the pinned revision")
    path.write_text(content.replace(old, new, 1), encoding="utf-8")


def prepare_pigen(pigen_dir: Path) -> None:
    build_script = pigen_dir / "build-docker.sh"
    replace_once(
        build_script,
        '${DOCKER} build ${PLATFORM:+--platform "${PLATFORM}"} --build-arg BASE_IMAGE=${BASE_IMAGE} -t pi-gen "${DIR}"',
        '${DOCKER} buildx build --load --cache-from type=gha,scope=pigen-builder '
        '--cache-to type=gha,scope=pigen-builder,mode=max '
        '${PLATFORM:+--platform "${PLATFORM}"} --build-arg BASE_IMAGE=${BASE_IMAGE} '
        '-t pi-gen "${DIR}"',
        "Docker build command",
    )

    common = pigen_dir / "scripts" / "common"
    mount = """on_chroot() {
	if [ -n "${APT_ARCHIVE_CACHE:-}" ]; then
		# APT normally removes downloaded archives after installation.
		# Keep them in the shared runner cache so later builds can reuse them.
		mkdir -p "${ROOTFS_DIR}/etc/apt/apt.conf.d"
		printf '%s\\n' 'APT::Keep-Downloaded-Packages "true";' > "${ROOTFS_DIR}/etc/apt/apt.conf.d/99-pigen-archive-cache"
		if ! mount | grep -q " ${ROOTFS_DIR}/var/cache/apt/archives "; then
			mkdir -p "${ROOTFS_DIR}/var/cache/apt/archives"
			mount --bind "${APT_ARCHIVE_CACHE}" "${ROOTFS_DIR}/var/cache/apt/archives"
		fi
	fi
"""
    replace_once(common, "on_chroot() {\n", mount, "APT cache mount hook")
    replace_once(
        common,
        'setarch linux32 capsh $CAPSH_ARG "--chroot=${ROOTFS_DIR}/" -- -e "$@"',
        'APT_ARCHIVE_CACHE="${APT_ARCHIVE_CACHE:-}" setarch linux32 capsh '
        '$CAPSH_ARG "--chroot=${ROOTFS_DIR}/" -- -e "$@"',
        "chroot invocation",
    )

    export_cleanup = pigen_dir / "export-image" / "02-set-sources" / "01-run.sh"
    replace_once(
        export_cleanup,
        "apt-get clean\n",
        'if [ -z "${APT_ARCHIVE_CACHE:-}" ]; then\n\tapt-get clean\nfi\n',
        "APT cleanup command",
    )
    cleanup_config = (
        'rm -f "${ROOTFS_DIR}/etc/apt/apt.conf.d/99-pigen-archive-cache"\n'
    )
    content = export_cleanup.read_text(encoding="utf-8")
    if not content.endswith("EOF\n"):
        raise SystemExit("pi-gen export cleanup no longer ends with the expected chroot heredoc")
    export_cleanup.write_text(content + cleanup_config, encoding="utf-8")

    # pi-gen's finalise stage pre-populates apt-listchanges' database by
    # walking changelogs for the entire installed package set. That scan is
    # expensive in image builds and has no reader; retain timer disablement
    # but leave apt-listchanges itself available for later interactive APT use.
    finalise = pigen_dir / "export-image" / "05-finalise" / "01-run.sh"
    replace_once(
        finalise,
        '\t\tpython3 -m apt_listchanges.populate_database --profile apt\\n',
        "",
        "apt-listchanges changelog database population",
    )


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: prepare_pigen_build.py PI_GEN_DIRECTORY")
    prepare_pigen(Path(sys.argv[1]))
