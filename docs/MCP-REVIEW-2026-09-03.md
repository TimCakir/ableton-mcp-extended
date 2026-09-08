# Ableton MCP review and improvement plan

> Historical review of the September 3 baseline. Implementation followed in
> [release 1.1.0](RELEASE-1.1.0.md). The findings and defect reproductions below
> describe the original source; use the positive tests in `tests/unit/` to
> verify the updated code. Live acceptance remains separate.

Reviewed 2026-09-03. Local branch: `working`, commit `8b38e6584a0b419c7439a347e899e14b724d3de6`, including the four pre-existing uncommitted files.

The integration has substantial capability already. The highest-value next release should make edits, cancellation, recording and completion reporting dependable. Then add workflow tools that combine those reliable primitives: verified exports, precise MIDI edits, arrangement variants and session comparisons.

This is a review and implementation plan. Production source, dependencies, installed scripts and Ableton Sets were not changed. The new files are this report and two offline evidence scripts.

## Verified baseline

| Item | Result |
|---|---|
| Existing test suite | `226 passed in 0.68s`, using `.venv/bin/python -m pytest -q` |
| Additional review evidence | 10 command scenarios and 4 transport/MCP scenarios reproduced with mocks |
| Actual registered tools | 144, enumerated through the installed SDK |
| Tool metadata | No annotations on any tool; all 144 output schemas describe a generic string result |
| Resources / resource templates / prompts | 0 / 0 / 0 |
| Tool-list size | 174,006 UTF-8 bytes when serialized with `json.dumps`; descriptions total 70,534 characters. This is a local serialization measurement, not a token or latency benchmark. |
| Main source files | `MCP_Server/server.py`: 6,918 lines; `AbletonMCP_Remote_Script/__init__.py`: 9,184 lines |
| Installed packages | MCP 1.28.1; ElevenLabs 2.54.0; Pydantic 2.13.4; pytest 9.1.1 |
| Installed Live application | 12.4.5, build string `2026-08-19_225ce5e356`, from its application Info.plist |
| Live connection | `get_build_info` could not connect. Loaded script identity, current Set and real audio behavior were not verified during this review. |
| Upstream ancestry | Local HEAD contains upstream `11164498a31e782ca3d3f4ac67cbb526df0166e7`, with 55 local commits ahead and 0 behind |

The review covered command safety, recording state machines, transport/lifecycle behavior, MCP contracts, tests, installation/deployment, documentation and current primary-source updates. It does not establish that every Live API wrapper works on the installed application. Some evidence uses mocked Live behavior, specifically the exclusive-arm side effect documented in the repository.

The current browser change adds User Library roots to URI lookup. Its corresponding new regression test passes. Preserve it when preparing the next implementation branch.

## Fix first: data integrity and reliable operation control

P1 means fix before expanding workflows that depend on the affected operation. Line references below are to the reviewed working tree, not future revisions.

### 1. P1: MIDI replacement deletes before validating replacement data

**Location:** `AbletonMCP_Remote_Script/__init__.py:6863–6886`, `_add_notes_extended`.

With `replace=True`, existing notes are removed before each incoming note is parsed. A malformed pitch therefore empties the original clip and then raises an error. A separate swallowed removal exception allows the function to append notes while reporting that replacement succeeded. Both paths reproduced offline.

**Change:** Parse and validate the entire replacement first. Preserve original note data, fail explicitly if removal fails, and use a bounded undo scope plus an explicit recovery path for insertion failure. Do not describe an undo group as an atomic transaction: Live mutations can still partially apply.

**Acceptance:** Invalid pitch, non-finite timing, invalid duration, removal failure and insertion failure preserve or recover the original notes and report the true outcome.

### 2. P1: Cancelled recording callbacks can restart or terminate another job

**Locations:** remote script `:7747–7785` (stem preparation), `:7380–7382`, `:7910–7912`, `:8032–8034` (scheduled steps).

Callbacks refer to the shared `_auto_rec` state without checking that they own the current operation. Reproductions show a cancelled stem preparation starting recording on the next tick, and an old 0–4-beat callback marking a replacement 100–200-beat recording done at beat 100.1.

**Change:** Give every job an immutable operation ID, Set identity, captured object references and explicit lifecycle. Every scheduled callback and finalizer must check ownership before touching Live. Cancel preparation as well as recording; cancel callbacks before accepting a conflicting new job.

**Acceptance:** Cancel-before-first-tick, cancel-during-record, cancel-then-restart, new-Set load and track insertion during a job cannot revive or retarget old work.

### 3. P1: Failed or cancelled freeze deactivates the original

**Location:** remote script `:7227–7231`, `_finish_auto_rec`.

The source track is deactivated for every finish status, before confirming a usable bounce. Cancelling before the first tick reproduced a muted original with no recorded clip.

**Change:** Deactivate only after a completed capture, later-tick clip/file verification and validation of the captured bounds. Keep the original enabled on failure, cancellation, no-start and interruption.

The term “freeze” also overpromises: the implementation bounces and changes `track_activator`; it does not invoke native Freeze or explicitly disable the device chain. The CPU-saving claim in `MCP_Server/server.py:3574–3579` needs measurement or corrected wording.

### 4. P1: Rejected recording requests still change the Set

**Locations:** remote script `:7823–7827` (`_freeze_track`) and `:7615–7617` (`_bounce_to_audio`).

Track creation happens before the active-recording and valid-range checks. Asking to freeze during a recording returns an error but creates a track and, under the documented exclusive-arm behavior, disarms the active source. An invalid bounce range also leaves a new track behind. Both reproduced.

**Change:** Complete all preflight checks before structural edits. Snapshot state before creation, record owned temporary objects, and clean up only those objects on setup failure.

### 5. P1: A timed-out mutation can execute after failure was reported

**Location:** remote script `:1121–1136`; client receive timeout at `MCP_Server/server.py:76`.

The dispatcher schedules a mutation and waits ten seconds. On timeout it returns an error without expiring the scheduled callback. The offline reproduction returned an error, then created the mock track when that callback subsequently ran. Long batches emit no response until completion and can therefore exceed the client's 15-second socket read timeout.

**Change:** Correlate requests with IDs and deadlines. Expire work that has not started. For work already running, return a pending/unknown outcome with a queryable operation ID. Preserve its result for reconciliation. Never replay an ambiguous write automatically.

**Acceptance:** A late callback cannot execute an expired queued request; reconnecting can retrieve a running or completed operation without duplicating it.

### 6. P1: Blocking tools prevent MCP cancellation and stop requests

**Location:** synchronous tool handlers throughout `MCP_Server/server.py`; especially `measure_section:5987–6009`.

With installed MCP 1.28.1, synchronous handlers execute on the event loop. A 300 ms mocked connection blocked an independent 50 ms coroutine until the tool ended. Section measurement blocks for its real-time duration, so cancellation and other MCP requests cannot be serviced promptly.

**Change:** Introduce nonblocking I/O with one serialized owner of the socket. Make long operations start/status/cancel jobs. Moving every function into independent threads is insufficient: the current shared socket has no request correlation or lock protecting a full exchange.

**Acceptance:** A mocked slow operation leaves discovery, status and cancellation responsive; simultaneous callers receive only their own responses.

### 7. P1: Fresh installation can select an incompatible MCP major version

**Locations:** `pyproject.toml:16`, `MCP_Server/server.py:7`, and the bundled ElevenLabs server's SDK imports.

The requirement `mcp[cli]>=1.3.0` permits v2, but the code imports the v1 `mcp.server.fastmcp` API. Official v2 release notes recommend a `<2` upper bound until migration. This is a release-contract mismatch verified from code and upstream documentation; no replacement environment was installed here. [MCP v2 release](https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.0.0)

**Change:** First constrain the supported major and record a reproducible dependency set. Then migrate the core and bundled integration deliberately, with real SDK discovery/call tests. Keep the Remote Script independent of the host's SDK dependency.

## Additional concrete defects

| Priority | Finding and evidence | Required improvement |
|---|---|---|
| P2 | Bounce snapshots arm state after creating its track; stems restore only pre-existing tracks. Reproductions lose the original arm state and leave new recording tracks armed. Remote `:7667`, `:7785`, `:7978–7988`, cleanup `:7234–7246`. | Snapshot before creation; disarm every owned recording track; restore original object references on every exit path. |
| P2 | Stalled playback and reaching the requested end both produce `done`. Remote `:8073–8074`, `:7934–7935`, `:7427–7436`. | Distinguish completed, interrupted, cancelled and failed; report requested and actual bounds. |
| P2 | `capture_session_to_arrangement` enables arrangement recording without the unrelated-track arm protection used by the other recorders. Remote `:7868–7874`. | Snapshot/disarm unrelated tracks and return an explicit overlap plan before recording. Verify on a disposable Set. |
| P2 | Tool exceptions become ordinary successful MCP responses. Server `:419–421` and many similar handlers. Actual SDK reproduction and the failed live build query both returned `isError:false`. | Return tool execution errors through the MCP error contract; keep structured partial-batch results. |
| P2 | `batch` hides successful per-command values. Server `:5551–5560`. | Return each result and applied/skipped/error status, including useful read results. |
| P2 | Default get/modify/remove windows use time 0 plus `clip.length`. A loop at 16–32 is queried over 0–16, missing audible material. Remote `:3057–3059`, `:3091–3101`, `:3181–3191`; also documented locally on August 28. | Explicit scopes: all stored notes, audible region, or provided range. Return markers and query bounds; paginate notes rather than truncating their usable output. |
| P2 | Region duplication passes an end coordinate as a length. Remote `:3223–3224`: start 8/end 12 copies 12 beats rather than 4. | Pass `region_end - region_start`; test nonzero starts. The documented argument is `region_length`. [Clip API](https://docs.cycling74.com/apiref/lom/clip/#duplicate_region) |
| P2 | Repeated “deterministic” humanization accumulates shifts despite the no-drift promise. Remote `:3118–3120`. Reproduced 1.000 → 1.012 → 1.024 beats. | Anchor to original note IDs/times or document cumulative behavior; do not mark it idempotent. |
| P2 | Hybrid TCP uses the same port 9877 and has success-reporting placeholders for operations. Hybrid `:16`, `:63–68`, `:377–410`; installation docs recommend coexistence. | Make it UDP-only or share the real dispatch implementation. Unknown/unimplemented operations must fail. |
| P2 | Shutdown closes the listener but leaves accepted client sockets pending; a command can dispatch after `running=False`. Remote `:122–137`, `:203–233`. Reproduced. | Track and close accepted sockets, drain jobs and recheck lifecycle before dispatch. |
| P2 | ElevenLabs config generation replaces the whole existing Claude configuration. `elevenlabs_mcp/__main__.py:92–95`. | Merge the named server entry, preserve other settings and write atomically with recovery. |
| P2 | Build checking can say all three versions match when one ID is absent. Server `:5613–5631`. Deploy also says no restart is needed on a second run without observing either process. `deploy.sh:60–70`. | Report unknown separately from matching; compare actual loaded hashes and protocol versions. |

Two important corrections to the existing backlog: `_prepare_batch_commands` only rebases named index fields; it does not decrement arbitrary numeric values such as `destination_time`. However, wrapper-to-wire parameter differences remain real, including `position` and bar/beat conversion. Also, `manage_clip_region` still resolves session clips only; documentation saying all region tools already support arrangement clips is too broad.

## Updates available now

### Upstream project

The latest published upstream main commit is `1116449` from May 7, 2026. It merges group-track guards; another May merge corrected the README Mac installation path. The local tracking ref matches that commit, and the current branch already includes it. There is no upstream main update to pull. [Upstream history](https://github.com/uisato/ableton-mcp-extended/commits/main/)

The fork's implementation has moved substantially beyond its README. Arrangement support, export workflows and many other tools exist while the README still presents some as future work. Treat upstream proposals and open pull requests as candidates to inspect, not released features.

### MCP Python SDK

Installed: **1.28.1**. Latest stable verified: **2.1.1**, released August 25, 2026. V2 changes `FastMCP` to `MCPServer` and supports the July 28 protocol revision while retaining older clients. The v2.1.1 release specifically directs old `mcp.server.fastmcp` imports to the migration guide. [Current release](https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.1.1), [migration guide](https://py.sdk.modelcontextprotocol.io/migration/)

Do this in two steps: contain the unbounded dependency, then migrate in an isolated environment with tool discovery, typed-result, error, stdio and cancellation tests. The first substantial improvement does not require migrating: v1 already supports meaningful structured outputs and tool annotations.

Migration changes concurrency too: v2 runs synchronous handlers on worker threads. That helps responsiveness but would expose this server's shared socket to concurrent exchanges unless serialization is fixed first. Build application-level job IDs and polling independently of optional protocol task support. [SDK migration guide](https://py.sdk.modelcontextprotocol.io/migration/)

The July 28 protocol revision adds server discovery, revises subscriptions and moves background tasks into an extension. Let the SDK handle those wire changes. Application job status should remain usable regardless of a host's extension support. [Protocol changelog](https://modelcontextprotocol.io/specification/2026-07-28/changelog)

SDK 2.1.0 also changes exception and content-return handling. Test actionable `ToolError` responses and real `structuredContent`/`outputSchema` behavior rather than treating the migration as an import rename. [SDK 2.1.0 release](https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.1.0)

### Ableton Live

Installed **12.4.5** matches the latest official stable release, dated August 26, 2026. Its fixes include crashes around modulated or remotely controlled device parameters. Live 12.4.3 also added opening/closing plug-in editor windows through the LOM, a useful feature candidate for this server. [Ableton release notes](https://www.ableton.com/en/release-notes/live-12/)

A released Max for Live capability is evidence for a candidate, not proof of the Python Remote Script signature. Check the exact installed object and guard the feature before publishing it. No live capability probes were possible in this review.

The published LOM overview identifies Live 12.3.5, so it can lag the installed 12.4.5 application. Use it for documented semantics while checking newer member availability against the actual running build. [LOM reference](https://docs.cycling74.com/apiref/lom/)

## Features worth building

| Order | Feature | What changes for music work | Implementation and proof |
|---|---|---|---|
| 1 | Reliable recording/export jobs | Start a bounce or stems pass, see progress, cancel reliably, and receive a file manifest that distinguishes complete, partial and failed outputs. | Shared operation engine; source handles; requested/captured range; sample rate/channels/duration; actual file peak/RMS and optional LUFS/true peak. Validate silence and suspicious identical stems without declaring intentionally silent material a failure. Require two distinct audible sources in Live acceptance. |
| 2 | Stable targets and edit previews | “Lower Bass” cannot silently turn into “lower the next track” after an insertion. Larger edits show exactly what will change. | Session-scoped handles tied to a Set epoch; expected identity/revision checks; ambiguous-name errors; overlap detection; explicit apply. Names alone are not stable IDs. Route batch and expert operations through the same checks. |
| 3 | Precise MIDI editing and variants | Transform the selected notes, preserve performance details and generate alternate sections without rebuilding the original clip. | All-note reads, marker-aware scope, note-ID editing and note pagination. Probability/velocity metadata must survive round trips. Use documented note-ID APIs when available and capability checks for older Live. [Clip API](https://docs.cycling74.com/apiref/lom/clip/) |
| 4 | Session inspection and comparison | See routing, missing media references, devices, automation overrides and changes since an earlier snapshot in one useful report. | Read-only JSON snapshots plus diff; duplicate-name handling; explicit unknown values. A snapshot is not a full `.als` backup. Verify saved-file persistence separately when requested. |
| 5 | Arrangement builder with controlled variants | Place intro/verse/break/drop material and generate changes such as removing kicks or thinning chords in one understandable operation. | Compose corrected clip and note tools; preflight target ranges and overlaps; one bounded undo scope where supported; return per-action results. Keep originals available and disclose partial application. |
| 6 | Small plug-in workflow improvements | Open the correct plug-in editor, inspect mapped parameters in real units and compare settings. | Probe the newly documented editor control; extend existing plugin discovery and aliases. Do not infer internal parameter access merely because the editor can be opened. |
| 7 | Take-lane management for vocals | Import, label and inspect takes without losing their relationship to the source recording. | Correct lane inspection to use documented `arrangement_clips`; wrap supported audio/MIDI lane creation/import. Native region comping remains a separate capability investigation. [TakeLane API](https://docs.cycling74.com/apiref/lom/takelane/) |

The most useful audio feature is an analysis step after a verified bounce: detect silence, clipping, incomplete duration, mono/stereo properties and level changes from the rendered file. Existing Live meter readings are display values and do not establish LUFS, true peak, waveform dynamics or whether the musical result sounds good.

Defer native freeze/flatten, offline rendering, per-clip scale repair and automatic comping until their exact mechanisms are verified. The repository records a failed per-clip scale probe on an older running build; that is not an evergreen statement about every future Live version. Offline `.als` editing should be a separate, copied-file workflow, not an implicit fallback inside a live edit.

## Improve the MCP interface and code structure

1. **Create one command registry.** Define wire name, typed arguments, index/unit conversion, mutability, required capabilities and handler together. Generate dispatch checks and tool metadata from it. The current duplicated declarations and prose about wire differences are avoidable sources of drift.
2. **Separate transport from domain logic.** Extract framed message encoding, request IDs, size limits, connect/read deadlines and reconnection from `server.py`. Keep one ordered socket exchange owner. Recover connection state without repeating writes.
3. **Consolidate recording state machines.** Bounce, stems, arrangement automation and capture should share lifecycle, ownership, cleanup and output verification. Parameterize their source/routing behavior rather than copying callback loops.
4. **Return domain data.** `TrackInfo`, `NotePage`, `BatchResult` and `OperationStatus` should be structured records with optional concise text. The existing generated `{result: string}` schemas do not expose useful fields to clients. Keep compatibility adapters while migrating consumers.
5. **Add truthful tool annotations.** Read-only, destructive and idempotent hints must reflect actual behavior. `measure_section` changes transport; `call_lom` can write or invoke arbitrary methods and cannot be labelled read-only. These hints help clients but do not replace server checks.
6. **Reduce discovery overhead.** Offer explicit tool groups such as inspect, edit, arrange and record, with backward-compatible full registration. Shorten repetitive descriptions and expose detailed examples as resources/prompts when the client supports them. Measure task completion and errors before claiming context savings from the 174 KB inventory.
7. **Add a diagnostic command.** Check application version, SDK version, endpoint identity, loaded build hashes, detected capabilities and active job state. Distinguish unreachable, unknown, mismatched and verified; present the exact recovery step for each.
8. **Make installs reproducible.** Add tested dependency bounds, a lock for development/deployment, a dev test extra, clean-install CI and separate optional ElevenLabs dependencies. The optional hybrid experiment should not determine core behavior.
9. **Split gradually along behavior boundaries.** Extract transport, operations, notes, arrangement, browser/devices and formatting behind the existing API. Avoid a broad rewrite before the reproduced defects have tests.

Thread confinement also needs an explicit design check: read handlers currently access Live from client threads, while a caught `schedule_message` assertion triggers direct mutation execution. This review did not establish the installed Framework's exact guarantees, so treat this as an investigation item, not a reproduced Live threading fault.

## Delivery sequence and gates

| Phase | Scope | Gate before proceeding |
|---|---|---|
| A: Contain failures | SDK bound; note prevalidation; recording preflight; operation IDs/cancellation; freeze success conditions; arm restoration; real error results. Add behavior tests in the existing suite. | Every reproduced integrity/control defect has a positive regression test. Failure/cancel paths preserve originals and restore owned state. |
| B: Reliable execution | Serialized nonblocking transport, deadlines and framed requests; consistent job status; output manifests; cleanup/shutdown; corrected MIDI scope/region/humanize contracts. | Hardware-free delay/disconnect/reconnect tests plus an opt-in disposable-Set smoke test; status and cancel remain responsive. |
| C: Useful workflows | Verified export/analysis, stable target handles, note variants, session diffs and arrangement previews. | End-to-end tasks produce inspectable changes and correct files, preserve originals and reject stale targets. |
| D: Modernization | Deliberate SDK v2 migration, generated registry, tool groups, resource support, modular source and documentation refresh. Parts may be moved earlier when they directly simplify A/B. | Clean install, real SDK stdio contract tests, supported-version matrix and host compatibility checks pass. |

Live acceptance should use a newly copied disposable Set with two simple, distinct sources and known MIDI. Check successful export, interrupted export, cancellation before routing, cancellation during recording, immediate restart, wrong Set, track insertion, malformed notes and recovery. Inspect actual recorded files and later-tick state. The default writing template and active production songs are not suitable test fixtures.

## Documentation repairs to include with implementation

- Reconcile the July “five tools never executed” handoff with the August 29 record of three nearly silent multi-track exports. Preserve the observed failure and label its cause unknown; this review did not reproduce that audio failure in Live.
- Correct the contradictory `exclusive_arm` writable/read-only claims, and the already-implemented `insert_device` backlog item.
- Correct the Mac install path, obsolete tool count, upstream clone instructions for this fork, wrong example server path, hybrid coexistence claim and whole-config replacement instructions.
- Track each feature as implemented / tested with mocks / verified in Live, with date, build, exact method and evidence. Avoid broad “impossible” claims from one failed API route.
- Keep registration consistency tests, but replace source-string checks that purport to verify arm cleanup or recorder behavior with scheduled-tick behavior tests. Existing source checks pass despite the reproduced defects.

## Reproduce this review

From the repository root:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python docs/review-evidence/command_repros.py
.venv/bin/python docs/review-evidence/transport_repros.py
```

The two evidence scripts deliberately assert that the reviewed defects exist. They are outside the test suite; a future fix should make the corresponding assertion fail, after which the scenario should become a positive regression test. They import the actual source but replace Live objects, connections and scheduled callbacks with mocks. They do not connect to Live or mutate a Set. The transport script uses installed SDK 1.28.1 internals and will need adaptation after an SDK migration.

The completed deliverable is the review, executable evidence and prioritized plan. Implementation, dependency migration and live acceptance remain future work.
