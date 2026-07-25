#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LIGHT = ROOT / "doc/assets/architecture-light.svg"
DEFAULT_DARK = ROOT / "doc/assets/architecture-dark.svg"
SOURCE = ROOT / "doc/architecture/auris-flow-system.mmd"
README = ROOT / "README.md"
DETAIL = ROOT / "doc/architecture/README.md"

EXPECTED_LABELS = (
    "React 工作台",
    "首页 · 数据资产 · 调听",
    "标签 · 知识 · 评测",
    "洞察 · 设置 · 发布",
    "通用 OIDC IdP",
    "OIDC · RBAC",
    "FastAPI BFF",
    "领域服务",
    "Worker · Outbox",
    "内部执行适配器",
    "Dagster",
    "签名状态回写",
    "MySQL",
    "权威业务事实",
    "对象存储",
    "权威音频 · 证据",
    "Redis",
    "可重建缓存 · 锁",
    "Qdrant",
    "可重建语义索引",
    "模型 / Embedding",
    "OTel",
    "Prometheus",
    "Grafana",
    "非 /readyz 硬依赖",
)
FORBIDDEN_SVG_PATTERNS = {
    "<script": "script element",
    "<foreignobject": "foreignObject element",
    "data:": "embedded data URI",
    "base64": "base64 payload",
    "file://": "local file URI",
    "/users/": "personal absolute path",
    "\\users\\": "personal Windows path",
}


def _normalized_text(root: ET.Element) -> str:
    return " ".join("".join(root.itertext()).split())


def _class_tokens(element: ET.Element) -> set[str]:
    return set(element.attrib.get("class", "").split())


def _view_box(root: ET.Element) -> tuple[float, float]:
    parts = root.attrib.get("viewBox", "").split()
    if len(parts) != 4:
        raise ValueError("SVG must define a four-value viewBox")
    width = float(parts[2])
    height = float(parts[3])
    if width <= 0 or height <= 0:
        raise ValueError("SVG viewBox must have positive dimensions")
    return width, height


def validate_svg(path: Path) -> tuple[list[str], tuple[int, int]]:
    failures: list[str] = []
    if not path.is_file():
        return [f"missing architecture asset: {path}"], (0, 0)
    raw = path.read_text(encoding="utf-8")
    lowered = raw.lower()
    for pattern, description in FORBIDDEN_SVG_PATTERNS.items():
        if pattern in lowered:
            failures.append(f"{path.name} contains forbidden {description}")
    if re.search(r"(?:href|xlink:href)\\s*=\\s*[\"'](?!#)", raw, re.IGNORECASE):
        failures.append(f"{path.name} contains an external href")
    if len(raw.encode("utf-8")) > 1_500_000:
        failures.append(f"{path.name} exceeds the 1.5 MB README asset budget")

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as error:
        return failures + [f"{path.name} is invalid XML: {error}"], (0, 0)

    tag = root.tag.rsplit("}", 1)[-1]
    if tag != "svg":
        failures.append(f"{path.name} root element must be svg")
    try:
        width, height = _view_box(root)
    except ValueError as error:
        failures.append(f"{path.name}: {error}")
    else:
        if width / height < 1.65:
            failures.append(
                f"{path.name} must remain a landscape blueprint; "
                f"viewBox ratio is {width / height:.2f}"
            )

    text = _normalized_text(root)
    for label in EXPECTED_LABELS:
        if label not in text:
            failures.append(f"{path.name} is missing label: {label}")
    if "Auris Flow production architecture" not in text:
        failures.append(f"{path.name} is missing the accessible title")

    node_count = sum(1 for element in root.iter() if "node" in _class_tokens(element))
    edge_count = sum(
        1 for element in root.iter() if "flowchart-link" in _class_tokens(element)
    )
    if not 15 <= node_count <= 18:
        failures.append(f"{path.name} must contain 15-18 nodes, found {node_count}")
    if edge_count > 22:
        failures.append(
            f"{path.name} must contain at most 22 primary edges, found {edge_count}"
        )
    return failures, (node_count, edge_count)


def validate_sources() -> list[str]:
    failures: list[str] = []
    for path in (SOURCE, README, DETAIL):
        if not path.is_file():
            failures.append(f"missing architecture source: {path}")
    if failures:
        return failures

    source = SOURCE.read_text(encoding="utf-8")
    for required in (
        "layout: elk",
        "securityLevel: strict",
        "htmlLabels: false",
        "flowchart TB",
        "accTitle:",
        "accDescr:",
        'subgraph EXPERIENCE["01 · 产品体验',
        'subgraph CONTROL["02 · 领域控制',
        'subgraph INFRA["03 · 数据与基础设施',
        'MYSQL[("MySQL<br/>权威业务事实")]',
        'OBJECTS[("对象存储<br/>权威音频 · 证据")]',
        'REDIS[("Redis<br/>可重建缓存 · 锁")]',
        'QDRANT[("Qdrant<br/>可重建语义索引")]',
        'EXECUTION["内部执行适配器<br/>Dagster"]',
        'OBSERVABILITY["OTel · Prometheus · Grafana<br/>非 /readyz 硬依赖"]',
    ):
        if required not in source:
            failures.append(f"Mermaid source is missing invariant: {required}")
    if source.count("subgraph ") != 3:
        failures.append("Mermaid source must keep exactly three horizontal layers")

    readme = README.read_text(encoding="utf-8")
    for required in (
        '<a href="doc/architecture/README.md">',
        '<source media="(prefers-color-scheme: dark)" '
        'srcset="doc/assets/architecture-dark.svg">',
        'src="doc/assets/architecture-light.svg"',
        "MySQL 是权威业务存储",
        "对象存储保存权威对象",
        "Redis 与 Qdrant",
        "Dagster 只承担后台执行",
    ):
        if required not in readme:
            failures.append(f"README architecture is missing invariant: {required}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate the safe, dual-theme README architecture blueprint."
    )
    parser.add_argument("--light", type=Path, default=DEFAULT_LIGHT)
    parser.add_argument("--dark", type=Path, default=DEFAULT_DARK)
    args = parser.parse_args()

    failures = validate_sources()
    light_failures, light_shape = validate_svg(args.light)
    dark_failures, dark_shape = validate_svg(args.dark)
    failures.extend(light_failures)
    failures.extend(dark_failures)
    if light_shape != dark_shape:
        failures.append(
            "light and dark architecture assets have different node/edge topology: "
            f"{light_shape} != {dark_shape}"
        )

    if failures:
        print("README architecture verification failed:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    print(
        "README architecture verification ok "
        f"({light_shape[0]} nodes, {light_shape[1]} edges, two themes)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
