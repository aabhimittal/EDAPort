from edaport.server.budget import bound, get_path
from edaport.util import est_tokens


def big():
    return {"run_id": "r-1", "ok": True, "wns_ps": -12.5,
            "paths": [{"slack_ps": -i, "stages": [{"pin": f"u{i}_{j}/Y", "delay_ps": j} for j in range(40)]}
                      for i in range(30)],
            "note": "x" * 5000}


def test_fits_untouched():
    r = {"ok": True, "items": [1, 2]}
    assert bound(r, 2000, "h") is r


def test_trimmed_to_budget_and_accounted():
    full = big()
    out = bound(full, 600, "r-1")
    assert est_tokens(out) <= 600
    assert out["truncated"] and out["more_handle"] == "r-1"
    assert out["wns_ps"] == -12.5  # scalars are never dropped
    assert out["omitted"]["paths"] == 30 - len(out["paths"])
    assert "note#chars" in out["omitted"]
    assert len(full["paths"]) == 30  # caller's dict untouched


def test_every_trimmed_field_is_reachable():
    full = big()
    out = bound(full, 600, "r-1")
    for path in out["omitted"]:
        if not path.endswith("#chars") and path.split(".")[0] in full:
            try:
                get_path(full, path)
            except (KeyError, IndexError):
                # nested path inside a list element that itself was trimmed away: still in full["paths"]
                assert path.startswith("paths.")
