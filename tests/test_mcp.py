import anyio
import jsonschema

from edaport.models import RESULT_MODELS
from edaport.server.mcp_server import build_server

from .conftest import ROOT, need


@need("verilator")
def test_mcp_roundtrip(engine):
    async def go():
        from mcp.client.client import Client
        async with Client(build_server(engine)) as c:
            tools = (await c.list_tools()).tools
            assert sorted(t.name for t in tools) == sorted(RESULT_MODELS)
            for t in tools:
                assert t.output_schema, t.name
                assert all(ch.isalnum() or ch in "_-" for ch in t.name)  # Anthropic-API-safe names
            imp = await c.call_tool("snapshot_import", {"path": str(ROOT / "examples/counter"), "label": "main"})
            assert not imp.is_error and imp.structured_content["ok"]
            r = await c.call_tool("lint_run", {"design_ref": "main"})
            schema = next(t.output_schema for t in tools if t.name == "lint_run")
            jsonschema.validate(r.structured_content, schema)
            assert r.structured_content["warnings_grouped"] == {"UNUSEDSIGNAL": 1}
    anyio.run(go)
