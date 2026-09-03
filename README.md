# xr-tape

`xr-tape` records what a VR mod **submits** to OpenXR, and checks it.

There are three things between a game and a headset. Two already have tools:
`apitrace` captures what the game drew, and `xr-sim` supplies a runtime the mod
can talk to with no headset present. The mod in the middle — the actual product —
has only ever been observable by a person wearing a headset and remembering what
they saw.

```
   game draw stream  ──►  the mod  ──►  OpenXR runtime  ──►  headset
   ▲                      ▲               ▲
   apitrace            xr-tape          xr-sim
```

**62% of the fleet's failure atlas (106 of 171 rows) names a detection procedure
that is "record something and compare it".** This is that recorder, and the
comparisons.

## What it is

An **OpenXR API layer**. It intercepts the calls, writes a trace, and forwards
everything unchanged — it never alters a result.

Because it sits at a specification boundary rather than inside a game:

- it needs **no integration**: it works on every mod in the fleet today, and on
  mods you did not write;
- it links **no graphics API**, so one binary covers D3D9/10/11/12, OpenGL,
  Vulkan and headless;
- it reads **the wire, not the mod's variables**, so a mod that lies to itself
  cannot lie to the trace.

## Quick start

```powershell
.\tools\build.ps1 -Architecture all
.\tools\Install-XrTape.ps1 -Architecture x64
.\tools\Invoke-XrTape.ps1 -Executable C:\path\to\app.exe -Check
```

Verify the whole thing, including that every check can fail:

```powershell
.\tools\Test-XrTape.ps1
```

Check a trace you already have:

```powershell
python tools\xrtape_check.py <trace.ndjson> --expect-runtime xr-sim
```

Exit code `0` if nothing failed, `1` otherwise.

## What it checks

Twenty checks, each naming the failure-atlas row it implements, so a red line
leads to the write-up rather than to a guess. Highlights:

| Check | Catches |
|---|---|
| `submitted_fov_matches_located` | the declared projection is not the one that was rendered (`FAIL-STR-009`) |
| `submitted_pose_matches_located` | the value that reached the matrix is not the one computed (`FAIL-XR-011`) |
| `eye_order` | eye label and offset sign both wrong, cancelling (`FAIL-STR-003`) |
| `eye_subimages_distinct` | a duplicated mono render wearing a stereo costume (`FAIL-STR-001`) |
| `eye_pair_shares_display_time` | eyes rendered at different ages (`FAIL-STR-006`) |
| `no_submit_with_invalid_pose` | submitting a pose whose validity bits are clear (`FAIL-XR-013`) |
| `layer_budget` | over-submission that freezes the HMD while the flat game runs |
| `no_silent_truncation` | a capped recording reading as a complete one |

The first two are the ones no in-process test can perform on itself: they compare
what the runtime was **told** against what the runtime **said**, from outside
both. See [docs/CHECKS.md](docs/CHECKS.md) for the full list and for what is
deliberately **not** covered.

## Every check is proven able to fail

```powershell
python tools\xrtape_selftest.py
```

Synthesizes a well-formed trace, asserts all twenty checks pass, then injects one
fault at a time and requires each to flip **exactly** the checks it should — no
more. A fault that flips nothing is a check that does not work; a fault that
flips something extra is a check measuring the wrong thing.

It has already earned its keep twice. It caught a toe-in fault that rotated both
eyes the same way and therefore was not toe-in at all, and it caught the head
frame being taken from the left eye alone — which made a toe-in defect report
itself as vertical disparity.

## Guards

Layer selection is per process via `XR_API_LAYER_PATH` and `XR_ENABLE_API_LAYERS`.
**Nothing is written to the registry**, so the machine's OpenXR configuration is
untouched and a connected headset keeps working during a taped run.

`Invoke-XrTape.ps1` turns three silent failures into thrown errors:

| Trap | Guard |
|---|---|
| An **elevated shell** makes the loader ignore layer selection | refuses to run elevated |
| A **bitness mismatch** makes the loader skip the layer with no error | compares the layer's PE machine against the application's |
| Either leaves the app running perfectly with nothing recorded | **requires a new trace with a header record** |

The third is the point. A run that recorded nothing looks exactly like a run that
recorded everything, right up until you read the results.

## Known limits

- **It sees only applications that use the Khronos loader.** A probe that
  `LoadLibrary`s a runtime DLL and calls its dispatch table directly never
  creates a loader instance, so there is nothing to insert a layer into.
  Sims4VR's `xrprobe` is deliberately built that way; real mods use the loader.
- **v1 records geometry, contract and timing — not pixels.** That is where the
  most generic checks live, and it is what keeps one binary working across every
  graphics binding. Per-eye image capture is the v2 boundary; xr-sim already does
  it for D3D11.
- **A pass is not a headset.** Comfort, depth, world scale and whether something
  *feels* right remain irreducibly human.

## Layout

| | |
|---|---|
| `layer/xrtape_layer.cpp` | the API layer — the only C++ here |
| `tools/xrtape_check.py` | trace model and the check library |
| `tools/xrtape_selftest.py` | the falsification matrix |
| `tools/Install-XrTape.ps1` | copy the layer and write its manifest |
| `tools/Invoke-XrTape.ps1` | run an application under the layer, with the guards |
| `tools/Test-XrTape.ps1` | selftest, then tape and check every client found |
| `docs/SCHEMA.md` | the trace format, and its versioning promise |
| `docs/CHECKS.md` | every check, and what is not covered |

Only the OpenXR headers are required to build. A pinned copy is bundled under
`third_party/openxr`; advanced builds can override it with
`-DXRTAPE_OPENXR_INCLUDE=`.

The installer writes API-layer manifests as BOM-free UTF-8 and validates the
installed JSON before returning. Preserve that explicit encoding: Windows
PowerShell 5.1's `Set-Content -Encoding UTF8` adds a BOM that the OpenXR loader
rejects at byte zero.
