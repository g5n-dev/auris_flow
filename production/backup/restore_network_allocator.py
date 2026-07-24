#!/usr/bin/env python3
"""Select an unused RFC1918 /24 for an isolated restore drill.

Docker's network-inspect JSON is accepted on stdin.  The allocator validates
the inspect shape and every observed CIDR before choosing a deterministic
private subnet that does not overlap a Docker network or an explicitly
provided host route.
"""

from __future__ import annotations

import argparse
import bisect
import ipaddress
import json
import sys
from collections.abc import Iterator, Mapping, Sequence
from typing import Any


MAX_INSPECT_JSON_BYTES = 1024 * 1024
MAX_NETWORKS = 2048
MAX_IPAM_CONFIGS_PER_NETWORK = 64
MAX_HOST_ROUTE_CIDRS = 4096
MAX_CIDR_CHARS = 64
EDGE_HOST_OFFSET = 10

_PREFERRED_BLOCK = ipaddress.IPv4Network("172.31.48.0/20")
_RFC1918_BLOCKS: tuple[ipaddress.IPv4Network, ...] = (
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("192.168.0.0/16"),
)


class AllocationError(ValueError):
    """Raised when network observations are invalid or no subnet is free."""


def _reject_nonfinite_json(_value: str) -> None:
    raise AllocationError("Docker network inspect JSON is invalid")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AllocationError("Docker network inspect JSON has duplicate keys")
        result[key] = value
    return result


def _load_inspect_json(raw: bytes) -> object:
    if not isinstance(raw, bytes):
        raise AllocationError("Docker network inspect input must be bytes")
    if not raw:
        raise AllocationError("Docker network inspect JSON is empty")
    if len(raw) > MAX_INSPECT_JSON_BYTES:
        raise AllocationError("Docker network inspect JSON is too large")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AllocationError("Docker network inspect JSON is invalid") from exc
    if text.startswith("\ufeff") or "\x00" in text:
        raise AllocationError("Docker network inspect JSON is invalid")
    try:
        return json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_nonfinite_json,
        )
    except AllocationError:
        raise
    except (RecursionError, ValueError) as exc:
        raise AllocationError("Docker network inspect JSON is invalid") from exc


def _canonical_network(
    value: object,
    *,
    label: str,
) -> ipaddress.IPv4Network | ipaddress.IPv6Network:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_CIDR_CHARS
        or not value.isascii()
        or value.strip() != value
    ):
        raise AllocationError(f"{label} must be a canonical CIDR")
    try:
        network = ipaddress.ip_network(value, strict=True)
    except ValueError as exc:
        raise AllocationError(f"{label} must be a canonical CIDR") from exc
    if str(network) != value:
        raise AllocationError(f"{label} must be a canonical CIDR")
    return network


def _docker_subnets(document: object) -> list[ipaddress.IPv4Network]:
    if not isinstance(document, list):
        raise AllocationError("Docker network inspect JSON must be an array")
    if len(document) > MAX_NETWORKS:
        raise AllocationError("Docker network inspect JSON has too many networks")

    observed: list[ipaddress.IPv4Network] = []
    for network_index, network in enumerate(document):
        if not isinstance(network, Mapping):
            raise AllocationError("Docker network inspect entry is invalid")
        ipam = network.get("IPAM")
        if not isinstance(ipam, Mapping):
            raise AllocationError("Docker network inspect IPAM is invalid")
        configs = ipam.get("Config")
        if not isinstance(configs, list):
            raise AllocationError("Docker network inspect IPAM Config is invalid")
        if len(configs) > MAX_IPAM_CONFIGS_PER_NETWORK:
            raise AllocationError(
                "Docker network inspect JSON has too many IPAM configs"
            )
        for config_index, config in enumerate(configs):
            if not isinstance(config, Mapping):
                raise AllocationError("Docker network inspect IPAM config is invalid")
            if "Subnet" not in config:
                continue
            parsed = _canonical_network(
                config["Subnet"],
                label=(f"Docker network inspect subnet {network_index}:{config_index}"),
            )
            if isinstance(parsed, ipaddress.IPv4Network):
                observed.append(parsed)
    return observed


def _host_route_subnets(
    cidrs: Sequence[str],
) -> list[ipaddress.IPv4Network]:
    if isinstance(cidrs, (str, bytes)) or not isinstance(cidrs, Sequence):
        raise AllocationError("host route CIDRs must be a sequence")
    if len(cidrs) > MAX_HOST_ROUTE_CIDRS:
        raise AllocationError("too many host route CIDRs")

    observed: list[ipaddress.IPv4Network] = []
    for index, cidr in enumerate(cidrs):
        parsed = _canonical_network(cidr, label=f"host route CIDR {index}")
        if isinstance(parsed, ipaddress.IPv4Network):
            observed.append(parsed)
    return observed


def _merged_intervals(
    networks: Sequence[ipaddress.IPv4Network],
) -> tuple[list[int], list[int]]:
    intervals = sorted(
        (int(network.network_address), int(network.broadcast_address))
        for network in networks
    )
    merged: list[tuple[int, int]] = []
    for start, end in intervals:
        if merged and start <= merged[-1][1] + 1:
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return (
        [start for start, _end in merged],
        [end for _start, end in merged],
    )


def _overlaps_intervals(
    candidate: ipaddress.IPv4Network,
    starts: Sequence[int],
    ends: Sequence[int],
) -> bool:
    candidate_start = int(candidate.network_address)
    candidate_end = int(candidate.broadcast_address)
    index = bisect.bisect_right(starts, candidate_end) - 1
    return index >= 0 and ends[index] >= candidate_start


def _candidate_subnets() -> Iterator[ipaddress.IPv4Network]:
    yield from _PREFERRED_BLOCK.subnets(new_prefix=24)
    for block in _RFC1918_BLOCKS:
        for subnet in block.subnets(new_prefix=24):
            if subnet.subnet_of(_PREFERRED_BLOCK):
                continue
            yield subnet


def allocate_from_inspect_json(
    raw: bytes,
    *,
    host_route_cidrs: Sequence[str] = (),
) -> dict[str, str]:
    """Return a collision-free subnet and its static edge address."""

    docker_networks = _docker_subnets(_load_inspect_json(raw))
    host_routes = _host_route_subnets(host_route_cidrs)
    starts, ends = _merged_intervals((*docker_networks, *host_routes))

    for candidate in _candidate_subnets():
        if _overlaps_intervals(candidate, starts, ends):
            continue
        edge_ip = ipaddress.IPv4Address(
            int(candidate.network_address) + EDGE_HOST_OFFSET
        )
        if edge_ip not in candidate:
            raise AllocationError("allocator produced an invalid edge address")
        return {
            "subnet": str(candidate),
            "edge_ip": str(edge_ip),
        }
    raise AllocationError("no collision-free RFC1918 /24 subnet is available")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read Docker network inspect JSON from stdin and allocate an "
            "unused RFC1918 /24."
        )
    )
    parser.add_argument(
        "--host-route",
        action="append",
        default=[],
        metavar="CIDR",
        help="reserve a canonical host-route CIDR; may be repeated",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    raw = sys.stdin.buffer.read(MAX_INSPECT_JSON_BYTES + 1)
    try:
        allocation = allocate_from_inspect_json(
            raw,
            host_route_cidrs=tuple(args.host_route),
        )
    except AllocationError as exc:
        print(
            f"restore network allocation failed: {exc}",
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            allocation,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
