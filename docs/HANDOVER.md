# xr-tape handovers

One self-contained block per in-house mod. Paste the block for your project to that
project's agent — each stands alone and assumes no prior context.

---

## Shared preamble (included in every block below)

**`xr-tape` is a new shared tool at `D:\Dev Debug\xr-tape`.** It is an OpenXR **API layer** that
records what your mod *submits* — both eye poses, both projections, the submitted layer set, frame
timing, depth info — into a versioned trace, plus 20 checks that turn that trace into a verdict.

**It needs no code change in your repo.** It attaches at the loader, so it works on your build today.

```powershell
& 'D:\Dev Debug\xr-tape\tools\Install-XrTape.ps1' -Architecture <x86|x64>   # once
& 'D:\Dev Debug\xr-tape\tools\Invoke-XrTape.ps1' -Executable <your.exe> -Check
python 'D:\Dev Debug\xr-tape\tools\xrtape_check.py' <trace.ndjson> --expect-runtime xr-sim
```

Exit `0` if nothing failed, `1` otherwise — so it drops straight into an existing test script.

**Selection is per process** via `XR_API_LAYER_PATH` + `XR_ENABLE_API_LAYERS`. Nothing is written to
the registry; the machine's real runtime is untouched and a connected headset keeps working. It
records under **xr-sim and under a real headset runtime alike**.

**Three guards, because each of these fails silently:** an elevated shell makes the loader ignore
layer selection; a bitness mismatch makes it skip the layer with no error; and either leaves your app
running perfectly with nothing recorded. `Invoke-XrTape.ps1` throws on all three and **requires a new
trace with a header record** before calling a run taped.

**The two checks nothing else can do:** `submitted_fov_matches_located` and
`submitted_pose_matches_located` compare what the runtime was *told* against what it *said*, from
outside both. No in-process test can perform either on itself.

**How to read a stereo failure:**

| Result | Means |
|---|---|
| a geometry check fails **alone** | the runtime reported a bad rig, your mod passed it through — look at the runtime |
| geometry **and** submitted-vs-located fail | your mod mangled a good rig — look at the mod |

**Pairing with the RE MCPs — xr-tape localises, they explain.** A failing check names the frame index,
display time and the exact value that was wrong; RenderDoc or apitrace then opens *that* frame, Ghidra
finds the function that built the value, x32dbg/x64dbg breaks on that frame at the submit site, and
ReGenny/Cheat Engine uses the traced pose as an exact live-memory search target. Without the trace you
are searching a capture; with it you are opening one draw. Combined with xr-sim's control channel
(`head`, `hand`, `ipd`, `fov`, `hazard`, `state`) you get a scripted scenario with an automatic
pass/fail, which neither tool does alone.

**Limits, stated up front:** v1 records geometry, contract and timing — **not pixels**. It sees only
applications that use the Khronos loader (a probe that `LoadLibrary`s a runtime DLL and calls its
dispatch table directly is invisible to it). And a pass is not a headset: comfort, depth, world scale
and whether something *feels* right stay human.

Docs: `README.md`, `docs/SCHEMA.md`, `docs/CHECKS.md`. Playbook:
[XR-007](../../VR%20Modding/docs/pattern-catalog.md), ch09 `#submitted-frame-recorder`.

---

## FarCry2-vr — architecture `x86`

**You are the natural first x86 target.** The 32-bit layer is built and its guards are verified, but
it has **never been exercised against a live 32-bit client** — every client taped so far is x64. Far
Cry 2 is the highest-value first run in the fleet for that reason alone, and a clean trace from a
32-bit process is a result the whole fleet needs.

**What it buys you specifically:**

- `bridged=0` means the XR half is proven and the content half is not. A trace is how the XR half
  **stays** proven while you chase menu entry — a regression there would otherwise surface as a
  confusing content symptom.
- Your `.xrs` sequences already drive the rig through xr-sim. They currently produce observations;
  xr-tape supplies the **verdict** they lack. Run a sequence, then check the trace — `Run-Sequence.ps1`
  and `xrtape_check.py` compose directly.
- Your transport is a mod-owned D3D10.1 shared surface into a private D3D11 OpenXR device. The trace
  records the graphics binding actually presented to `xrCreateSession`, so the thing your architecture
  is unusual about is stated in the data rather than remembered.
- `claimRatioH` and `eyeSeparationM` are numbers you already assert on; the trace adds the four FOV
  tangents, both poses and the layer set alongside them, per frame.

**Start here:** `Install-XrTape.ps1 -Architecture x86`, then tape the injected game under xr-sim with
`-LayerDir` left default. If the layer does not load, the error will name which of the three guards
caught it — read it rather than retrying.

---

## PreyVR — architecture `x64`

**You already have a passing baseline: `preyvr_xr_session_probe` scores 18 of 18 applicable checks
under xr-tape today.** Nothing needs building to start.

**What it buys you specifically:**

- `HEADLESS_TESTING.md` states the principle already — *"every runtime probe should emit a bounded
  fixture or summary that can be replayed in the headless suite afterward… this turns each headset
  session into new permanent test coverage instead of a one-off observation."* **A trace is that
  fixture, produced automatically, for every run.** You currently hand-carve them one finding at a
  time (`aim_state_fixture`, `wrench_query_fixture`, `frame_dump`).
- **The open format-28 gamma question is a trace diff.** You found xr-sim encodes a format-28
  swapchain as linear and asked whether VirtualDesktopXR does the same. Tape the same probe under both
  runtimes and compare — that turns your standing "must be measured before the format is fixed" into
  one command.
- Your asymmetry solve (`AsymmetryFromFovTangents`) is validated against your own recomputation, which
  you correctly flagged as self-referential. `submitted_fov_matches_located` checks it against **the
  runtime's own numbers**, from outside your process — a genuinely independent second opinion.
- `layer_budget` currently SKIPs on your traces because the session probe never calls
  `xrGetSystemProperties`. Worth adding to the probe; it is one call.

---

## Swat4-VR — architecture `x86`

**F-0025 is the reason to adopt this now.** Your 9On12 gate fails with no OpenXR session in existence
at the moment the factory is created, and two named causes were refuted by experiment.

**What it buys you specifically:**

- A trace states **exactly which frames reached submission and which did not**, with timing, without
  adding a single log line to the bridge. Your existing funnel counters tell you a stage was reached;
  the trace tells you when, in what order, and with what values.
- **32-bit, D3D12 via 9On12** — the binding actually passed to `xrCreateSession` is recorded, so the
  question "did this run really go through 9On12?" is answered by the trace rather than by a log line
  you have to trust.
- `autotest.ps1` already exits non-zero on a failed census gate. `xrtape_check.py` exits `1` on a
  failed check, so it drops in beside `census_parse.py gate` with no new machinery.
- Your `tools/xrsim-launch.ps1` already refuses elevation and asserts the runtime name. xr-tape's
  launcher does the same for the *layer*, using the identical reasoning — pass `-RuntimeJson` to get
  both in one run.

**Note:** you retired `tools/xrsim32` to avoid two definitions of correct. Same logic applies here —
if xr-tape's checks disagree with a private frame comparer, resolve it rather than keeping both.

---

## SOMAVR — architecture `x64`

**You are the only fleet project submitting `XrCompositionLayerDepthInfoKHR`, and xr-tape's schema
reserves the depth slot at v1 specifically because of you.** The vendored runtime you assessed had
*zero* depth handling, so that path could be neither observed nor failed. xr-tape records depth
sub-images, near/far planes and min/max depth per view, and `depth_submission_consistent` fails if
depth is attached to some views but not others.

**What it buys you specifically:**

- Your `XRSIM_INTEGRATION_ASSESSMENT.md` makes the sharpest point in the fleet: *"a green catalog run
  is an environment gate, not a regression gate."* **xr-tape is the regression gate.**
  `never_submits_zero_layers` is F-02 as an executable check. `layer_budget` is F-05's `appendLayer`
  clamp measured against the system's actual `maxLayerCount`. Neither needs a probe that links your
  code, because the layer reads the wire.
- `somavr_xrsim_smoke` already passes **18 of 18** applicable checks under xr-tape, and it is an
  OpenGL client — the first proof that one layer binary covers a non-D3D binding.
- Your route-1 recommendation (lift the layer-budget and hold decisions into `somavr_render_math` and
  unit-test them) is still the right first move and still does not depend on this. xr-tape covers the
  part that genuinely needs a live session: F-19 and F-20 across a transient fault, driven by xr-sim's
  `hazard` and `instanceloss` commands.

---

## ss2vr-work — architecture `x64`

**The layer budget is your own bug, and it is now a check.** `layer_budget` asserts the submitted layer
count against the system's `maxLayerCount` — which is exactly the v3.45 defect where the parry overlay
went 5 to 7 layers, `xrEndFrame` returned `XR_ERROR_LAYER_LIMIT_EXCEEDED`, the runtime dropped the
whole frame, and the VR view froze while the flat game kept running. That cost a session to identify.

**What it buys you specifically:**

- `layer-budget.xrs` arms every layer-bearing feature; xr-tape **measures** the result. Your scenario
  currently asserts `layersLastFrame <= 16` through xr-sim's state file; the trace records the layer
  *set* per frame, so when it exceeds the budget you can see which features were armed.
- Your quad layers — pointer beam, arm holograms, wrist reveals, snap-turn blackout, comfort vignette —
  are composited by the runtime and appear in neither `@shot`'s backbuffer nor the scene texture. The
  trace records every one of them by struct type, so you can count and identify them without a headset.
- `stereo-pair.xrs` freezes the head so the only sanctioned difference between the eyes is the IPD
  baseline. `eye_pair_shares_display_time` and `submitted_pose_matches_located` measure the eye-phase
  slip directly rather than by inference.
- **Known:** `xr_hello64` submits zero layers by design ("pumping 60 empty frames"), so it trips
  `never_submits_zero_layers`. That is recorded in `Test-XrTape.ps1` as an expected finding with the
  reason attached — not suppressed, and the harness reports it if it ever *stops* firing.

---

## DishonoredVR — architecture `x86`

**Your probe passes on D3D11 while your shipping route is D3D12 via 9On12 — and the trace records the
binding that was actually used.** "We validated the wrong binding" stops being something you have to
remember and becomes a field in the data.

**What it buys you specifically:**

- Your doc 15 already lists the three guards (refuse elevation, check PE machine, assert the runtime
  name) and calls the third *"the load-bearing one"*. xr-tape's launcher implements all three for the
  layer, for the same reasons, so adopting it costs you no new discipline.
- You found your probe only ever tested one of the two legal eye topologies (texture array vs per-eye
  swapchain) and treated a refusal as a broken runtime. The trace records `arraySize`, `faceCount` and
  the per-view `imageArrayIndex`, so which topology a run actually obtained is recorded rather than
  assumed — and `eye_subimages_distinct` catches the case where both eyes end up reading the same
  pixels.
- **32-bit**, so use `-Architecture x86`. The x86 layer is built and guard-verified but not yet run
  against a live 32-bit client; you and FarCry2-vr are the two candidates.
- Your per-boot adapter LUID finding (four values on one card) is exactly the kind of thing a trace
  makes cheap to re-establish: the session record carries what was requested, per run.

---

## Sims4VR — architecture `x64`

**Read this first: `tools/xrprobe` hand-loads the runtime DLL, so xr-tape cannot see it.** It calls
`xrNegotiateLoaderRuntimeInterface` itself and never creates a loader instance, so there is no loader
for an API layer to be inserted into. This was found by taping it — the probe passed all twelve checks,
exited 0, and wrote no trace, and the launcher correctly refused to call that a taped run. That is a
permanent property of the probe's design, not a bug in either tool, and `Test-XrTape.ps1` now detects
and reports it as a SKIP with the reason.

**What it buys you when a real client exists:**

- The moment Sims4VR has an in-game OpenXR client, xr-tape adds the two checks your probe
  **structurally cannot perform on itself**: submitted FOV and submitted pose against what the runtime
  actually reported. Your twelve checks read `xrLocateViews`; xr-tape compares that against what you
  then submitted.
- Your falsification matrix and xr-tape's are the same discipline, and yours taught it the sharper
  version: the right-eye-only FOV case that caught a check reading eye 0 alone is in xr-tape's matrix
  as `right_eye_only_fov`.
- `harness/` (game, no mod) and `tools/xrprobe` (mod, no game) are complementary. xr-tape is a third
  axis — **the real client, in the real process, from outside it** — and it is the only one of the
  three that will work unchanged once the native layer exists.

---

## BioshockVR — architecture `x86`

**You have not adopted xr-sim, and you do not need to in order to use this.** xr-tape records under a
real headset runtime exactly as it does under a simulator — that is the point of sitting at the loader.

**What it buys you specifically:**

- **It turns a headset session into a permanent asset.** Today a trip answers the questions you thought
  to ask; a trace lets you ask new ones of an old session afterwards. Given that every question here
  has historically cost a session, this is the highest-leverage change available to you.
- Your project traced a reproducible fail-fast to a **third-party implicit API layer** (a 32-bit motion
  compensation layer) sitting in your call chain. xr-tape is itself an explicit layer selected per
  process, and the trace records the runtime's own name and version — so "what was actually in the
  chain that run?" is answerable after the fact.
- **32-bit**, so `-Architecture x86`.
- If you later want headless runs, `D:\Dev Debug\xr-sim` has a catalog profile system and covers x86
  D3D11 — but that is a separate decision, and xr-tape does not depend on it.

---

## Reporting back

If a check fires and you believe the check is wrong rather than the code, say so — the check library
has a falsification matrix (`tools/xrtape_selftest.py`) and adding a fault case is the way that
argument gets settled. Two checks were fixed that way during the build; one of them was reporting a
toe-in defect as vertical disparity.
