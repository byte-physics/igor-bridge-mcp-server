# Igor Pro Bridge

An MCP server that exposes a running Igor Pro instance to Claude (or any MCP
client) as a set of tools: run commands, read wave data, manage compilation,
launch/relaunch Igor Pro, and more. It talks to Igor Pro over the
[ZeroMQ-XOP](https://github.com/AllenInstitute/ZeroMQ-XOP)'s `CallFunction`
JSON protocol (a plain localhost TCP socket), calling into Igor-side helper
functions in `procedure/ZMQ_BridgeHelpers.ipf` (the `ZBR` independent module).

## Key functions

- **Run commands** — `execute_igor_command` / `execute_igor_command_unattended`
  run a command string on Igor's command line and return what it printed
  (`_unattended` also disables/restores Igor's Debugger around the call, so a
  runtime error can't pop an undismissable modal dialog).
- **Long-running commands** — `submit_igor_command` / `submit_igor_command_unattended`
  queue a command and return a token immediately without waiting; `poll_igor_command`
  checks completion later. Reliable across bridge/Claude Desktop restarts, and
  across waits of any length (hours to weeks), since all state lives in Igor
  Pro's own data waves, not in this bridge's process.
- **Read data back** — `get_wave` returns an existing wave's full data and
  metadata (any dimensionality, numeric/complex/text/wave-reference).
  `read_session_history` reads back everything sent to Igor's history area.
- **Compilation management** — `check_compilation_state` /
  `reload_and_compile_procedures` check and refresh Igor's compiled state
  after editing a `.ipf` file on disk; `dismiss_compile_error_dialog` closes a
  stuck "Function Compilation Error" dialog without needing OS focus.
- **Debugger control** — `get_debugger_state` / `set_debugger_enabled` /
  `restore_debugger_settings` read, change, and restore Igor's Debugger
  settings (must be disabled for unattended/automated use).
- **Environment inspection** — `get_environment_summary` summarizes the live
  instance (version, loaded experiment, XOPs, included procedure files, data
  folders, Debugger settings). `read_help_file` reads an Igor help file
  (`.ihf`) as structured text. `get_bridge_version` reports the running
  bridge/Python/package versions.
- **Launching and switching instances** — `configure_igor_launch` records the
  Igor Pro executable path to use, and optionally a custom ZeroMQ port (for
  talking to more than one Igor Pro instance, one at a time);
  `launch_igor_pro_unattended` launches it with `/UNATTENDED` and waits for it
  to become reachable; `load_experiment` quits the running instance and
  relaunches it with a `.pxp` file path as a launch argument.
- **Health check** — `check_bridge_health` diagnoses whether this bridge can
  reach Igor Pro's ZeroMQ server right now.

## How to install

This bridge is a *local* MCP server (stdio transport): it only works from a
Claude Desktop session running on the same Windows machine as Igor Pro.

1. **One-time Igor-side setup**:
   1. Add the ZeroMQ XOP to Igor Pro
   2. The XOP is shipped in the ZeroMQ-XOP folder
   3. Unpack the zip file with subfolders
   4. Open Igor Pro and from the Help menu choose `Show Igor Pro User Files`
   5. navigate to the output\win\x64\xop\Release subfolder from the unpacked XOP
   6. Put a shortcut to `ZeroMQ-64.xop` in the `Igor Extensions (64-bit)` folder
   7. Put a shortcut to the `procedure\ZMQ_BridgeHelpers.ipf` file in the `Igor Procedures` folder

2. **Install the pinned Python dependencies**, into the same Python
   environment Claude Desktop itself resolves when it launches the bridge:

   ```
   .\install.ps1
   ```

   Run this from an elevated PowerShell (elevation is required only for
   `pywin32`'s one-time COM-support DLL registration, not for running the
   bridge or Igor Pro afterward). Optionally, pass `-PythonPath` to target a specific
   interpreter if auto-resolution picks the wrong one. See
   `Get-Help ./install.ps1 -Full` for details.

3. **Install the Claude Desktop extension (`.mcpb`)**. A pre-built package is
   included at `mcp/igor-pro-bridge-2.3.2.mcpb`. In Claude Desktop, go to
   Settings → Extensions → Advanced settings → Extension Developer → Install
   Extension, and select that file. Do not register this bridge by manually
   editing `claude_desktop_config.json` — that does not work reliably for
   local MCP servers in current Claude Desktop builds.

4. **Restart Claude Desktop** fully, then call the bridge's `get_bridge_version`
   tool to confirm it's running, and `check_bridge_health` to confirm it can
   reach Igor Pro.

## First steps

- When starting Igor Pro there should be a printout in the history `Igor Pro Bridge MCP bound through ZMQ at port 5680`.
- Close Igor Pro
- In Claude Desktop (or your agent), say you want to start Igor Pro. It should ask you for the path. Give the path and it will remember it and start Igor Pro.
- Ask to print "Hello World" to the Igor Pro history

The general idea is that you have some repository / folder where your .ipf files are that you work on. Allow Claude write access to this folder.
Then it can edit these files on disk and reload the changes in Igor Pro and recompile.
It is worthwhile to hint Claude that it should read the Igor Help for specific tasks first as it has a dedicated function for this included.

## Advanced Use

The mcp folder includes some session notes to help Claude.

Allow Claude to create and include an own procedure file for scratch code, like small utility functions that Claude can create and run.
This enables much more powerful tooling than just running commands or calling already present function code.

The MCP Bridge is able to run multiple instances of Igor Pro with different ports for ZeroMQ communication.
You can ask Claude to start a new Igor Pro instance with a custom port. (default is 5680)
Then Claude can talk and identify each instance by its port and work with each Igor Pro instance independently.

Claude Desktop can take advantage of the [Igor Programming Tool](https://docs.byte-physics.de/ipt/) !
If you put ipt in your working folder or PATH then Claude can call it to generate symbol tables or the advanced syntax tree of your Igor Pro code and thus, understand it better. The ipt tool is also suitable for automatically formatting Igor code.

## Requirements

- Igor Pro 9.00 or later, running on Windows, with the ZeroMQ-XOP installed
  and loaded.
- `procedure/ZMQ_BridgeHelpers.ipf` (or a copy of it) `#include`-d and
  compiled into the target experiment.
- Python (accessible as `python` on PATH), with the pinned packages in
  `requirements.txt` installed into that same environment — `mcp`, `pyzmq`,
  and (on Windows) `pywin32`. `requirements.txt` pins every package, direct
  and transitive, with hashes for supply-chain integrity; install with
  `install.ps1` rather than a plain `pip install`.
