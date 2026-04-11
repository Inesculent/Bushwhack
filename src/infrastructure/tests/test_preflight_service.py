from src.domain.schemas import PreflightRequest, RunMetadata
from src.infrastructure.preflight.service import PreflightManifestService


def _request_with_files() -> PreflightRequest:
    return PreflightRequest(
        run_metadata=RunMetadata(
            repo="synthetic-control-repo",
            base_sha="a1b2c3d",
            head_sha="e4f5g6h",
            run_id="run-123",
        ),
        files=[
            {
                "filepath": "b/utils.py",
                "change_type": "M",
                "additions": 3,
                "deletions": 2,
                "raw_diff": "@@ -1,2 +1,3 @@",
            },
            {
                "filepath": "a/core.py",
                "change_type": "A",
                "additions": 10,
                "deletions": 0,
            },
        ],
    )


def test_preflight_service_fallback_builds_manifest_from_structured_files() -> None:
    service = PreflightManifestService()
    request = _request_with_files()

    manifest = service.build_diff_manifest(request)

    assert manifest.run_metadata.repo == "synthetic-control-repo"
    assert manifest.aggregate_metrics.total_files_changed == 2
    assert manifest.aggregate_metrics.total_additions == 13
    assert manifest.aggregate_metrics.total_deletions == 2
    assert manifest.aggregate_metrics.total_hunks == 0
    assert manifest.warnings == []

    # Entries are sorted by filepath for determinism.
    assert [entry.filepath for entry in manifest.files] == ["a/core.py", "b/utils.py"]
    assert manifest.files[0].language == "Python"
    assert manifest.risk_hints == []
    assert manifest.ambiguity_flags == []


def test_preflight_service_manifest_id_is_deterministic_for_identical_input() -> None:
    service = PreflightManifestService()
    request = _request_with_files()

    first = service.build_diff_manifest(request)
    second = service.build_diff_manifest(request)

    assert first.manifest_id == second.manifest_id


def test_preflight_service_parses_raw_diff_end_to_end() -> None:
    service = PreflightManifestService()
    request = PreflightRequest(
        run_metadata=RunMetadata(
            repo="synthetic-control-repo",
            base_sha="a1b2c3d",
            head_sha="e4f5g6h",
        ),
        raw_diff=(
            "diff --git a/utils.py b/utils.py\n"
            "index 2222222..3333333 100644\n"
            "--- a/utils.py\n"
            "+++ b/utils.py\n"
            "@@ -1,2 +1,3 @@\n"
            "-def calculate_discount(price, discount_rate):\n"
            "-    return price * (1 - discount_rate)\n"
            "+def calculate_discount(price, discount_rate, user_tier):\n"
            "+    tier_bonus = 0.05 if user_tier == 'gold' else 0.0\n"
            "+    return price * (1 - (discount_rate + tier_bonus))\n"
        ),
    )

    manifest = service.build_diff_manifest(request)

    assert manifest.warnings == []
    assert manifest.aggregate_metrics.total_files_changed == 1
    assert manifest.aggregate_metrics.total_additions == 3
    assert manifest.aggregate_metrics.total_deletions == 2
    assert manifest.aggregate_metrics.total_hunks == 1
    assert manifest.files[0].filepath == "utils.py"
    assert manifest.files[0].change_type == "M"
    assert manifest.files[0].language == "Python"
