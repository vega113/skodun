# Fragment: MCP server config (operators)

Paste into the client project’s MCP docs or runbook. Adjust the binary path if
`skodun` is not on the default `PATH`.

## Claude Code (CLI)

```bash
claude mcp add skodun -- skodun mcp
# From a skodun source tree without an installed console script:
# claude mcp add skodun -- python3 -m skodun mcp
```

## JSON-shaped hosts (Claude Code `.mcp.json`, Cursor-style, etc.)

```json
{
  "mcpServers": {
    "skodun": {
      "type": "stdio",
      "command": "skodun",
      "args": ["mcp"]
    }
  }
}
```

### Optional: project-local store

Use a **real absolute path** unless your host expands workspace placeholders:

```json
{
  "mcpServers": {
    "skodun": {
      "type": "stdio",
      "command": "skodun",
      "args": ["mcp"],
      "env": {
        "SKODUN_DB": "/absolute/path/to/project/.skodun/skodun.db"
      }
    }
  }
}
```

### After installing or upgrading skodun

1. Restart the MCP client (stdio servers do not hot-reload code).
2. Run `skodun doctor --repo <project>` in a shell.
3. Confirm tools: `gate`, `review`, `log`, `surface`, triage family.
4. Prefer agents pass absolute `repo` on tool calls (server cwd may differ).

### What is *not* an MCP tool

Shell out if needed: `skodun doctor`, `skodun providers`, `skodun retain`,
`skodun schedule install`, `skodun install-hooks`.
