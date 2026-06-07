"""Tests for SnapshotLoader."""

from pathlib import Path

import pytest

from src.config import Settings
from src.infrastructure.snapshot_loader import SnapshotLoader


class TestSnapshotLoader:
    """Test cases for SnapshotLoader."""

    @pytest.fixture
    def loader(self, tmp_path: Path) -> SnapshotLoader:
        """Create a SnapshotLoader with temporary base path."""
        settings = Settings(snapshot_base_path=tmp_path)
        return SnapshotLoader(settings)

    def test_load_snapshot_pointer_valid(self, loader: SnapshotLoader, tmp_path: Path) -> None:
        """Test loading a valid snapshot.json file."""
        # Create test snapshot structure
        run_dir = tmp_path / "test_run_123"
        run_dir.mkdir()
        snapshot_json = run_dir / "snapshot.json"
        snapshot_json.write_text(
            '{'
            '"run_id": "test:repo__pr1",'
            '"snapshot_id": "abc123def4567890",'
            '"snapshot_root": "C:/test/path",'
            '"status": "exploration_complete",'
            '"community_count": 5,'
            '"total_nodes": 100,'
            '"total_edges": 500,'
            '"unresolved_call_count": 0,'
            '"extraction_gap_count": 0'
            '}',
            encoding="utf-8"
        )

        snapshot = loader.load_snapshot_pointer("test_run_123")

        assert snapshot.run_id == "test:repo__pr1"
        assert snapshot.snapshot_id == "abc123def4567890"
        assert snapshot.status == "exploration_complete"
        assert snapshot.community_count == 5

    def test_load_snapshot_pointer_missing(self, loader: SnapshotLoader) -> None:
        """Test that FileNotFoundError is raised for missing snapshot."""
        with pytest.raises(FileNotFoundError, match="Snapshot not found"):
            loader.load_snapshot_pointer("nonexistent_run")

    def test_load_snapshot_pointer_invalid_json(self, loader: SnapshotLoader, tmp_path: Path) -> None:
        """Test that ValueError is raised for malformed JSON."""
        run_dir = tmp_path / "invalid_run"
        run_dir.mkdir()
        snapshot_json = run_dir / "snapshot.json"
        snapshot_json.write_text('{ invalid json }', encoding="utf-8")

        with pytest.raises(Exception):  # json.JSONDecodeError or ValueError
            loader.load_snapshot_pointer("invalid_run")

    def test_load_snapshot_pointer_missing_required_field(self, loader: SnapshotLoader, tmp_path: Path) -> None:
        """Test that ValueError is raised for missing required fields."""
        run_dir = tmp_path / "incomplete_run"
        run_dir.mkdir()
        snapshot_json = run_dir / "snapshot.json"
        snapshot_json.write_text('{"run_id": "test"}', encoding="utf-8")

        with pytest.raises(ValueError, match="Missing required field"):
            loader.load_snapshot_pointer("incomplete_run")

    def test_load_graph_payload(self, loader: SnapshotLoader, tmp_path: Path) -> None:
        """Test loading full_graph.json."""
        run_dir = tmp_path / "graph_run"
        run_dir.mkdir()
        graph_dir = run_dir / "graph"
        graph_dir.mkdir()
        full_graph = graph_dir / "full_graph.json"
        full_graph.write_text('{"nodes": [], "edges": []}', encoding="utf-8")

        payload = loader.load_graph_payload(str(run_dir))

        assert payload == {"nodes": [], "edges": []}

    def test_load_graph_payload_missing(self, loader: SnapshotLoader, tmp_path: Path) -> None:
        """Test FileNotFoundError for missing full_graph.json."""
        run_dir = tmp_path / "no_graph_run"
        run_dir.mkdir()

        with pytest.raises(FileNotFoundError, match="Full graph not found"):
            loader.load_graph_payload(str(run_dir))

    def test_load_topology(self, loader: SnapshotLoader, tmp_path: Path) -> None:
        """Test loading topology.json."""
        run_dir = tmp_path / "topology_run"
        run_dir.mkdir()
        graph_dir = run_dir / "graph"
        graph_dir.mkdir()
        topology_json = graph_dir / "topology.json"
        topology_json.write_text(
            '{"algorithm": "louvain", "community_count": 3, "communities": [], "node_to_community": {}, "splits_applied": 0}',
            encoding="utf-8"
        )

        topology = loader.load_topology(str(run_dir))

        assert topology.algorithm == "louvain"
        assert topology.community_count == 3

    def test_load_topology_missing(self, loader: SnapshotLoader, tmp_path: Path) -> None:
        """Test FileNotFoundError for missing topology.json."""
        run_dir = tmp_path / "no_topology_run"
        run_dir.mkdir()

        with pytest.raises(FileNotFoundError, match="Topology not found"):
            loader.load_topology(str(run_dir))

    def test_load_global_summary(self, loader: SnapshotLoader, tmp_path: Path) -> None:
        """Test loading global_summary.md."""
        run_dir = tmp_path / "summary_run"
        run_dir.mkdir()
        semantic_dir = run_dir / "semantic"
        semantic_dir.mkdir()
        global_summary = semantic_dir / "global_summary.md"
        global_summary.write_text("# Repository Summary\n\nThis is a test.", encoding="utf-8")

        content = loader.load_global_summary(str(run_dir))

        assert content == "# Repository Summary\n\nThis is a test."

    def test_load_global_summary_missing(self, loader: SnapshotLoader, tmp_path: Path) -> None:
        """Test FileNotFoundError for missing global_summary.md."""
        run_dir = tmp_path / "no_summary_run"
        run_dir.mkdir()

        with pytest.raises(FileNotFoundError, match="Global summary not found"):
            loader.load_global_summary(str(run_dir))

    def test_load_diagnostics(self, loader: SnapshotLoader, tmp_path: Path) -> None:
        """Test loading diagnostics.json."""
        run_dir = tmp_path / "diagnostics_run"
        run_dir.mkdir()
        semantic_dir = run_dir / "semantic"
        semantic_dir.mkdir()
        diagnostics_json = semantic_dir / "diagnostics.json"
        diagnostics_json.write_text('{"god_nodes": [], "bridge_nodes": [], "cross_community_edges": [], "knowledge_gaps": []}', encoding="utf-8")

        diagnostics = loader.load_diagnostics(str(run_dir))

        assert diagnostics.god_nodes == []
        assert diagnostics.cross_community_edges == []

    def test_load_diagnostics_missing(self, loader: SnapshotLoader, tmp_path: Path) -> None:
        """Test FileNotFoundError for missing diagnostics.json."""
        run_dir = tmp_path / "no_diagnostics_run"
        run_dir.mkdir()

        with pytest.raises(FileNotFoundError, match="Diagnostics not found"):
            loader.load_diagnostics(str(run_dir))

    def test_load_community_shards_empty(self, loader: SnapshotLoader, tmp_path: Path) -> None:
        """Test that empty list is returned when no community files exist."""
        run_dir = tmp_path / "no_communities_run"
        run_dir.mkdir()
        semantic_dir = run_dir / "semantic"
        semantic_dir.mkdir()
        # Don't create communities directory

        summaries = loader.load_community_shards(str(run_dir))

        assert summaries == []

    def test_load_community_shards_with_files(self, loader: SnapshotLoader, tmp_path: Path) -> None:
        """Test loading community markdown files."""
        run_dir = tmp_path / "communities_run"
        run_dir.mkdir()
        semantic_dir = run_dir / "semantic"
        semantic_dir.mkdir()
        communities_dir = semantic_dir / "communities"
        communities_dir.mkdir()

        # Create a test community file
        community_file = communities_dir / "00_test_community.md"
        community_file.write_text(
            '# Community 0: Test Community\n\n'
            '**Purpose:** This is a test purpose.\n\n'
            '## Files\n'
            '- `src/test.py`: Test file (confidence 0.90)\n\n'
            '## Symbols\n'
            '- `abc123:test_func`: A test function (confidence 0.85)\n'
            '  - _Rationale:_ Test rationale.\n\n'
            '## Cross-community dependencies\n'
            '1, 2\n\n'
            '## Unverified / resolved calls\n'
            '- unresolved: `unknown_func` from `abc123:test_func` - Test call.\n',
            encoding="utf-8"
        )

        summaries = loader.load_community_shards(str(run_dir))

        assert len(summaries) == 1
        assert summaries[0].community_id == 0
        assert summaries[0].label == "Test Community"
        assert summaries[0].purpose == "This is a test purpose."
        assert len(summaries[0].file_summaries) == 1
        assert summaries[0].file_summaries[0].file_node_id == "src/test.py"
        assert len(summaries[0].symbol_summaries) == 1
        assert summaries[0].symbol_summaries[0].rationale == "Test rationale."
        assert summaries[0].cross_community_dependencies == [1, 2]

    def test_load_community_shards_preserves_order(self, loader: SnapshotLoader, tmp_path: Path) -> None:
        """Test that community files are loaded in sorted order."""
        run_dir = tmp_path / "ordered_communities_run"
        run_dir.mkdir()
        semantic_dir = run_dir / "semantic"
        semantic_dir.mkdir()
        communities_dir = semantic_dir / "communities"
        communities_dir.mkdir()

        # Create files in non-sorted order
        (communities_dir / "02_second.md").write_text('# Community 2: Second', encoding="utf-8")
        (communities_dir / "00_first.md").write_text('# Community 0: First', encoding="utf-8")
        (communities_dir / "01_third.md").write_text('# Community 1: Third', encoding="utf-8")

        summaries = loader.load_community_shards(str(run_dir))

        assert len(summaries) == 3
        assert summaries[0].community_id == 0
        assert summaries[1].community_id == 1
        assert summaries[2].community_id == 2


class TestSnapshotLoaderIntegration:
    """Integration tests using real snapshots from bushwhack_runs."""

    def test_load_from_real_snapshot_graph(self, tmp_path: Path) -> None:
        """Test loading graph from an actual snapshot in bushwhack_runs."""
        # Use existing snapshot for integration test
        settings = Settings(snapshot_base_path=Path("bushwhack_runs"))
        loader = SnapshotLoader(settings)

        # Find an existing snapshot directory
        base = Path("bushwhack_runs")
        if not base.exists():
            pytest.skip("bushwhack_runs directory not found")

        snapshot_dirs = sorted(d for d in base.iterdir() if d.is_dir())
        if not snapshot_dirs:
            pytest.skip("No snapshot directories found in bushwhack_runs")

        snapshot_dir = next((d for d in snapshot_dirs if (d / "snapshot.json").is_file()), None)
        if snapshot_dir is None:
            pytest.skip("No run directory with snapshot.json under bushwhack_runs")

        run_id = snapshot_dir.name

        # Test loading snapshot pointer
        snapshot = loader.load_snapshot_pointer(run_id)
        assert snapshot.snapshot_id is not None
        assert snapshot.run_id is not None

        # Test loading graph payload
        payload = loader.load_graph_payload(snapshot.snapshot_root)
        assert "nodes" in payload
        assert "edges" in payload

        # Test loading topology
        topology = loader.load_topology(snapshot.snapshot_root)
        assert topology.algorithm is not None

    def test_load_global_summary_from_real_snapshot(self, tmp_path: Path) -> None:
        """Test loading global summary from an actual snapshot."""
        settings = Settings(snapshot_base_path=Path("bushwhack_runs"))
        loader = SnapshotLoader(settings)

        base = Path("bushwhack_runs")
        if not base.exists():
            pytest.skip("bushwhack_runs directory not found")

        snapshot_dirs = sorted(d for d in base.iterdir() if d.is_dir())
        if not snapshot_dirs:
            pytest.skip("No snapshot directories found in bushwhack_runs")

        snapshot_dir = next((d for d in snapshot_dirs if (d / "snapshot.json").is_file()), None)
        if snapshot_dir is None:
            pytest.skip("No run directory with snapshot.json under bushwhack_runs")

        run_id = snapshot_dir.name

        snapshot = loader.load_snapshot_pointer(run_id)

        # May or may not exist depending on semantic enrichment
        try:
            content = loader.load_global_summary(snapshot.snapshot_root)
            assert isinstance(content, str)
        except FileNotFoundError:
            # Expected if semantic enrichment was disabled
            pass
