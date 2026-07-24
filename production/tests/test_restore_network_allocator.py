from __future__ import annotations

import importlib.util
import ipaddress
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest


ROOT = Path(__file__).resolve().parents[2]
ALLOCATOR = ROOT / "production" / "backup" / "restore_network_allocator.py"


def _load_allocator() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "restore_network_allocator", ALLOCATOR
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _inspect_json(*subnets: str) -> bytes:
    networks = [
        {
            "Name": f"network-{index}",
            "IPAM": {"Config": [{"Subnet": subnet}]},
        }
        for index, subnet in enumerate(subnets)
    ]
    return json.dumps(networks).encode("utf-8")


def test_source_project_subnet_is_never_reused() -> None:
    module = _load_allocator()

    allocation = module.allocate_from_inspect_json(_inspect_json("172.31.48.0/24"))

    subnet = ipaddress.ip_network(allocation["subnet"], strict=True)
    edge_ip = ipaddress.ip_address(allocation["edge_ip"])
    assert subnet == ipaddress.ip_network("172.31.49.0/24")
    assert subnet.is_private
    assert edge_ip in subnet
    assert edge_ip not in {subnet.network_address, subnet.broadcast_address}


def test_docker_default_pool_and_host_routes_are_avoided() -> None:
    module = _load_allocator()

    allocation = module.allocate_from_inspect_json(
        _inspect_json("172.17.0.0/16"),
        host_route_cidrs=("172.31.48.0/24", "172.31.49.0/25"),
    )

    subnet = ipaddress.ip_network(allocation["subnet"], strict=True)
    assert subnet == ipaddress.ip_network("172.31.50.0/24")
    assert not subnet.overlaps(ipaddress.ip_network("172.17.0.0/16"))
    assert not subnet.overlaps(ipaddress.ip_network("172.31.48.0/24"))
    assert not subnet.overlaps(ipaddress.ip_network("172.31.49.0/25"))


def test_all_rfc1918_space_occupied_fails_closed() -> None:
    module = _load_allocator()

    with pytest.raises(module.AllocationError, match="no collision-free"):
        module.allocate_from_inspect_json(
            _inspect_json(
                "10.0.0.0/8",
                "172.16.0.0/12",
                "192.168.0.0/16",
            )
        )


@pytest.mark.parametrize(
    "document",
    [
        _inspect_json("172.31.48.1/24"),
        _inspect_json("172.031.048.0/24"),
        b'[{"IPAM":{"Config":[{"Subnet":"172.31.48.0/24",'
        b'"Subnet":"172.31.49.0/24"}]}}]',
        b'{"IPAM":{"Config":[]}}',
        b'[{"IPAM":{"Config":"172.31.48.0/24"}}]',
        b'[{"IPAM":{"Config":[{"Subnet":NaN}]}}]',
    ],
)
def test_noncanonical_or_malformed_inspect_json_is_rejected(
    document: bytes,
) -> None:
    module = _load_allocator()

    with pytest.raises(module.AllocationError):
        module.allocate_from_inspect_json(document)


def test_noncanonical_and_excessive_host_routes_are_rejected() -> None:
    module = _load_allocator()

    with pytest.raises(module.AllocationError, match="canonical"):
        module.allocate_from_inspect_json(
            b"[]",
            host_route_cidrs=("10.10.10.7/24",),
        )

    with pytest.raises(module.AllocationError, match="too many"):
        module.allocate_from_inspect_json(
            b"[]",
            host_route_cidrs=("10.0.0.0/24",) * (module.MAX_HOST_ROUTE_CIDRS + 1),
        )


def test_oversized_and_deeply_nested_json_are_rejected() -> None:
    module = _load_allocator()

    oversized = b" " * (module.MAX_INSPECT_JSON_BYTES + 1)
    with pytest.raises(module.AllocationError, match="too large"):
        module.allocate_from_inspect_json(oversized)

    nested = ("[" * 2_000 + "]" * 2_000).encode("ascii")
    with pytest.raises(module.AllocationError, match="invalid"):
        module.allocate_from_inspect_json(nested)


def test_excessive_network_or_ipam_config_counts_are_rejected() -> None:
    module = _load_allocator()

    too_many_networks = json.dumps(
        [{"IPAM": {"Config": []}}] * (module.MAX_NETWORKS + 1)
    ).encode("utf-8")
    with pytest.raises(module.AllocationError, match="too many"):
        module.allocate_from_inspect_json(too_many_networks)

    too_many_configs = json.dumps(
        [
            {
                "IPAM": {
                    "Config": [{"Subnet": "172.31.48.0/24"}]
                    * (module.MAX_IPAM_CONFIGS_PER_NETWORK + 1)
                }
            }
        ]
    ).encode("utf-8")
    with pytest.raises(module.AllocationError, match="too many"):
        module.allocate_from_inspect_json(too_many_configs)


def test_cli_emits_exact_compact_json_shape() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(ALLOCATOR),
            "--host-route",
            "172.31.48.0/24",
        ],
        input=_inspect_json("172.17.0.0/16"),
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr.decode("utf-8")
    assert result.stderr == b""
    assert result.stdout == (b'{"edge_ip":"172.31.49.10","subnet":"172.31.49.0/24"}\n')


def test_cli_failure_does_not_echo_attacker_controlled_input() -> None:
    untrusted_marker = "do-not-reflect-this-value"
    result = subprocess.run(
        [sys.executable, str(ALLOCATOR)],
        input=json.dumps(
            [
                {
                    "IPAM": {
                        "Config": [
                            {
                                "Subnet": untrusted_marker,
                            }
                        ]
                    }
                }
            ]
        ).encode("utf-8"),
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert result.stdout == b""
    assert untrusted_marker.encode("utf-8") not in result.stderr
