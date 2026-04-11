from src.domain.schemas import PreflightDiffFileInput
from src.infrastructure.preflight.normalizer import DiffManifestNormalizer
from src.infrastructure.preflight.parser import ParsedFilePatch


def test_normalizer_normalizes_paths_and_enriches_metadata() -> None:
    normalizer = DiffManifestNormalizer()
    parsed = [
        ParsedFilePatch(
            filepath="b\\src\\utils.py",
            old_filepath="a\\src\\utils.py",
            change_type="M",
            additions=3,
            deletions=2,
            hunk_count=1,
            is_binary_hint=False,
            raw_diff="diff --git a/src/utils.py b/src/utils.py",
            parse_issues=[],
        )
    ]

    entries = normalizer.normalize(parsed_patches=parsed, fallback_files=[])

    assert len(entries) == 1
    entry = entries[0]

    assert entry.filepath == "src/utils.py"
    assert entry.old_filepath == "src/utils.py"
    assert entry.language == "Python"
    assert entry.is_binary is False
    assert entry.is_vendor is False
    assert entry.is_generated is False


def test_normalizer_falls_back_when_parser_entries_are_missing() -> None:
    normalizer = DiffManifestNormalizer()
    fallback = [
        PreflightDiffFileInput(
            filepath="vendor/lib.min.js",
            change_type="A",
            additions=20,
            deletions=0,
            raw_diff=None,
        )
    ]

    entries = normalizer.normalize(parsed_patches=[], fallback_files=fallback)

    assert len(entries) == 1
    entry = entries[0]

    assert entry.filepath == "vendor/lib.min.js"
    assert entry.change_type == "A"
    assert entry.additions == 20
    assert entry.language == "JavaScript"
    assert entry.is_vendor is True
    assert entry.is_generated is True
