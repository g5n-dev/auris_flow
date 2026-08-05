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
        "error_code",
        "retryable",
        "received_at",
    }
    assert completion["properties"]["completion_receipt_id"]["pattern"] == (
        "^[A-Za-z0-9][A-Za-z0-9_:-]{0,127}$"
    )
    assert completion["properties"]["error_code"]["pattern"] == ("^[A-Z][A-Z0-9_]{2,127}$")
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


def test_run_detail_declares_stable_business_status_and_omits_processed_event_id() -> None:
    document = _document()
    schemas = document["components"]["schemas"]
    run_detail = schemas["RunDetail"]
    properties = run_detail["properties"]

    assert properties["business_status"] == {
        "type": "string",
        "description": "面向产品的业务阶段，不暴露底层执行引擎或 Outbox 状态。",
    }
    assert "processed_event_id" not in properties
    assert "processed_event_id" in run_detail["x-auris-recursively-omitted-fields"]
    assert "failed_event_id" in run_detail["x-auris-recursively-omitted-fields"]
    assert "dead_letter_event_id" in run_detail["x-auris-recursively-omitted-fields"]
    assert "artifact_uri" in run_detail["x-auris-recursively-omitted-fields"]
    assert "partial_artifact_uri" in run_detail["x-auris-recursively-omitted-fields"]
    assert "uri" in run_detail["x-auris-recursively-omitted-fields"]
    assert "url" in run_detail["x-auris-recursively-omitted-fields"]
    assert "provider_artifact_ref" in run_detail["x-auris-recursively-omitted-fields"]
    assert "result_storage_object_ids" in run_detail["x-auris-recursively-omitted-fields"]
    assert "result_storage_object_sha256" in run_detail["x-auris-recursively-omitted-fields"]


def test_external_completion_receipt_contract_uses_explicit_request_and_responses() -> None:
    document = _document()
    operation = document["paths"]["/runs/{id}/external-completion-receipts"]["post"]
    schemas = document["components"]["schemas"]
    parameters = document["components"]["parameters"]

    assert parameters["IdempotencyKey"]["schema"] == {
        "type": "string",
        "minLength": 1,
        "maxLength": 128,
        "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    }
    assert parameters["XRequestId"]["schema"] == {"type": "string"}

    expected_security = [
        {
            "aurisCompletionSignature": [],
            "aurisCompletionKeyId": [],
        },
        {
            "aurisCompletionSignature": [],
            "aurisCompletionLegacyKeyId": [],
        },
    ]
    assert operation["security"] == expected_security
    for scheme in {
        "aurisCompletionSignature",
        "aurisCompletionKeyId",
        "aurisCompletionLegacyKeyId",
    }:
        assert document["components"]["securitySchemes"][scheme]["type"] == "apiKey"
    assert operation["requestBody"]["$ref"] == (
        "#/components/requestBodies/ExternalRunCompletionReceipt"
    )
    request_body = document["components"]["requestBodies"]["ExternalRunCompletionReceipt"]
    assert request_body["required"] is True
    assert request_body["content"]["application/json"]["schema"]["$ref"] == (
        "#/components/schemas/ExternalRunCompletionReceiptRequest"
    )
    external_request = schemas["ExternalRunCompletionReceiptRequest"]
    assert set(external_request["required"]) == {
        "adapter",
        "completion_receipt_id",
        "external_id",
    }
    assert external_request["allOf"] == [
        {"$ref": "#/components/schemas/RunCompletionReceiptRequest"}
    ]
    shared_request = schemas["RunCompletionReceiptRequest"]
    assert shared_request["additionalProperties"] is False
    assert shared_request["properties"]["adapter"]["enum"] == [
        "dagster",
        "object_storage",
        "external_callback",
    ]
    assert external_request["properties"]["completion_receipt_id"]["pattern"] == (
        "^[A-Za-z0-9][A-Za-z0-9_:-]{0,127}$"
    )
    assert (
        operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/RunDetailResponse"
    )
    assert (
        operation["responses"]["202"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/RunCompletionReceiptPendingResponse"
    )

    pending_data = schemas["RunCompletionReceiptPendingData"]
    assert pending_data["additionalProperties"] is False
    assert set(pending_data["required"]) == {
        "run_id",
        "status",
        "completion_receipt_id",
        "receipt_state",
        "trace_id",
    }
    assert pending_data["properties"]["receipt_state"]["enum"] == [
        "pending_binding",
        "pending_cancellation_resolution",
        "materializing",
    ]
    assert pending_data["properties"]["business_status"]["enum"] == ["materializing"]
    pending_response = schemas["RunCompletionReceiptPendingResponse"]
    assert pending_response["additionalProperties"] is False
    assert set(pending_response["required"]) == {"data", "meta"}
    assert pending_response["properties"]["data"]["$ref"] == (
        "#/components/schemas/RunCompletionReceiptPendingData"
    )
    assert pending_response["properties"]["meta"]["$ref"] == (
        "#/components/schemas/InsightResponseMeta"
    )

    signature_parameters = {
        document["components"]["parameters"][parameter["$ref"].rsplit("/", 1)[-1]][
            "name"
        ]: document["components"]["parameters"][parameter["$ref"].rsplit("/", 1)[-1]]
        for parameter in operation["parameters"]
        if parameter["$ref"].rsplit("/", 1)[-1].startswith("XAuris")
    }
    assert set(signature_parameters) == {
        "X-Auris-Key-Id",
        "X-Auris-Signature-Id",
        "X-Auris-Timestamp",
        "X-Auris-Nonce",
        "X-Auris-Source",
        "X-Auris-Signature-Mode",
        "X-Auris-Signature",
    }
    assert signature_parameters["X-Auris-Key-Id"]["required"] is False
    assert signature_parameters["X-Auris-Signature-Id"]["required"] is False
    assert signature_parameters["X-Auris-Signature-Id"]["deprecated"] is True
    for header in {
        "X-Auris-Timestamp",
        "X-Auris-Nonce",
        "X-Auris-Source",
        "X-Auris-Signature-Mode",
        "X-Auris-Signature",
    }:
        assert signature_parameters[header]["required"] is True
    assert signature_parameters["X-Auris-Source"]["schema"]["enum"] == [
        "dagster",
        "object_storage",
        "external_callback",
    ]
    assert signature_parameters["X-Auris-Signature-Mode"]["schema"]["enum"] == ["hmac-sha256"]


def test_runtime_external_completion_receipt_contract_declares_request_and_both_successes() -> None:
    document = app.openapi()
    operation = document["paths"]["/api/v1/runs/{id}/external-completion-receipts"]["post"]

    assert operation["security"] == [
        {
            "aurisCompletionSignature": [],
            "aurisCompletionKeyId": [],
        },
        {
            "aurisCompletionSignature": [],
            "aurisCompletionLegacyKeyId": [],
        },
    ]

    request_schema = operation["requestBody"]["content"]["application/json"]["schema"]
    assert request_schema["$ref"].endswith("/ExternalRunCompletionReceiptRequest")

    schemas = document["components"]["schemas"]
    external_request = schemas[request_schema["$ref"].rsplit("/", 1)[-1]]
    assert external_request.get("additionalProperties") is False
    assert set(external_request["required"]) == {
        "adapter",
        "completion_receipt_id",
        "external_id",
    }
    assert external_request["properties"]["adapter"]["enum"] == [
        "dagster",
        "object_storage",
        "external_callback",
    ]
    assert external_request["properties"]["source"]["anyOf"][0]["enum"] == [
        "dagster",
        "object_storage",
        "external_callback",
    ]
    assert external_request["properties"]["completion_receipt_id"]["pattern"] == (
        "^[A-Za-z0-9][A-Za-z0-9_:-]{0,127}$"
    )

    signature_parameters = {
        parameter["name"]: parameter
        for parameter in operation["parameters"]
        if parameter["name"].startswith("X-Auris-")
    }
    assert set(signature_parameters) == {
        "X-Auris-Key-Id",
        "X-Auris-Signature-Id",
        "X-Auris-Timestamp",
        "X-Auris-Nonce",
        "X-Auris-Source",
        "X-Auris-Signature-Mode",
        "X-Auris-Signature",
    }
    assert signature_parameters["X-Auris-Key-Id"]["required"] is False
    assert signature_parameters["X-Auris-Signature-Id"]["required"] is False
    for header in {
        "X-Auris-Timestamp",
        "X-Auris-Nonce",
        "X-Auris-Source",
        "X-Auris-Signature-Mode",
        "X-Auris-Signature",
    }:
        assert signature_parameters[header]["required"] is True
    assert signature_parameters["X-Auris-Source"]["schema"]["enum"] == [
        "dagster",
        "object_storage",
        "external_callback",
    ]
    assert signature_parameters["X-Auris-Signature-Mode"]["schema"]["const"] == ("hmac-sha256")
    context_parameters = {parameter["name"]: parameter for parameter in operation["parameters"]}
    for header in {"X-Tenant-Id", "X-Project-Id", "Idempotency-Key"}:
        assert context_parameters[header]["required"] is True
    assert context_parameters["Idempotency-Key"]["schema"] == {
        "type": "string",
        "minLength": 1,
        "maxLength": 128,
        "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
        "description": (
            "写操作幂等键；1–128 个 ASCII 字符，首字符为字母或数字，"
            "其余可使用字母、数字、点、下划线、冒号或连字符。"
        ),
        "title": "Idempotency-Key",
    }

    response_200 = operation["responses"]["200"]["content"]["application/json"]["schema"]
    response_202 = operation["responses"]["202"]["content"]["application/json"]["schema"]
    response_413 = operation["responses"]["413"]["content"]["application/json"]["schema"]
    response_422 = operation["responses"]["422"]["content"]["application/json"]["schema"]
    assert response_200
    assert response_202
    assert response_413["$ref"].endswith("/ApiErrorEnvelope")
    assert response_422["$ref"].endswith("/ApiErrorEnvelope")

    pending_reference = response_202["$ref"].rsplit("/", 1)[-1]
    pending_response = schemas[pending_reference]
    assert pending_response.get("additionalProperties") is False
    pending_data_reference = pending_response["properties"]["data"]["$ref"].rsplit("/", 1)[-1]
    pending_data = schemas[pending_data_reference]
    assert pending_data.get("additionalProperties") is False
    assert set(pending_data["required"]) == {
        "run_id",
        "status",
        "completion_receipt_id",
        "receipt_state",
        "trace_id",
    }
    assert pending_data["properties"]["receipt_state"]["enum"] == [
        "pending_binding",
        "pending_cancellation_resolution",
        "materializing",
    ]


def test_runtime_audio_intelligence_request_and_playback_range_contract_are_typed() -> None:
    document = app.openapi()

    intelligence_operation = document["paths"]["/api/v1/audio-sessions/{id}/intelligence-runs"][
        "post"
    ]
    request_schema = intelligence_operation["requestBody"]["content"]["application/json"]["schema"]
    assert request_schema["$ref"].endswith("/AudioIntelligenceRunRequest")
    intelligence_schema = document["components"]["schemas"][
        request_schema["$ref"].rsplit("/", 1)[-1]
    ]
    assert {
        "capabilities",
        "recording_id",
        "provider",
        "model_version",
        "execution_mode",
        "language",
    }.issubset(intelligence_schema["properties"])
    accepted_schema = intelligence_operation["responses"]["202"]["content"]["application/json"][
        "schema"
    ]
    assert accepted_schema["$ref"].endswith("/PublicRunEnvelope_PublicRunDetail_")

    playback_operations = (
        ("/api/v1/audio-playback", "get", True),
        ("/api/v1/audio-playback", "head", False),
        ("/api/v1/audio-sessions/{id}/recording", "get", True),
        ("/api/v1/audio-sessions/{id}/recording", "head", False),
    )
    for path, method, has_body in playback_operations:
        operation = document["paths"][path][method]
        parameters = {
            (parameter["in"], parameter["name"]): parameter for parameter in operation["parameters"]
        }
        for header_name in ("Range", "If-Range"):
            header = parameters[("header", header_name)]
            assert header["required"] is False
            assert header["schema"]["anyOf"][0]["type"] == "string"

        partial = operation["responses"]["206"]
        assert partial["headers"]["Accept-Ranges"]["schema"] == {
            "type": "string",
            "enum": ["bytes"],
        }
        assert partial["headers"]["Content-Range"]["required"] is True
        if has_body:
            assert partial["content"]["audio/wav"]["schema"] == {
                "type": "string",
                "format": "binary",
            }
        else:
            assert "content" not in partial

        unsatisfiable = operation["responses"]["416"]
        assert unsatisfiable["headers"]["Accept-Ranges"]["schema"] == {
            "type": "string",
            "enum": ["bytes"],
        }
        assert unsatisfiable["headers"]["Content-Range"]["required"] is True


def test_runtime_public_completion_summary_is_closed_and_engine_neutral() -> None:
    document = app.openapi()
    schemas = document["components"]["schemas"]
    completion_property = schemas["PublicRunDetail"]["properties"]["completion_receipt"]
    summary_references = [
        variant["$ref"] for variant in completion_property["anyOf"] if "$ref" in variant
    ]

    assert summary_references == ["#/components/schemas/RunCompletionSummary"]
    summary = schemas["RunCompletionSummary"]
    fields = set(summary["properties"])
    assert summary["additionalProperties"] is False
    assert set(summary["required"]) == {"completion_receipt_id", "status"}
    assert fields == {
        "completion_receipt_id",
        "status",
        "result_ref",
        "metrics",
        "error_code",
        "retryable",
        "received_at",
    }
    assert not fields.intersection(
        {
            "adapter",
            "auth",
            "external_id",
            "receipt_hash",
            "signature_key_id",
            "source",
        }
    )
    assert summary["properties"]["completion_receipt_id"]["pattern"] == (
        "^[A-Za-z0-9][A-Za-z0-9_:-]{0,127}$"
    )
    error_code_variants = summary["properties"]["error_code"]["anyOf"]
    assert {variant.get("pattern") for variant in error_code_variants} == {
        "^[A-Z][A-Z0-9_]{2,127}$",
        None,
    }
    for field in {"result_ref", "metrics"}:
        assert summary["properties"][field]["type"] == "object"
    received_at_variants = summary["properties"]["received_at"]["anyOf"]
    assert {variant.get("format") for variant in received_at_variants} == {
        "date-time",
        None,
    }


def test_runtime_all_operations_use_the_stable_validation_error_envelope() -> None:
    document = app.openapi()
    http_methods = {"get", "post", "put", "patch", "delete", "options", "head", "trace"}
    checked_operations: list[str] = []

    for path, path_item in document["paths"].items():
        for method, operation in path_item.items():
            if method not in http_methods:
                continue
            operation_name = f"{method.upper()} {path}"
            checked_operations.append(operation_name)
            validation_response = operation.get("responses", {}).get("422")
            assert validation_response is not None, operation_name
            validation_schema = validation_response["content"]["application/json"]["schema"]
            assert validation_schema == {"$ref": "#/components/schemas/ApiErrorEnvelope"}, (
                operation_name
            )

    assert checked_operations


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
