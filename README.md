# Igor Pro Bridge

An MCP server that exposes a running Igor Pro instance to Claude (or any MCP
client) as a set of tools: run commands, read wave data, manage compilation,
launch/relaunch Igor Pro, and more. It talks to Igor Pro over the
[ZeroMQ-XOP](https://github.com/AllenInstitute/ZeroMQ-XOP)'s `CallFunction`
JSON protocol (a plain localhost TCP socket), calling into Igor-side helper
functions in `procedure/ZMQ_BridgeHelpers.ipf` (the `ZBR` independent module).

## Key functions

- **Run code** — `run_igor_code` compiles and runs procedure code as a ProcGlobal
  function (flow control, local variables, the experiment's own constants and functions).
  It is the default way to run anything beyond a short command.
- **Run commands** — `execute_igor_command` runs a command line and returns what it
  printed and its error message. By default Igor's Debugger is disabled for the call,
  so a Debugger pause can't hang it; errors never open a modal dialog.
- **Long-running commands** — `submit_igor_command` queues a command and returns a
  token immediately without waiting; `poll_igor_command` checks completion later.
  Reliable across bridge/Claude Desktop restarts, and across waits of any length
  (hours to weeks), since all state lives in Igor Pro's own data waves, not in this
  bridge's process.
- **Tests** — `run_igor_tests` compiles changed procedures and starts a test run of
  [IgorTest](https://github.com/byte-physics/igortest); `get_igor_test_results` returns
  its results from the JUnit output once the run has finished.
- **Read data back** — `get_wave` returns an existing wave's full data and
  metadata (any dimensionality, numeric/complex/text/wave-reference).
  `read_session_history` reads back everything sent to Igor's history area.
- **Compilation management** — `check_compilation_state` /
  `reload_and_compile_procedures` check and refresh Igor's compiled state
  after editing a `.ipf` file on disk.
- **Windows and dialogs** — `list_igor_windows` lists Igor's top-level windows,
  e.g. a modal dialog blocking the bridge; `close_igor_window` closes one
  (WM_CLOSE, Escape, and only on request Enter, which selects the default
  button) and `send_key_to_igor_window` sends a key, both without needing OS
  focus. `check_bridge_health` reports such dialogs.
- **Debugger control** — `get_debugger_state` / `set_debugger_enabled` /
  `restore_debugger_settings` read, change, and restore Igor's Debugger
  settings (must be disabled for unattended/automated use).
- **Environment inspection** — `get_environment_summary` summarizes the live
  instance (version, loaded experiment, XOPs, included procedure files, data
  folders, Debugger settings). `read_help_file` reads an Igor help file
  (`.ihf`) as structured text, optionally with character styles, and caches it;
  `edit_help_file` edits a help file with Notebook operations and verifies its topics. `get_bridge_version` reports the running
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
   8. Put a shortcut to the `procedure\ZBR_UserProcedures` folder in the `User Procedures` folder.
      The bridge includes `ZBR_ProcGlobal.ipf` from there on demand (`run_igor_code`), see below.

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
   included at `mcp/igor-pro-bridge-<version>.mcpb` (build a new one with `mcpb pack mcp`). In Claude Desktop, go to
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

## Working with the bridge

- **`run_igor_code` is the default for running code.** It runs as a compiled ProcGlobal function:
  flow control, local variables, WAVE/NVAR/SVAR references and the experiment's own constants and
  functions all work. Use `print` to return data.
- **`execute_igor_command` is for quick queries** and for what only works outside functions, e.g.
  `Quit` or `Execute/P ...`. On Igor's command line, results of built-in functions must be printed or
  assigned, there is no flow control, and SVAR/NVAR/WAVE references are not allowed.
- **Recompiling stops background tasks.** `run_igor_code` recompiles every time and refuses while
  named background tasks run (e.g. a data acquisition); call already compiled code with
  `execute_igor_command` then.
- **Reload before tests.** `run_igor_tests` compiles changed procedure files first, otherwise Igor Pro
  goes uncompiled in the middle of the run when it notices them.
- **When the bridge does not answer**, `check_bridge_health` tells whether Igor Pro runs, is busy or
  shows a dialog; `list_igor_windows`/`close_igor_window` close such dialogs.

## Advanced Use

The mcp folder includes some session notes to help Claude.

`run_igor_code` compiles and runs scratch code as functions in ProcGlobal, e.g. small utility functions.
The code is written to `procedure\ZBR_UserProcedures\ZBR_Scratch.ipf`, which `ZBR_ProcGlobal.ipf` includes. On first use the bridge
adds `ZBR_ProcGlobal.ipf` to the experiment with `INSERTINCLUDE`; `remove_scratch_procedures` takes it out again.
While it is included, commands run in ProcGlobal context as well, so the experiment's constants and functions resolve as on Igor's own command line.

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
