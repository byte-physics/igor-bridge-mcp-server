"""
Igor Pro MCP bridge server
==========================

Exposes a running Igor Pro instance to Claude (or any MCP client) as a set of MCP tools.

**v3.0.0**: `run_igor_code` runs code as compiled ProcGlobal functions via the scratch
procedures in procedure/ZBR_UserProcedures, and commands run in ProcGlobal context once
those are included; queued commands never open modal dialogs; tools for Igor's windows and
dialogs, IgorTest runs, symbol lookups and help file editing. The two `_unattended` tools
and `dismiss_compile_error_dialog` are removed, see the manifest's long_description.

**v2.0.0: transport rewritten from COM to ZeroMQ.** Versions through 1.27.0 talked to
Igor Pro as a COM Automation *client* (win32com, `IgorPro.Application`, `Execute2`).
From 2.0.0 on, this bridge instead talks to Igor Pro's ZeroMQ-XOP
(https://github.com/AllenInstitute/ZeroMQ-XOP) over a plain TCP socket, sending
`CallFunction` JSON requests and calling into Igor-side helper functions in
`Packages/MIES/ZMQ_BridgeHelpers.ipf` (the `ZBR` independent module). See
SESSION_NOTES.md for the full COM-vs-ZeroMQ evaluation that led here, and
Packages/doc/igor-pro-bridge.rst for the up-to-date setup steps.

Why this changed: COM requires this Python process and Igor Pro to run at the SAME
Windows privilege level (both elevated, or both not) -- a mismatch is a real,
easy-to-miss failure mode (e.g. Claude Desktop reopened normally after Igor Pro was
left running elevated from before). ZeroMQ is a plain localhost TCP socket with no
such requirement at all -- **this bridge no longer cares about elevation in any way**.
That is also why `launch_igor_pro_unattended` no longer has an elevation-branching
code path (see its docstring): it always launches Igor Pro as a plain, non-elevated
child process, at whatever privilege level this bridge process itself is running at.

**Setup requirement, new in v2.0.0**: unlike the COM transport (which worked against
a completely stock Igor Pro installation with zero custom procedure code), this
transport requires `Packages/MIES/ZMQ_BridgeHelpers.ipf` to be `#include`-d and
compiled into whatever Igor Pro experiment this bridge talks to -- there is no
bootstrap path over ZeroMQ itself (if that file isn't loaded, there is nothing
listening on the port at all). This repo's own `Packages/MIES_Include.ipf` already
does this permanently. Any OTHER Igor Pro experiment that wants to use this bridge
needs `ZMQ_BridgeHelpers.ipf` copied onto its own procedure search path with a
matching `#include` added by hand, then a recompile -- see
Packages/doc/igor-pro-bridge.rst for the exact steps. This is a one-time,
per-experiment setup cost that didn't exist before; it is the price of everything
else this transport buys (see SESSION_NOTES.md's ZeroMQ-evaluation section for the
full trade-off discussion).

Protocol summary (confirmed empirically this session against a live Igor Pro 9.06
instance, and against https://github.com/AllenInstitute/ZeroMQ-XOP's own README):

- Endpoint: `tcp://127.0.0.1:5680` by default (`_igor_zmq_endpoint()` below) --
  matches `ZBR_ZEROMQ_ENDPOINT`/`ZBR_ZEROMQ_DEFAULT_PORT` in ZMQ_BridgeHelpers.ipf,
  deliberately NOT MIES's own `ZEROMQ_BIND_REP_PORT` (5670) so this can coexist with
  MIES's own (currently short-circuited) ZeroMQ subsystem in the same experiment.
  configure_igor_launch(port=...) can override the port this bridge itself connects
  to (every ZMQ-talking function goes through `_igor_zmq_endpoint()`, not a fixed
  constant, so they all pick up a configured custom port together).
- One JSON `CallFunction` request per ZeroMQ REQ-socket round trip: send
  `{"version": 1, "messageID": ..., "CallFunction": {"name": ..., "params": [...]}}`,
  receive `{"errorCode": {"value": ..., "msg": ...}, "result": ...}`. A NEW REQ socket
  is created for every single call (see `call_function` below) rather than one
  reused across calls -- a REQ socket that times out waiting for a reply is left in a
  state where it cannot send again without being recreated (confirmed empirically
  this session), so per-call sockets sidestep that fragility entirely at negligible
  cost for a local TCP connection.
- **Confirmed documentation bug in the XOP's own README/help**: it says
  `CallFunction.name` should be "a ProcGlobal function without module and/or
  independent module specification, i.e. without `#`" -- empirically confirmed this
  session that this is simply wrong for independent-module functions (like
  everything in the `ZBR` module): the qualified form (`"ZBR#ZBR_Ping"`) is what
  actually works; the unqualified form fails with `errorCode.value=101` ("Unknown
  function"). Every ZBR call in this file uses the qualified form.
- Multi-return Igor functions (`Function [a, b] Foo()`, Igor 8+) are fully supported
  over this protocol -- `result` becomes a JSON array of typed values in declaration
  order. `call_function` below decodes this into a plain Python list automatically.
- Wave return values carry the ENTIRE wave (dimensions, units, note, complex/text/
  wave-ref support) natively-serialized as JSON -- see `_decode_wave` below. This
  replaces the old COM bridge's per-point `GetNumericWavePointValue` loop entirely,
  and is why the new `get_wave` supports far more than the old "1D real waves only"
  limitation.
- **Central architectural constraint, unchanged from the COM-vs-ZeroMQ evaluation**:
  Igor's `Execute` operation (used to run arbitrary free-form command text) cannot be
  called unqueued from inside a Function -- only `Execute/P` (deferred: queued to run
  only after the calling function returns) is legal there. This means a single
  CallFunction round trip cannot synchronously "run this arbitrary command string and
  hand back what it printed" -- there is no direct equivalent of the COM bridge's
  `Execute2`. `execute_igor_command` below instead
  use a submit-then-poll pattern (`ZBR_SubmitCommand`/`ZBR_SubmitCommandUnattended`
  queue the command and return a token immediately; `ZBR_PollCommand`, called via a
  LATER separate request, reports completion and the captured output). Every OTHER
  tool below that doesn't need to run arbitrary free-form text (get_wave,
  check_compilation_state, get_debugger_state, get_environment_summary's underlying
  queries, read_help_file, etc.) is backed by a small, purpose-built, directly-callable
  Igor function instead, and is a single synchronous round trip -- no polling needed.
- **Commands with an unknown or long runtime (v2.1.0+)**: `execute_igor_command`
  blocks the whole MCP tool call while polling, up to
  their own `timeout_seconds` -- workable for anything expected to finish in seconds,
  but not for a calculation that might run for hours or weeks, since the underlying
  MCP transport itself has been observed to time out a single tool call well under a
  minute regardless of what `timeout_seconds` requests. `submit_igor_command`
  exposes the same submit step as its own tool
  (returns a token immediately, no waiting at all), and `poll_igor_command(token)`
  exposes the same poll step as its own tool (one cheap, instant check, callable any
  number of times, spaced arbitrarily far apart). Because all of the actual state
  (done flag, captured text) lives entirely in Igor Pro's own data waves
  (`root:Packages:ZBR`), not in this bridge's Python process, polling stays reliable
  no matter how long the job runs or how many times this bridge process/Claude
  Desktop itself restarts in the meantime -- the only thing that actually ends the
  job is Igor Pro itself quitting, crashing, or restarting.

**Behavior changes from the COM version worth knowing about**:

- `execute_igor_command` returns everything printed to the history while the command
  ran, including its own echo, as "output" -- COM's `Execute2` could separate fprintf
  output from the history, this transport only has Igor's own history-diffing
  (`CaptureHistory`) to go on. Prefix a command with `Silent 1;` if you want the echo
  suppressed from the returned text.
- **Use `print`, not `fprintf 0, ...`, to get data back.** Confirmed live this
  session: `CaptureHistory` (which the submit/poll mechanism above relies on)
  captures `print` output but does NOT capture `fprintf`-to-history-refnum output at
  all, whether directed at refnum 0 (history), -1, or -2 -- a command consisting of
  only `fprintf 0, "..."` runs without error but returns an empty "output"
  every time, even though the exact same value printed via `print` is captured
  correctly. This is a real behavior change from the COM version, whose `Execute2`
  had its own dedicated mechanism for capturing `fprintf(0,...)` output specifically
  (unrelated to `CaptureHistory`), which is why that was the documented pattern
  before. `fprintf` remains fine (and necessary) for anything that ISN'T about
  getting data back through this bridge, e.g. writing to a wave or a real file.
- `load_experiment` no longer hot-swaps the open experiment in the running instance
  (that was `IApplication.LoadExperiment`, a COM-only method -- confirmed neither
  "LoadExperiment" nor "OpenFile" appear anywhere in Igor Reference.ihf, only in
  Automation Server.ihf, and there is no procedure-language equivalent). It instead
  asks the running instance to quit, then relaunches the configured Igor Pro
  executable with the target file path as a launch argument -- a real process
  restart, not an in-place swap. **Any unsaved changes in the currently-open
  experiment are lost** (the same as before -- COM's LoadExperiment never auto-saved
  either -- but now there is also no "hot" instance left to save from afterward if
  you forgot). Call `execute_igor_command('SaveExperiment')` first if that matters.
- `ensure_igor_pro_bridge_defined` is retired. It existed to make sure a
  `#ifdef IGOR_PRO_BRIDGE`-gated procedure file's optional code got compiled in
  without a human hand-editing the experiment's Procedure window. `ZBR`'s own
  functions have no such gating (the whole point of an independent module is that it
  compiles on its own regardless of ProcGlobal's `#define` state), so this bridge no
  longer needs it for its own purposes.
- `check_bridge_health`/`check_compilation_state` can no longer distinguish "Igor Pro
  isn't running" from "Igor Pro is running but ZMQ_BridgeHelpers.ipf isn't
  included/compiled/bound" from "wrong port" as cleanly as COM's `GetActiveObject`
  could (a clean binary "is there a registered COM object" signal) -- a ZeroMQ REQ
  socket that gets no reply at all looks the same in all three cases. See
  `check_bridge_health`'s docstring for what to check by hand if this happens.

Setup
-----
    pip install mcp pyzmq pywin32

(pywin32 is still needed for the window tools (list_igor_windows etc.) and
for launching the Igor Pro process -- see below. It is no longer needed for, or
involved in, talking to Igor Pro itself.)

Registering with Claude Desktop
--------------------------------
Do NOT register this by manually editing claude_desktop_config.json -- that does not
work reliably for local MCP servers in current Claude Desktop builds. Instead, package
this directory as a Claude Desktop Extension (.mcpb) and install it via Settings ->
Extensions -> Advanced settings -> Extension Developer -> Install Extension:

    mcpb pack tools/igor-mcp-bridge tools/igor-mcp-bridge/igor-pro-bridge-X.Y.Z.mcpb

See Packages/doc/igor-pro-bridge.rst ("Installation") for the full, up-to-date
installation steps, including the one-time ZMQ_BridgeHelpers.ipf setup step for any
experiment other than this repo's own.

This is a *local* MCP server (stdio transport) -- it only works from a Claude Desktop
session running on the same Windows machine as Igor Pro, not from a cloud/Cowork sandbox.
"""

import hashlib
import html.parser
import importlib.metadata
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from typing import Optional

if sys.platform != "win32":
    raise RuntimeError(
        "tools/igor-mcp-bridge/server.py is Windows-only (requires pywin32 for "
        "the window tools (list_igor_windows etc.) and for launching the "
        "configured Igor Pro executable). It cannot run on this platform "
        f"({sys.platform!r}) -- e.g. running it by accident on a non-Windows dev "
        "machine or in CI. The ZeroMQ transport itself is cross-platform; this "
        "restriction is only about this file's OS-level helper tools."
    )

import win32api
import win32con
import win32gui
import win32process

import comtypes.client
import zmq

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("igor-pro")

# --- ZeroMQ transport ----------------------------------------------------------------

# Matches ZBR_ZEROMQ_ENDPOINT/ZBR_ZEROMQ_DEFAULT_PORT in
# Packages/MIES/ZMQ_BridgeHelpers.ipf.
IGOR_ZMQ_HOST = "tcp://127.0.0.1"
IGOR_ZMQ_DEFAULT_PORT = 5680
_ZMQ_DEFAULT_RECV_TIMEOUT_MS = 5000
_ZMQ_SEND_TIMEOUT_MS = 2000
_ZMQ_LINGER_MS = 0

# Set by configure_igor_launch(port=...) -- see that tool's docstring. None means
# "use IGOR_ZMQ_DEFAULT_PORT".
_configured_igor_port = None

_zmq_context = None


def _get_zmq_context():
    global _zmq_context
    if _zmq_context is None:
        _zmq_context = zmq.Context()
    return _zmq_context


def _igor_zmq_endpoint():
    """The ZeroMQ endpoint this bridge currently connects to. Every function that
    talks to Igor Pro over ZeroMQ must go through this (not a fixed string) so they
    all consistently follow whatever custom port configure_igor_launch(port=...) set,
    instead of some functions silently still targeting the default port."""
    port = (
        _configured_igor_port
        if _configured_igor_port is not None
        else IGOR_ZMQ_DEFAULT_PORT
    )
    return f"{IGOR_ZMQ_HOST}:{port}"


class IgorZmqError(RuntimeError):
    """Igor Pro replied, but reported errorCode.value != 0 for the CallFunction call."""


class IgorZmqUnreachable(RuntimeError):
    """No reply was received at all within the timeout. Could mean: Igor Pro isn't
    running, ZMQ_BridgeHelpers.ipf isn't #include-d/compiled in the running instance,
    its ZeroMQ server socket isn't bound to this bridge's currently configured
    endpoint (see _igor_zmq_endpoint), or the reply is simply slow (e.g. a
    long-running command; pass a longer timeout_ms)."""


def _decode_number(value):
    """Non-normal numbers (NaN/Inf/-Inf) are encoded as strings per the ZeroMQ-XOP's
    own spec ("Messages consist of JSON ... NaN, Inf and -Inf are not supported by
    JSON, so we encode these non-normal numbers as strings"). Decode those back to
    real Python floats; pass anything else through unchanged."""
    if isinstance(value, str):
        low = value.strip().lower()
        if low == "nan":
            return float("nan")
        if low in ("inf", "+inf"):
            return float("inf")
        if low == "-inf":
            return float("-inf")
    return value


def _reshape_column_major(flat, dim_size):
    """Reshape a flat, column-major list per dim_size (1 to 4 entries, per the
    ZeroMQ-XOP's wave serialization spec) into nested Python lists indexed
    data[row][col][layer][chunk] -- matches the spec's own worked example
    (`np.array(raw).reshape(dim_size, order='F')`) without requiring numpy."""
    if not dim_size or list(dim_size) == [0]:
        return []

    dims = list(dim_size) + [1] * (4 - len(dim_size))
    rows, cols, layers, chunks = dims[:4]
    if rows == 0:
        return []

    def at(r, c=0, l=0, k=0):
        return flat[r + rows * (c + cols * (l + layers * k))]

    ndims = len(dim_size)
    if ndims <= 1:
        return [at(r) for r in range(rows)]
    if ndims == 2:
        return [[at(r, c) for c in range(cols)] for r in range(rows)]
    if ndims == 3:
        return [
            [[at(r, c, l) for l in range(layers)] for c in range(cols)]
            for r in range(rows)
        ]
    return [
        [
            [[at(r, c, l, k) for k in range(chunks)] for l in range(layers)]
            for c in range(cols)
        ]
        for r in range(rows)
    ]


def _decode_wave(value):
    """value is None (an invalid/free wave reference, `$""`) or the wave-serialization
    object documented in https://github.com/AllenInstitute/ZeroMQ-XOP's README ("Wave
    serialization format"). Returns None, or a dict with the wave's type, shape, data
    (reshaped into nested Python lists matching the wave's own dimensionality), unit,
    and note. Supports numeric (real and complex), text, and wave-reference waves."""
    if value is None:
        return None

    wave_type = value.get("type", "")
    dimension = value.get("dimension", {}) or {}
    dim_size = dimension.get("size", [])
    data = value.get("data", {}) or {}
    raw = data.get("raw", [])

    if wave_type == "WAVE_TYPE":
        decoded_data = [
            _decode_wave(item) if item is not None else None for item in raw
        ]
    elif isinstance(raw, dict) and "real" in raw:
        # Complex wave: raw = {"real": [...], "imag": [...]}.
        real = [_decode_number(x) for x in raw.get("real", [])]
        imag = [_decode_number(x) for x in raw.get("imag", [])]
        decoded_data = [complex(r, i) for r, i in zip(real, imag)]
    else:
        decoded_data = [_decode_number(x) for x in raw]

    return {
        "type": wave_type,
        "dim_size": dim_size,
        "data": _reshape_column_major(decoded_data, dim_size),
        "unit": data.get("unit"),
        "note": value.get("note", ""),
        "dimension": dimension,
    }


def _decode_typed(typed):
    """typed is {"type": "variable"|"string"|"wave"|"dfref", "value": ...} -- return
    the corresponding plain Python value (numbers get NaN/Inf decoded, waves get fully
    decoded via _decode_wave, everything else passes through as-is)."""
    if not isinstance(typed, dict):
        return typed
    kind = typed.get("type")
    value = typed.get("value")
    if kind == "variable":
        return _decode_number(value)
    if kind == "wave":
        return _decode_wave(value)
    return value


def call_function(name, params=None, timeout_ms=_ZMQ_DEFAULT_RECV_TIMEOUT_MS):
    """Send one CallFunction request to Igor Pro and return its decoded result.

    name must be the FULLY QUALIFIED function name for anything in the ZBR
    independent module, e.g. "ZBR#ZBR_Ping" -- see the module docstring's
    documentation-bug note for why (the XOP's own docs say to omit the "#"; that is
    wrong for independent-module functions, confirmed empirically).

    Returns a plain Python value: None/number/string for a single scalar return, a
    dict for a wave return (see _decode_wave), or a list of such values (in
    declaration order) for a multi-return ("Function [a, b] Foo()") function.

    Raises IgorZmqUnreachable if no reply arrives within timeout_ms at all, or
    IgorZmqError if Igor Pro replied but reported errorCode.value != 0 (the error
    message includes any "history" the XOP reports alongside the error, which often
    shows exactly where inside the called function things went wrong).
    """
    request = {
        "version": 1,
        "messageID": uuid.uuid4().hex,
        "CallFunction": {"name": name, "params": list(params) if params else []},
    }
    payload = json.dumps(request)

    endpoint = _igor_zmq_endpoint()
    sock = _get_zmq_context().socket(zmq.REQ)
    sock.setsockopt(zmq.RCVTIMEO, timeout_ms)
    sock.setsockopt(zmq.SNDTIMEO, _ZMQ_SEND_TIMEOUT_MS)
    sock.setsockopt(zmq.LINGER, _ZMQ_LINGER_MS)
    sock.connect(endpoint)
    try:
        sock.send_string(payload)
        reply_raw = sock.recv_string()
    except zmq.error.Again:
        raise IgorZmqUnreachable(
            f"No reply from Igor Pro within {timeout_ms}ms while calling {name!r}. "
            f"{_diagnose_no_reply()}"
        ) from None
    finally:
        sock.close()

    try:
        reply = json.loads(reply_raw)
    except json.JSONDecodeError as e:
        raise IgorZmqError(
            f"Malformed reply from Igor Pro for {name!r}: {reply_raw!r}"
        ) from e

    error_code = reply.get("errorCode") or {}
    if error_code.get("value", 0) != 0:
        detail = (
            f"Igor Pro reported an error calling {name!r} (code "
            f"{error_code.get('value')}): {error_code.get('msg', '(no message)')}"
        )
        history = reply.get("history")
        if history:
            detail += f"\nIgor history during this call: {history!r}"
        raise IgorZmqError(detail)

    result = reply.get("result")
    if isinstance(result, list):
        return [_decode_typed(item) for item in result]
    return _decode_typed(result)


_REBIND_KEEPER_PERIOD_SECONDS = 10


def _rebind_request_file():
    """File by which the bridge asks Igor Pro to rebind its socket, see ZBR_BindKeeperTick in
    ZMQ_BridgeHelpers.ipf: %LOCALAPPDATA%\\igor-pro-bridge\\rebind-request-<port>."""
    port = _igor_zmq_endpoint().rsplit(":", 1)[1]
    return os.path.join(
        os.environ.get("LOCALAPPDATA", ""), "igor-pro-bridge", f"rebind-request-{port}"
    )


def _request_rebind() -> bool:
    """Ask Igor Pro to rebind the socket of this bridge, e.g. after zeromq_stop() of other
    code. Its background task ZBR_BindKeeper checks for the request file every
    _REBIND_KEEPER_PERIOD_SECONDS while no procedure code runs. A request while Igor Pro is
    only busy is harmless: the bind attempt then just fails with \"Address in use\"."""
    if not os.environ.get("LOCALAPPDATA"):
        return False
    path = _rebind_request_file()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8"):
            pass
    except OSError:
        return False
    return True


def _diagnose_no_reply() -> str:
    """Short diagnosis why Igor Pro does not answer, from the OS process and window list.
    Requests a rebind of the socket if Igor Pro runs and shows no dialog, see
    _request_rebind."""
    try:
        pids = _list_igor_pids()
        if not pids:
            return "Igor Pro is not running."
        dialogs = _possible_dialogs()
    except Exception:
        return "See check_bridge_health."
    if dialogs:
        return (
            "Igor Pro is running and shows these windows besides its main window: "
            f"{_describe_dialogs(dialogs)}. A modal dialog blocks the bridge -- see "
            "list_igor_windows, click_igor_dialog_button and close_igor_window."
        )
    rebind = (
        f"Requested a rebind of the socket at {_igor_zmq_endpoint()} (e.g. after zeromq_stop() "
        "of other code), which Igor Pro does within "
        f"{_REBIND_KEEPER_PERIOD_SECONDS} s while no procedure code runs; retry then. "
        if _request_rebind()
        else ""
    )
    return (
        "Igor Pro is running and shows no dialog, so it is probably busy (e.g. a long "
        "command, saving a large experiment) or its socket is not bound. "
        + rebind
        + "Otherwise try again later or with a longer timeout; if it never answers, "
        "ZMQ_BridgeHelpers.ipf may not be loaded, see check_bridge_health."
    )


def _reachable(timeout_ms=1000):
    """True if ZBR#ZBR_Ping answers within timeout_ms, False otherwise. Used for
    "is anything listening right now" checks (already_running / poll loops) where the
    caller doesn't need the actual reply."""
    try:
        call_function("ZBR#ZBR_Ping", timeout_ms=timeout_ms)
        return True
    except (IgorZmqError, IgorZmqUnreachable):
        return False


# --- Command execution (submit/poll) ---------------------------------------------------

_SUBMIT_POLL_INTERVAL_SECONDS = 0.1
_SUBMIT_POLL_TIMEOUT_SECONDS = 30.0


def _submit_and_poll(submit_function: str, command: str, timeout_seconds: float) -> str:
    """Submit `command` via the given ZBR submit function (ZBR_SubmitCommand or
    ZBR_SubmitCommandUnattended) and poll ZBR_PollCommand until it reports done,
    returning the captured text. See the module docstring for why this submit/poll
    dance is needed at all (Execute cannot run unqueued inside a Function)."""
    token = call_function(f"ZBR#{submit_function}", [command])

    deadline = time.monotonic() + timeout_seconds
    while True:
        is_done, text = call_function("ZBR#ZBR_PollCommand", [token])
        if is_done:
            return text
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"Timed out after {timeout_seconds:.0f}s waiting for a submitted "
                f"command to finish (token {token!r}). Command was: {command}"
            )
        time.sleep(_SUBMIT_POLL_INTERVAL_SECONDS)


def _split_error(text: str) -> tuple:
    """ZBR_FinishToken() prefixes the captured text with "ERROR: <message>" when the command
    failed; split that into (output, error)."""
    if isinstance(text, str) and text.startswith("ERROR: "):
        first, _, rest = text.partition("\r")
        return rest, first[len("ERROR: ") :]
    return text, ""


def _submit_function(debugger: bool) -> str:
    return "ZBR_SubmitCommand" if debugger else "ZBR_SubmitCommandUnattended"


@mcp.tool()
def execute_igor_command(
    command: str,
    timeout_seconds: float = _SUBMIT_POLL_TIMEOUT_SECONDS,
    debugger: bool = False,
) -> dict:
    """Run a single command line in the running Igor Pro instance and return what it
    printed.

    **Prefer run_igor_code for anything beyond a short command.** The command line has no
    flow control, results of built-in functions must be printed or assigned, and SVAR/NVAR/
    WAVE references are not allowed. run_igor_code runs the same as compiled function code.
    Use this tool for quick queries, for operations which only work outside functions
    (e.g. `Quit`, `Execute/P ...`), and for calling already compiled code while background
    tasks run that a recompile would stop.

    Context: if ZBR_ProcGlobal.ipf is included (run_igor_code adds it), the command runs in
    ProcGlobal context, so the experiment's constants, functions and macros resolve as on
    Igor's own command line. Otherwise it runs in the bridge's independent module, where
    only calls of ProcGlobal functions are resolved and constants are unknown.

    To get data back, use `print` (CaptureHistory does not capture `fprintf` to the history).
    For a command with an unknown or long runtime, use submit_igor_command/
    poll_igor_command instead: this tool blocks until the command finishes or
    `timeout_seconds` elapses.

    `debugger` (default False) keeps Igor's Debugger as it is. By default it is disabled
    for the command and restored afterward, as a Debugger pause in procedure code cannot be
    resumed by the bridge and would hang the command (see set_debugger_enabled).

    Errors never open a modal dialog. Returns {"output": captured history text, "error":
    error message or ""}.

    Example: execute_igor_command('WaveStats/Q jack; print V_avg')
    """
    text = _submit_and_poll(_submit_function(debugger), command, timeout_seconds)
    output, error = _split_error(text)
    return {"output": output, "error": error}


_UNKNOWN_TOKEN_PREFIX = "ERROR: unknown token "


@mcp.tool()
def submit_igor_command(command: str, debugger: bool = False) -> dict:
    """Queue `command` for deferred execution and return a token immediately, without
    waiting for it to finish -- for commands with an unknown or long runtime (minutes up to
    weeks). Check it with poll_igor_command(token), as often and as far apart as needed.

    The token is a row in waves of root:Packages:ZBR maintained by Igor Pro itself, so it
    stays valid across restarts of this bridge or Claude Desktop; only Igor Pro quitting
    ends the job and its token. See execute_igor_command for the command context and the
    `debugger` argument -- keep the Debugger disabled (the default) for long jobs, a
    Debugger pause would otherwise look like a job still running."""
    token = call_function(f"ZBR#{_submit_function(debugger)}", [command])
    return {"token": token}


@mcp.tool()
def poll_igor_command(token: str) -> dict:
    """Check whether a command of submit_igor_command has finished, and return its output
    if so.

    Returns {"done": False} while pending, otherwise {"done": True, "output": captured
    history text, "error": error message or ""}. Raises if `token` is unknown, e.g. from an
    Igor Pro instance which has quit since."""
    is_done, text = call_function("ZBR#ZBR_PollCommand", [token])
    if not is_done:
        return {"done": False}
    if isinstance(text, str) and text.startswith(_UNKNOWN_TOKEN_PREFIX):
        raise RuntimeError(text)
    output, error = _split_error(text)
    return {"done": True, "output": output, "error": error}


@mcp.tool()
def read_session_history(stop: bool = False) -> dict:
    """Read back everything sent to Igor's history area (print output, command
    echoing, error messages, etc.) since this bridge (specifically, ZMQ_BridgeHelpers.
    ipf's own capture) started tracking it -- the reliable way to verify a PAST
    execute_igor_command call's output actually
    happened, without asking a human to look at Igor's screen.

    A capture is started automatically, Igor-side, the first time it's needed (see
    ZBR_EnsureCaptureStarted in ZMQ_BridgeHelpers.ipf) so this always has something to
    report. Each call returns the FULL accumulated text since that start point, not
    just what's new since the last read -- calling this repeatedly with stop=False
    (the default) is always safe.

    stop=True stops the capture (no further text will be recorded for it) and returns
    whatever was captured up to that point; the next call (to this tool, or the next
    command run through this bridge) transparently starts a brand-new capture.
    """
    text = call_function("ZBR#ZBR_ReadSessionHistory", [1 if stop else 0])
    return {"history_text": text, "capture_stopped": stop}


@mcp.tool()
def get_wave(wave_path: str) -> dict:
    """Return an existing Igor wave's data and metadata.

    wave_path should be an absolute Igor path, e.g. "root:testWave" or
    "root:myFolder:testWave".

    Unlike the old COM-based version of this tool (limited to 1D, real-valued waves,
    read one point at a time via GetNumericWavePointValue), this transport's native
    wave serialization supports any dimensionality (up to 4D), real and complex
    numeric waves, text waves, and wave-reference waves (waves of waves) -- the entire
    wave comes back from ONE CallFunction round trip. See the module docstring's wave
    serialization notes for the JSON format this is decoded from.

    Returns a dict with "wave_path", "type" (e.g. "NT_FP64", "TEXT_WAVE_TYPE",
    "WAVE_TYPE"), "dim_size" (1 to 4 numbers), "data" (nested Python lists matching
    the wave's own dimensionality -- data[row][col]... for multi-dimensional waves,
    or a single flat list for 1D), "unit", "note", and "dimension" (delta/offset/
    label/unit per dimension, if set).

    Raises if wave_path does not refer to an existing wave.
    """
    wave = call_function("ZBR#ZBR_GetWaveGeneric", [wave_path])
    if wave is None:
        raise RuntimeError(f"Wave not found: {wave_path}")
    return {"wave_path": wave_path, **wave}


# --- Compilation state -----------------------------------------------------------------


@mcp.tool()
def check_compilation_state() -> dict:
    """Check whether Igor Pro's procedure code is currently compiled or uncompiled.

    Functions from procedure code can only be called while compiled. Igor Pro enters
    the uncompiled state when procedure code is edited inside Igor (only possible
    while nothing is running), or when nothing is running and an included procedure
    file changed on disk. Use reload_and_compile_procedures to get back to compiled
    after editing a .ipf file on disk.

    Note: since ZBR (the independent module this bridge's Igor-side code lives in)
    compiles separately from ProcGlobal/regular MIES code, a "true" result here
    specifically means ProcGlobal's compile state -- confirmed reachable via ZBR at
    all already implies ZBR itself is compiled (otherwise this call would have failed
    with IgorZmqUnreachable/IgorZmqError instead of returning a result).
    """
    compiled = call_function("ZBR#ZBR_IsCompiled")
    return {"compiled": bool(compiled)}


_COMPILE_POLL_INTERVAL_SECONDS = 0.5
_COMPILE_ERROR_REGEXP = re.compile(r"^.+?:\d+:\d+: error: .*$", re.MULTILINE)
_COMPILE_POLL_TIMEOUT_SECONDS = 15.0


def _read_compile_counter():
    """ZBR_ReadCompileCounter(), or None if the call itself fails (e.g. Igor is
    mid-recompile and briefly unreachable) -- treated as "unknown", never fatal, same
    as the counter's own -1-means-unavailable convention Igor-side."""
    try:
        value = call_function("ZBR#ZBR_ReadCompileCounter")
    except (IgorZmqError, IgorZmqUnreachable):
        return None
    return None if value is None or value < 0 else value


@mcp.tool()
def reload_and_compile_procedures() -> dict:
    """Force Igor Pro to reload procedure code from the .ipf files on disk and attempt
    a fresh compilation, then report whether it ended up compiled.

    Use this after editing a .ipf file directly on disk. Only call this while Igor Pro
    is not currently running other procedure code.

    **Caution, carried over from the COM-based version**: Igor Pro has been observed
    becoming unreachable shortly after a reload/compile attempt on more than one
    occasion during this bridge's development (crashed or was closed). As of v2.3.0,
    crash-dump analysis traced this to a genuine EXCEPTION_ACCESS_VIOLATION deep inside
    Igor64.exe itself (not this bridge's own code), and a likely mechanism was
    identified: this bridge's ZeroMQ handler runs as a background thread that keeps
    dispatching incoming CallFunction requests regardless of what Igor's main thread is
    doing, so a request arriving while COMPILEPROCEDURES is mid-rebuild of Igor's own
    internal function/symbol tables is a plausible cross-thread race. v2.3.0 mitigates
    this by stopping the ZeroMQ handler before RELOAD CHANGED PROCS/COMPILEPROCEDURES
    run and restarting it only after compilation finishes. This is a well-reasoned
    mitigation, not a proven fix -- Igor64.exe ships no public symbols, so the exact
    fault can't be confirmed from here, and the crash was already rare/nondeterministic.
    If a tool call after this one starts failing anyway, check_bridge_health() and be
    prepared for Igor Pro to need relaunching.

    **v2.3.1 fix -- handler could stay stopped forever on a failed compile**: v2.3.0's
    restart path was AfterCompiledHook alone, which Igor only calls after a
    *successful* compile. If the edited .ipf had a syntax error, COMPILEPROCEDURES
    failed, AfterCompiledHook never fired, and the ZeroMQ handler -- already stopped by
    ZBR_StopHandlerBeforeRecompile -- stayed stopped permanently, killing the bridge
    with no recovery path short of restarting Igor. Fixed by ZBR_ArmRecompileWatchdog/
    ZBR_RecompileWatchdogTick in ZMQ_BridgeHelpers.ipf: a named background task, armed
    right before the handler is stopped, that unconditionally rebinds/restarts the
    handler regardless of whether the compile succeeds or fails. It is registered with
    `start=(ticks + 60)` (an explicit ~1-second floor before its first possible tick, independent
    of its `period=30` interval) so it cannot fire before RELOAD CHANGED
    PROCS/COMPILEPROCEDURES have finished draining Igor's operation queue -- confirmed
    necessary by live timing instrumentation (stopmstimer(-2)) showing the background
    task could otherwise tick within ~62ms of being armed, well before a real compile
    (~414ms observed) finishes. AfterCompiledHook still restarts the handler
    immediately on the success path; the watchdog is what covers the failure path, and
    self-disarms (CtrlNamedBackground .. stop) the first time either one runs.

    **v2.3.1 fix -- ZBR_IsCompiled() checked the wrong module**: its FunctionInfo() call
    was unqualified, so it resolved against ZBR's own (independent-module) namespace
    -- which compiles separately from ProcGlobal -- instead of ProcGlobal's. This meant
    it could report "compiled" even while ProcGlobal itself had a compile error. Fixed
    by qualifying the lookup as `FunctionInfo("ProcGlobal#...")`.

    **v2.3.1 fix -- "Function Execution Module is still active" dialog**: an unrelated
    pre-existing MIES background thread (not task) left running during
    COMPILEPROCEDURES could raise this modal dialog and freeze the entire operation
    queue. Mitigated by a new BeforeUncompiledHook in ZMQ_BridgeHelpers.ipf that calls
    ThreadGroupRelease(-2) to release any running thread groups before Igor uncompiles.

    Mechanism: calls ZBR_SubmitReloadAndCompile(), which queues (as three independent
    Execute/P entries) a call to stop the ZeroMQ handler and arm the watchdog, then
    `Execute/P "RELOAD CHANGED PROCS "`, then `Execute/P "COMPILEPROCEDURES "`
    Igor-side (see that function's docstring in ZMQ_BridgeHelpers.ipf), then polls for
    completion using two independent signals, same as the COM-based version did:

    1. compileCounter in root:Packages:ZBR (ZBR_ReadCompileCounter), bumped by
       AfterCompiledHook every time Igor confirms a successful compile -- race-free:
       any increase over the baseline read before submitting is trusted immediately.
    2. ZBR_IsCompiled() (the FunctionInfo-based check), as a fallback.

    Poll errors (IgorZmqError/IgorZmqUnreachable) during either check are treated as
    "not ready yet" rather than fatal, since Igor Pro can be briefly unreachable while
    genuinely mid-recompile.

    If this returns "compiled": False, "errors" holds the compile error lines Igor Pro
    printed during this compile, in its "<file>:<line>:<col>: error: <message>" format --
    Igor Pro only prints them if it was launched with /UNATTENDED (see
    launch_igor_pro_unattended), otherwise it shows a modal dialog instead. If NOT launched /UNATTENDED, a
    stuck "Function Compilation Error" dialog is also possible -- this tool tries to
    close it, see also list_igor_windows/close_igor_window. As of v2.3.1, the bridge itself should still be
    reachable in this case (see the watchdog fix above) -- fix the .ipf and call this
    tool again rather than needing to relaunch Igor Pro.
    """
    return _reload_and_compile()


def _history_length():
    try:
        return call_function("ZBR#ZBR_GetHistoryLength")
    except (IgorZmqError, IgorZmqUnreachable):
        return None


def _compile_errors_since(history_start) -> list:
    """The compile error lines Igor Pro printed to the history since history_start, in its
    "<file>:<line>:<col>: error: <message>" format (only printed instead of a dialog if
    Igor Pro was launched with /UNATTENDED)."""
    if history_start is None:
        return []
    try:
        text = call_function("ZBR#ZBR_GetHistorySince", [history_start])
    except (IgorZmqError, IgorZmqUnreachable):
        return []
    return _COMPILE_ERROR_REGEXP.findall((text or "").replace("\r", "\n"))


def _reload_and_compile(before_submit=None) -> dict:
    """Run the reload/compile flow of reload_and_compile_procedures and return its result.

    before_submit, if given, is called right before the reload/compile is queued, e.g. to
    queue an INSERTINCLUDE that the same compile then picks up."""
    baseline_counter = _read_compile_counter()
    history_start = _history_length()

    if before_submit is not None:
        before_submit()

    call_function("ZBR#ZBR_SubmitReloadAndCompile")

    deadline = time.monotonic() + _COMPILE_POLL_TIMEOUT_SECONDS
    attempts = 0
    while True:
        attempts += 1

        counter = _read_compile_counter()
        if (
            baseline_counter is not None
            and counter is not None
            and counter > baseline_counter
        ):
            return {
                "compiled": True,
                "poll_attempts": attempts,
                "confirmed_via": "AfterCompiledHook counter (ZBR_ReadCompileCounter)",
            }

        # Before the queued reload/compile ran, ZBR_IsCompiled() still reports the state
        # before it, so it only decides once the compile is over.
        try:
            if not call_function("ZBR#ZBR_IsRecompilePending"):
                if call_function("ZBR#ZBR_IsCompiled"):
                    return {
                        "compiled": True,
                        "poll_attempts": attempts,
                        "confirmed_via": "ZBR_IsCompiled",
                    }
                break
        except (IgorZmqError, IgorZmqUnreachable):
            pass  # treat as "not ready yet", e.g. while the handler is stopped

        if time.monotonic() >= deadline:
            break
        time.sleep(_COMPILE_POLL_INTERVAL_SECONDS)

    errors = _compile_errors_since(history_start)
    dismiss_result = _attempt_dismiss_compile_error_dialog()
    return {
        "compiled": False,
        "errors": errors,
        "poll_attempts": attempts,
        "auto_dismiss_attempted": dismiss_result,
        "note": (
            "Not compiled. See 'errors' for the '<file>:<line>:<col>: error: ...' lines of "
            "Igor's history (Igor Pro launched with /UNATTENDED prints them there instead of "
            "showing a dialog), or check for a stuck compile-error dialog (see "
            "'auto_dismiss_attempted' above). If 'errors' is empty and Igor Pro did not "
            f"answer within {_COMPILE_POLL_TIMEOUT_SECONDS:.0f}s, see check_bridge_health."
        ),
    }


# --- Scratch procedures (run_igor_code) ----------------------------------------------
#
# Code that needs more than Igor's command line offers (flow control, local variables,
# assigning results of functions, the experiment's constants) is written into
# ZBR_Scratch.ipf and run as a function. That file is included by ZBR_ProcGlobal.ipf, both
# live in procedure/ZBR_UserProcedures/ next to ZMQ_BridgeHelpers.ipf. The folder is linked
# into Igor's "User Procedures" folder, so INSERTINCLUDE finds it, and both files compile
# in ProcGlobal like the experiment's own code. With ZBR_ProcGlobal.ipf included, commands
# of execute_igor_command run in ProcGlobal context as well, see ZBR_QueueCommand().

_SCRATCH_FILE_NAME = "ZBR_Scratch.ipf"
_SCRATCH_MAIN_FUNCTION = "ZBR_Scratch_Main"
_SCRATCH_HEADER = (
    "#pragma rtFunctionErrors = 1\n"
    '#pragma TextEncoding     = "UTF-8"\n'
    "#pragma rtGlobals        = 3\n"
    "\n"
    "// Written by the Igor Pro Bridge (run_igor_code), every call replaces the contents.\n"
)


def _scratch_file_path() -> str:
    """Native path of ZBR_Scratch.ipf, resolved Igor-side from the location of
    ZMQ_BridgeHelpers.ipf, see ZBR_GetUserProceduresFolder()."""
    folder = call_function("ZBR#ZBR_GetUserProceduresFolder")
    if not folder or not os.path.isdir(folder):
        raise RuntimeError(
            f"The folder for the scratch procedures ({folder!r}) does not exist. It must be "
            "the folder procedure/ZBR_UserProcedures next to ZMQ_BridgeHelpers.ipf, see the "
            "installation steps in README.md."
        )
    return os.path.join(folder, _SCRATCH_FILE_NAME)


def _write_scratch_file(contents: str) -> str:
    path = _scratch_file_path()
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(_SCRATCH_HEADER + contents)
    return path


def _running_background_tasks() -> list:
    tasks = call_function("ZBR#ZBR_GetRunningBackgroundTasks") or ""
    return [t for t in tasks.split(";") if t]


def _compile_with_scratch(contents: str) -> dict:
    """Write `contents` into ZBR_Scratch.ipf, make sure ZBR_ProcGlobal.ipf is included and
    compile. On a compile error the scratch file is emptied and compiled again, so the
    experiment does not stay uncompiled, and the result of the failed compile is returned."""
    _write_scratch_file(contents)

    def _insert_if_needed():
        if not call_function("ZBR#ZBR_ProcGlobalIsInInclude"):
            call_function("ZBR#ZBR_SubmitInsertProcGlobal")

    result = _reload_and_compile(before_submit=_insert_if_needed)
    if result.get("compiled"):
        return result

    _write_scratch_file("")
    recovery = _reload_and_compile()
    result["recompiled_without_scratch_code"] = recovery.get("compiled", False)
    return result


@mcp.tool()
def run_igor_code(
    code: str = "",
    functions: str = "",
    call: str = "",
    timeout_seconds: float = _SUBMIT_POLL_TIMEOUT_SECONDS,
    force: bool = False,
) -> dict:
    """Compile and run procedure code in Igor Pro -- the default way to run anything
    beyond a trivial one-line command.

    `code` is the body of a function, which this tool wraps into
    `Function ZBR_Scratch_Main() ... End` and calls. `functions` holds complete function
    definitions (constants, structures, helper functions) placed before it. `call`
    overrides what is run afterward, e.g. `"MyHelper(1, 2)"` for a function from
    `functions`; it defaults to `ZBR_Scratch_Main()`. Use `print` to return data.

    Unlike execute_igor_command, the code runs as compiled function code in ProcGlobal: flow
    control, local variables, WAVE/NVAR/SVAR references, multiple return syntax and the
    experiment's own constants and functions all work, and the result of a built-in
    function does not need the command line's workarounds.

    Mechanism: the code is written to ZBR_Scratch.ipf in procedure/ZBR_UserProcedures/ (next
    to ZMQ_BridgeHelpers.ipf). ZBR_ProcGlobal.ipf, which includes it, is added to the
    experiment with INSERTINCLUDE on first use -- this adds an `#include` line to the
    experiment's Procedure window, see remove_scratch_procedures to take it out again. Every
    call replaces the scratch file and recompiles all procedures.

    **Recompiling stops procedure code running in background tasks**, e.g. a data
    acquisition. If any named background task (other than the bridge's own) is running,
    this refuses and lists them, unless `force` is True. Already compiled helpers can still
    be called with execute_igor_command while they run.

    On a compile error, the scratch file is emptied and the procedures are compiled again,
    so the experiment does not stay uncompiled. The returned "compile" entry then holds the
    compile result, including any error lines.

    Returns {"compiled": bool, "output": captured history text, "error": runtime error
    message or ""}, plus "compile" with the details of the compile step.
    """
    if not code and not call:
        raise RuntimeError("Pass `code` (a function body) and/or `call`.")

    if not force:
        tasks = _running_background_tasks()
        if tasks:
            return {
                "compiled": False,
                "refused": True,
                "running_background_tasks": tasks,
                "note": (
                    "Recompiling would stop these background tasks. Pass force=True to "
                    "compile anyway, or call already compiled code via execute_igor_command."
                ),
            }

    contents = functions.strip() + "\n" if functions.strip() else ""
    if code:
        contents += f"\nFunction {_SCRATCH_MAIN_FUNCTION}()\n\n{code}\nEnd\n"

    compile_result = _compile_with_scratch(contents)
    if not compile_result.get("compiled"):
        return {"compiled": False, "output": "", "error": "", "compile": compile_result}

    command = call or f"{_SCRATCH_MAIN_FUNCTION}()"
    text = _submit_and_poll("ZBR_SubmitCommandUnattended", command, timeout_seconds)
    text, error = _split_error(text)

    return {"compiled": True, "output": text, "error": error, "compile": compile_result}


@mcp.tool()
def remove_scratch_procedures() -> dict:
    """Remove the include of ZBR_ProcGlobal.ipf (and so ZBR_Scratch.ipf) from the experiment
    again, which run_igor_code added with INSERTINCLUDE, and recompile.

    Use this before saving an experiment that should not reference the bridge's procedure
    files. Commands of execute_igor_command run in the bridge's own module context
    afterward, see its docstring."""
    if not call_function("ZBR#ZBR_ProcGlobalIsInInclude"):
        return {"removed": False, "note": "ZBR_ProcGlobal.ipf is not included."}

    _write_scratch_file("")
    result = _reload_and_compile(
        before_submit=lambda: call_function("ZBR#ZBR_SubmitDeleteProcGlobal")
    )
    return {"removed": True, "compile": result}


_EXISTS_MEANINGS = {
    0: "unknown name (also constants, which exists() does not report)",
    1: "wave",
    2: "numeric or string variable",
    3: "built-in or external (XOP) function",
    4: "operation",
    5: "macro",
    6: "user-defined function",
}


@mcp.tool()
def get_igor_symbol_info(name: str) -> dict:
    """Look up a name in ProcGlobal context, i.e. as the experiment's own code sees it:
    `exists(name)` and, for functions, `FunctionInfo(name)` (procedure file, line, parameters).

    Use this instead of running `exists()`/`FunctionInfo()` through execute_igor_command
    without the scratch procedures: there they run in the bridge's own independent module
    and report 0/nothing for the experiment's functions. Needs ZBR_ProcGlobal.ipf, which
    run_igor_code includes; without it only "function_info" is available.

    Returns {"exists": code, "meaning": text, "function_info": {key: value}}."""
    if call_function("ZBR#ZBR_ProcGlobalIncluded"):
        raw = call_function("ZBR_PG_SymbolInfo", [name])
    else:
        raw = "EXISTS:;" + (call_function("ZBR#ZBR_FunctionInfo", ["ProcGlobal#" + name]) or "")

    info = {}
    for item in (raw or "").split(";"):
        key, sep, value = item.partition(":")
        if sep:
            info[key] = value

    exists_raw = info.pop("EXISTS", "")
    exists = int(exists_raw) if exists_raw.strip().lstrip("-").isdigit() else None
    return {
        "exists": exists,
        "meaning": _EXISTS_MEANINGS.get(exists, "not available without ZBR_ProcGlobal.ipf"),
        "function_info": info,
    }


# --- Tests (IgorTest) -----------------------------------------------------------------
#
# A test run of IgorTest (https://github.com/byte-physics/igortest) often outlives a single
# MCP tool call: test cases with reentry continue in background tasks after the starting
# command returned. The run is therefore started by run_igor_tests and its JUnit output,
# which IgorTest writes at the very end, is collected by get_igor_test_results.

_JUNIT_FILE_PATTERN = re.compile(r"^JU_.*\.xml$", re.IGNORECASE)
_JUNIT_STABLE_SECONDS = 1.0


def _junit_files(folder: str) -> dict:
    """{path: modification time} of the JUnit files of IgorTest in `folder`."""
    result = {}
    try:
        names = os.listdir(folder)
    except OSError:
        return result
    for name in names:
        if _JUNIT_FILE_PATTERN.match(name):
            path = os.path.join(folder, name)
            try:
                result[path] = os.path.getmtime(path)
            except OSError:
                pass
    return result


def _parse_junit(path: str) -> dict:
    import xml.etree.ElementTree as ElementTree

    root = ElementTree.parse(path).getroot()
    suites = []
    failures = []
    for suite in root.iter("testsuite"):
        name = suite.get("name", "")
        if name.startswith("@"):  # IgorTest's own hook/data generator pseudo suites
            continue
        suites.append(
            {
                "name": name,
                "tests": int(suite.get("tests", 0)),
                "failures": int(suite.get("failures", 0)),
                "errors": int(suite.get("errors", 0)),
                "skipped": int(suite.get("skipped", 0)),
                "time": float(suite.get("time", 0) or 0),
            }
        )
        for case in suite.iter("testcase"):
            for kind in ("failure", "error"):
                for problem in case.findall(kind):
                    failures.append(
                        {
                            "suite": name,
                            "test_case": case.get("name", ""),
                            "kind": kind,
                            "message": problem.get("message", ""),
                        }
                    )

    total = {
        key: sum(suite[key] for suite in suites)
        for key in ("tests", "failures", "errors", "skipped")
    }
    return {"junit_file": path, "total": total, "suites": suites, "failures": failures}


def _default_junit_folder() -> str:
    folder = call_function("ZBR#ZBR_GetHomePath")
    if not folder:
        raise RuntimeError(
            "The experiment is not saved, so it has no home folder for the JUnit output. "
            "Pass junit_folder explicitly."
        )
    return folder


@mcp.tool()
def run_igor_tests(command: str, junit_folder: str = "", compile_first: bool = True) -> dict:
    """Start a test run of IgorTest and return immediately; collect the results later with
    get_igor_test_results.

    `command` starts the run and must enable the JUnit output of IgorTest, e.g.
    `RunTest("MyTests.ipf", enableJU = 1)` or a project wrapper around it. `junit_folder` is
    where IgorTest writes its JU_*.xml file, by default the experiment's home folder.

    With `compile_first` (default), procedure files changed on disk are reloaded and
    compiled before the run starts. Otherwise Igor Pro notices the change later, e.g. when
    a window is activated, and goes uncompiled in the middle of the run, which stops test
    cases running in background tasks.

    The command runs with Igor's Debugger disabled, see execute_igor_command.

    Returns {"started_at": time stamp to pass to get_igor_test_results, "junit_folder",
    "token" of the starting command, "compile": result of the compile step}."""
    folder = junit_folder or _default_junit_folder()

    compile_result = None
    if compile_first:
        compile_result = _reload_and_compile()
        if not compile_result.get("compiled"):
            return {"started": False, "compile": compile_result}

    started_at = time.time()
    token = call_function("ZBR#ZBR_SubmitCommandUnattended", [command])
    return {
        "started": True,
        "started_at": started_at,
        "junit_folder": folder,
        "token": token,
        "compile": compile_result,
    }


@mcp.tool()
def get_igor_test_results(junit_folder: str, started_at: float = 0.0) -> dict:
    """Return the results of a test run of IgorTest from its JUnit output, see
    run_igor_tests.

    Looks for the newest JU_*.xml file in `junit_folder` written after `started_at` (as
    returned by run_igor_tests; 0 takes the newest file). Returns {"finished": False} while
    there is none yet or it is still being written -- call again later, the test run itself
    is not affected.

    Once finished: {"finished": True, "junit_file", "total": {"tests", "failures",
    "errors", "skipped"}, "suites": per test suite, "failures": every failed assertion or
    error with its test case and message}."""
    files = {
        path: mtime
        for path, mtime in _junit_files(junit_folder).items()
        if mtime >= started_at
    }
    if not files:
        return {"finished": False}

    newest = max(files, key=files.get)
    if time.time() - files[newest] < _JUNIT_STABLE_SECONDS:
        return {"finished": False, "note": f"{newest} is still being written."}

    try:
        result = _parse_junit(newest)
    except Exception as e:
        return {"finished": False, "note": f"Could not parse {newest} yet: {e}"}
    return {"finished": True, **result}


# --- Debugger control --------------------------------------------------------------
#
# Unchanged in spirit from the COM-based version -- see that version's extensive
# comment block (still true) for why the Debugger MUST be disabled for any
# unattended/automated session: there is no scriptable way to resume, step, or
# dismiss the Debugger window once something pauses it, and a paused call hangs
# forever. DebuggerOptions is NOT subject to the Execute-only restriction (confirmed
# live), so ZBR_GetDebuggerState/ZBR_SetDebuggerEnabled/ZBR_RestoreDebuggerSettings
# are all single, synchronous CallFunction calls -- no submit/poll needed.

_saved_debugger_settings = None


def _decode_debugger_state(multi) -> dict:
    enable, debug_on_error, debug_on_abort, nvar_checking = multi
    return {
        "enable": bool(enable),
        "debug_on_error": bool(debug_on_error),
        "debug_on_abort": bool(debug_on_abort),
        "nvar_svar_wave_checking": bool(nvar_checking),
    }


@mcp.tool()
def get_debugger_state() -> dict:
    """Read Igor Pro's current Debugger settings (enable, debugOnError, debugOnAbort,
    NVAR_SVAR_WAVE_Checking) without changing them, and save a snapshot inside this
    bridge process for restore_debugger_settings to restore later.

    **Call this before starting any unattended/automated session**, immediately
    before calling set_debugger_enabled(False).
    """
    global _saved_debugger_settings
    state = _decode_debugger_state(call_function("ZBR#ZBR_GetDebuggerState"))
    _saved_debugger_settings = dict(state)
    return state


@mcp.tool()
def set_debugger_enabled(
    enabled: bool,
    debug_on_error: bool = None,
    debug_on_abort: bool = None,
    nvar_svar_wave_checking: bool = None,
) -> dict:
    """Turn Igor Pro's Debugger on or off (and optionally its debugOnError/
    debugOnAbort/NVAR_SVAR_WAVE_Checking sub-settings).

    **For any unattended/automated session, the debugger MUST be disabled: call
    set_debugger_enabled(False) before starting.** See get_debugger_state's docstring
    and the module-level Debugger-control comment above for why.

    enabled=False clears all four settings regardless of the other arguments -- this
    is Igor's own documented DebuggerOptions behavior, not a limitation of this
    function -- so the sub-flags are only applied when enabled=True. Any sub-flag left
    as None (the default) falls back to Igor's CURRENT setting for that flag rather
    than being forced off.

    Recommended pattern around an unattended session:
        get_debugger_state()           # read + save the current settings
        set_debugger_enabled(False)    # disable for the unattended run
        ... run the unattended session ...
        restore_debugger_settings()    # put the saved settings back
    """
    current = _decode_debugger_state(call_function("ZBR#ZBR_GetDebuggerState"))
    if not enabled:
        call_function("ZBR#ZBR_SetDebuggerEnabled", [0])
    else:
        call_function(
            "ZBR#ZBR_RestoreDebuggerSettings",
            [
                1,
                int(
                    current["debug_on_error"]
                    if debug_on_error is None
                    else debug_on_error
                ),
                int(
                    current["debug_on_abort"]
                    if debug_on_abort is None
                    else debug_on_abort
                ),
                int(
                    current["nvar_svar_wave_checking"]
                    if nvar_svar_wave_checking is None
                    else nvar_svar_wave_checking
                ),
            ],
        )
    return _decode_debugger_state(call_function("ZBR#ZBR_GetDebuggerState"))


@mcp.tool()
def restore_debugger_settings() -> dict:
    """Restore Igor Pro's Debugger settings to whatever get_debugger_state last
    captured.

    **Call this when an unattended/automated session ends.**

    Raises if get_debugger_state was never called in this bridge process.
    """
    if _saved_debugger_settings is None:
        raise RuntimeError(
            "No saved Debugger settings to restore -- call get_debugger_state() "
            "before starting the unattended session so there is something to "
            "restore afterward."
        )
    s = _saved_debugger_settings
    call_function(
        "ZBR#ZBR_RestoreDebuggerSettings",
        [
            int(s["enable"]),
            int(s["debug_on_error"]),
            int(s["debug_on_abort"]),
            int(s["nvar_svar_wave_checking"]),
        ],
    )
    return _decode_debugger_state(call_function("ZBR#ZBR_GetDebuggerState"))


# --- Environment summary -----------------------------------------------------------
#
# Composed client-side from the small, generic ZBR_IgorInfo/ZBR_WinList/
# ZBR_ProcedureText/ZBR_DataFolderDir wrappers in ZMQ_BridgeHelpers.ipf -- exactly
# mirroring how the COM-based version worked (it also just ran fprintf-wrapped
# built-in calls and parsed/structured the raw string results in Python). See that
# file's own comments for the confirmed IgorInfo() index meanings and the
# ProcedureText("", 0, "Procedure") argument-order gotcha.


def _categorize_procedure_file(name: str) -> str:
    """Bucket an included procedure file name into a coarse category, purely to make a
    ~250-entry file list skimmable in a summary. Buckets reflect this specific repo's
    naming conventions (MIES_*, UTF_* unit tests, igortest-* test framework, IPNWB_*),
    not a general Igor Pro convention."""
    if name.startswith("igortest"):
        return "igortest_framework"
    if name.startswith("UTF_"):
        return "unit_tests"
    if name.startswith("IPNWB"):
        return "ipnwb"
    if name.startswith("MIES_"):
        return "mies_production"
    return "other"


@mcp.tool()
def get_environment_summary() -> dict:
    """Summarize the current Igor Pro instance's live environment: Igor version, the
    loaded experiment, loaded external operations (XOPs), which procedure files are
    actually included right now, the contents of the always-present "Procedure"
    window, and the top-level global data folder layout.

    Returns a dict with:
      - igor_version_info / os_info: raw IgorInfo(0) / IgorInfo(3) strings
      - experiment_file_name / experiment_file_kind: e.g. "Basic.pxp" / "Packed"
      - loaded_xops: list of loaded external operations
      - procedure_window_text: raw contents of the special "Procedure" window --
        inspect this for experiment-specific #include/#define directives
      - included_procedure_file_count / included_procedure_files_by_category /
        included_procedure_files: currently included .ipf files
      - data_folders / top_level_waves: top-level layout under root:
      - debugger_settings: current Debugger state (see set_debugger_enabled's
        docstring for why this matters before any unattended session)
    """
    igor_version_info = call_function("ZBR#ZBR_IgorInfo", [0])
    os_info = call_function("ZBR#ZBR_IgorInfo", [3])
    loaded_xops_raw = call_function("ZBR#ZBR_IgorInfo", [10])
    experiment_file_kind = call_function("ZBR#ZBR_IgorInfo", [11])
    experiment_file_name = call_function("ZBR#ZBR_IgorInfo", [12])
    included_raw = call_function("ZBR#ZBR_WinList", ["*", "WIN:128"])
    data_folders_raw = call_function("ZBR#ZBR_DataFolderDir", [3])
    procedure_window_text = call_function("ZBR#ZBR_ProcedureText", ["", 0, "Procedure"])
    debugger_settings = _decode_debugger_state(
        call_function("ZBR#ZBR_GetDebuggerState")
    )

    included_procedure_files = [
        name for name in included_raw.split(";") if name and name != "Procedure"
    ]
    loaded_xops = [x for x in loaded_xops_raw.split(";") if x]

    folders_part, waves_part = "", ""
    for part in data_folders_raw.split("\r"):
        part = part.strip()
        if part.startswith("FOLDERS:"):
            folders_part = part[len("FOLDERS:") :].rstrip(";")
        elif part.startswith("WAVES:"):
            waves_part = part[len("WAVES:") :].rstrip(";")
    data_folders = [f for f in folders_part.split(",") if f]
    top_level_waves = [w for w in waves_part.split(",") if w]

    category_counts: dict = {}
    for name in included_procedure_files:
        category = _categorize_procedure_file(name)
        category_counts[category] = category_counts.get(category, 0) + 1

    return {
        "igor_version_info": igor_version_info,
        "os_info": os_info,
        "experiment_file_name": experiment_file_name,
        "experiment_file_kind": experiment_file_kind,
        "loaded_xops": loaded_xops,
        "procedure_window_text": procedure_window_text,
        "included_procedure_file_count": len(included_procedure_files),
        "included_procedure_files_by_category": category_counts,
        "included_procedure_files": included_procedure_files,
        "data_folders": data_folders,
        "top_level_waves": top_level_waves,
        "debugger_settings": debugger_settings,
    }


# --- Reading .ihf help files ---------------------------------------------------------
#
# The actual CloseHelp/OpenNotebook/SaveNotebook/KillWindow/OpenHelp sequence now runs
# synchronously, Igor-side, in ZBR_ReadHelpFile (ZMQ_BridgeHelpers.ipf) -- none of
# those operations are subject to the Execute-only restriction, so this needs no
# submit/poll. The exported HTML is written to a temp file (built here, same as the
# COM-based version did) and read directly off disk afterward rather than serialized
# back through the CallFunction reply -- both processes run on the same machine, so
# this sidesteps any question about reply-size limits for a potentially large export.


_CSS_RULE_REGEXP = re.compile(r"P\.([\w+\-]+)\s*\{([^}]*)\}", re.IGNORECASE)


def _css_flags(css: str, base: dict) -> dict:
    """Apply the bold/italic/underline declarations of `css` on top of `base`."""
    flags = dict(base)
    for declaration in css.split(";"):
        key, _, value = declaration.partition(":")
        key, value = key.strip().lower(), value.strip().lower()
        if key == "font-weight":
            flags["bold"] = value in ("bold", "bolder") or value.isdigit() and int(value) >= 600
        elif key == "font-style":
            flags["italic"] = value in ("italic", "oblique")
        elif key == "text-decoration":
            flags["underline"] = "underline" in value
    return flags


class _NotebookHTMLParser(html.parser.HTMLParser):
    """Extracts one {"style": ..., "text": ...} record per <P> paragraph from a notebook's
    HTML export (SaveNotebook/S=5). With `char_styles`, each record also gets "runs": the
    text split into runs with their effective "bold"/"italic"/"underline" flags, combined
    from the paragraph's CSS class (the ruler) and the SPAN styles inside it."""

    _PLAIN = {"bold": False, "italic": False, "underline": False}

    def __init__(self, char_styles: bool = False):
        super().__init__(convert_charrefs=True)
        self.paragraphs = []
        self._char_styles = char_styles
        self._in_style_block = False
        self._css = []
        self._class_flags = {}
        self._in_paragraph = False
        self._style = ""
        self._text_parts = []
        self._runs = []
        self._flag_stack = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attrs = {key.lower(): value for key, value in attrs}
        if tag == "style":
            self._in_style_block = True
        elif tag == "p":
            self._in_paragraph = True
            self._style = attrs.get("class") or ""
            self._text_parts = []
            self._runs = []
            self._flag_stack = [self._class_flags.get(self._style, self._PLAIN)]
        elif tag == "span" and self._in_paragraph:
            self._flag_stack.append(_css_flags(attrs.get("style") or "", self._flag_stack[-1]))

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "style" and self._in_style_block:
            self._in_style_block = False
            for name, body in _CSS_RULE_REGEXP.findall("".join(self._css)):
                self._class_flags[name] = _css_flags(body, self._PLAIN)
        elif tag == "span" and self._in_paragraph and len(self._flag_stack) > 1:
            self._flag_stack.pop()
        elif tag == "p" and self._in_paragraph:
            record = {"style": self._style, "text": "".join(self._text_parts).strip()}
            if self._char_styles:
                record["runs"] = self._runs
            self.paragraphs.append(record)
            self._in_paragraph = False

    def handle_data(self, data):
        if self._in_style_block:
            self._css.append(data)
        elif self._in_paragraph:
            self._text_parts.append(data)
            flags = self._flag_stack[-1]
            if self._runs and all(self._runs[-1][k] == v for k, v in flags.items()):
                self._runs[-1]["text"] += data
            else:
                self._runs.append({"text": data, **flags})


_HELP_CACHE_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", tempfile.gettempdir()), "igor-pro-bridge", "help-cache"
)


def _help_cache_path(file_path: str, char_styles: bool):
    """Cache file for a help file, keyed by the file's size and modification time, this
    bridge's version and Igor Pro's version and build. None if Igor Pro does not answer."""
    try:
        igor_info = call_function("ZBR#ZBR_IgorInfo", [0])
    except (IgorZmqError, IgorZmqUnreachable):
        return None
    igor_version = ";".join(
        part for part in (igor_info or "").split(";") if part.split(":")[0] in ("IGORVERS", "BUILD")
    )
    stat = os.stat(file_path)
    key = json.dumps(
        [file_path.lower(), stat.st_size, stat.st_mtime_ns, _BRIDGE_VERSION, igor_version, char_styles]
    )
    return os.path.join(_HELP_CACHE_DIR, hashlib.sha256(key.encode()).hexdigest() + ".json")


@mcp.tool()
def read_help_file(file_path: str, char_styles: bool = False, timeout_ms: int = 30000) -> dict:
    """Read an Igor Pro help file (.ihf) as structured, formatted text -- e.g. to look up an
    operation's exact flags/behavior straight from Igor's own docs -- without leaving any
    lasting change to Igor's help-window state.

    file_path must be a full path to an existing .ihf file (e.g. one found via
    get_environment_summary's "loaded_xops" field plus the global/user Help Files folders
    under Igor's own installation for XOP-supplied help files).

    Results are cached per machine, keyed by the file's size and modification time, this
    bridge's version and Igor Pro's version, so reading the same file again does not need
    Igor Pro to export it again.

    Returns a dict with:
      - "paragraphs": [{"style": "Topic", "text": "Debugging"}, ...] -- "style" is the
        ruler name, i.e. WaveMetrics' own paragraph-class convention (e.g. "Topic" for a
        heading, "Subtopic" for a function entry, "Code1" for example code), "" if unset.
        With `char_styles`, each paragraph also has "runs": its text split into runs with
        their effective "bold"/"italic"/"underline" flags, e.g. to check the bold and
        underlined name of a subtopic, see edit_help_file.
      - "restore_failures": the file's own help window, if it was open before and could
        not be reopened. Only that window is closed while reading, all other help windows
        stay open.

    timeout_ms defaults to 30 seconds: exporting a large help file (e.g. "Igor
    Reference.ihf") can take longer than the usual 5 seconds.

    Raises if file_path does not exist, or if the underlying OpenNotebook/SaveNotebook
    sequence fails Igor-side -- help-window restoration is still attempted even then.
    """
    normalized = os.path.abspath(file_path)
    if not os.path.isfile(normalized):
        raise RuntimeError(f"'{normalized}' does not exist or is not a file.")

    cache_path = _help_cache_path(normalized, char_styles)
    if cache_path and os.path.isfile(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass

    tmp_fd, tmp_html_path = tempfile.mkstemp(suffix=".html", prefix="igor_help_")
    os.close(tmp_fd)
    os.remove(tmp_html_path)  # SaveNotebook must create it fresh; only the name is reused

    try:
        status = call_function(
            "ZBR#ZBR_ReadHelpFile", [normalized, tmp_html_path], timeout_ms=timeout_ms
        )
        parts = status.split("|")
        outcome = parts[0] if parts else ""
        restore_failures = [f for f in (parts[-1] if parts else "").split(";") if f]

        if outcome != "OK":
            message = parts[1] if len(parts) > 1 else "(no message)"
            raise RuntimeError(
                f"Could not read help file {normalized!r}: {message} "
                f"(restore_failures={restore_failures!r})"
            )

        if not os.path.isfile(tmp_html_path):
            raise RuntimeError(
                f"ZBR_ReadHelpFile reported success but {tmp_html_path!r} was not created."
            )

        with open(tmp_html_path, "r", encoding="utf-8") as f:
            html_text = f.read()
        parser = _NotebookHTMLParser(char_styles=char_styles)
        parser.feed(html_text)
    finally:
        try:
            if os.path.isfile(tmp_html_path):
                os.remove(tmp_html_path)
        except Exception:
            pass

    result = {"paragraphs": parser.paragraphs, "restore_failures": restore_failures}
    if cache_path:
        try:
            os.makedirs(_HELP_CACHE_DIR, exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump({"paragraphs": parser.paragraphs, "restore_failures": []}, f)
        except OSError:
            pass
    return result


def _igor_string_literal(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


@mcp.tool()
def edit_help_file(
    file_path: str,
    notebook_code: str,
    verify_topics: Optional[list] = None,
    force: bool = False,
) -> dict:
    """Edit an Igor Pro help file (.ihf) with Notebook operations, and verify its topics.

    Igor Pro help files are formatted notebooks: closed as help window, opened as notebook,
    edited, saved and reopened as help file, which compiles it. This tool does exactly
    that, with `notebook_code` as the editing step. `notebook_code` is Igor function code
    which refers to the opened notebook by the local string `nb`, e.g.

        Notebook $nb, selection={endOfFile, endOfFile}
        Notebook $nb, ruler=Subtopic, text="MyFunc(arg)\\r"

    Afterward, each name of `verify_topics` is checked with `DisplayHelpTopic/Z`. If the code
    fails, the file is not saved. The code runs via run_igor_code (see there for `force`).

    **Formatting rules of help files** (Igor manual, "Syntax of a Help File"; copy the
    formatting of existing entries of the same file, check it with read_help_file and
    `char_styles`):
      - Topic: ruler `Topic`, the paragraph starts with a plain bullet and tab.
      - Subtopic: a ruler whose name starts with `Subtopic`. The subtopic name is the
        **bold and underlined** text, usually the ruler's default style. Text inserted via
        `Notebook text=` gets the plain style of the insertion point, so select the name
        afterward and set `fStyle=-1` (the ruler's style), and `fStyle=0` for the argument
        list -- otherwise DisplayHelpTopic does not find the subtopic (error 5018).
      - Function entries in WaveMetrics' style: `Subtopic` for the signature,
        `FunctionBody1` for the description with an empty `FunctionBody1` paragraph between
        entries, `OperationSection` for headings like "Parameters" (`fStyle=-1`),
        `OperationKeywords1a` for parameters ("name<tab>text"), parameter names italic
        (`fStyle=2`).
      - Select text with `Notebook $nb, findText={"text", 2 + 4}` (case-sensitive, whole
        words, searching from the end of the current selection; V_flag is 1 if found).
      - A file may end without a final paragraph break: the first "\\r" appended at the
        end then only terminates the last paragraph.
      - A help window of the same name from another folder can hide the edited file in
        DisplayHelpTopic; close it first.

    Returns run_igor_code's result plus "saved" and "topics": {name: True if found}.
    """
    normalized = os.path.abspath(file_path)
    if not os.path.isfile(normalized):
        raise RuntimeError(f"'{normalized}' does not exist or is not a file.")

    topics = list(verify_topics or [])
    verify_lines = "".join(
        f"\tDisplayHelpTopic/Z {_igor_string_literal(topic)}\n"
        f"\tprintf \"ZBR_TOPIC:%d:%s\\r\", V_flag, {_igor_string_literal(topic)}\n"
        for topic in topics
    )
    code = (
        '\tstring nb = "ZBR_HelpEdit"\n'
        f"\tstring path = {_igor_string_literal(normalized)}\n"
        "\tvariable err\n"
        "\n"
        "\tCloseHelp/FILE=path; err = GetRTError(1)\n"
        "\tOpenNotebook/N=$nb/V=0 path\n"
        "\ttry\n"
        f"{notebook_code}\n"
        "\t\tAbortOnRTE\n"
        "\t\tSaveNotebook $nb; AbortOnRTE\n"
        "\tcatch\n"
        "\t\terr = GetRTError(1)\n"
        '\t\tprint "ZBR_EDIT_ERROR:" + GetErrMessage(err)\n'
        "\t\tKillWindow/Z $nb\n"
        "\t\tOpenHelp/V=0/INT=0/Z=1 path\n"
        "\t\treturn 0\n"
        "\tendtry\n"
        "\tKillWindow/Z $nb\n"
        "\tOpenHelp/V=0/INT=0/Z=1 path\n"
        '\tprintf "ZBR_OPENHELP:%d\\r", V_flag\n'
        f"{verify_lines}"
    )

    result = run_igor_code(code=code, force=force)
    output = (result.get("output") or "").replace("\r", "\n")
    result["saved"] = result.get("compiled", False) and "ZBR_EDIT_ERROR:" not in output
    result["topics"] = {}
    for line in output.split("\n"):
        if line.startswith("ZBR_TOPIC:"):
            _, flag, name = line.split(":", 2)
            result["topics"][name] = flag.strip() == "0"
        elif line.startswith("ZBR_EDIT_ERROR:"):
            result["error"] = line[len("ZBR_EDIT_ERROR:") :]
    return result


# --- Bridge identity / health --------------------------------------------------------

# Kept as a hardcoded constant (not read from manifest.json at runtime) for the same
# reason as before: the on-disk layout after Claude Desktop installs a .mcpb isn't
# guaranteed to keep server.py and manifest.json at a fixed relative path, and this
# needs to be confirmable from inside a conversation independent of that.
_BRIDGE_VERSION = "3.0.0"


def _installed_package_version(distribution_name: str) -> str | None:
    try:
        return importlib.metadata.version(distribution_name)
    except importlib.metadata.PackageNotFoundError:
        return None


@mcp.tool()
def get_bridge_version() -> dict:
    """Return the version of this Igor Pro Bridge build that is actually running right
    now, plus which Python interpreter and package versions it's running with.

    Call this whenever it matters to confirm which build is active -- e.g. before
    relying on a specific fix, or when reporting results from a test that depends on
    a particular fix being in effect. Installing a newer .mcpb requires restarting
    Claude Desktop; this is the only way to confirm afterward which version actually
    loaded.
    """
    return {
        "version": _BRIDGE_VERSION,
        "python_executable": sys.executable,
        "python_version": sys.version.split()[0],
        "mcp_package_version": _installed_package_version("mcp"),
        "pyzmq_version": _installed_package_version("pyzmq"),
    }


@mcp.tool()
def check_bridge_health() -> dict:
    """Check whether this bridge can actually reach Igor Pro's ZeroMQ server right
    now, and report what's known if not.

    Call this first whenever a command fails or behaves unexpectedly.

    Unlike the old COM-based version, this can no longer cleanly distinguish "Igor
    Pro isn't running" from "Igor Pro is running but ZMQ_BridgeHelpers.ipf isn't
    included/compiled/bound" from "wrong port/firewall" -- a ZeroMQ REQ socket that
    gets no reply at all looks the same in all three cases (see the module docstring's
    "Behavior changes" section). If this reports FAIL, check by hand: is Igor64.exe
    actually running (Task Manager)? Is Packages/MIES/ZMQ_BridgeHelpers.ipf
    #include-d and does check_compilation_state-equivalent info suggest it's
    compiled? Try `netstat -a -b` (needs admin) to see whether anything is actually
    listening on this bridge's currently configured port (see _igor_zmq_endpoint;
    reported in the "problem" message below on FAIL) -- if a custom port was set via
    configure_igor_launch(port=...), make sure the target Igor Pro instance was
    actually launched with that same port in effect.

    Returns a dict with a "status" key ("OK" or "FAIL") and, on FAIL, a "problem" key.
    """
    try:
        info = call_function("ZBR#ZBR_Ping")
    except IgorZmqUnreachable as e:
        pids = _list_igor_pids()
        dialogs = _possible_dialogs() if pids else []
        if not pids:
            problem = "Igor Pro is not running."
        elif dialogs:
            problem = (
                "Igor Pro is running, but does not answer. It shows the dialogs listed in "
                "'possible_dialogs', which most likely block the bridge -- close them with "
                "close_igor_window (or ask the user to), then check again."
            )
        else:
            # the message of IgorZmqUnreachable holds the diagnosis, see _diagnose_no_reply
            problem = str(e)
        return {
            "status": "FAIL",
            "problem": problem,
            "igor_pids": pids,
            "possible_dialogs": dialogs,
        }
    except IgorZmqError as e:
        return {
            "status": "FAIL",
            "problem": f"Igor Pro's ZeroMQ server replied but reported an error: {e}",
        }

    result = {"status": "OK", "igor_info": info}
    dialogs = _possible_dialogs()
    if dialogs:
        result["possible_dialogs"] = dialogs
        result["warning"] = (
            "Igor Pro answers, but shows the dialogs listed in 'possible_dialogs'. A modal "
            "dialog blocks Igor's operation queue, so queued commands (execute_igor_command, "
            "run_igor_code, ...) do not run until it is closed, see close_igor_window."
        )
    return result


# --- Igor windows and dialogs ----------------------------------------------------------
#
# Pure OS-level window handling (win32gui/win32api), independent of the ZeroMQ transport.
# A modal dialog in Igor Pro (e.g. an error of a bare Execute, "Igor Pro wants to know...",
# a compile error without /UNATTENDED) blocks the operation queue and with it the bridge,
# although the process itself still responds. These helpers make such dialogs visible and
# closable without OS focus changes. Window titles are not matched against a fixed list
# (except for the automatic compile-error dismissal), as they depend on Igor's language.

_IGOR_PROCESS_NAME_PREFIX = "igor"
_KNOWN_STUCK_DIALOG_TITLES = ("Function Compilation Error",)
_POSTED_KEY_GAP_SECONDS = 0.05
_POSTED_KEY_SETTLE_SECONDS = 0.2
_CLOSE_STEP_WAIT_SECONDS = 1.0
# "Igor Pro 9.06B01 64-bit" for an untitled experiment, "<experiment> - Igor Pro 9.06B01 64-bit"
# otherwise; dialogs like "Igor Pro wants to know..." have no version number
_MAIN_WINDOW_TITLE_REGEXP = re.compile(r"(^|\s-\s)Igor Pro \d+\.\d+")

_KEY_CODES = {
    "escape": win32con.VK_ESCAPE,
    "enter": win32con.VK_RETURN,
    "return": win32con.VK_RETURN,
    "tab": win32con.VK_TAB,
    "space": win32con.VK_SPACE,
    "left": win32con.VK_LEFT,
    "right": win32con.VK_RIGHT,
    "up": win32con.VK_UP,
    "down": win32con.VK_DOWN,
}


def _get_process_exe_name(pid: int):
    ACCESS = win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ
    hProcess = None
    try:
        hProcess = win32api.OpenProcess(ACCESS, False, pid)
        path = win32process.GetModuleFileNameEx(hProcess, 0)
        return os.path.basename(path)
    except Exception:
        return None
    finally:
        if hProcess is not None:
            win32api.CloseHandle(hProcess)


def _get_process_exe_path(pid: int):
    ACCESS = win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ
    hProcess = None
    try:
        hProcess = win32api.OpenProcess(ACCESS, False, pid)
        return win32process.GetModuleFileNameEx(hProcess, 0)
    except Exception:
        return None
    finally:
        if hProcess is not None:
            win32api.CloseHandle(hProcess)


def _list_pids_for_exe_basename(basename_lower: str) -> list:
    """Return the PIDs of every running process whose module file name matches
    basename_lower exactly (case-insensitively), e.g. "igor64.exe".

    Used by load_experiment to wait for an Igor Pro process to fully exit -- **not** the
    same thing as it going quiet over ZeroMQ: ZeroMQ stops answering well before the
    process terminates, and launching `Igor64.exe /UNATTENDED <path>` while the old process
    is still alive either redirects into the old instance or is silently dropped."""
    matches = []
    for pid in win32process.EnumProcesses():
        if pid == 0:
            continue
        exe_name = _get_process_exe_name(pid)
        if exe_name and exe_name.lower() == basename_lower:
            matches.append(pid)
    return matches


def _list_igor_pids() -> list:
    pids = []
    for pid in win32process.EnumProcesses():
        if pid == 0:
            continue
        exe_name = _get_process_exe_name(pid)
        if exe_name and exe_name.lower().startswith(_IGOR_PROCESS_NAME_PREFIX):
            pids.append(pid)
    return pids


def _igor_windows(include_hidden: bool = False) -> list:
    windows = []

    def _callback(hwnd, _extra):
        visible = bool(win32gui.IsWindowVisible(hwnd))
        if not visible and not include_hidden:
            return True
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        exe_name = _get_process_exe_name(pid)
        if not exe_name or not exe_name.lower().startswith(_IGOR_PROCESS_NAME_PREFIX):
            return True
        title = win32gui.GetWindowText(hwnd)
        owner = win32gui.GetWindow(hwnd, win32con.GW_OWNER)
        windows.append(
            {
                "hwnd": int(hwnd),
                "title": title,
                "class_name": win32gui.GetClassName(hwnd),
                "visible": visible,
                "owner_hwnd": int(owner) if owner else None,
                "is_main_window": bool(_MAIN_WINDOW_TITLE_REGEXP.search(title)),
                "process": exe_name,
                "pid": pid,
            }
        )
        return True

    win32gui.EnumWindows(_callback, None)
    for window in windows:
        if window["visible"] and not window["is_main_window"]:
            window.update(_dialog_contents(window["hwnd"]))
    return windows


def _possible_dialogs() -> list:
    """Visible top-level Igor windows other than the main window, typically modal dialogs
    when the bridge does not answer."""
    return [w for w in _igor_windows() if not w["is_main_window"] and w["title"]]


def _describe_dialogs(dialogs: list) -> str:
    return ", ".join(
        f"{d['title']!r} ({' '.join(d['texts'])})" if d.get("texts") else repr(d["title"])
        for d in dialogs
    )


def _require_igor_window(hwnd: int):
    if not win32gui.IsWindow(hwnd):
        raise RuntimeError(f"Window {hwnd} does not exist (anymore).")
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    exe_name = _get_process_exe_name(pid)
    if not exe_name or not exe_name.lower().startswith(_IGOR_PROCESS_NAME_PREFIX):
        raise RuntimeError(f"Window {hwnd} does not belong to an Igor Pro process.")


def _post_key(hwnd: int, virtual_key: int):
    win32api.PostMessage(hwnd, win32con.WM_KEYDOWN, virtual_key, 0)
    time.sleep(_POSTED_KEY_GAP_SECONDS)
    win32api.PostMessage(hwnd, win32con.WM_KEYUP, virtual_key, 0)
    time.sleep(_POSTED_KEY_SETTLE_SECONDS)


def _window_gone(hwnd: int) -> bool:
    return not win32gui.IsWindow(hwnd) or not win32gui.IsWindowVisible(hwnd)


def _wait_until_gone(hwnd: int) -> bool:
    deadline = time.monotonic() + _CLOSE_STEP_WAIT_SECONDS
    while time.monotonic() < deadline:
        if _window_gone(hwnd):
            return True
        time.sleep(0.1)
    return False


_uia_client = None
_uia_module = None


def _uia():
    """The UI Automation client and the module with its interfaces and constants. Igor's
    dialogs are Qt windows without native child controls, only UI Automation sees their
    texts and buttons."""
    global _uia_client, _uia_module
    if _uia_client is None:
        _uia_module = comtypes.client.GetModule("UIAutomationCore.dll")
        _uia_client = comtypes.client.CreateObject(
            _uia_module.CUIAutomation, interface=_uia_module.IUIAutomation
        )
    return _uia_client, _uia_module


def _dialog_elements(hwnd: int) -> list:
    client, uia = _uia()
    found = client.ElementFromHandle(hwnd).FindAll(
        uia.TreeScope_Descendants, client.CreateTrueCondition()
    )
    return [found.GetElement(i) for i in range(found.Length)]


def _dialog_contents(hwnd: int) -> dict:
    texts = []
    buttons = []
    try:
        _, uia = _uia()
        for element in _dialog_elements(hwnd):
            name = element.CurrentName
            if not name:
                continue
            control_type = element.CurrentControlType
            if control_type == uia.UIA_TextControlTypeId:
                texts.append(name)
            elif control_type == uia.UIA_ButtonControlTypeId:
                buttons.append(
                    {
                        "name": name,
                        "access_key": element.CurrentAccessKey or "",
                        "enabled": bool(element.CurrentIsEnabled),
                    }
                )
    except Exception as e:
        return {"texts": texts, "buttons": buttons, "contents_error": str(e)}
    return {"texts": texts, "buttons": buttons}


def _button_caption(name: str) -> str:
    return name.replace("&", "").strip().lower()


@mcp.tool()
def list_igor_windows(include_hidden: bool = False) -> dict:
    """List the top-level windows of the running Igor Pro process(es), e.g. to find a modal
    dialog which blocks the bridge.

    Each entry has "hwnd" (for click_igor_dialog_button/close_igor_window/
    send_key_to_igor_window), "title", "class_name", "visible", "owner_hwnd" (dialogs are
    usually owned by the main window), "is_main_window", "process" and "pid". Visible
    windows other than the main window also have "texts" (e.g. the message of a dialog) and
    "buttons" (each with "name", "access_key" and "enabled"), read via UI Automation, so the
    title and texts tell which dialog it is and how to answer it.

    Use this when the bridge does not answer although Igor Pro is running: a modal dialog
    blocks Igor's operation queue and with it the bridge, see check_bridge_health."""
    return {"windows": _igor_windows(include_hidden)}


@mcp.tool()
def close_igor_window(hwnd: int, press_enter: bool = False) -> dict:
    """Close a window of Igor Pro, typically a modal dialog blocking the bridge, without
    changing the OS focus.

    Tries in order: WM_CLOSE and the Escape key, and checks after each step whether the
    window is gone. Some dialogs only react to keys, e.g. the "Igor Pro wants to know..."
    confirmation after an error ignores WM_CLOSE but closes on Escape. Closing one dialog
    can open the next one, so call list_igor_windows/check_bridge_health again afterward.

    Dialogs without a Cancel button, e.g. a Yes/No question of DoAlert, ignore both; answer
    them with click_igor_dialog_button. With `press_enter`, the Enter key is tried last,
    which **selects the dialog's default button** -- e.g. "Yes", or "Save" in a "save
    changes?" dialog. Only pass it when that choice is fine, ask the user otherwise.

    Returns {"closed": bool, "method": the step which closed it, "title": its title,
    "texts": the texts of the window before closing it}."""
    _require_igor_window(hwnd)
    title = win32gui.GetWindowText(hwnd)
    texts = _dialog_contents(hwnd)["texts"]

    steps = (
        ("WM_CLOSE", lambda: win32api.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)),
        ("escape", lambda: _post_key(hwnd, win32con.VK_ESCAPE)),
    )
    if press_enter:
        steps += (("enter", lambda: _post_key(hwnd, win32con.VK_RETURN)),)
    for name, action in steps:
        try:
            action()
        except Exception:
            pass
        if _wait_until_gone(hwnd):
            return {"closed": True, "method": name, "title": title, "texts": texts}

    result = {"closed": False, "method": None, "title": title, "texts": texts}
    if not press_enter:
        result["note"] = (
            "Neither WM_CLOSE nor Escape closed it, the dialog probably has no Cancel "
            "button. Answer it with click_igor_dialog_button."
        )
    return result


@mcp.tool()
def click_igor_dialog_button(hwnd: int, button: str) -> dict:
    """Click a button of an Igor Pro dialog by its caption (case-insensitive), without
    changing the OS focus, e.g. "No" in the "Do you want to save changes to experiment
    ...?" dialog, which neither close_igor_window nor a key press can select.

    Decide on the button from the dialog's "title", "texts" and "buttons" of
    list_igor_windows. Only click a button whose consequence is clear, ask the user
    otherwise. Answering one dialog can open the next one.

    Returns {"clicked": True, "button": its caption, "title", "texts": the texts of the
    dialog, "closed": bool}. Raises if the dialog has no enabled button with that caption."""
    _require_igor_window(hwnd)
    title = win32gui.GetWindowText(hwnd)
    contents = _dialog_contents(hwnd)
    _, uia = _uia()
    wanted = _button_caption(button)
    for element in _dialog_elements(hwnd):
        if (
            element.CurrentControlType == uia.UIA_ButtonControlTypeId
            and _button_caption(element.CurrentName or "") == wanted
            and element.CurrentIsEnabled
        ):
            break
    else:
        raise RuntimeError(
            f"Dialog {title!r} has no enabled button {button!r}, its buttons: "
            f"{contents['buttons']}."
        )

    caption = element.CurrentName
    pattern = element.GetCurrentPattern(uia.UIA_InvokePatternId)
    pattern.QueryInterface(uia.IUIAutomationInvokePattern).Invoke()
    return {
        "clicked": True,
        "button": caption,
        "title": title,
        "texts": contents["texts"],
        "closed": _wait_until_gone(hwnd),
    }


@mcp.tool()
def send_key_to_igor_window(hwnd: int, key: str) -> dict:
    """Send a key press to a window of Igor Pro without changing the OS focus, e.g. to pick
    a button of a dialog by its keyboard shortcut.

    `key` is one of "escape", "enter"/"return", "tab", "space", "left", "right", "up",
    "down", or a single character (e.g. "y")."""
    _require_igor_window(hwnd)
    name = key.lower()
    if name in _KEY_CODES:
        virtual_key = _KEY_CODES[name]
    elif len(key) == 1:
        virtual_key = win32api.VkKeyScan(key) & 0xFF
    else:
        raise RuntimeError(f"Unknown key {key!r}.")

    _post_key(hwnd, virtual_key)
    return {"sent": True, "key": key, "window_still_open": not _window_gone(hwnd)}


def _attempt_dismiss_compile_error_dialog() -> dict:
    """Close a "Function Compilation Error" dialog (only shown if Igor Pro was not launched
    with /UNATTENDED), see reload_and_compile_procedures."""
    for window in _possible_dialogs():
        if any(t.lower() in window["title"].lower() for t in _KNOWN_STUCK_DIALOG_TITLES):
            result = close_igor_window(window["hwnd"])
            return {"attempted": True, **result}
    return {"attempted": False, "igor_windows_seen": _possible_dialogs()}


# --- Launching / relaunching Igor Pro -------------------------------------------------
#
# Still pure Python/OS-level (starting a whole new Igor Pro *process* has to be done
# from outside any already-running instance -- no transport can do this from the
# inside). **No more elevation branching at all** -- see the module docstring: ZeroMQ
# has no privilege-matching requirement, so Igor Pro is always launched as a plain
# child process of this bridge process, at whatever privilege level that already is.

# Matches ZBR_ZEROMQ_ENV_PORT in Packages/MIES/ZMQ_BridgeHelpers.ipf: when set, a
# launched Igor Pro instance's ZBR_EnsureZeroMQBound binds its ZeroMQ socket to this
# port instead of its own default (5680). Preparation for talking to more than one
# Igor Pro instance -- see configure_igor_launch. _configured_igor_port itself lives
# up in the "ZeroMQ transport" section since _igor_zmq_endpoint() also reads it.
_IGOR_PRO_BRIDGE_PORT_ENV_VAR = "IGOR_PRO_BRIDGE_PORT"

_configured_igor_exe_path = None

_CONFIG_FILE = os.path.join(
    os.environ.get("APPDATA", os.path.expanduser("~")), "igor-pro-bridge", "config.json"
)


def _save_launch_config():
    os.makedirs(os.path.dirname(_CONFIG_FILE), exist_ok=True)
    with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump({"exe_path": _configured_igor_exe_path, "port": _configured_igor_port}, f)


def _load_launch_config():
    """Restore the settings of configure_igor_launch from a previous bridge process."""
    global _configured_igor_exe_path, _configured_igor_port
    try:
        with open(_CONFIG_FILE, encoding="utf-8") as f:
            config = json.load(f)
    except (OSError, ValueError):
        return
    exe_path = config.get("exe_path")
    if exe_path and os.path.isfile(exe_path):
        _configured_igor_exe_path = exe_path
    port = config.get("port")
    if isinstance(port, int) and 1 <= port <= 65535:
        _configured_igor_port = port
        os.environ[_IGOR_PRO_BRIDGE_PORT_ENV_VAR] = str(port)


def _resolve_igor_exe_path():
    """The configured executable, or else the one of a running Igor Pro process."""
    global _configured_igor_exe_path
    if _configured_igor_exe_path:
        return _configured_igor_exe_path
    for pid in _list_igor_pids():
        path = _get_process_exe_path(pid)
        if path and os.path.isfile(path):
            _configured_igor_exe_path = path
            _save_launch_config()
            return path
    return None


_load_launch_config()


def _build_igor_launch_env():
    """Return an environment dict for subprocess.Popen when launching Igor Pro,
    patching in COMSPEC if this process's own environment is missing it.

    Confirmed necessary during this bridge's development: MIES's own startup hook
    (IgorStartOrNewHook -> ... -> ExecuteGitForMIESVersion, MIES_GlobalStringAndVariable
    Access.ipf) shells out to git via ExecuteScriptText using GetCmdPath()/COMSPEC to
    find cmd.exe. A child process launched via subprocess.Popen with no explicit env
    inherits this process's own environment -- which may not have COMSPEC set (a
    normal interactive login session always does; this bridge process's own
    environment, inherited from whatever launched Claude Desktop, might not). Without
    it, MIES's git-shell-out becomes malformed and
    `ASSERT(!V_flag, "We have git installed but could not regenerate version.txt")`
    trips on every launch via this path.

    Also carries through _IGOR_PRO_BRIDGE_PORT_ENV_VAR if configure_igor_launch set
    (or cleared) it on this process's own os.environ -- no extra handling needed here
    since this starts from a plain copy of that.
    """
    env = os.environ.copy()
    if not env.get("COMSPEC"):
        system_root = env.get("SystemRoot", r"C:\Windows")
        env["COMSPEC"] = os.path.join(system_root, "System32", "cmd.exe")
    return env


@mcp.tool()
def configure_igor_launch(exe_path: str, port: Optional[int] = None) -> dict:
    """Record the full path to the Igor Pro executable (e.g. "...\\IgorBinaries_x64\\
    Igor64.exe") to use for launch_igor_pro_unattended/load_experiment, for the rest
    and save it, together with `port`, in %APPDATA%\\igor-pro-bridge\\config.json, so it
    survives restarts of this bridge.

    Without a configured path, launch_igor_pro_unattended/load_experiment use the
    executable of a running Igor Pro process. If neither exists, ask the user for the path
    -- do not guess a typical installation path, the folder names differ between
    installations.

    port, if given, is a custom ZeroMQ port for the NEXT launched instance to bind
    to, instead of its own default (5680) -- preparation for eventually talking to
    more than one Igor Pro instance at once. Setting this writes
    IGOR_PRO_BRIDGE_PORT=str(port) into this bridge process's own environment;
    launch_igor_pro_unattended/load_experiment inherit that when they start Igor Pro
    (see _build_igor_launch_env), and the launched instance's own
    ZBR_EnsureZeroMQBound (ZMQ_BridgeHelpers.ipf) reads it back to decide which port
    to bind. **Omitting port (or passing None) clears any previously-configured
    custom port** -- it removes IGOR_PRO_BRIDGE_PORT from this process's environment
    entirely, so the next launch uses Igor's own default port again. This is not
    "leave unchanged": repeat the same port value on every call while a custom port
    should stay in effect.

    Also updates which port THIS bridge itself connects to for every subsequent tool
    call (see _igor_zmq_endpoint) -- every ZMQ-talking function goes through that one
    helper, so setting/clearing port here immediately retargets all of them, not just
    the next launch. This bridge still only tracks ONE currently-configured
    endpoint at a time, though -- talking to two Igor Pro instances simultaneously
    (rather than switching which single one this points at) isn't supported yet.

    Raises if exe_path does not point to an existing file, or if port is given but
    is not a valid TCP port number (1-65535). Does not otherwise validate that
    exe_path is actually Igor Pro (beyond a soft filename check).
    """
    global _configured_igor_exe_path, _configured_igor_port

    normalized = os.path.abspath(exe_path)
    if not os.path.isfile(normalized):
        raise RuntimeError(
            f"'{normalized}' does not exist or is not a file. Ask the user for the "
            "exact full path to the Igor Pro executable (typically something like "
            r'"C:\Program Files\WaveMetrics\Igor Pro 9 Folder\IgorBinaries_x64\Igor64.exe"'
            " -- the exact folder name varies by Igor Pro version) and try again."
        )

    if port is not None and (
        not isinstance(port, int) or isinstance(port, bool) or not (1 <= port <= 65535)
    ):
        raise RuntimeError(
            f"'{port}' is not a valid TCP port -- expected an integer between 1 and "
            "65535, or omit/None to clear a previously-configured custom port."
        )

    note = None
    if "igor" not in os.path.basename(normalized).lower():
        note = (
            "This file name does not look like a typical Igor Pro executable "
            "(expected something like 'Igor64.exe'). Proceeding anyway in case the "
            "user has a renamed executable."
        )

    if port is not None:
        os.environ[_IGOR_PRO_BRIDGE_PORT_ENV_VAR] = str(port)
    else:
        os.environ.pop(_IGOR_PRO_BRIDGE_PORT_ENV_VAR, None)

    _configured_igor_exe_path = normalized
    _configured_igor_port = port
    _save_launch_config()
    return {"configured_exe_path": normalized, "configured_port": port, "note": note}


_POST_LAUNCH_POLL_INTERVAL_SECONDS = 1.0


@mcp.tool()
def launch_igor_pro_unattended(wait_for_ready_seconds: float = 30.0) -> dict:
    """Launch the configured Igor Pro executable with the /UNATTENDED command-line
    flag.

    Per Igor Pro Folder/Igor Help Files/Advanced Topics.ihf ("Calling Igor from
    Scripts"), /UNATTENDED "suppresses certain interactions that are inconvenient for
    unattended operations" -- documented examples are the About Autosave dialog and
    (Igor Pro 10+) the license activation dialog. Also confirmed empirically (not
    documented anywhere in Igor's help files): /UNATTENDED also suppresses the modal
    "Function Compilation Error" dialog on a bad procedure compile, reporting the
    error as a plain history line instead (format "<file>:<line>:<col>: error:
    <message>", readable via read_session_history).

    **Requires configure_igor_launch(exe_path) to have been called first.**

    Refuses to launch (returns "launched": False) if something already answers
    ZBR#ZBR_Ping right now -- launching the executable again with only /UNATTENDED
    starts a genuinely new instance rather than reusing an existing one (Advanced
    Topics.ihf), which would leave two Igor64.exe processes running.

    **Always launches as a plain child process (subprocess.Popen), at whatever
    privilege level this bridge process itself is running at -- no elevation request,
    no UAC prompt, ever.** This is a deliberate change from the COM-based version:
    ZeroMQ has no privilege-matching requirement at all (unlike COM, which needed this
    process and Igor Pro to run at the SAME privilege level), so there is nothing to
    branch on anymore.

    Patches COMSPEC into the child's environment if missing (see
    _build_igor_launch_env) -- needed for MIES's own git-based startup hook.

    After launching, polls for ZBR#ZBR_Ping to start answering (every ~1s) up to
    wait_for_ready_seconds. Returns whether it became ready and how many polling
    attempts that took.
    """
    if not _resolve_igor_exe_path():
        raise RuntimeError(
            "No Igor Pro executable path configured yet. Ask the user for the full "
            "path to their Igor Pro executable (e.g. "
            r'"C:\Program Files\WaveMetrics\Igor Pro 9 Folder\IgorBinaries_x64\Igor64.exe")'
            ", then call configure_igor_launch(exe_path) with it before calling "
            "this tool."
        )

    if _reachable(timeout_ms=1000):
        return {
            "launched": False,
            "reason": (
                "Something already answered ZBR#ZBR_Ping over ZeroMQ. Refusing to "
                "launch a second Igor Pro instance -- close the existing one first "
                "if a genuinely fresh one is actually wanted."
            ),
        }

    try:
        subprocess.Popen(
            [_configured_igor_exe_path, "/UNATTENDED"],
            env=_build_igor_launch_env(),
        )
    except Exception as e:
        return {"launched": False, "reason": f"Failed to start the process ({e})."}

    deadline = time.monotonic() + wait_for_ready_seconds
    attempts = 0
    while time.monotonic() < deadline:
        attempts += 1
        if _reachable(timeout_ms=1000):
            return {"launched": True, "zmq_ready": True, "poll_attempts": attempts}
        time.sleep(_POST_LAUNCH_POLL_INTERVAL_SECONDS)

    return {
        "launched": True,
        "zmq_ready": False,
        "poll_attempts": attempts,
        "note": (
            f"The process was started, but nothing answered ZBR#ZBR_Ping within "
            f"{wait_for_ready_seconds:.0f}s. Igor Pro may still be initializing "
            "(slower on first launch or a cold machine), or ZMQ_BridgeHelpers.ipf may "
            "not be #include-d/compiled in whatever experiment this instance opened "
            "by default -- try check_bridge_health() again after waiting longer."
        ),
    }


@mcp.tool()
def load_experiment(
    file_path: str,
    wait_for_ready_seconds: float = 30.0,
    process_exit_timeout_seconds: float = 30.0,
) -> dict:
    """Load an Igor Pro experiment file (.pxp), replacing whatever is currently open.

    **Behavior change from the COM-based version, read carefully**: COM's
    IApplication.LoadExperiment hot-swapped the experiment inside the SAME running
    Igor Pro process. That method is COM-only (confirmed: neither "LoadExperiment"
    nor "OpenFile" appear anywhere in Igor Reference.ihf, only in Automation
    Server.ihf) -- there is no procedure-language equivalent, so this transport
    cannot replicate it. Instead, this tool:

    1. Asks the currently-running instance to quit (`Quit/N`, submitted via
       ZBR_SubmitCommand -- deferred, so this step itself still gets a normal reply
       before Igor actually exits).
    2. Waits for the underlying Igor64.exe **OS process** to fully disappear from the
       process list -- see below for why this can't just check ZeroMQ reachability.
    3. Relaunches the configured Igor Pro executable (see configure_igor_launch) with
       `/UNATTENDED` plus the target file path as a launch argument -- passing a file
       path on launch is documented (Advanced Topics.ihf, "Calling Igor from
       Scripts") to open that specific file.
    4. Polls for the new instance to start answering ZBR#ZBR_Ping, same as
       launch_igor_pro_unattended.

    **Step 2 is not optional, and checking ZeroMQ reachability alone is not enough --
    confirmed live (user report) after an early version of this tool relied on exactly
    that and silently failed.** ZeroMQ goes quiet well before the Igor64.exe process
    actually terminates (Igor can take several seconds to fully exit after `Quit/N`
    runs). If the replacement `Igor64.exe /UNATTENDED <path>` command line is launched
    while the old process is still alive -- even mid-shutdown -- Windows/Igor's
    single-instance-per-user behavior does not spawn a new process at all: it either
    (a) redirects the launch into the still-live old instance, which pops an unhandled
    "save changes?" dialog if it happens to have unsaved edits, or (b) if the old
    instance is already mid-quit, silently drops the request altogether. Case (b) is
    especially deceptive: nothing errors, no new process appears, and the net effect
    looks exactly like the relaunch had no effect whatsoever. This tool instead polls
    the actual OS process list (matching the configured executable's own file name,
    e.g. "Igor64.exe") until no such process remains, up to
    process_exit_timeout_seconds, before ever invoking the relaunch command line. If
    that timeout elapses with the process still present, this raises rather than
    proceeding -- proceeding anyway would just reproduce the same silent-failure risk
    this check exists to prevent. A stuck "save changes?" dialog on the OLD instance
    (e.g. if something modified the experiment after your last save) is the most
    likely cause; check for one by hand if this happens.

    This is a genuine process restart, not an in-place swap. **Any unsaved changes in
    the currently-open experiment are lost** -- call
    execute_igor_command('SaveExperiment') first if that matters (matching the old
    version's own behavior: LoadExperiment never auto-saved either, but there was at
    least still a "hot" instance to save from afterward if you forgot; now there
    isn't).

    Uses the executable of configure_igor_launch, or else the one of the running Igor
    Pro process.

    **Quit hooks of the open experiment still run** (`Quit/N` only skips Igor's own "save
    changes?" question), and a hook may save the experiment itself, e.g. one which silently
    saves an unmodified experiment on quit. Keep that in mind for experiment files under
    version control.

    Raises if file_path does not point to an existing file, or if the old Igor Pro
    process does not fully exit within process_exit_timeout_seconds.
    """
    if not _resolve_igor_exe_path():
        raise RuntimeError(
            "No Igor Pro executable path configured yet and no Igor Pro process is "
            "running -- call configure_igor_launch(exe_path) first."
        )

    normalized = os.path.abspath(file_path)
    if not os.path.isfile(normalized):
        raise RuntimeError(f"'{normalized}' does not exist or is not a file.")

    igor_basename_lower = os.path.basename(_configured_igor_exe_path).lower()

    try:
        call_function("ZBR#ZBR_SubmitCommand", ["Quit/N"])
    except (IgorZmqError, IgorZmqUnreachable):
        pass  # nothing was running/reachable to begin with -- nothing to quit

    quit_deadline = time.monotonic() + process_exit_timeout_seconds
    remaining_pids = _list_pids_for_exe_basename(igor_basename_lower)
    while remaining_pids and time.monotonic() < quit_deadline:
        time.sleep(0.5)
        remaining_pids = _list_pids_for_exe_basename(igor_basename_lower)

    if remaining_pids:
        raise RuntimeError(
            f"'{igor_basename_lower}' did not fully exit within "
            f"{process_exit_timeout_seconds:.0f}s of sending Quit/N (PIDs still "
            f"running: {remaining_pids}). Relaunching now would risk silently doing "
            "nothing (see this tool's own docstring) -- check whether the existing "
            "Igor Pro instance is stuck on an unhandled dialog (e.g. 'save changes?' "
            "if something modified the experiment since your last save) and resolve "
            "it by hand, or retry with a longer process_exit_timeout_seconds."
        )

    try:
        subprocess.Popen(
            [_configured_igor_exe_path, "/UNATTENDED", normalized],
            env=_build_igor_launch_env(),
        )
    except Exception as e:
        raise RuntimeError(
            f"Failed to relaunch Igor Pro with {normalized!r}: {e}"
        ) from e

    deadline = time.monotonic() + wait_for_ready_seconds
    attempts = 0
    while time.monotonic() < deadline:
        attempts += 1
        if _reachable(timeout_ms=1000):
            return {
                "loaded_file": normalized,
                "zmq_ready": True,
                "poll_attempts": attempts,
            }
        time.sleep(_POST_LAUNCH_POLL_INTERVAL_SECONDS)

    return {
        "loaded_file": normalized,
        "zmq_ready": False,
        "poll_attempts": attempts,
        "possible_dialogs": _possible_dialogs(),
        "note": (
            f"Process relaunched with {normalized!r}, but nothing answered "
            f"ZBR#ZBR_Ping within {wait_for_ready_seconds:.0f}s. If 'possible_dialogs' "
            "lists a dialog, it blocks the startup (see close_igor_window); otherwise try "
            "check_bridge_health() again after waiting longer."
        ),
    }


if __name__ == "__main__":
    mcp.run()
