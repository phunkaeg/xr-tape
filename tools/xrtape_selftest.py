#!/usr/bin/env python3
"""Falsification matrix for the xr-tape check library.

The checks pass on their first run against a well-behaved trace, and a trace is
not something you can easily make misbehave on demand. That is precisely the
condition under which a check is decoration: it has never been observed failing,
so nothing distinguishes "correct" from "broken".

So: synthesize a nominal trace, assert every check passes, then inject one fault
at a time and require it to flip EXACTLY the checks it should. A fault that
flips nothing is a check that does not work. A fault that flips something extra
is a check that is measuring the wrong thing, and that is a failure too.

    python xrtape_selftest.py
"""

from __future__ import annotations

import copy
import json
import math
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import xrtape_check as xt  # noqa: E402


# --------------------------------------------------------------- synthesis

IPD = 0.063
EYE_HEIGHT = 1.6

# Measured on VirtualDesktopXR / Quest 3 by Sims4VR and PreyVR independently:
# l -54.0, r +40.0, u +44.0, d -55.0 degrees. The nominal trace previously used
# u/d = +-55, which is xr-sim's VERTICALLY SYMMETRIC frustum - so the matrix was
# calibrated against the substitute's simplification and would have passed a mod
# that assumed the optical axis sits at the centre of the eye texture. On a
# 2880-tall image that assumption is a 279-pixel vertical error.
FOV_LEFT = {"l": -0.9425, "r": 0.6981, "u": 0.7679, "d": -0.9599}
FOV_RIGHT = {"l": -0.6981, "r": 0.9425, "u": 0.7679, "d": -0.9599}


def _quat_y(radians):
    return [0.0, math.sin(radians / 2.0), 0.0, math.cos(radians / 2.0)]


def _rotate(q, v):
    return list(xt.quat_rotate(tuple(q), tuple(v)))


def _view(pose, fov, swapchain, array_index, with_depth=True):
    view = {
        "pose": {"p": list(pose[0]), "o": list(pose[1])},
        "fov": dict(fov),
        "sub": {"swapchain": swapchain, "arrayIndex": array_index,
                "rect": [0, 0, 2064, 2208]},
    }
    if with_depth:
        view["depth"] = {"minDepth": 0.0, "maxDepth": 1.0, "nearZ": 0.05, "farZ": 1000.0,
                         "swapchain": 4242, "arrayIndex": array_index}
    return view


def _eye_poses(yaw_radians):
    """Both eyes for a head yawed by `yaw_radians`, so the geometry checks are
    exercised against a rotated head rather than only the identity case they
    would trivially pass."""
    q = _quat_y(yaw_radians)
    offset = _rotate(q, [IPD / 2.0, 0.0, 0.0])
    centre = [0.0, EYE_HEIGHT, 0.0]
    left = ([centre[i] - offset[i] for i in range(3)], q)
    right = ([centre[i] + offset[i] for i in range(3)], q)
    return left, right


def nominal_records(frames=8):
    records = [
        {"r": "header", "schema": 1, "layer": "XR_APILAYER_XRTAPE_recorder",
         "layerVersion": "0.1.0", "exe": "C:\\synthetic\\nominal.exe", "pid": 1234,
         "utc": "20260831-120000", "qpcFreq": 10000000, "maxFrames": 20000,
         "pointerBits": 64},
        {"r": "instance", "appName": "nominal", "appVersion": 1, "engineName": "",
         "apiVersion": 281474976784384, "extensions": ["XR_KHR_D3D11_enable"]},
        {"r": "runtime", "name": "xr-sim", "version": 4294967296},
        {"r": "system", "systemId": 1, "name": "Meta Quest 3", "vendorId": 0,
         "maxLayerCount": 16, "maxSwapchainW": 16384, "maxSwapchainH": 16384,
         "orientationTracking": True, "positionTracking": True},
        {"r": "viewconfig", "type": 2, "views": [
            {"recW": 2064, "recH": 2208, "maxW": 4096, "maxH": 4096, "recSamples": 1},
            {"recW": 2064, "recH": 2208, "maxW": 4096, "maxH": 4096, "recSamples": 1}]},
        {"r": "session", "binding": "d3d11", "bindingType": 1000027000, "systemId": 1,
         "result": 0},
        {"r": "swapchain", "handle": 1111, "format": 28, "w": 2064, "h": 2208,
         "arraySize": 2, "faceCount": 1, "mipCount": 1, "sampleCount": 1, "usage": 32,
         "result": 0},
        {"r": "space", "handle": 2222, "spaceType": 2,
         "pose": {"p": [0, 0, 0], "o": [0, 0, 0, 1]}, "result": 0},
        {"r": "beginsession", "viewConfig": 2, "result": 0},
        {"r": "sessionstate", "state": 5, "time": 1000, "qpc": 100},
    ]

    for i in range(frames):
        seq = i + 1
        display = 1000000 + seq * 11111
        # Sweep the head so the local-space transform is genuinely exercised.
        left, right = _eye_poses(yaw_radians=(i - frames / 2.0) * 0.15)
        located = [
            {"pose": {"p": left[0], "o": left[1]}, "fov": dict(FOV_LEFT)},
            {"pose": {"p": right[0], "o": right[1]}, "fov": dict(FOV_RIGHT)},
        ]
        records.append({"r": "wait", "seq": seq, "displayTime": display,
                        "displayPeriod": 11111, "shouldRender": True,
                        "qpcIn": seq * 100, "qpcOut": seq * 100 + 10, "result": 0})
        records.append({"r": "begin", "seq": seq, "qpc": seq * 100 + 11, "result": 0})
        records.append({"r": "views", "seq": seq, "displayTime": display, "viewConfig": 2,
                        "space": 2222, "stateFlags": 0xF,
                        "views": copy.deepcopy(located), "qpc": seq * 100 + 12})
        records.append({"r": "end", "seq": seq, "displayTime": display, "blendMode": 1,
                        "layerCount": 1, "layers": [
                            {"type": "projection", "flags": 0, "space": 2222, "views": [
                                _view(left, FOV_LEFT, 1111, 0),
                                _view(right, FOV_RIGHT, 1111, 1)]}],
                        "qpc": seq * 100 + 20, "result": 0})

    records.append({"r": "footer", "records": len(records) + 1, "recordedFrames": frames,
                    "truncated": False, "maxFrames": 20000})
    return records


# ------------------------------------------------------------------- faults


def _frames_of(records, kind):
    return [r for r in records if r.get("r") == kind]


def _first_layer_views(records, index=0):
    return _frames_of(records, "end")[index]["layers"][0]["views"]


def _rig_pairs(records):
    """Yield (left, right) for BOTH the located views and the submitted views of
    every frame.

    Geometry faults are applied to both sides on purpose. Corrupting only the
    submitted side models a mod that mangled the rig, which trips the
    submitted-vs-located comparison as well - so the geometry check under test is
    no longer isolated. Corrupting both models a runtime reporting a bad rig that
    the mod passed through faithfully, which is the case that must be caught by
    the geometry check ALONE. Keeping those two scenarios distinct is what makes
    a red line mean 'the mod is wrong' or 'the runtime is wrong' rather than
    just 'something is wrong'.
    """
    for views in _frames_of(records, "views"):
        yield views["views"]
    for end in _frames_of(records, "end"):
        for layer in end["layers"]:
            if layer.get("type") == "projection":
                yield layer["views"]


def _compose_yaw(view, degrees):
    x1, y1, z1, w1 = view["pose"]["o"]
    x2, y2, z2, w2 = _quat_y(math.radians(degrees))
    view["pose"]["o"] = [
        w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
        w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
        w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2,
        w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
    ]


def fault_eye_swap(records):
    for pair in _rig_pairs(records):
        pair[0], pair[1] = pair[1], pair[0]


def fault_toe_in(records):
    # Opposite signs, or the eyes stay parallel to each other and the fault does
    # not exist. The first version of this rotated both eyes the same way and the
    # matrix correctly reported that it flipped nothing.
    for pair in _rig_pairs(records):
        _compose_yaw(pair[0], +2.0)
        _compose_yaw(pair[1], -2.0)


def fault_vertical_offset(records):
    for pair in _rig_pairs(records):
        pair[1]["pose"]["p"][1] += 0.005


def fault_huge_ipd(records):
    for pair in _rig_pairs(records):
        extra = _rotate(pair[0]["pose"]["o"], [0.07, 0.0, 0.0])
        for axis in range(3):
            pair[0]["pose"]["p"][axis] -= extra[axis]
            pair[1]["pose"]["p"][axis] += extra[axis]


def fault_symmetric_submitted_fov(records):
    for end in _frames_of(records, "end"):
        for view in end["layers"][0]["views"]:
            view["fov"] = {"l": -0.8552, "r": 0.8552, "u": 0.9599, "d": -0.9599}


def fault_right_eye_only_fov(records):
    """The case that caught a real bug in a fleet probe: a check that looked at
    eye 0 alone passed a runtime handing the same frustum to both eyes."""
    for end in _frames_of(records, "end"):
        end["layers"][0]["views"][1]["fov"] = {"l": -0.8552, "r": 0.8552,
                                               "u": 0.9599, "d": -0.9599}


def fault_pose_drift(records):
    # Translate BOTH eyes equally, so separation, vertical offset and orientation
    # are untouched and only the submitted-vs-located comparison can notice.
    for end in _frames_of(records, "end"):
        for view in end["layers"][0]["views"]:
            view["pose"]["p"][2] += 0.01


def fault_same_subimage(records):
    for end in _frames_of(records, "end"):
        views = end["layers"][0]["views"]
        views[1]["sub"] = copy.deepcopy(views[0]["sub"])


def fault_invalid_pose_flags(records):
    for views in _frames_of(records, "views"):
        views["stateFlags"] = 0x4          # tracked, but not valid


def fault_zero_layers(records):
    end = _frames_of(records, "end")[3]
    end["layerCount"] = 0
    end["layers"] = []


def fault_over_budget(records):
    for end in _frames_of(records, "end"):
        extra = [{"type": "other", "structType": 1000010000, "flags": 0}
                 for _ in range(17)]
        end["layers"] = end["layers"] + extra
        end["layerCount"] = len(end["layers"])


def fault_missing_begin(records):
    target = _frames_of(records, "begin")[2]
    records.remove(target)


def fault_display_time_regression(records):
    # Move the located and submitted times together. Changing only the submitted
    # one also breaks the pair-shares-display-time check, which would leave this
    # case unable to say which defect it was demonstrating.
    ends = _frames_of(records, "end")
    regressed = ends[1]["displayTime"] - 5
    ends[5]["displayTime"] = regressed
    _frames_of(records, "views")[5]["displayTime"] = regressed


def fault_no_runtime(records):
    for record in list(records):
        if record.get("r") == "runtime":
            records.remove(record)


def fault_bad_schema(records):
    for record in records:
        if record.get("r") == "header":
            record["schema"] = 99


def fault_display_time_mismatch(records):
    _frames_of(records, "views")[4]["displayTime"] += 11111


def fault_truncated(records):
    for record in records:
        if record.get("r") == "footer":
            record["truncated"] = True


def fault_no_footer(records):
    for record in list(records):
        if record.get("r") == "footer":
            records.remove(record)


def fault_never_focused(records):
    for record in records:
        if record.get("r") == "sessionstate":
            record["state"] = 3           # VISIBLE, never FOCUSED


def fault_mono(records):
    for views in _frames_of(records, "views"):
        del views["views"][1]
    for end in _frames_of(records, "end"):
        del end["layers"][0]["views"][1]
    for config in _frames_of(records, "viewconfig"):
        del config["views"][1]


def fault_partial_depth(records):
    del _first_layer_views(records, 2)[1]["depth"]


# Each fault must flip exactly this set of checks. Declaring the set rather than
# a single name is deliberate: some faults legitimately break more than one
# thing, and pretending otherwise would mean either a weakened assertion or a
# check quietly tuned to make the matrix look tidy.
MATRIX = [
    ("eye_swap", fault_eye_swap, {"eye_order"}),
    ("toe_in", fault_toe_in, {"eyes_parallel"}),
    ("vertical_offset", fault_vertical_offset, {"no_vertical_disparity"}),
    ("huge_ipd", fault_huge_ipd, {"ipd_plausible"}),
    ("symmetric_submitted_fov", fault_symmetric_submitted_fov,
     {"submitted_fov_matches_located"}),
    ("right_eye_only_fov", fault_right_eye_only_fov, {"submitted_fov_matches_located"}),
    ("pose_drift", fault_pose_drift, {"submitted_pose_matches_located"}),
    ("same_subimage", fault_same_subimage, {"eye_subimages_distinct"}),
    ("invalid_pose_flags", fault_invalid_pose_flags, {"no_submit_with_invalid_pose"}),
    ("zero_layers", fault_zero_layers, {"never_submits_zero_layers"}),
    ("over_budget", fault_over_budget, {"layer_budget"}),
    ("missing_begin", fault_missing_begin, {"frame_contract"}),
    ("display_time_regression", fault_display_time_regression, {"frame_loop_advances"}),
    ("display_time_mismatch", fault_display_time_mismatch,
     {"eye_pair_shares_display_time"}),
    ("truncated", fault_truncated, {"no_silent_truncation"}),
    ("no_footer", fault_no_footer, {"no_silent_truncation"}),
    ("never_focused", fault_never_focused, {"session_reached_focused"}),
    ("mono", fault_mono, {"stereo_view_count_is_2", "both_eyes_submitted"}),
    ("partial_depth", fault_partial_depth, {"depth_submission_consistent"}),
    ("no_runtime", fault_no_runtime, {"runtime_identified"}),
    ("bad_schema", fault_bad_schema, {"trace_is_readable"}),
]


# -------------------------------------------------------------------- driver


def write_trace(records, directory, name):
    path = Path(directory) / (name + ".ndjson")
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
    return path


def statuses(path, opts=None):
    trace = xt.Trace(path)
    return {r.name: r.status for r in xt.run_checks(trace, opts or {})}


def main():
    failures = []
    with tempfile.TemporaryDirectory(prefix="xrtape-selftest-") as tmp:
        nominal_path = write_trace(nominal_records(), tmp, "nominal")
        nominal = statuses(nominal_path)

        print("nominal trace")
        bad = sorted(n for n, s in nominal.items() if s != xt.PASS)
        if bad:
            for name in bad:
                print("  FAIL %-34s expected PASS, got %s" % (name, nominal[name]))
            failures.append("nominal trace did not pass every check: %s" % bad)
        else:
            print("  PASS all %d checks pass on a well-formed trace" % len(nominal))
        print("")

        print("falsification matrix")
        for name, mutate, expected in MATRIX:
            records = nominal_records()
            mutate(records)
            path = write_trace(records, tmp, "fault-" + name)
            after = statuses(path)

            flipped = {n for n, s in after.items()
                       if s == xt.FAIL and nominal.get(n) == xt.PASS}
            if flipped == expected:
                print("  PASS %-26s flipped %s" % (name, ", ".join(sorted(flipped))))
                continue

            missing = expected - flipped
            extra = flipped - expected
            detail = []
            if missing:
                detail.append("did NOT flip %s" % ", ".join(sorted(missing)))
            if extra:
                detail.append("also flipped %s" % ", ".join(sorted(extra)))
            print("  FAIL %-26s %s" % (name, "; ".join(detail)))
            failures.append("%s: %s" % (name, "; ".join(detail)))

        # A check that cannot be reached by any fault is untested, and saying so
        # is cheaper than discovering it during an incident.
        covered = set()
        for _, _, expected in MATRIX:
            covered |= expected
        uncovered = sorted(set(xt.CHECK_NAMES) - covered)
        print("")
        if uncovered:
            print("checks with no fault case (untested by this matrix):")
            for name in uncovered:
                print("  ---- %s" % name)
        else:
            print("every check has at least one fault case")

    print("")
    if failures:
        print("SELFTEST FAILED (%d)" % len(failures))
        for line in failures:
            print("  - %s" % line)
        return 1
    print("SELFTEST PASS: %d checks, %d fault cases" % (len(xt.CHECK_NAMES), len(MATRIX)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
