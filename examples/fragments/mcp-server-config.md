# Fragment: MCP server config (operators)

Paste into the client project’s MCP docs or runbook. Adjust the binary path if
`skodun` is not on the default `PATH`.

## Claude Code / JSON-shaped MCP hosts

```json
{
  "mcpServers": {
    "skodun": {
      "command": "skodun",
      "args": ["mcp"]
    }
  }
}
```

### Optional: project-local store

```json
{
  "mcpServers": {
    "skodun": {
      "command": "skodun",
      "args": ["mcp"],
      "env": {
        "SKODUN_DB": "${workspaceFolder}/.skodun/skodun.db"
      }
    }
  }
}
```

(`${workspaceFolder}` is host-specific — substitute a real absolute path if the
client does not expand it.)

### After installing or upgrading skodun

1. Restart the MCP client (stdio servers do not hot-reload).
2. Run `skodun doctor --repo <project>` in a shell.
3. Confirm tools: `gate`, `review`, `log`, `surface`, triage family.

### What is *not* an MCP tool

Shell out if needed: `skodun doctor`, `skodun retain`, `skodun schedule install`,
`skodun install-hooks`, `skodun providers`.
