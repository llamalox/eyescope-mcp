# Eyescope

Search and rough-cut your Eyescope library from Cursor, Claude, and Codex. The plugin starts a local MCP server on your computer. That server talks only to the Eyescope app on this machine. It reads the app's local credential file so the client config never contains a token. Footage, transcripts, and cuts stay on the computer. Nothing in this plugin uploads a library to Eyescope's website or to the marketplace.

## Requirements

- **Eyescope desktop app** installed and the local engine running (tray or library window).
- **Eyescope sign-in** on that app. Tool calls return a sign-in message until the app has synced your account session.
- **This machine only** — the server binds to loopback; cloud agents cannot reach it.

If the app is closed, tool calls ask you to open EyeScope. Sign in from the app or the menu bar tray if agents report that sign-in is required.

The Mac app icon is `icon.png`.

## Marketplace packages

This repository is the plugin root.

- Cursor reads `.cursor-plugin/plugin.json`, root `mcp.json`, and `.cursor-plugin/mcp.json` (Directory scans). The logo path is `icon.png`.
- Claude reads `.claude-plugin/plugin.json` and `.mcp.json`. Submit this repository as a plugin bundle at the Claude developer portal. Do not submit it as a remote connector.
- Codex reads `.codex-plugin/plugin.json` and `.codex-mcp.json`. The package is desktop-only because the server is local. The public OpenAI directory accepts that package only after OpenAI allowlists the submitting organization.

The installable `.mcpb` for the official MCP registry is the **`v0.1.3`** release asset, not a file in this tree:

https://github.com/llamalox/eyescope-mcp/releases/download/v0.1.3/eyescope.mcpb

Icon (registry / catalog): https://github.com/llamalox/eyescope-mcp/releases/download/v0.1.3/icon.png

Product site: https://eyescope.io

## Release notes

**v0.1.3** — MCP tools require an Eyescope account session on the desktop app (HTTP 401 with a sign-in message until resolved). The stdio launcher forwards Streamable HTTP `mcp-session-id` so tool listing works after `initialize`. Metadata and launcher aligned with the desktop app session gate.