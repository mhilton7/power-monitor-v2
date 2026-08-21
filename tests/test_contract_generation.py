from __future__ import annotations

import json

from scripts.generate_contracts import generated_files


def test_generated_contracts_are_committed_without_drift() -> None:
    differences = [
        str(path)
        for path, expected in generated_files().items()
        if not path.is_file() or path.read_bytes() != expected
    ]
    assert differences == [], f"regenerate shared contracts: {differences}"


def test_stateless_telemetry_contract_is_additive_strict_and_has_no_success_rejection() -> None:
    request_schema = json.loads(
        generated_files()[
            next(
                path
                for path in generated_files()
                if path.name == "device-stateless-telemetry-v2.schema.json"
            )
        ]
    )
    response_schema = json.loads(
        generated_files()[
            next(
                path
                for path in generated_files()
                if path.name == "server-stateless-telemetry-v2-response.schema.json"
            )
        ]
    )
    assert request_schema["additionalProperties"] is False
    assert request_schema["properties"]["telemetry_protocol"]["const"] == ("pm-telemetry/2.0.0")
    assert request_schema["properties"]["firmware_build_id"]["pattern"] == ("^[0-9a-f]{64}$")
    assert response_schema["properties"]["status"]["enum"] == ["accepted", "duplicate"]
    assert "rejected" not in json.dumps(response_schema)

    openapi_path = next(
        path for path in generated_files() if path.name == "power-meter-v2.openapi.json"
    )
    openapi = json.loads(generated_files()[openapi_path])
    operation = openapi["paths"]["/api/v1/device/telemetry/v2"]["post"]
    openapi_request = operation["requestBody"]["content"]["application/json"]["schema"]
    assert openapi_request["additionalProperties"] is False
    assert openapi_request["properties"]["telemetry_protocol"]["const"] == ("pm-telemetry/2.0.0")
    assert openapi_request["properties"]["firmware_build_id"]["pattern"] == ("^[0-9a-f]{64}$")
    response_ref = operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    assert response_ref.endswith("/StatelessTelemetryResponse")
