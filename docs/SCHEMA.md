# Trace schema v1

A trace is **NDJSON**: one JSON object per line, each with an `"r"` field naming
its record type. Line-oriented so a killed process still leaves a readable
prefix, and so a trace can be tailed while a run is in progress.

## The versioning promise

**Additive changes only.** Seven repositories will read these files; a breaking
change breaks all of them at once, and the fleet has already paid for that shape
once — a shared tool whose contract moved under its consumers.

- New record types and new fields may be added at any time.
- An existing field's **name, type and meaning never change**.
- A field is never removed.
- `schema` is bumped only if that promise must be broken, and the checker
  refuses a schema it does not understand rather than guessing.

A consumer must therefore ignore unknown record types and unknown fields.

## Records

Emitted once, near the start:

| `r` | Fields |
|---|---|
| `header` | `schema`, `layer`, `layerVersion`, `exe`, `pid`, `utc`, `qpcFreq`, `maxFrames`, `pointerBits` |
| `instance` | `appName`, `appVersion`, `engineName`, `apiVersion`, `extensions[]` |
| `runtime` | `name`, `version` — read from `xrGetInstanceProperties`, not from what was requested |
| `system` | `systemId`, `name`, `vendorId`, `maxLayerCount`, `maxSwapchainW/H`, `orientationTracking`, `positionTracking` |
| `viewconfig` | `type`, `views[]` of `recW/recH/maxW/maxH/recSamples` |
| `session` | `binding`, `bindingType`, `systemId`, `result` |
| `swapchain` | `handle`, `format`, `w`, `h`, `arraySize`, `faceCount`, `mipCount`, `sampleCount`, `usage`, `result` |
| `space` | `handle`, `spaceType`, `pose`, `result` |
| `beginsession` / `endsession` | `viewConfig`, `result` |

Emitted as they happen:

| `r` | Fields |
|---|---|
| `sessionstate` | `state`, `time`, `qpc` |
| `instanceloss` | `qpc` |
| `stamp` | `text` — whatever the mod wrote to `XRTAPE_STAMP_FILE` |

Per frame:

| `r` | Fields |
|---|---|
| `wait` | `seq`, `displayTime`, `displayPeriod`, `shouldRender`, `qpcIn`, `qpcOut`, `result` |
| `begin` | `seq`, `qpc`, `result` |
| `views` | `seq`, `displayTime`, `viewConfig`, `space`, `stateFlags`, `views[]`, `qpc` |
| `end` | `seq`, `displayTime`, `blendMode`, `layerCount`, `layers[]`, `qpc`, `result` |

Emitted last:

| `r` | Fields |
|---|---|
| `footer` | `records`, `recordedFrames`, `truncated`, `maxFrames` |

**A trace with no `footer` is not a complete trace.** The layer writes it from
`xrDestroyInstance`; a process that was killed never gets there. The checker
reports that rather than assuming the run simply ended.

**A missing `footer` does not mean a missing trace.** Read the paragraph above as
scoped to the footer record only. The records themselves are already on disk: the
writer is fully buffered at 64 KiB (`setvbuf(_IOFBF, 1 << 16)`) *and* flushes
explicitly every 256 records --- `if ((recorded % 256) == 0) g_trace.Flush();`
([layer/xrtape_layer.cpp](../layer/xrtape_layer.cpp)). A process that crashed or
was killed therefore leaves a usable trace behind, missing only the footer and at
most the handful of records written since the last flush.

This is worth stating explicitly because the opposite was assumed in practice.
During SOMAVR crash work (2026-09-01) an agent read the footer paragraph as
"a crashing run produces no trace by construction", recorded that as a limitation
to report upstream, and did not open the trace directory to check. Two crashed
runs had in fact written 2.4 MB of trace each --- the evidence for the crash being
investigated was sitting on disk, unread, across three debugging sessions.
**Crashing runs are exactly when the trace matters most. Always list the trace
directory before concluding a run produced nothing.**

## Poses, FOVs and sub-images

```json
"pose": {"p": [x, y, z], "o": [x, y, z, w]}
"fov":  {"l": -0.9425, "r": 0.7679, "u": 0.9599, "d": -0.9599}
"sub":  {"swapchain": 1234, "arrayIndex": 0, "rect": [x, y, w, h]}
```

FOV is stored as **four independent angles, always**. Collapsing them to a scalar
or a symmetric pair is itself a defect (`FAIL-STR-014`), and a trace that stored
fewer could not detect it.

Floats are written with `%.9g`, which round-trips a 32-bit float exactly. Storing
fewer digits would make "does the submitted value equal the located one" a
question about the trace format rather than about the mod.

## Depth

`XrCompositionLayerDepthInfoKHR` is in the schema at v1 **deliberately**, even
though most projects do not submit depth yet:

```json
"depth": {"minDepth": 0.0, "maxDepth": 1.0, "nearZ": 0.05, "farZ": 1000.0,
          "swapchain": 4242, "arrayIndex": 0}
```

SOMAVR submits depth layers, and the runtime it was first tested against had no
depth handling at all — so that path could be neither observed nor failed.
Reserving the slot now costs nothing; adding it later would break the promise
above for every other consumer.

## Handles

`swapchain` and `space` handles are **opaque identity tokens**. Compare them for
equality; never interpret them, and never persist one across runs.

They are recorded as unsigned integers via a bit-copy, because an OpenXR handle
is a pointer in a 64-bit process and a `uint64_t` in a 32-bit one. Windows
user-mode pointers stay well below 2^53, so a JSON consumer that parses numbers
as doubles will still round-trip them — but a consumer that needs certainty
should treat them as opaque strings on read.

## Frame correlation, and its limit

Records are correlated by **`seq`**, the layer's own `xrWaitFrame` counter — not
by `displayTime`.

That is not an implementation detail. Using `displayTime` as the key would put a
frame whose views were located for one time and submitted for another into two
separate frames, and silently turn the check that exists to catch exactly that
(`eye_pair_shares_display_time`) into a skip. The key must not be the thing under
test.

**Stated limit:** an application that waits for frame N+1 on another thread
before ending frame N will correlate approximately, because `begin`, `views` and
`end` each stamp the counter's value at the moment they run. Every client taped
so far is single-threaded through the frame loop. If a decoupled one appears, the
fix is a per-frame token threaded from `xrWaitFrame`, which is an additive change.

## Environment

| Variable | Meaning |
|---|---|
| `XRTAPE_DIR` | where traces are written (default `%LOCALAPPDATA%\xr-tape\default`) |
| `XRTAPE_MAX_FRAMES` | recording cap, default 20000 (~3.7 min at 90 Hz) |
| `XRTAPE_STAMP_FILE` | optional file the mod writes state into; folded into the trace on change |

The cap stops **recording**, never the application. An instrument that changes
the behaviour it measures is worse than no instrument — and when the cap is hit,
`footer.truncated` says so and the checker fails on it, because a capped
recording that reads as a complete one is the same defect as a blank capture
that reads as success.

## The stamp seam

The one per-project hook. A mod writes key/value text to `XRTAPE_STAMP_FILE` —
level name, config hash, whether stereo is armed, hook version — and the layer
folds the contents into the trace whenever they change, polled from `xrEndFrame`
at 4 Hz.

**Stamp state, not events.** A writer that rewrites the file faster than the poll
interval will have writes coalesced. This is the same hazard FarCry2-vr's
`command.txt` seam already carries.

A trace is useful without any of this, which is what keeps xr-tape usable against
mods you did not write.
