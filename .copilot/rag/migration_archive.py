from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import tarfile
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qsl, urlsplit


CHECKSUM_FILE = "SHA256SUMS"
BUFFER_SIZE = 1024 * 1024
REQUIRED_INTEGRATION_FILES = {
    "instructions/rag.instructions.md",
    "skills/local-rag/SKILL.md",
    "skills/local-rag-admin/SKILL.md",
}
ALLOWED_OUTSIDE_RAG_DIRECTORIES = {
    "instructions",
    "skills",
    "skills/local-rag",
    "skills/local-rag-admin",
}


class MigrationArchiveError(RuntimeError):
    pass


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(BUFFER_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def safe_relative_path(value: str) -> PurePosixPath:
    if (
        not value
        or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise MigrationArchiveError("invalid archive path")
    relative = PurePosixPath(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise MigrationArchiveError(f"unsafe archive path: {value}")
    return relative


def iter_regular_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        if path.is_symlink():
            raise MigrationArchiveError(f"symlink is not allowed: {path}")
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            safe_relative_path(relative)
            files.append(path)
        elif path.exists() and not path.is_dir():
            raise MigrationArchiveError(
                f"special filesystem entry is not allowed: {path}"
            )
    return sorted(files, key=lambda item: item.relative_to(root).as_posix())


def write_checksums(root: Path, output: Path) -> None:
    root = root.resolve()
    output = output.resolve()
    if output.parent != root:
        raise MigrationArchiveError("checksum file must be under the bundle root")
    lines: list[str] = []
    for path in iter_regular_files(root):
        if path == output:
            continue
        relative = path.relative_to(root).as_posix()
        lines.append(f"{sha256_path(path)}  {relative}\n")
    output.write_text("".join(lines), encoding="utf-8", newline="\n")


def parse_checksums(root: Path) -> dict[str, str]:
    checksum_path = root / CHECKSUM_FILE
    try:
        lines = checksum_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise MigrationArchiveError("SHA256SUMS is missing or unreadable") from exc
    expected: dict[str, str] = {}
    for line in lines:
        if len(line) < 67 or line[64:66] != "  ":
            raise MigrationArchiveError("invalid SHA256SUMS record")
        digest = line[:64].casefold()
        if any(character not in "0123456789abcdef" for character in digest):
            raise MigrationArchiveError("invalid SHA-256 digest")
        relative = safe_relative_path(line[66:]).as_posix()
        if relative == CHECKSUM_FILE or relative in expected:
            raise MigrationArchiveError("duplicate or recursive checksum record")
        expected[relative] = digest
    return expected


def parse_manifest(root: Path) -> dict[str, str]:
    manifest_path = root / "MANIFEST.txt"
    try:
        lines = manifest_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise MigrationArchiveError("MANIFEST.txt is missing or unreadable") from exc
    manifest: dict[str, str] = {}
    for line in lines:
        if not line or "=" not in line:
            raise MigrationArchiveError("invalid MANIFEST.txt record")
        key, value = line.split("=", 1)
        if not key or key in manifest:
            raise MigrationArchiveError("duplicate or empty manifest key")
        manifest[key] = value
    required = {
        "schema": "local-rag-migration-v1",
        "rag_policy": "blacklist",
        "outside_rag_policy": "exact_whitelist",
        "outside_rag_whitelist": (
            "instructions/rag.instructions.md,"
            "skills/local-rag/SKILL.md,"
            "skills/local-rag-admin/SKILL.md"
        ),
    }
    for key, expected in required.items():
        if manifest.get(key) != expected:
            raise MigrationArchiveError(
                f"invalid migration manifest contract: {key}"
            )
    if manifest.get("network_config_included") not in {"true", "false"}:
        raise MigrationArchiveError(
            "invalid migration manifest contract: network_config_included"
        )
    return manifest


def verify_payload_topology(root: Path, manifest: dict[str, str]) -> None:
    copilot = root / ".copilot"
    rag = copilot / "rag"
    if not copilot.is_dir() or not rag.is_dir():
        raise MigrationArchiveError("required .copilot/rag payload is missing")
    for relative in REQUIRED_INTEGRATION_FILES:
        if not (copilot / relative).is_file():
            raise MigrationArchiveError(
                f"required integration file is missing: {relative}"
            )

    for path in copilot.rglob("*"):
        relative = path.relative_to(copilot).as_posix()
        if relative == "rag" or relative.startswith("rag/"):
            continue
        if path.is_file() and relative in REQUIRED_INTEGRATION_FILES:
            continue
        if path.is_dir() and relative in ALLOWED_OUTSIDE_RAG_DIRECTORIES:
            continue
        raise MigrationArchiveError(
            f"unexpected path outside .copilot/rag: {relative}"
        )

    network_config = rag / "config/network.json"
    network_expected = manifest["network_config_included"] == "true"
    if network_config.is_file() != network_expected:
        raise MigrationArchiveError(
            "network_config_included does not match the payload"
        )
    try:
        expected_count = int(manifest["file_count"])
    except (KeyError, ValueError) as exc:
        raise MigrationArchiveError("invalid manifest file_count") from exc
    actual_count = sum(1 for path in copilot.rglob("*") if path.is_file())
    if expected_count != actual_count:
        raise MigrationArchiveError(
            f"manifest file_count mismatch: expected={expected_count}, "
            f"actual={actual_count}"
        )


def verify_extracted(root: Path) -> None:
    root = root.resolve()
    expected = parse_checksums(root)
    actual: dict[str, Path] = {}
    for path in iter_regular_files(root):
        relative = path.relative_to(root).as_posix()
        if relative == CHECKSUM_FILE:
            continue
        actual[relative] = path
    if set(expected) != set(actual):
        missing = sorted(set(expected) - set(actual))
        unlisted = sorted(set(actual) - set(expected))
        raise MigrationArchiveError(
            "checksum coverage mismatch: "
            f"missing={missing[:3]}, unlisted={unlisted[:3]}"
        )
    for relative, expected_digest in expected.items():
        if sha256_path(actual[relative]) != expected_digest:
            raise MigrationArchiveError(f"checksum mismatch: {relative}")
    manifest = parse_manifest(root)
    verify_payload_topology(root, manifest)


def validated_members(
    package: tarfile.TarFile,
    *,
    bundle_root: str,
) -> list[tarfile.TarInfo]:
    expected_root = PurePosixPath(bundle_root)
    seen: set[str] = set()
    members: list[tarfile.TarInfo] = []
    for member in package.getmembers():
        relative = safe_relative_path(member.name)
        if relative != expected_root and expected_root not in relative.parents:
            raise MigrationArchiveError(
                f"archive member is outside {bundle_root}: {member.name}"
            )
        normalized = relative.as_posix()
        if normalized in seen:
            raise MigrationArchiveError(f"duplicate archive member: {normalized}")
        seen.add(normalized)
        if not (member.isfile() or member.isdir()):
            raise MigrationArchiveError(
                f"links and special archive members are forbidden: {member.name}"
            )
        members.append(member)
    if bundle_root not in seen:
        raise MigrationArchiveError("bundle root directory is missing")
    return members


def safe_extract_and_verify(
    archive: Path,
    destination: Path,
    *,
    bundle_root: str,
) -> None:
    archive = archive.resolve()
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise MigrationArchiveError("verification directory must be empty")

    with tarfile.open(archive, mode="r:gz") as package:
        members = validated_members(package, bundle_root=bundle_root)
        required_bytes = sum(member.size for member in members if member.isfile())
        if required_bytes > shutil.disk_usage(destination).free:
            raise MigrationArchiveError(
                "insufficient free space for archive verification"
            )
        for member in members:
            relative = safe_relative_path(member.name)
            target = destination.joinpath(*relative.parts)
            resolved_parent = target.parent.resolve()
            try:
                resolved_parent.relative_to(destination)
            except ValueError as exc:
                raise MigrationArchiveError(
                    f"archive path escapes verification root: {member.name}"
                ) from exc
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = package.extractfile(member)
            if source is None:
                raise MigrationArchiveError(
                    f"archive member is unreadable: {member.name}"
                )
            with source, target.open("xb") as output:
                shutil.copyfileobj(source, output, length=BUFFER_SIZE)
            os.chmod(target, stat.S_IMODE(member.mode) & 0o777)

    verify_extracted(destination / bundle_root)


def tree_fingerprint(root: Path, output: Path) -> None:
    root = root.resolve()
    records: list[dict[str, object]] = []
    if root.is_dir():
        for path in iter_regular_files(root):
            if path.name.endswith(("-shm", "-wal", "-journal")):
                continue
            records.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "size": path.stat().st_size,
                    "sha256": sha256_path(path),
                }
            )
    payload = {"schema": "local-rag-tree-fingerprint-v1", "files": records}
    output.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


def validate_network_config(path: Path) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MigrationArchiveError(
            "network configuration is missing, unreadable, or invalid"
        ) from exc
    if not isinstance(payload, dict):
        raise MigrationArchiveError("network configuration must be a JSON object")

    def sensitive_key(value: object) -> bool:
        normalized = "".join(
            character
            for character in str(value).casefold()
            if character.isalnum()
        )
        if normalized in {"user", "username", "proxyuser", "proxyusername"}:
            return True
        return any(
            marker in normalized
            for marker in ("password", "passwd", "token", "secret", "credential")
        )

    def reject_persisted_credentials(value: object) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if sensitive_key(key):
                    raise MigrationArchiveError(
                        "proxy credentials must not be stored in "
                        "network configuration"
                    )
                reject_persisted_credentials(child)
        elif isinstance(value, list):
            for child in value:
                reject_persisted_credentials(child)

    reject_persisted_credentials(payload)

    proxy_url = payload.get("proxy_url")
    if proxy_url is None or proxy_url == "":
        return
    if not isinstance(proxy_url, str):
        raise MigrationArchiveError("proxy_url must be a string or null")
    parsed = urlsplit(proxy_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise MigrationArchiveError("proxy_url must be a valid HTTP or HTTPS URL")
    if parsed.username is not None or parsed.password is not None:
        raise MigrationArchiveError(
            "proxy URL must not contain a username or password"
        )
    url_parameters = [
        *parse_qsl(parsed.query, keep_blank_values=True),
        *parse_qsl(parsed.fragment, keep_blank_values=True),
    ]
    if any(sensitive_key(key) for key, _ in url_parameters):
        raise MigrationArchiveError(
            "proxy URL must not contain credential query parameters"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    checksums = subparsers.add_parser("write-checksums")
    checksums.add_argument("--root", type=Path, required=True)
    checksums.add_argument("--output", type=Path, required=True)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--archive", type=Path, required=True)
    verify.add_argument("--destination", type=Path, required=True)
    verify.add_argument("--bundle-root", required=True)

    fingerprint = subparsers.add_parser("fingerprint-tree")
    fingerprint.add_argument("--root", type=Path, required=True)
    fingerprint.add_argument("--output", type=Path, required=True)

    network = subparsers.add_parser("validate-network-config")
    network.add_argument("--path", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "write-checksums":
            write_checksums(args.root, args.output)
        elif args.command == "verify":
            safe_extract_and_verify(
                args.archive,
                args.destination,
                bundle_root=args.bundle_root,
            )
        elif args.command == "fingerprint-tree":
            tree_fingerprint(args.root, args.output)
        elif args.command == "validate-network-config":
            validate_network_config(args.path)
        else:
            raise MigrationArchiveError("unknown command")
    except (MigrationArchiveError, OSError, tarfile.TarError) as exc:
        print(f"error: {exc}", file=os.sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
