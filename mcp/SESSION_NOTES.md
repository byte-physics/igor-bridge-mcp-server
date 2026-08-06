# Session Notes

## Purpose

Facts, corrections, and design rationale for the Igor Pro Bridge MCP server. Kept here so
both the user and Claude can recall them accurately in later sessions rather than
re-deriving or re-arguing them from scratch. Organized by topic, not chronology, and
scoped to facts relevant to the *current* (ZeroMQ-based, v2.3.2) bridge.

## Standing instructions

- **Verify findings carefully before reporting an error** -- read the documentation,
  trace the actual code, or find corroborating evidence, rather than asserting Igor Pro
  semantics from memory. Most of the facts below exist because that wasn't done
  carefully enough the first time.
- **This bridge must always work against a stock Igor Pro installation.** It must never
  depend on anything specific to any one procedure tree/repository. `procedure/
  ZMQ_BridgeHelpers.ipf` and `mcp/server.py` must stand on their own for a bare Igor Pro
  instance with nothing beyond the application itself installed -- any project-specific
  wording, tool descriptions, or error messages that assume a particular codebase is
  present are bugs. (Concrete precedent: an early bridge feature once hardcoded a check
  against one specific procedure file and put project-specific wording in its docstring
  -- fixed by making that a caller-supplied parameter instead of a baked-in assumption.
  Treat that as the template for any future capability that might be tempted to assume a
  particular procedure file/repo is present.)
- **`read_session_history()` should be saved straight to a file, not read inline**, for
  any nontrivial amount of output. Igor's history area can grow past 10,000 lines during
  normal use (e.g. test runs with heavy log output); inline reads reliably exceed the
  context window past a certain point. Write the result to a file first and grep it for
  the specific markers needed (`error:`, a specific line, etc.) rather than waiting to
  hit a token-limit error first.
- **If something inconsistent turns up mid-session** (a function/file that should exist
  doesn't, a line number or piece of code referenced earlier no longer matches what's on
  disk, an unexpected compile/test result) **check `git branch --show-current`/`git log
  -1` before assuming a code regression or a mistake on Claude's part** -- a branch
  switch the user forgot to mention changes what every file tool sees identically and
  immediately.

## `ipt`/`ipt.exe` (the Igor Programming Tool)

A separate static-analysis tool for Igor Pro procedure code (`ipt` on Linux, `ipt.exe`
on Windows; docs at docs.byte-physics.de/ipt). Reach for it proactively in any Igor Pro
related workflow where it can sharpen understanding, not only when explicitly asked for
a parse/AST -- use it as a standing habit alongside direct reading of the source, not a
replacement for it.

- **`ipt check [--print-ast] <files>`** -- confirms a file/edit actually parses, or
  returns an authoritative AST (node types like `Function`/`Declaration`/`Assignment`/
  `OperationStatement`, each with precise line:column spans) without needing a live
  Igor Pro instance. Confirmed reliable against real, non-trivial production code
  (near-100% clean parse rate across a large real codebase in practice; the only
  parse failures found were a deliberately-malformed test fixture file, not real bugs).
- **`ipt lint <files>`** -- catches known real bug/style patterns, e.g.
  `BugproneReservedKeywordsAsIdentifier` (flags a variable named after a reserved
  **keyword/type name**, like `variable wave`). **Confirmed gap**: it does NOT flag a
  variable/string named after a built-in **function** name (`variable abs`, `string
  print`, `string log` all pass with zero warnings, including with that rule explicitly
  included) -- the AST has no built-in-name-resolution semantics at all (confirmed from
  the printed tree: a shadowing name shows up as a plain `Id` node, no annotation
  distinguishing it from an ordinary local). This class of bug (a local named after a
  built-in *function*, silently shadowing it within its own scope) must be caught by
  review/manual attention instead.
- **`ipt rename --print-symbol-table <files>`** -- a genuine cross-referenced symbol
  table (declarations plus every read/write/definition point with exact spans, full
  function signatures including multi-return `[a, b] = Func()` syntax), useful whenever
  tracing how a variable/function is actually used matters more than just seeing the
  parse tree. `--print-symbol-table` lives under `ipt rename`, not `ipt check`. Each
  record carries an `id:` that is an ephemeral, process-memory-derived identifier --
  **not stable across runs**, only useful for cross-referencing within one invocation.
  **Confirmed bug**: running with no rename target (`-f`/`-l`/`-c`/`-n` all omitted)
  prints the symbol table correctly, then crashes (`Bug: target file was not parsed`,
  SIGABRT) instead of exiting cleanly -- the printed content before the crash is still
  complete and trustworthy; just don't rely on the exit code in that case, or supply a
  real target if a clean exit matters.
- **`ipt analyze`** -- available, not deeply evaluated; worth trying when a task's shape
  matches its evident purpose (broader rule-based analysis).
- **`ipt format`** -- confirmed in real use to consistently reformat an entire file
  (aligned `=` across runs of consecutive assignment/declaration lines, a blank line
  separating a function's local-variable declarations from its first statement), not
  just the lines just edited by hand -- worth reaching for after any manual
  restructuring, not only after small line-level edits. Run this immediately after
  editing any `.ipf` file, every time, so diffs stay canonically formatted.
- **`ipt` only ever knows about the file(s) explicitly passed via `files`/`-f`** -- it
  does not resolve `#include`s itself, so pass every file actually relevant to the
  question at hand. In practice this is a non-issue when a live Igor Pro instance is
  reachable through this bridge: `get_environment_summary()`'s `included_procedure_files`
  field reports the complete, authoritative list of every procedure file actually
  included in the running instance right now (derived from `WinList(..., "WIN:128")`).
  That field returns bare file names, not filesystem paths, so resolve each to its
  actual on-disk path before passing the list to `ipt`.
- Performance note: parsing is not uniformly fast across every file in a large codebase
  -- most files process in tens of milliseconds, but occasional outliers parse
  roughly 10x slower. Worth chunking/batching invocations rather than assuming a single
  whole-codebase call finishes quickly.

## Current transport architecture (v2.0.0+, ZeroMQ)

This bridge talks to Igor Pro over the [ZeroMQ-XOP](https://github.com/AllenInstitute/ZeroMQ-XOP)'s
`CallFunction` JSON protocol (a plain localhost TCP socket), calling into Igor-side
helper functions in `procedure/ZMQ_BridgeHelpers.ipf` (the `ZBR` independent module).
This replaced an earlier COM Automation Server transport (v1.x) whose main drawback was
requiring this bridge's Python process and Igor Pro to run at the exact same Windows
privilege level (both elevated, or both not) -- an easy-to-miss mismatch. **ZeroMQ has
no such requirement at all**: it is a plain TCP socket, so elevation/privilege-matching
is a complete non-issue for the current transport.

### Why an independent module (`#pragma IndependentModule = ZBR`)

Per Igor's own "Advanced Topics" help (Independent Modules section): an independent
module compiles separately from all other procedures, so it "can run when other
procedures are in an uncompiled state" -- placing this bridge's Igor-side code in its
own independent module means it stays callable via ZeroMQ even if the rest of the
experiment currently has a compile error. Limitation (same help topic): "Functions in an
independent module can not call functions in other modules except through the Execute
operation" -- this is why arbitrary command execution goes through `Execute/P` rather
than a direct call. Direct `WAVE`/`DFREF` references are NOT module-scoped, so wave
access needs no `Execute` at all.

### Why submit-then-poll instead of one blocking call

Igor's `Execute` operation (used to run arbitrary command text) cannot be called
unqueued from inside a `Function` at all -- only `Execute/P` (deferred: queued to run
only *after* the calling function returns to Igor's main loop) is legal there. So a
single ZeroMQ round trip cannot synchronously "run this command and hand back what it
printed." Solved with submit (`ZBR_SubmitCommand`, returns a token immediately) + poll
(`ZBR_PollCommand`, checked later, any number of times, spaced arbitrarily far apart)
instead. All actual state (a done flag, captured text) lives in Igor Pro's own data
waves (`root:Packages:ZBR`), not in this bridge's Python process -- reliable no matter
how long a submitted job runs or how many times the bridge process/Claude Desktop itself
restarts in the meantime. The only thing that ends a submitted job is Igor Pro itself
quitting, crashing, or restarting.

- **`cmd` and its finish-callback are queued as TWO SEPARATE `Execute/P` entries, never
  joined into one string with `;`.** Confirmed live: if `cmd` fails to parse or hits a
  runtime error partway through, Igor aborts the REST of that same top-level command
  string -- a joined `"cmd; finishCall"` string would silently drop the finish-callback
  whenever `cmd` errors, leaving `poll_igor_command` reporting "not done" forever,
  indistinguishable from a job still genuinely running.
- **There is no reliable, generic way to tell "cmd ran and legitimately printed nothing"
  apart from "cmd errored out partway through with no output."** `GetRTError(1)` reads
  `0` inside the finish-callback even after a genuinely-erroring `cmd` -- each
  `Execute/P` entry is dispatched as its own independent top-level execution, so Igor
  clears any pending runtime-error state before the queue advances to the next entry.
  Igor also does not append anything about the error to the `CaptureHistory`-tracked
  history stream. If a submitted command's success needs to be verifiable, have it
  `print` an explicit sentinel value/message itself.
- **Use `print`, not `fprintf 0, ...`, to get data back.** `CaptureHistory` (which the
  submit/poll mechanism relies on) captures `print` output but does NOT capture
  `fprintf`-to-history-refnum output at all (refnum 0, -1, or -2 all silently produce
  nothing, even though the command runs without error).
- **The finish-callback bounds-checks its row index before writing.** The index is
  captured at submission time, but the callback runs later in its own separate
  `Execute/P` entry -- if the underlying storage waves are ever resized smaller in the
  meantime (e.g. cleanup of old tokens), the index can end up out of range. Writing to
  an out-of-range wave index throws an uncaught Igor runtime error, which pops a real
  modal dialog and blocks Igor's entire main thread until a human dismisses it -- same
  class of problem as the stale-refnum bug below. If the row no longer exists, the
  write is skipped silently, and polling that token reports a clean "unknown token"
  error instead of hanging.
- **`RELOAD CHANGED PROCS`/`COMPILEPROCEDURES` must be issued as two SEPARATE `Execute/P`
  calls**, not joined into one compound string, and each needs its own mandatory
  trailing space (`"RELOAD CHANGED PROCS "`, `"COMPILEPROCEDURES "`). This function
  deliberately does NOT use the submit/poll token+callback mechanism -- confirmed live
  that a finish-callback queued via `Execute/P` *after* `COMPILEPROCEDURES` never
  actually runs (recompiling the whole procedure set appears to discard/invalidate
  whatever was still pending behind it in the operation queue). Instead, poll
  `ZBR_IsCompiled()`/the compile counter directly -- both are already synchronous,
  standalone checks that don't depend on anything surviving the recompile.

### `CaptureHistory`: signature and the stale-refnum bug

Real signature (confirmed from Igor's own reference docs): `CaptureHistory(refnum,
stopCapturing)` -- `refnum` must be the value `CaptureHistoryStart()` returned, not
omitted.

**Stale-refnum bug, fixed**: the stored refnum is a plain `Variable/G`, which Igor
persists into a saved experiment like any other global -- but the refnum is only
meaningful within the OS process that created it. Reloading a saved experiment (via this
bridge's `load_experiment`, or a user manually reopening a `.pxp`) brings the OLD numeric
value back even though the process is brand new. Using it then throws a genuine Igor
runtime error ("there is no open file with this reference number") from a plain
top-level `Execute/P` entry, which pops a real modal dialog and blocks Igor's whole main
thread (and every ZeroMQ reply) until a human dismisses it. Fix: don't just check
existence -- actually try using the stored refnum, wrapped in `try`/`catch`/`endtry`
with an explicit `AbortOnRTE` right after the risky call (a runtime error inside a `try`
block does NOT by itself jump to `catch`; only `AbortOnRTE` converts it into a catchable
abort). If the refnum turns out stale, silently start a fresh capture instead of ever
surfacing this to the user.

**Same-line rule**: the probe call and `AbortOnRTE` are kept on the SAME line, not split
across two. Igor's Debug on Error check happens at the END of each line, not each
statement -- if they were on separate lines, Debug on Error (if enabled) would trigger a
Debugger popup right when the stale refnum's runtime error occurs, before `AbortOnRTE`
ever gets a chance to convert it into a catchable abort. This same reasoning applies to
every other "risky call; err = GetRTError(1)" same-line pattern in the codebase (e.g.
around `zeromq_server_bind`/`zeromq_handler_start`).

### `ZBR_IsCompiled()`: must qualify `FunctionInfo` with `ProcGlobal#`

`FunctionInfo()` for a deliberately non-existent function returns `""` when procedures
are compiled, and a non-empty string ("Procedures Not Compiled") otherwise. An
unqualified `functionNameStr` resolves relative to the CALLING function's own module
context -- since this check runs from inside the independent `ZBR` module, it must be
qualified as `FunctionInfo("ProcGlobal#...")` to actually ask about ProcGlobal's compile
state, not `ZBR`'s own (which compiles separately and could be fine even while
ProcGlobal has a real error).

**Related fact, easy to misdiagnose**: `FunctionInfo(anything)` reports "Procedures Not
Compiled" for **every** function, including ones that have nothing to do with the actual
problem, whenever **any** compile error exists **anywhere** in the experiment -- a single
duplicate-definition or syntax error in one file is enough to make everything else
report as uncompiled too. Don't conclude a specific, unrelated file has a "separate"
compile problem just because its own functions report this way; check the actual
history/error text for the real offending file and line first.

### Compile-counter polling is race-free by construction

A dedicated global (bumped only inside `AfterCompiledHook`, at the exact moment Igor
itself confirms a successful compile) is trustworthy the instant it's observed to
increase over a baseline read before triggering a reload/compile -- no
repeated-confirmation dance needed, unlike the `FunctionInfo`-based check above. Reads
as `-1` (a real counter value can never be negative) if the global doesn't exist yet.

### ZeroMQ bind/handler lifecycle: bind once at startup, recompiles only stop/restart the handler

**Current design**: `zeromq_server_bind` happens once, in `IgorStartOrNewHook` (fires on
Igor launch and on creating a new experiment) -- NOT on every compile. `AfterCompiledHook`
no longer touches the ZeroMQ socket/handler at all; it only bumps the compile-confirmation
counter. Around a recompile, the handler is only stopped/restarted
(`zeromq_handler_stop()`/`zeromq_handler_start()`), never rebound -- a rebind is a
property of the ZeroMQ-XOP itself and doesn't need repeating just because a procedure
file recompiled.

**Never calls `zeromq_stop()` first**, unlike the three-call idiom shown in the
ZeroMQ-XOP's own introductory example. `zeromq_stop()` stops *every* ZeroMQ bind/
connection/handler for the whole Igor Pro instance, not just this module's own --
calling it unconditionally would tear down any other ZeroMQ subsystem the same
experiment might have set up independently. Calling `zeromq_server_bind` directly,
without stopping first, is safe to repeat: if this module's own socket is already
bound, the call simply errors ("Address in use"), caught rather than propagated.

**Why the handler-stop-before-recompile step exists at all**: a background thread inside
the ZeroMQ-XOP keeps dispatching incoming `CallFunction` requests regardless of what
Igor's main thread is doing -- a request arriving while `COMPILEPROCEDURES` is
mid-rebuild of Igor's own internal function/symbol tables is a plausible cross-thread
race (this bridge has observed genuine `EXCEPTION_ACCESS_VIOLATION` crashes deep inside
Igor64.exe coinciding with recompiles; not conclusively proven to be this exact
mechanism, since Igor64.exe ships no public symbols, but the mitigation below is
considered a well-reasoned fix). Mitigation: stop the handler before
`RELOAD CHANGED PROCS`/`COMPILEPROCEDURES` run, restart it after.

**A background-task watchdog (not just `AfterCompiledHook`) is required to restart the
handler, because `AfterCompiledHook` only fires after a *successful* compile.** If the
edited `.ipf` fails to compile, the hook never runs, and a restart path that relies on it
alone leaves the handler stopped forever -- the bridge permanently dead with no recovery
short of restarting Igor. Fix: a named background task, armed immediately before the
handler is stopped, that unconditionally restarts/rebinds the handler regardless of
whether the compile succeeds or fails, then self-disarms. **Must be registered with an
explicit `start=` tick-floor (not just relying on its `period`)** -- confirmed live via
timing instrumentation that the watchdog could otherwise tick well before a real compile
finishes, reintroducing the exact cross-thread race the stop-before-recompile step
exists to prevent. Igor's background-task scheduler and its deferred `Execute/P` queue
are two independent subsystems with no inherent ordering guarantee between them; the
`start=` floor is what actually enforces "don't fire before the queue has finished
draining."

**A separate, unrelated hazard**: a background thread group left running during
`COMPILEPROCEDURES` can raise a blocking "Function Execution Module is still active"
dialog, freezing Igor's entire operation queue (though direct `CallFunction` calls that
don't route through the queue keep working). Mitigated by releasing any running thread
groups (`ThreadGroupRelease(-2)`) before Igor uncompiles.

### `ZBR_ReadHelpFile()`: sequence and the `Abort` pitfall

Reads an Igor Pro help file (`.ihf`) as structured, formatted text. `CloseHelp`/
`OpenNotebook`/`SaveNotebook`/`KillWindow`/`OpenHelp` are ordinary window/notebook
operations, not subject to the Execute-only-from-top-level restriction, so the whole
sequence runs as one direct, synchronous round trip:

1. Snapshot every currently open help file (visible or hidden) and every open
   plain-notebook window.
2. `CloseHelp/ALL` (required: an `.ihf` can't be opened as a notebook while Igor
   considers it already open as a help file).
3. `OpenNotebook/R filePath`, then diff the notebook window list against the step-1
   snapshot to find the name Igor assigned the new window (`OpenNotebook/R` doesn't
   return this directly).
4. `SaveNotebook/O/S=5/H=...` export to a temp HTML file, read directly off disk by the
   Python side afterward (both processes run on the same machine, so this sidesteps any
   question about reply-size limits for a potentially large export).
5. `KillWindow/Z` the temporary notebook.
6. Restore every help file captured in step 1.

Steps 5-6 always run (via `try`/`catch`, since Igor procedure code has no `finally`)
even if an earlier step failed, so a failure partway through still restores whatever
help state existed before the call.

**`Abort "<message>"` pitfall, hit twice, now fixed both times**: the message-string form
of `Abort` displays a real alert dialog the instant `Abort` executes -- BEFORE control
ever reaches an enclosing `try`'s `catch` block, so wrapping it in `try`/`catch` does NOT
suppress the popup the way it does for an ordinary runtime error. This would hang
unattended use exactly like the stale-refnum and out-of-range-write bugs above. **General
rule: never use `Abort "<message>"` for an expected/recoverable failure path in any code
this bridge might run unattended -- set an error status value directly instead**, since
there is no scriptable way to dismiss the resulting dialog.

### `ZBR_ProcedureText`: window name is the THIRD argument

`ProcedureText(funcName, flags, winTitle)` -- pass `funcName=""` and `winTitle` set to a
specific window name (e.g. `"Procedure"`) to retrieve that whole window's contents. Hard
-won: the window name goes in the THIRD argument, not the first -- passing it first
silently returns `""` rather than raising an error, with no indication anything was
wrong.

### Custom ZeroMQ port / multi-instance support

`ZBR_EnsureZeroMQBound` reads an `IGOR_PRO_BRIDGE_PORT` environment variable at bind
time, falling back to the default port (5680) if unset, and binds to
`tcp://127.0.0.1:<port>` accordingly.

On the Python side, `configure_igor_launch(exe_path, port=None)`:
- Setting `port` writes `IGOR_PRO_BRIDGE_PORT=str(port)` into this bridge process's own
  environment (validated as an integer 1-65535; `bool` is explicitly excluded since
  `bool` is a subclass of `int` in Python and would otherwise silently pass an
  `isinstance(port, int)` check). The launched Igor Pro child process inherits this
  automatically since its environment is built from a copy of this process's own.
- **Omitting `port` (or passing `None`) explicitly clears** a previously-configured
  custom port -- a real removal of the environment variable, not just "don't set it."
  Python cannot distinguish "argument omitted" from "explicitly passed as `None`", so
  calling `configure_igor_launch` again to update just `exe_path` will also clear any
  previously-set port unless `port=` is repeated on that same call.
- **Every ZeroMQ-talking function resolves its endpoint through one
  `_igor_zmq_endpoint()` helper** (reading a module-level `_configured_igor_port`, `None`
  meaning "use the default"), not a fixed constant -- so setting/clearing the port
  immediately retargets every tool call together, not just the next launch.
- **Known limitation**: this bridge tracks only ONE currently-configured endpoint at a
  time. Talking to two Igor Pro instances truly simultaneously (rather than switching
  which single one this bridge points at) is not supported -- switching is done by
  calling `configure_igor_launch` again with a different (or omitted) `port`.

**Live-verified two-instance test** (confirms the above holds against real Igor Pro
processes, not just a standalone logic test): started a second Igor Pro instance on a
custom port alongside an already-running default-port instance; confirmed the
already-running-instance guard in `launch_igor_pro_unattended` checks reachability only
on the currently-configured port, so it correctly launched a genuinely new process
rather than mistaking the other instance for "already running"; confirmed
`configure_igor_launch` (with and without `port`) immediately retargets
`check_bridge_health`/every other tool call to the corresponding instance; had each
instance independently report its own bound port back via
`GetEnvironmentVariable("IGOR_PRO_BRIDGE_PORT")` (falling back to `"5680"` when unset),
confirming the whole round trip (Python env-var write -> Igor-side read -> actual bind
-> Python-side retargeting) agrees with reality. Closed both instances via
`submit_igor_command("Quit/N")` (not `execute_igor_command`) -- `Quit/N` terminates the
process before any finish-callback can run, so polling for completion would just
repeatedly hit an unreachable-socket error as the process disappears; the fire-and-forget
submit primitive sidesteps this by never waiting for a reply at all.

### Compile-error dialogs and the debugger

- **`dismiss_compile_error_dialog`**: closes a stuck "Function Compilation Error" dialog
  by posting a simulated Escape key press to it (via window enumeration), without
  needing OS focus. There is no scriptable way to resume, step, or otherwise dismiss
  Igor's Debugger window once something pauses it -- a paused call hangs forever. This is
  why the Debugger MUST be disabled for any unattended/automated session
  (`set_debugger_enabled(False)`, or use the `_unattended` tool variants, which
  disable/restore it automatically around the call).
- **Genuine compile errors vs. a stuck dialog, told apart reliably**: get a fresh history
  capture immediately before triggering a recompile, then read it back afterward and
  look for an `error:` line. This surfaces the exact `<file>:<line>:<col>: error:
  <message>` reliably when Igor Pro was launched with `/UNATTENDED` (compile errors are
  redirected to the history area instead of a modal dialog in that mode).

### Commands run through the bridge execute as interpreted code, not compiled procedure code

Commands sent via `execute_igor_command(_unattended)` run the same way as Igor's own
command line: as *interpreted* code, not compiled procedure code. Several Igor language
features only work inside compiled functions and are rejected (often with an unhelpful
generic error) when attempted this way: `Make/FREE ...` (free waves have no valid scope
outside a function), `WAVE ref = SomeFunc()` (assigning a wave reference from a function
call), multiple-return-value destructuring (`[val1, val2] = SomeFunc()`), and calling a
`static` function by its bare name (needs `ModuleName#FunctionName` from outside its own
module). Igor's command line also does not support multi-line control-flow blocks
(`if`/`else`/`endif`, `for`/`endfor`) at all -- such a block fails as a whole (every line
reported `NOT EXECUTED`), even though each line would be valid inside a real function.

**Workaround**: create/extend a small compiled scratch procedure file in the target
project, `#include` it from wherever that project's own procedure files are included,
`reload_and_compile_procedures`, then call a single compiled helper function from that
file via the command line -- everything inside the function body runs as compiled code,
so none of the above restrictions apply. Reach for this proactively the moment a command
needs one of the above-listed compiled-only features, rather than attempting the
unsupported syntax directly first.

**Separately**: variables declared on the command line (e.g. `String win`) persist as
global command-line variables across separate calls within the same Igor session -- they
are not scoped to a single bridge tool call. Re-declaring the same name in a later call
fails with `"the name already exists as a variable"`; just assign to it directly in
follow-up calls instead of re-declaring.

## `install.ps1` / `requirements.txt`

- `requirements.txt` pins every package -- direct AND transitive -- to an exact version
  with hash(es) (pip's hash-checking mode, triggered automatically once any requirement
  has a `--hash`). This is a supply-chain-integrity measure on top of ordinary version
  pinning. Regenerating it after a deliberate version bump requires resolving the full
  dependency tree per target Python minor version (wheels/versions can differ between
  Python versions -- confirmed for at least one transitive dependency), not just editing
  version numbers by hand.
- **`mcp` is pinned to the last 1.x release deliberately.** The MCP Python SDK's v2 line
  is a breaking rework (`FastMCP` renamed/moved out of `mcp.server.fastmcp`); `server.py`
  still uses the v1 API, so an unpinned or `>=1.0.0` requirement would silently resolve
  to 2.x and break the bridge outright on import. Do not remove this exact pin without
  migrating `server.py` to the v2 API first.
- `install.ps1` resolves `python.exe` from the Machine and User PATH *registry* values
  directly, deliberately ignoring the invoking shell's own possibly-customized
  `$env:Path` -- this mirrors what a freshly launched Claude Desktop process actually
  sees when it resolves the bare command `python`, which is not guaranteed to match an
  interactively-opened console's own resolution (e.g. a PowerShell profile activating a
  conda environment only for that session).
- `install.ps1` requires elevation only for `pywin32`'s post-install step (registers
  COM-support DLLs into protected system locations) -- this is a one-time, install-time
  requirement unrelated to whether Claude Desktop or Igor Pro themselves need to be
  elevated at runtime (they don't; see the transport-architecture section above).
- The install-verification step is written to a temp `.py` file and run as a script,
  rather than passed inline via `python -c <string>` -- PowerShell's native-argument
  quoting is unreliable for a single argument that itself contains both embedded double
  quotes and newlines (confirmed live: this mangled a multi-line Python snippet with
  f-strings into a `SyntaxError` when first tried inline). A temp file sidesteps
  command-line quoting/escaping entirely.
