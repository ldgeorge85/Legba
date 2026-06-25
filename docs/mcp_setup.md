# Legba MCP Server Setup

*Last updated: 2026-06-17.*

Legba exposes analytical capabilities via the Model Context Protocol (MCP), so
Claude Code and other MCP clients can call into the platform. Two surfaces:

- **`legba-mcp`** (stdio) — the MCP server process
  (`src/legba/ui/mcp_server.py`, console script `legba-mcp`). Its tool catalog
  is sourced entirely from the **descriptor-driven MCP tool registry**
  (`legba.data.outputs.mcp_tool.MCP_TOOL_REGISTRY`): every analyst descriptor
  whose body declares an `outputs.mcp_tool` binding is surfaced as an MCP tool.
- **`legba-registry`** (HTTP, port 8090) — the registry API server (descriptor
  / stack / vault / vocabulary CRUD + WebSocket events). Not an MCP server; its
  on-demand analytical surface is `POST /api/v1/consult` (see `AI_MODELS.md`).

The pre-pivot hand-wired catalog (eight read-only analytical tools backed by
the legacy embedded stores) was retired along with the legacy agent tree. That
analytical surface is rebuilt descriptor-first: the on-demand `consult`
capability is the `consult_on_demand` analyst kind, exposable as an MCP tool
via an `outputs.mcp_tool` binding.

## How a tool gets into the catalog

An analyst descriptor declares the binding:

```yaml
outputs:
  - kind: mcp_tool
    config:
      tool_name: "intelligence.india_energy_assessment"
      description: "Latest energy-security assessment for India."
      input_schema:
        type: object
        properties:
          question: {type: string}
        required: [question]
      mode: latest_output        # default — return the analyst's most recent
                                 # output for the bound scope
      # mode: consult_on_demand  # trigger an on-demand analyst run with the
                                 # call's args as input
```

When the descriptor activates, the runtime calls
`MCPToolRegistry.register_from_descriptor`; on retire it unregisters. All
registered tools are **read-only or run-triggering** — no registry mutations
ride MCP.

> **Declared seam (see `docs/SEAMS.md`):** `MCP_TOOL_REGISTRY` is an
> in-process registry populated by the **runtime** process at descriptor
> activation. The standalone `legba-mcp` stdio process does not yet bootstrap
> the catalog from the registry at startup, so a separately-launched stdio
> server lists an empty catalog until that wiring lands. It fails loud (an
> unknown tool call returns the available-tool list) — it never fabricates
> output.

## Running it

### Via Docker (the canonical client-launch shape)

The MCP client launches the container per conversation:

```jsonc
{
  "mcpServers": {
    "legba": {
      "command": "docker",
      "args": ["run", "-i", "--rm", "--network=legba_default",
               "--env-file=/usr/local/deployments/active/legba/.env",
               "legba/legba-mcp:latest"]
    }
  }
}
```

Build the image with `docker compose --profile mcp build` (it is the
`docker/Dockerfile.mcp` image). After adding the config, restart Claude Code;
registered tools appear in the tool list.

### Host-mode (module or entry point)

```bash
cd /usr/local/deployments/active/legba
PYTHONPATH=src python -m legba.ui.mcp_server   # as a module
legba-mcp                                      # via entry point, after pip install -e .
```

Transport is stdio (JSON-RPC); logging goes to stderr so stdout stays clean
for the protocol.

## Architecture

```
Claude Code (MCP client)
    |
    | stdio (JSON-RPC 2.0)
    v
legba-mcp (src/legba/ui/mcp_server.py)
    |
    |-- list_tools  -> MCP_TOOL_REGISTRY.list_mcp_tools()
    |-- call_tool   -> MCP_TOOL_REGISTRY.handle(name, args)
                         |-- mode latest_output      -> analyst's most recent output
                         |-- mode consult_on_demand  -> dispatch an analyst run
```

The registry deliberately defers importing the `mcp` SDK until tool listing /
dispatch, so the data layer stays importable without the SDK; in production
`mcp>=1.0` is a required dependency.
