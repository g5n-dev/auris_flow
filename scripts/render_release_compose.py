#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_COMPOSE = ROOT / "production" / "compose.yaml"
DEFAULT_ENV_FILE = ROOT / "production" / ".env.example"
IMAGE_LOCK_SCHEMA = "auris.release-image-lock.v1"
ARTIFACT_MANIFEST_SCHEMA = "auris.release-artifact-manifest.v1"
IMAGE_SCAN_PLAN_SCHEMA = "auris.release-image-scan-plan.v1"
RELEASE_TAG_PATTERN = re.compile(
    r"v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:-rc\.[1-9]\d*)?"
)
COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}")


class ReleaseComposeError(RuntimeError):
    """Raised when release image provenance is incomplete or ambiguous."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_bytes_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(content)
    temporary.replace(path)


def _json_bytes(document: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode()


def validate_release_tag(value: str) -> str:
    if RELEASE_TAG_PATTERN.fullmatch(value) is None:
        raise ReleaseComposeError(
            "release tag must be SemVer vMAJOR.MINOR.PATCH or vMAJOR.MINOR.PATCH-rc.N"
        )
    return value


def validate_source_commit(value: str) -> str:
    normalized = value.strip().lower()
    if COMMIT_PATTERN.fullmatch(normalized) is None:
        raise ReleaseComposeError("source commit must be a complete Git object id")
    return normalized


def validate_image_reference(value: str) -> str:
    if "${" in value or value.count("@sha256:") != 1:
        raise ReleaseComposeError("release image must use a concrete sha256 reference")
    name_and_tag, digest = value.rsplit("@sha256:", 1)
    final_segment = name_and_tag.rsplit("/", 1)[-1]
    if ":" not in final_segment:
        raise ReleaseComposeError(
            "release image must retain an explicit tag before digest"
        )
    tag = final_segment.rsplit(":", 1)[1]
    if not tag or tag.casefold() == "latest":
        raise ReleaseComposeError("release image tag must be explicit and non-latest")
    if DIGEST_PATTERN.fullmatch(digest) is None:
        raise ReleaseComposeError("release image digest must be lowercase sha256")
    return value


def create_image_lock(
    *,
    release_tag: str,
    source_commit: str,
    images: Mapping[str, str],
) -> dict[str, Any]:
    if not images:
        raise ReleaseComposeError("release image mapping must not be empty")
    normalized: dict[str, str] = {}
    for service, reference in sorted(images.items()):
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", service):
            raise ReleaseComposeError(f"invalid Compose service name: {service}")
        if service in normalized:
            raise ReleaseComposeError(f"duplicate release image service: {service}")
        normalized[service] = validate_image_reference(reference)
    return {
        "schema_version": IMAGE_LOCK_SCHEMA,
        "release_tag": validate_release_tag(release_tag),
        "source_commit": validate_source_commit(source_commit),
        "images": normalized,
    }


def validate_image_lock(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict) or set(document) != {
        "schema_version",
        "release_tag",
        "source_commit",
        "images",
    }:
        raise ReleaseComposeError("release image lock has missing or unexpected fields")
    if document.get("schema_version") != IMAGE_LOCK_SCHEMA:
        raise ReleaseComposeError("unsupported release image lock schema")
    images = document.get("images")
    if not isinstance(images, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in images.items()
    ):
        raise ReleaseComposeError("release image lock images must be a string map")
    return create_image_lock(
        release_tag=str(document.get("release_tag", "")),
        source_commit=str(document.get("source_commit", "")),
        images=images,
    )


def build_image_scan_plan(image_lock: Mapping[str, Any]) -> dict[str, Any]:
    images = image_lock.get("images")
    if not isinstance(images, Mapping):
        raise ReleaseComposeError("release image lock images are required")
    grouped: dict[str, dict[str, Any]] = {}
    for service, raw_reference in sorted(images.items()):
        reference = validate_image_reference(str(raw_reference))
        digest = "sha256:" + reference.rsplit("@sha256:", 1)[1]
        record = grouped.setdefault(
            digest,
            {
                "digest": digest,
                "image": reference,
                "references": [],
                "services": [],
                "platforms": ["linux/amd64", "linux/arm64"],
            },
        )
        record["references"].append(reference)
        record["services"].append(str(service))
    records = []
    for digest in sorted(grouped):
        record = grouped[digest]
        record["references"] = sorted(set(record["references"]))
        record["services"] = sorted(record["services"])
        records.append(record)
    return {
        "schema_version": IMAGE_SCAN_PLAN_SCHEMA,
        "release_tag": image_lock["release_tag"],
        "source_commit": image_lock["source_commit"],
        "images": records,
    }


def render_release_document(
    source: Mapping[str, Any], image_lock: Mapping[str, Any]
) -> dict[str, Any]:
    services = source.get("services")
    images = image_lock.get("images")
    if not isinstance(services, Mapping) or not isinstance(images, Mapping):
        raise ReleaseComposeError("Compose services and image lock are required")
    source_services = set(services)
    locked_services = set(images)
    if source_services != locked_services:
        missing = sorted(source_services - locked_services)
        extra = sorted(locked_services - source_services)
        detail = []
        if missing:
            detail.append("missing=" + ",".join(missing))
        if extra:
            detail.append("unexpected=" + ",".join(extra))
        raise ReleaseComposeError(
            "image lock does not match Compose services: " + "; ".join(detail)
        )

    result = copy.deepcopy(dict(source))
    rendered_services = result["services"]
    for service_name in sorted(source_services):
        service = rendered_services[service_name]
        if not isinstance(service, dict):
            raise ReleaseComposeError(f"invalid Compose service: {service_name}")
        service["image"] = validate_image_reference(str(images[service_name]))
        service.pop("build", None)
    result["x-auris-release"] = {
        "schema_version": IMAGE_LOCK_SCHEMA,
        "release_tag": image_lock["release_tag"],
        "source_commit": image_lock["source_commit"],
    }
    return result


def relocate_file_references(
    document: dict[str, Any], *, source_dir: Path, output_dir: Path
) -> None:
    for section_name in ("configs", "secrets"):
        section = document.get(section_name)
        if not isinstance(section, dict):
            continue
        for entry in section.values():
            if not isinstance(entry, dict) or not isinstance(entry.get("file"), str):
                continue
            if "${" in entry["file"] and source_dir.resolve() != output_dir.resolve():
                raise ReleaseComposeError(
                    "Compose files with configurable secret paths must be rendered "
                    "beside the source Compose file"
                )
            configured_path = Path(entry["file"])
            if configured_path.is_absolute():
                raise ReleaseComposeError(
                    f"{section_name} file reference must remain repository-relative"
                )
            source_path = (source_dir / configured_path).resolve()
            try:
                source_path.relative_to(ROOT.resolve())
            except ValueError as exc:
                raise ReleaseComposeError(
                    f"{section_name} file reference escapes the repository"
                ) from exc
            entry["file"] = os.path.relpath(source_path, output_dir.resolve())


def render_source_compose(compose_file: Path) -> dict[str, Any]:
    command = [
        "docker",
        "compose",
        "--file",
        str(compose_file),
        "config",
        "--no-interpolate",
        "--no-normalize",
        "--no-path-resolution",
        "--format",
        "json",
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=compose_file.parent,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise ReleaseComposeError("docker compose is required") from exc
    except subprocess.CalledProcessError as exc:
        diagnostic = (exc.stderr or exc.stdout or "Compose rendering failed").strip()
        raise ReleaseComposeError(diagnostic) from exc
    try:
        document = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ReleaseComposeError("docker compose returned invalid JSON") from exc
    if not isinstance(document, dict):
        raise ReleaseComposeError("rendered Compose document must be an object")
    for section_name in ("configs", "secrets", "volumes", "networks"):
        section = document.get(section_name)
        if isinstance(section, dict):
            for entry in section.values():
                if isinstance(entry, dict):
                    entry.pop("name", None)
    return document


def build_artifact_manifest(
    *,
    base_dir: Path,
    image_lock: Mapping[str, Any],
    excluded: Sequence[Path],
) -> tuple[dict[str, Any], str]:
    base = base_dir.resolve()
    excluded_resolved = {path.resolve() for path in excluded}
    artifacts: list[dict[str, Any]] = []
    for path in sorted(base_dir.rglob("*")):
        if not path.is_file() or path.resolve() in excluded_resolved:
            continue
        try:
            relative = path.resolve().relative_to(base).as_posix()
        except ValueError as exc:
            raise ReleaseComposeError(
                "release artifact escaped the output directory"
            ) from exc
        artifacts.append(
            {
                "path": relative,
                "sha256": _sha256_file(path),
                "size": path.stat().st_size,
            }
        )
    if not artifacts:
        raise ReleaseComposeError("release artifact directory is empty")
    manifest = {
        "schema_version": ARTIFACT_MANIFEST_SCHEMA,
        "release_tag": image_lock["release_tag"],
        "source_commit": image_lock["source_commit"],
        "artifacts": artifacts,
    }
    checksum_lines = [f"{item['sha256']}  {item['path']}" for item in artifacts]
    return manifest, "\n".join(checksum_lines) + "\n"


def _read_json(path: Path, description: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseComposeError(f"invalid {description}") from exc


def _parse_image_mappings(values: Sequence[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        service, separator, reference = value.partition("=")
        if not separator or not service or not reference:
            raise ReleaseComposeError("--image must use SERVICE=TAG@sha256:DIGEST")
        if service in result:
            raise ReleaseComposeError(f"duplicate release image service: {service}")
        result[service] = reference
    return result


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create and enforce release image provenance"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    lock_parser = subparsers.add_parser("create-lock")
    lock_parser.add_argument("--release-tag", required=True)
    lock_parser.add_argument("--source-commit", required=True)
    lock_parser.add_argument("--image", action="append", default=[], required=True)
    lock_parser.add_argument("--output", type=Path, required=True)

    render_parser = subparsers.add_parser("render")
    render_parser.add_argument("--compose-file", type=Path, default=DEFAULT_COMPOSE)
    render_parser.add_argument("--image-lock", type=Path, required=True)
    render_parser.add_argument("--output", type=Path, required=True)

    scan_parser = subparsers.add_parser("scan-plan")
    scan_parser.add_argument("--image-lock", type=Path, required=True)
    scan_parser.add_argument("--output", type=Path, required=True)

    manifest_parser = subparsers.add_parser("manifest")
    manifest_parser.add_argument("--base-dir", type=Path, required=True)
    manifest_parser.add_argument("--image-lock", type=Path, required=True)
    manifest_parser.add_argument("--output", type=Path, required=True)
    manifest_parser.add_argument("--checksums-output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "create-lock":
            document = create_image_lock(
                release_tag=args.release_tag,
                source_commit=args.source_commit,
                images=_parse_image_mappings(args.image),
            )
            _write_bytes_atomic(args.output, _json_bytes(document))
        elif args.command == "render":
            image_lock = validate_image_lock(_read_json(args.image_lock, "image lock"))
            source = render_source_compose(args.compose_file)
            rendered = render_release_document(source, image_lock)
            relocate_file_references(
                rendered,
                source_dir=args.compose_file.resolve().parent,
                output_dir=args.output.resolve().parent,
            )
            _write_bytes_atomic(args.output, _json_bytes(rendered))
        elif args.command == "scan-plan":
            image_lock = validate_image_lock(_read_json(args.image_lock, "image lock"))
            scan_plan = build_image_scan_plan(image_lock)
            _write_bytes_atomic(args.output, _json_bytes(scan_plan))
        else:
            image_lock = validate_image_lock(_read_json(args.image_lock, "image lock"))
            base_dir = args.base_dir.resolve()
            for artifact_path in (
                args.image_lock,
                args.output,
                args.checksums_output,
            ):
                try:
                    artifact_path.resolve().relative_to(base_dir)
                except ValueError as exc:
                    raise ReleaseComposeError(
                        "release manifest inputs and outputs must stay inside base-dir"
                    ) from exc
            manifest, checksums = build_artifact_manifest(
                base_dir=args.base_dir,
                image_lock=image_lock,
                excluded=[args.output, args.checksums_output],
            )
            _write_bytes_atomic(args.output, _json_bytes(manifest))
            checksum_with_manifest = checksums + (
                f"{_sha256_file(args.output)}  "
                f"{os.path.relpath(args.output, args.base_dir)}\n"
            )
            _write_bytes_atomic(args.checksums_output, checksum_with_manifest.encode())
    except ReleaseComposeError as exc:
        print(f"Release image policy failed closed: {exc}", file=sys.stderr)
        return 1
    print(f"Release image policy ok: {args.command}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
