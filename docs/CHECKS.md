# The check library

Twenty checks, each a pure function of a parsed trace, each naming the
failure-atlas row it implements.

Purity is not tidiness — it is what lets `tools/xrtape_selftest.py` feed every
check a known-bad trace and require it to fail. **A check that has never been
observed failing is decoration**, and the fleet has shipped a green suite over a
no-op three times.

## Status vocabulary

| | |
|---|---|
| `PASS` | the property was measured and holds |
| `FAIL` | the property was measured and does not hold |
| `SKIP` | the property could **not** be measured from this trace |

`SKIP` is printed, counted, and promotable to a failure with `--require <check>`
or `--require all`. That matters: an earlier tool in this fleet skipped a check
when its field was missing, so a menu run matched no pattern, nothing objected,
and a census of the main menu passed. **Absence of evidence was being read as
evidence of validity.**

## Contract and identity

| Check | Fails when | Atlas |
|---|---|---|
| `trace_is_readable` | no header, or a schema this checker does not understand | |
| `runtime_identified` | no runtime record, or its name does not match `--expect-runtime` | `FAIL-XR-006` |
| `no_silent_truncation` | the frame cap was hit, or the trace has no footer | |
| `session_reached_focused` | the session never reached `FOCUSED` | `FAIL-XR-001` |
| `frame_contract` | a frame ended without a matching begin or wait | `FAIL-XR-007` |
| `frame_loop_advances` | display time stops advancing while the session lives | `FAIL-XR-004` |

## Composition

| Check | Fails when | Atlas |
|---|---|---|
| `never_submits_zero_layers` | a frame with `shouldRender` submitted nothing | |
| `layer_budget` | layer count exceeded the system's `maxLayerCount` | |
| `both_eyes_submitted` | a projection layer did not carry exactly two views | `FAIL-STR-028` |
| `depth_submission_consistent` | depth is attached to some views but not others | |

## Stereo geometry

Measured in the **head's own frame** — the mean of the two eye orientations — so
these survive a trace where the head is turned, which is every real trace.

| Check | Fails when | Atlas |
|---|---|---|
| `stereo_view_count_is_2` | the view configuration is not stereo | |
| `eye_order` | view 0 sits to the right of view 1 | `FAIL-STR-003` |
| `ipd_plausible` | eye separation is outside 45–80 mm | |
| `no_vertical_disparity` | the eyes differ vertically or in depth by > 2 mm | `FAIL-STR-002` |
| `eyes_parallel` | the eye orientations differ (toe-in) | |
| `eye_subimages_distinct` | both eyes were sent the same swapchain, array index and rect | `FAIL-STR-001` |

## Submitted versus located

**These two are the reason the recorder sits at the loader boundary.** No
in-process test can perform them on itself: they compare what the runtime was
*told* against what the runtime *said*, from outside both.

| Check | Fails when | Atlas |
|---|---|---|
| `submitted_fov_matches_located` | the declared projection is not the rendered one | `FAIL-STR-009` |
| `submitted_pose_matches_located` | the pose that reached the matrix is not the one located | `FAIL-XR-011` |
| `eye_pair_shares_display_time` | views located for one display time were submitted for another | `FAIL-STR-006` |
| `no_submit_with_invalid_pose` | a frame was submitted with view-state validity bits clear | `FAIL-XR-013` |

## What `submitted_fov_matches_located` does and does not prove

There are **three** quantities in play, and the layer can see two of them:

| | Visible to xr-tape |
|---|---|
| the FOV the runtime **located** | yes |
| the FOV you **declared** in the submitted projection view | yes |
| the projection you actually **rendered with** | **no** |

The check compares the first two. The third is the engine's own projection matrix, which never crosses
the OpenXR boundary, so no API layer can see it.

**A trivially-passing case worth recognising.** A probe that draws a fixed colour card with no projection
of its own, takes the located views, and submits those same views, passes by construction. It has proved
the plumbing, not the geometry. DishonoredVR hit exactly this on first pixels and flagged it.

**Which outcome is correct depends on who owns the projection, and you must say which up front.**

| Your mod | Correct outcome | Because |
|---|---|---|
| **can force the engine's projection to the runtime's** (native stereo, source port, a rewritable projection) | **PASS** | you rendered with the located FOV, so declaring it is true |
| **cannot** — an injector over the game's own frustum | **FAIL** | your pixels came from the game's frustum; declaring the runtime's is a statement about the image that is not true |

**For the second class, a PASS is the defect.** An injected mod cannot change what the game renders, so
if declared equals located, something overwrote the declaration with the runtime's preferred FOV and the
image is a lie the validator is satisfied by. PreyVR measured it: Prey renders **120° × 88.507°,
zero asymmetry**; a Quest 3 reports `l -54.0, r +40.0, u +44.0, d -55.0`. Declaring honestly makes the
right edge differ by **20°** and this check fails — *"that failure is the pass."*

**The runtime cannot detect the lie.** It reprojects to whatever is claimed, so the error surfaces as
wrong depth and wrong scale rather than as an error — which is why the instinct to turn a red check green
is the exact wrong move here.

**This corrects an earlier version of this document**, which said passing was "necessary, not sufficient
— but not a failure signal". That is true only for the first row. DishonoredVR reported the second row
first and was right; PreyVR and Sims4VR then measured it independently.

Until `xr-tape` takes an explicit projection-authority mode, **record which row you are in beside the
result** — a bare green or red here means nothing without it.

## Reading a geometry failure

A geometry check and a submitted-versus-located check failing **together** means
something different from either failing alone, and the falsification matrix is
built to keep that distinction sharp:

- **geometry only** — the runtime reported a bad rig and the mod passed it
  through faithfully. Look at the runtime.
- **geometry *and* submitted-vs-located** — the mod mangled a good rig. Look at
  the mod.

The matrix injects each geometry fault on **both** the located and submitted
views for exactly this reason. A fault applied only to the submitted side would
trip both families at once and the distinction would be untested.

## What is deliberately not covered

Stated rather than left to be discovered. Roughly 106 of the atlas's 171 rows
describe a record-and-compare procedure; this library implements the subset
decidable from geometry, contract and timing alone.

**Needs pixels (the v2 boundary):**
`FAIL-STR-004` one-eye lighting · `FAIL-STR-005` corrupted reflections ·
`FAIL-STR-016` per-eye auto-exposure · `FAIL-STR-018` screen-space content
riding the head · `FAIL-STR-023` per-eye LOD and billboard divergence ·
`FAIL-STR-024` one eye black on specific screens · `FAIL-STR-029` a captured eye
containing an older frame.

**Needs the game, or a person:**
world scale, comfort, depth judgement, whether an unlit hand reads in a dark
corridor. `xr-tape` raises the yield of a headset trip. It does not remove it.

**Needs more than one trace** (planned, not built): the runtime-divergence diff —
the same client taped under `xr-sim` and under a real runtime, compared. That is
the check that would turn every project's standing caveat, *"a pass under the sim
is not a pass under VDXR"*, from a permanent asterisk into a number.

## Adding a check

1. Write it as a pure function of a `Trace`, returning a `Result`.
2. Register it in `CHECKS`.
3. **Add a fault case to `MATRIX` in `xrtape_selftest.py`** and declare the exact
   set of checks it must flip.
4. Run the selftest. If your fault flips something extra, one of the two checks
   is measuring the wrong thing — fix that before believing either.

Step 3 is not optional. The selftest reports any check with no fault case as
untested, by name.
