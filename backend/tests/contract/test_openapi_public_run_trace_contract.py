from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from yaml.nodes import MappingNode

from app.main import app

OPENAPI_PATH = Path(__file__).resolve().parents[3] / "doc/backend-spec/openapi-v0.1.yaml"


class _UniqueKeySafeLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueKeySafeLoader,
    node: MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            line = key_node.start_mark.line + 1
            raise AssertionError(f"duplicate OpenAPI key {key!r} at line {line}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _document() -> dict[str, Any]:
    return yaml.load(OPENAPI_PATH.read_text(encoding="utf-8"), Loader=_UniqueKeySafeLoader)


def _assert_recursive_schema_is_closed_and_locator_free(
    document: dict[str, Any],
    schema: dict[str, Any],
    *,
    path: str,
    seen_refs: set[str] | None = None,
) -> None:
    forbidden = {
        "adapter",
        "bucket",
        "details",
        "dispatch",
        "endpoint",
        "external_run_id",
        "graphql",
        "object_key",
        "object_uri",
        "provider",
        "storage_object_id",
    }
    seen_refs = seen_refs or set()
    reference = schema.get("$ref")
    if isinstance(reference, str):
        assert reference.startswith("#/components/schemas/")
        if reference in seen_refs:
            return
        seen_refs.add(reference)
        name = reference.rsplit("/", 1)[-1]
        _assert_recursive_schema_is_closed_and_locator_free(
            document,
            document["components"]["schemas"][name],
            path=f"{path}->{name}",
            seen_refs=seen_refs,
        )
        return

    properties = schema.get("properties")
    if isinstance(properties, dict):
        assert not forbidden.intersection(properties), path
        for field, child in properties.items():
            if isinstance(child, dict):
                _assert_recursive_schema_is_closed_and_locator_free(
                    document,
                    child,
                    path=f"{path}.{field}",
                    seen_refs=seen_refs,
                )

    if schema.get("type") == "object":
        additional = schema.get("additionalProperties")
        assert additional is False or isinstance(additional, dict), f"open object schema at {path}"
        if isinstance(additional, dict):
            _assert_recursive_schema_is_closed_and_locator_free(
                document,
                additional,
                path=f"{path}.*",
                seen_refs=seen_refs,
            )

    items = schema.get("items")
    if isinstance(items, dict):
        _assert_recursive_schema_is_closed_and_locator_free(
            document,
            items,
            path=f"{path}[]",
            seen_refs=seen_refs,
        )
    for keyword in ("allOf", "anyOf", "oneOf"):
        variants = schema.get(keyword)
        if isinstance(variants, list):
            for index, variant in enumerate(variants):
                if isinstance(variant, dict):
                    _assert_recursive_schema_is_closed_and_locator_free(
                        document,
                        variant,
                        path=f"{path}.{keyword}[{index}]",
                        seen_refs=seen_refs,
                    )


def test_task_run_reads_use_domain_run_responses_and_runtime_collection_envelope() -> None:
    document = _document()
    schemas = document["components"]["schemas"]
    task_run_detail = document["paths"]["/task-runs/{id}"]["get"]["responses"]["200"]

    assert (
        task_run_detail["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/RunDetailResponse"
    )

    run_list = schemas["RunListResponse"]
    assert run_list["additionalProperties"] is False
    assert set(run_list["required"]) == {"data", "meta"}
    assert run_list["properties"]["data"]["$ref"] == "#/components/schemas/RunListData"
    assert run_list["properties"]["meta"]["$ref"] == ("#/components/schemas/InsightResponseMeta")
    run_list_data = schemas["RunListData"]
    assert run_list_data["additionalProperties"] is False
    assert run_list_data["required"] == ["items"]
    assert run_list_data["properties"]["items"]["items"]["$ref"] == (
        "#/components/schemas/RunSummary"
    )
    meta = schemas["InsightResponseMeta"]["properties"]
    assert {"total", "limit", "next_cursor"}.issubset(meta)


def test_run_completion_summary_declares_no_execution_receipt_evidence() -> None:
    document = _document()
    schemas = document["components"]["schemas"]
    completion = schemas["RunCompletionSummary"]
    fields = set(completion["properties"])

    assert schemas["RunDetail"]["properties"]["completion_receipt"]["$ref"] == (
        "#/components/schemas/RunCompletionSummary"
    )
    assert completion["additionalProperties"] is False
    assert fields == {
        "completion_receipt_id",
        "status",
        "result_ref",
        "metrics",
        "note",
        "error_code",
        "retryable",
        "received_at",
    }
    assert not fields.intersection(
        {
            "adapter",
            "auth",
            "external_id",
            "external_run_id",
            "receipt_hash",
            "signature",
            "signature_key_id",
            "source",
        }
    )


def test_trace_contract_is_a_closed_domain_projection_with_offline_raw_evidence() -> None:
    document = _document()
    trace_operation = document["paths"]["/traces/{trace_id}"]["get"]
    description = " ".join(trace_operation["description"].split())
    span = document["components"]["schemas"]["TraceSpan"]
    fields = set(span["properties"])

    assert span["additionalProperties"] is False
    assert set(span["required"]) == {"kind", "id"}
    assert not fields.intersection(
        {
            "adapter",
            "dispatch",
            "operation",
            "remote_id",
            "request_hash",
            "request_sha256",
            "storage",
            "storage_payload",
            "tool",
            "tool_payload",
        }
    )
    assert all(source in description for source in ("DB", "Outbox", "Audit", "离线 verifier"))
    assert "原始证据" in description


def test_run_derived_export_and_label_schemas_are_closed_and_locator_free() -> None:
    document = _document()
    schemas = document["components"]["schemas"]
    export_detail = document["paths"]["/exports/{id}"]["get"]["responses"]["200"]
    assert export_detail["content"]["application/json"]["schema"]["$ref"] == (
        "#/components/schemas/ExportJobResponse"
    )
    assert schemas["ExportJob"]["additionalProperties"] is False
    assert schemas["ExportDownloadRef"]["additionalProperties"] is False
    assert set(schemas["ExportDownloadRef"]["properties"]) == {
        "kind",
        "status",
        "href",
        "content_type",
        "expires_at",
    }

    label_run = schemas["LabelExtractionRun"]
    assert label_run["additionalProperties"] is False
    assert schemas["PublicRunNextAction"]["additionalProperties"] is False
    for schema_name in ("ExportJobResponse", "LabelExtractionRunResponse"):
        _assert_recursive_schema_is_closed_and_locator_free(
            document,
            {"$ref": f"#/components/schemas/{schema_name}"},
            path=schema_name,
        )
    prompt_list = document["paths"]["/prompt-version-candidates"]["get"]["responses"]["200"]
    prompt_detail = document["paths"]["/prompt-version-candidates/{id}"]["get"]["responses"]["200"]
    assert prompt_list["content"]["application/json"]["schema"]["$ref"] == (
        "#/components/schemas/PublicPromptVersionCandidateListResponse"
    )
    assert prompt_detail["content"]["application/json"]["schema"]["$ref"] == (
        "#/components/schemas/PublicPromptVersionCandidateResponse"
    )
    for schema_name in (
        "PublicPromptVersionCandidateListResponse",
        "PublicPromptVersionCandidateResponse",
    ):
        _assert_recursive_schema_is_closed_and_locator_free(
            document,
            {"$ref": f"#/components/schemas/{schema_name}"},
            path=schema_name,
        )

    download = document["paths"]["/exports/{id}/download"]
    assert set(download) == {"get", "head"}
    for method in ("get", "head"):
        operation = download[method]
        assert "鉴权" in operation["description"]
        assert "上游对象定位器" in operation["description"]


def test_runtime_openapi_closes_export_and_label_run_responses() -> None:
    document = app.openapi()
    operations = (
        ("/api/v1/exports", "post", "202"),
        ("/api/v1/exports/{id}", "get", "200"),
        ("/api/v1/exports/{id}/completion-receipts", "post", "200"),
        ("/api/v1/label-extraction-runs", "post", "202"),
        ("/api/v1/label-extraction-runs/{extraction_run_id}", "get", "200"),
        ("/api/v1/prompt-version-candidates", "get", "200"),
        ("/api/v1/prompt-version-candidates/{id}", "get", "200"),
    )
    for route, method, status in operations:
        response_schema = document["paths"][route][method]["responses"][status]["content"][
            "application/json"
        ]["schema"]
        assert response_schema, f"runtime OpenAPI response is untyped: {method} {route}"
        _assert_recursive_schema_is_closed_and_locator_free(
            document,
            response_schema,
            path=f"{method.upper()} {route}",
        )
