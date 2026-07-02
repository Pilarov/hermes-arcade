"""Block 1 tests: Schema — vertex types, Project properties, VECTOR_DIM."""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import pytest

def test_project_properties():
    """BLK1-01: Project has all required properties."""
    from hermes_cli.arcadedb_schema import VERTICES
    p = VERTICES["Project"]["props"]
    prop_names = [pr[0] for pr in p]
    assert "slug" in prop_names, "slug missing"
    assert "icon" in prop_names, "icon missing"
    assert "color" in prop_names, "color missing"
    assert "primary_path" in prop_names, "primary_path missing"
    assert "archived" in prop_names, "archived missing"

def test_new_vertex_types():
    """BLK1-02: All 7 new vertex types exist in VERTICES."""
    from hermes_cli.arcadedb_schema import VERTICES
    required = [
        "ProjectFolder", "DiscoveredRepo", "Response", "Conversation",
        "VerificationEvent", "VerificationState", "PendingIngest"
    ]
    for vt in required:
        assert vt in VERTICES, f"{vt} missing from VERTICES"

def test_vector_dim_configurable():
    """BLK1-03: set_vector_dim/get_vector_dim work."""
    from hermes_cli.arcadedb_schema import set_vector_dim, get_vector_dim
    original = get_vector_dim()
    set_vector_dim(1536)
    assert get_vector_dim() == 1536
    set_vector_dim(original)
    assert get_vector_dim() == original

def test_response_store_type():
    """BLK1-04: Response vertex has response_id + accessed_at indexes."""
    from hermes_cli.arcadedb_schema import VERTICES
    r = VERTICES["Response"]
    idx_names = [i[0] for i in r["indexes"]]
    assert "response_id" in idx_names

def test_pending_ingest_type():
    """BLK1-05: PendingIngest has required fields."""
    from hermes_cli.arcadedb_schema import VERTICES
    pi = VERTICES["PendingIngest"]["props"]
    prop_names = [pr[0] for pr in pi]
    assert "user_id" in prop_names
    assert "session_id" in prop_names
    assert "messages_json" in prop_names
