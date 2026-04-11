from src.infrastructure.preflight.parser import UnifiedDiffParser


def test_unified_diff_parser_parses_modified_file_metrics() -> None:
    raw_diff = (
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
    )

    parser = UnifiedDiffParser()
    patches = parser.parse(raw_diff)

    assert len(patches) == 1
    patch = patches[0]

    assert patch.filepath == "utils.py"
    assert patch.change_type == "M"
    assert patch.additions == 3
    assert patch.deletions == 2
    assert patch.hunk_count == 1
    assert patch.raw_diff is not None


def test_unified_diff_parser_parses_rename_and_change_type() -> None:
    raw_diff = (
        "diff --git a/old_name.py b/new_name.py\n"
        "similarity index 88%\n"
        "rename from old_name.py\n"
        "rename to new_name.py\n"
        "--- a/old_name.py\n"
        "+++ b/new_name.py\n"
        "@@ -1 +1 @@\n"
        "-print('old')\n"
        "+print('new')\n"
    )

    parser = UnifiedDiffParser()
    patches = parser.parse(raw_diff)

    assert len(patches) == 1
    patch = patches[0]

    assert patch.change_type == "R"
    assert patch.filepath == "new_name.py"
    assert patch.old_filepath == "old_name.py"
