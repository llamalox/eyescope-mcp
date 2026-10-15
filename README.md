# Eyescope

Search and rough-cut an Eyescope library from Cursor, Claude, and Codex. The plugin starts a local MCP server on your computer. That server talks only to the Eyescope app on this machine. It reads the app's local credential file so the client config never contains a token. Footage, transcripts, and cuts stay on the computer. Nothing in this plugin uploads a library to Eyescope's website or to the marketplace.

Install the Eyescope desktop app first. The launcher looks for the app's Python and the running local engine. If the app is closed, tool calls ask you to open Eyescope.

The Mac app icon is `icon.png`.

## Marketplace packages

This repository is the plugin root.

- Cursor reads `.cursor-plugin/plugin.json` and `mcp.json`. The logo path is `icon.png`.
- Claude reads `.claude-plugin/plugin.json` and `.mcp.json`. Submit this repository as a plugin bundle at the Claude developer portal. Do not submit it as a remote connector.
- Codex reads `.codex-plugin/plugin.json` and `.codex-mcp.json`. The package is desktop-only because the server is local. The public OpenAI directory accepts that package only after OpenAI allowlists the submitting organization.

The installable `.mcpb` for the official MCP registry is the `v0.1.2` release asset, not a file in this tree:

https://github.com/llamalox/eyescope-mcp/releases/download/v0.1.2/eyescope.mcpb

Product site: https://eyescope.io
