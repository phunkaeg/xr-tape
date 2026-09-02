#!/usr/bin/env python3
"""xr-tape - read a trace, run the check library, return a verdict.

Every check here is a pure function of a parsed trace. That is deliberate: it
is what lets tools/xrtape_selftest.py feed each one a known-bad trace and prove
it can fail. A check that has never been observed failing is decoration.

Checks name the failure-atlas rows they implement so a red line leads straight
to the write-up rather than to a guess.

Stdlib only, and written for Python 3.8+ so it runs on every interpreter in the
fleet rather than only the newest.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

SCHEMA_SUPPORTED = 1

# openxr.h constants, spelled out so a reader does not have to look them up.
SESSION_STATE_FOCUSED = 5
VIEW_STATE_ORIENTATION_VALID = 0x1
VIEW_STATE_POSITION_VALID = 0x2

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


class Result:
    def __init__(self, name, status, detail, atlas=""):
        self.name = name
        self.status = status
        self.detail = detail
        self.atlas = atlas

    def __repr__(self):
        return "<%s %s>" % (self.name, self.status)


# --------------------------------------------------------------------- model


class Frame:
    """One iteration of the frame loop, correlated across four record types."""

    def __init__(self, key):
        self.key = key
        self.wait = None
        self.begin = None
        self.views = None
        self.end = None

    @property
    def display_time(self):
        for record in (self.end, self.views, self.wait):
            if record and record.get("displayTime"):
                return record["displayTime"]
        return None

    @property
    def should_render(self):
        return bool(self.wait and self.wait.get("shouldRender"))

    def projection_layers(self):
        if not self.end:
            return []
        return [layer for layer in self.end.get("layers", [])
                if layer.get("type") == "projection"]


class Trace:
    def __init__(self, path):
        self.path = Path(path)
        self.header = None
        self.runtime = None
        self.instance = None
        self.system = None
        self.viewconfigs = []
        self.sessions = []
        self.swapchains = []
        self.spaces = []
        self.states = []
        self.stamps = []
        self.footer = None
        self.frames = []
        self.malformed = 0
        self._load()

    def _load(self):
        text = self.path.read_text(encoding="utf-8", errors="replace")
        records = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except ValueError:
                # A truncated final line is normal if a process was killed. Count
                # it rather than dropping it silently.
                self.malformed += 1
        self._ingest(records)

    def _ingest(self, records):
        by_key = {}
        order = []
        for record in records:
            kind = record.get("r")
            if kind == "header":
                self.header = record
            elif kind == "runtime":
                self.runtime = record
            elif kind == "instance":
                self.instance = record
            elif kind == "system":
                self.system = record
            elif kind == "viewconfig":
                self.viewconfigs.append(record)
            elif kind == "session":
                self.sessions.append(record)
            elif kind == "swapchain":
                self.swapchains.append(record)
            elif kind == "space":
                self.spaces.append(record)
            elif kind == "sessionstate":
                self.states.append(record)
            elif kind == "stamp":
                self.stamps.append(record)
            elif kind == "footer":
                self.footer = record
            elif kind in ("wait", "begin", "views", "end"):
                # Correlate on the layer's own wait counter, NOT on displayTime.
                # Using displayTime as the key would put a frame whose views were
                # located for one time and submitted for another into two
                # separate frames - and silently turn the check that exists to
                # catch exactly that into a skip.
                #
                # Limitation, stated rather than hidden: an application that
                # waits for frame N+1 on another thread before ending frame N
                # will correlate approximately. See docs/SCHEMA.md.
                key = record.get("seq")
                if key is None:
                    key = "t:%s" % record.get("displayTime")
                if key not in by_key:
                    by_key[key] = Frame(key)
                    order.append(key)
                setattr(by_key[key], kind, record)
        self.frames = [by_key[k] for k in order]

    @property
    def rendered_frames(self):
        return [f for f in self.frames if f.end is not None]

    @property
    def view_count(self):
        for frame in self.frames:
            if frame.views and frame.views.get("views"):
                return len(frame.views["views"])
        for config in self.viewconfigs:
            if config.get("views"):
                return len(config["views"])
        return 0


# ----------------------------------------------------------------- geometry


def quat_conjugate(q):
    return (-q[0], -q[1], -q[2], q[3])


def quat_rotate(q, v):
    """Rotate vector v by quaternion q (x, y, z, w)."""
    qx, qy, qz, qw = q
    vx, vy, vz = v
    # t = 2 * cross(q.xyz, v)
    tx = 2.0 * (qy * vz - qz * vy)
    ty = 2.0 * (qz * vx - qx * vz)
    tz = 2.0 * (qx * vy - qy * vx)
    return (
        vx + qw * tx + (qy * tz - qz * ty),
        vy + qw * ty + (qz * tx - qx * tz),
        vz + qw * tz + (qx * ty - qy * tx),
    )


def quat_dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2] + a[3] * b[3]


def quat_normalize(q):
    norm = math.sqrt(sum(c * c for c in q))
    if norm == 0.0:
        return (0.0, 0.0, 0.0, 1.0)
    return tuple(c / norm for c in q)


def quat_mean(a, b):
    """Mean of two nearby rotations.

    Hemispheres are aligned first: q and -q are the same rotation, and averaging
    across that sign flip produces a direction that is not between either input.
    """
    if quat_dot(a, b) < 0.0:
        b = tuple(-c for c in b)
    return quat_normalize(tuple(x + y for x, y in zip(a, b)))


def eye_baseline_local(view_left, view_right):
    """Left-to-right offset expressed in the head's own frame.

    Comparing raw world positions only works while the head faces down -Z with an
    identity orientation, which is true at the first frame of a simulated run and
    false the moment anybody turns their head. Rotating into head-local space is
    what makes the eye-order and vertical-disparity checks survive a real trace.

    The head frame is the MEAN of the two eye orientations, not the left eye's.
    Using one eye means a toed-in rig rotates the measuring frame as well as the
    thing being measured, and the lateral baseline leaks into the vertical axis -
    so a toe-in fault would be reported as vertical disparity. The falsification
    matrix caught exactly that; with the mean, a symmetric toe-in leaves the
    baseline purely lateral and only the parallelism check objects.
    """
    pl = view_left["pose"]["p"]
    pr = view_right["pose"]["p"]
    delta = (pr[0] - pl[0], pr[1] - pl[1], pr[2] - pl[2])
    head = quat_mean(tuple(view_left["pose"]["o"]), tuple(view_right["pose"]["o"]))
    return quat_rotate(quat_conjugate(head), delta)


def fov_asymmetry(fov):
    return (abs(abs(fov["l"]) - abs(fov["r"])), abs(abs(fov["u"]) - abs(fov["d"])))


# ------------------------------------------------------------------- checks
#
# Signature is check(trace, opts) -> Result. Register at the bottom.


def check_trace_is_readable(trace, opts):
    if trace.header is None:
        return Result("trace_is_readable", FAIL, "no header record; not an xr-tape trace")
    schema = trace.header.get("schema")
    if schema != SCHEMA_SUPPORTED:
        return Result("trace_is_readable", FAIL,
                      "schema %s, this checker understands %s" % (schema, SCHEMA_SUPPORTED))
    detail = "schema %s, %s frames, exe %s" % (
        schema, len(trace.frames), trace.header.get("exe", "?"))
    if trace.malformed:
        detail += ", %d malformed line(s)" % trace.malformed
    return Result("trace_is_readable", PASS, detail)


def check_runtime_identified(trace, opts):
    """A run that silently used the wrong runtime looks exactly like a correct
    one. The trace records the runtime's own answer so the question is settled
    by data rather than by intent. FAIL-XR-006."""
    if not trace.runtime or not trace.runtime.get("name"):
        return Result("runtime_identified", FAIL, "no runtime record", "FAIL-XR-006")
    name = trace.runtime["name"]
    expected = opts.get("expect_runtime")
    if expected and expected.lower() not in name.lower():
        return Result("runtime_identified", FAIL,
                      "runtime is %r, expected %r" % (name, expected), "FAIL-XR-006")
    return Result("runtime_identified", PASS, "runtime %r" % name, "FAIL-XR-006")


def check_no_silent_truncation(trace, opts):
    """A capped recording that reads as a complete one is the same defect class
    as a blank capture that reads as success."""
    if trace.footer is None:
        return Result("no_silent_truncation", FAIL,
                      "no footer: the process did not destroy its instance, so the "
                      "trace may be incomplete and cannot be assumed whole")
    if trace.footer.get("truncated"):
        return Result("no_silent_truncation", FAIL,
                      "recording hit the %s-frame cap; this trace is a prefix, not a run"
                      % trace.footer.get("maxFrames"))
    return Result("no_silent_truncation", PASS,
                  "%s frames recorded, cap %s" % (trace.footer.get("recordedFrames"),
                                                  trace.footer.get("maxFrames")))


def check_session_reached_focused(trace, opts):
    if not trace.states:
        return Result("session_reached_focused", SKIP, "no session-state events recorded",
                      "FAIL-XR-001")
    reached = [s for s in trace.states if s.get("state") == SESSION_STATE_FOCUSED]
    if not reached:
        seen = sorted(set(s.get("state") for s in trace.states))
        return Result("session_reached_focused", FAIL,
                      "never reached FOCUSED; states seen: %s" % seen, "FAIL-XR-001")
    return Result("session_reached_focused", PASS, "reached FOCUSED")


def check_stereo_view_count_is_2(trace, opts):
    count = trace.view_count
    if count == 0:
        return Result("stereo_view_count_is_2", SKIP, "no views located")
    if count != 2:
        return Result("stereo_view_count_is_2", FAIL, "view count is %d" % count)
    return Result("stereo_view_count_is_2", PASS, "2 views")


def check_frame_contract(trace, opts):
    """Wait, begin and end must pair up. XR-005; five of eleven surveyed mods got
    this wrong."""
    if not trace.frames:
        return Result("frame_contract", SKIP, "no frames")
    missing_begin = [f.key for f in trace.frames if f.end is not None and f.begin is None]
    end_without_wait = [f.key for f in trace.frames if f.end is not None and f.wait is None]
    problems = []
    if missing_begin:
        problems.append("%d frame(s) ended without a begin (first: %s)"
                        % (len(missing_begin), missing_begin[0]))
    if end_without_wait:
        problems.append("%d frame(s) ended without a wait (first: %s)"
                        % (len(end_without_wait), end_without_wait[0]))
    if problems:
        return Result("frame_contract", FAIL, "; ".join(problems), "FAIL-XR-007")
    waits = sum(1 for f in trace.frames if f.wait)
    begins = sum(1 for f in trace.frames if f.begin)
    ends = sum(1 for f in trace.frames if f.end)
    return Result("frame_contract", PASS,
                  "wait %d, begin %d, end %d" % (waits, begins, ends), "FAIL-XR-007")


def check_frame_loop_advances(trace, opts):
    """A render loop that stops while the session lives freezes the compositor.
    FAIL-XR-004."""
    rendered = trace.rendered_frames
    if len(rendered) < 2:
        return Result("frame_loop_advances", SKIP, "fewer than two submitted frames",
                      "FAIL-XR-004")
    times = [f.display_time for f in rendered if f.display_time]
    if len(times) < 2:
        return Result("frame_loop_advances", SKIP, "no display times recorded", "FAIL-XR-004")
    regressions = sum(1 for a, b in zip(times, times[1:]) if b <= a)
    if regressions:
        return Result("frame_loop_advances", FAIL,
                      "%d non-advancing display time(s)" % regressions, "FAIL-XR-004")
    return Result("frame_loop_advances", PASS, "%d frames, display time monotonic"
                  % len(rendered), "FAIL-XR-004")


def check_never_submits_zero_layers(trace, opts):
    """Submitting nothing is not the safe default it looks like: the runtime has
    nothing to composite and the view freezes while the flat game runs on."""
    offenders = [f.key for f in trace.rendered_frames
                 if f.should_render and f.end.get("layerCount", 0) == 0]
    if not offenders:
        return Result("never_submits_zero_layers", PASS, "no empty submissions")
    return Result("never_submits_zero_layers", FAIL,
                  "%d frame(s) submitted zero layers while shouldRender was true (first: %s)"
                  % (len(offenders), offenders[0]))


def check_layer_budget(trace, opts):
    """Over-submission returns XR_ERROR_LAYER_LIMIT_EXCEEDED, the runtime drops
    the whole frame, and the headset freezes while the flat game keeps running."""
    if not trace.system or not trace.system.get("maxLayerCount"):
        return Result("layer_budget", SKIP, "system max layer count unknown")
    budget = trace.system["maxLayerCount"]
    worst = 0
    offenders = []
    for frame in trace.rendered_frames:
        count = frame.end.get("layerCount", 0)
        worst = max(worst, count)
        if count > budget:
            offenders.append(frame.key)
    if offenders:
        return Result("layer_budget", FAIL,
                      "%d frame(s) exceeded the %d-layer budget (peak %d)"
                      % (len(offenders), budget, worst))
    return Result("layer_budget", PASS, "peak %d of %d layers" % (worst, budget))


def _paired_views(trace):
    """Yield (frame, submitted_left, submitted_right) for stereo projection layers."""
    for frame in trace.rendered_frames:
        for layer in frame.projection_layers():
            views = layer.get("views", [])
            if len(views) == 2:
                yield frame, views[0], views[1]


def check_both_eyes_submitted(trace, opts):
    """Pair coherence: half a pair reaching the compositor is FAIL-STR-028."""
    layers = [(f, l) for f in trace.rendered_frames for l in f.projection_layers()]
    if not layers:
        return Result("both_eyes_submitted", SKIP, "no projection layers submitted",
                      "FAIL-STR-028")
    bad = [(f.key, len(l.get("views", []))) for f, l in layers
           if len(l.get("views", [])) != 2]
    if bad:
        return Result("both_eyes_submitted", FAIL,
                      "%d projection layer(s) did not carry exactly 2 views (first: frame %s "
                      "with %d)" % (len(bad), bad[0][0], bad[0][1]), "FAIL-STR-028")
    return Result("both_eyes_submitted", PASS, "%d stereo layer(s)" % len(layers),
                  "FAIL-STR-028")


def check_eye_order(trace, opts):
    """The left eye must be to the left. An eye label and an offset sign that are
    both wrong cancel, which is why FAIL-STR-003 survives an eye-swap toggle."""
    samples = list(_paired_views(trace))
    if not samples:
        return Result("eye_order", SKIP, "no stereo pairs", "FAIL-STR-003")
    wrong = 0
    for _, left, right in samples:
        if eye_baseline_local(left, right)[0] <= 0.0:
            wrong += 1
    if wrong:
        return Result("eye_order", FAIL,
                      "%d of %d pairs place view 0 to the RIGHT of view 1"
                      % (wrong, len(samples)), "FAIL-STR-003")
    return Result("eye_order", PASS, "view 0 is left in all %d pairs" % len(samples),
                  "FAIL-STR-003")


def check_ipd_plausible(trace, opts):
    samples = list(_paired_views(trace))
    if not samples:
        return Result("ipd_plausible", SKIP, "no stereo pairs")
    ipds = []
    for _, left, right in samples:
        local = eye_baseline_local(left, right)
        ipds.append(math.sqrt(sum(c * c for c in local)))
    lo, hi = min(ipds), max(ipds)
    if lo < 0.045 or hi > 0.080:
        return Result("ipd_plausible", FAIL,
                      "separation ranges %.4f..%.4f m, outside 0.045..0.080" % (lo, hi))
    return Result("ipd_plausible", PASS, "separation %.4f..%.4f m" % (lo, hi))


def check_no_vertical_disparity(trace, opts):
    """Vertical disparity does not fuse. It is also invisible on a monitor."""
    samples = list(_paired_views(trace))
    if not samples:
        return Result("no_vertical_disparity", SKIP, "no stereo pairs", "FAIL-STR-002")
    worst = 0.0
    worst_key = None
    for frame, left, right in samples:
        _, dy, dz = eye_baseline_local(left, right)
        offset = max(abs(dy), abs(dz))
        if offset > worst:
            worst, worst_key = offset, frame.key
    if worst > 0.002:
        return Result("no_vertical_disparity", FAIL,
                      "vertical/depth eye offset up to %.4f m (frame %s)" % (worst, worst_key),
                      "FAIL-STR-002")
    return Result("no_vertical_disparity", PASS, "max off-axis offset %.5f m" % worst,
                  "FAIL-STR-002")


def check_eyes_parallel(trace, opts):
    """Toe-in produces double vision at the edges and is not how HMD optics work."""
    samples = list(_paired_views(trace))
    if not samples:
        return Result("eyes_parallel", SKIP, "no stereo pairs")
    worst = 1.0
    for _, left, right in samples:
        worst = min(worst, abs(quat_dot(left["pose"]["o"], right["pose"]["o"])))
    # dot of 0.9999 is about 1.6 degrees of relative rotation.
    if worst < 0.9999:
        degrees = math.degrees(2.0 * math.acos(min(1.0, worst)))
        return Result("eyes_parallel", FAIL,
                      "eye orientations differ by up to %.2f degrees" % degrees)
    return Result("eyes_parallel", PASS, "orientation dot >= %.6f" % worst)


def _fov_delta(a, b):
    return max(abs(a[k] - b[k]) for k in ("l", "r", "u", "d"))


def check_submitted_fov_matches_located(trace, opts):
    """The declared projection must be the one that was rendered.

    This is the check no in-process test can perform on itself, and the reason
    the recorder sits at the loader boundary: it compares what the runtime was
    TOLD against what the runtime SAID, from outside both. FAIL-STR-009.
    """
    worst, worst_key = 0.0, None
    compared = 0
    for frame, left, right in _paired_views(trace):
        located = frame.views.get("views") if frame.views else None
        if not located or len(located) != 2:
            continue
        compared += 1
        for submitted, ref in ((left, located[0]), (right, located[1])):
            delta = _fov_delta(submitted["fov"], ref["fov"])
            if delta > worst:
                worst, worst_key = delta, frame.key
    if compared == 0:
        return Result("submitted_fov_matches_located", SKIP,
                      "no frame carried both located and submitted views", "FAIL-STR-009")
    tolerance = opts.get("fov_tolerance", 1e-5)
    if worst > tolerance:
        return Result("submitted_fov_matches_located", FAIL,
                      "submitted FOV differs from located by up to %.6g rad (frame %s), "
                      "tolerance %.6g" % (worst, worst_key, tolerance), "FAIL-STR-009")
    return Result("submitted_fov_matches_located", PASS,
                  "%d frames, max difference %.3g rad" % (compared, worst), "FAIL-STR-009")


def check_submitted_pose_matches_located(trace, opts):
    """Log the value that reaches the submitted matrix, not the one computed.
    FAIL-XR-011."""
    worst, worst_key = 0.0, None
    compared = 0
    for frame, left, right in _paired_views(trace):
        located = frame.views.get("views") if frame.views else None
        if not located or len(located) != 2:
            continue
        compared += 1
        for submitted, ref in ((left, located[0]), (right, located[1])):
            delta = max(abs(a - b) for a, b in
                        zip(submitted["pose"]["p"], ref["pose"]["p"]))
            delta = max(delta, max(abs(a - b) for a, b in
                                   zip(submitted["pose"]["o"], ref["pose"]["o"])))
            if delta > worst:
                worst, worst_key = delta, frame.key
    if compared == 0:
        return Result("submitted_pose_matches_located", SKIP,
                      "no frame carried both located and submitted views", "FAIL-XR-011")
    tolerance = opts.get("pose_tolerance", 1e-5)
    if worst > tolerance:
        return Result("submitted_pose_matches_located", FAIL,
                      "submitted pose differs from located by up to %.6g (frame %s), "
                      "tolerance %.6g" % (worst, worst_key, tolerance), "FAIL-XR-011")
    return Result("submitted_pose_matches_located", PASS,
                  "%d frames, max difference %.3g" % (compared, worst), "FAIL-XR-011")


def check_eye_subimages_distinct(trace, opts):
    """Both eyes reading identical pixels is a duplicated mono render wearing a
    stereo costume - two images that exist and have no depth. FAIL-STR-001."""
    samples = list(_paired_views(trace))
    if not samples:
        return Result("eye_subimages_distinct", SKIP, "no stereo pairs", "FAIL-STR-001")
    identical = []
    for frame, left, right in samples:
        if left.get("sub") == right.get("sub"):
            identical.append(frame.key)
    if identical:
        return Result("eye_subimages_distinct", FAIL,
                      "%d frame(s) sent both eyes the same swapchain, array index and rect "
                      "(first: %s)" % (len(identical), identical[0]), "FAIL-STR-001")
    return Result("eye_subimages_distinct", PASS,
                  "eye sub-images differ in all %d pairs" % len(samples), "FAIL-STR-001")


def check_no_submit_with_invalid_pose(trace, opts):
    """A pose whose validity bits are clear is not a pose. Submitting one leaves
    the world displaced by a fixed offset after a tracking glitch. FAIL-XR-013."""
    required = VIEW_STATE_ORIENTATION_VALID | VIEW_STATE_POSITION_VALID
    offenders = []
    checked = 0
    for frame in trace.rendered_frames:
        if not frame.views or not frame.projection_layers():
            continue
        checked += 1
        flags = frame.views.get("stateFlags", 0)
        if (flags & required) != required:
            offenders.append((frame.key, flags))
    if checked == 0:
        return Result("no_submit_with_invalid_pose", SKIP, "no located views on submitted frames",
                      "FAIL-XR-013")
    if offenders:
        return Result("no_submit_with_invalid_pose", FAIL,
                      "%d frame(s) submitted with incomplete view-state flags "
                      "(first: frame %s flags 0x%x)"
                      % (len(offenders), offenders[0][0], offenders[0][1]), "FAIL-XR-013")
    return Result("no_submit_with_invalid_pose", PASS,
                  "%d submitted frames all had valid orientation and position" % checked,
                  "FAIL-XR-013")


def check_eye_pair_shares_display_time(trace, opts):
    """Eyes rendered at different ages ghost during head motion. FAIL-STR-006."""
    mismatched = []
    compared = 0
    for frame in trace.rendered_frames:
        if not frame.views:
            continue
        located = frame.views.get("displayTime")
        submitted = frame.end.get("displayTime")
        if not located or not submitted:
            continue
        compared += 1
        if located != submitted:
            mismatched.append((frame.key, located, submitted))
    if compared == 0:
        return Result("eye_pair_shares_display_time", SKIP, "no comparable display times",
                      "FAIL-STR-006")
    if mismatched:
        key, located, submitted = mismatched[0]
        return Result("eye_pair_shares_display_time", FAIL,
                      "%d frame(s) submitted a display time different from the one the views "
                      "were located for (first: frame %s located %s, submitted %s)"
                      % (len(mismatched), key, located, submitted), "FAIL-STR-006")
    return Result("eye_pair_shares_display_time", PASS,
                  "%d frames located and submitted at the same display time" % compared,
                  "FAIL-STR-006")


def check_depth_submission_consistent(trace, opts):
    """SOMAVR submits depth layers, and the runtime it was first tested against
    could not see them at all. Intermittent depth is worse than none."""
    with_depth, without = 0, 0
    for _, left, right in _paired_views(trace):
        for view in (left, right):
            if "depth" in view:
                with_depth += 1
            else:
                without += 1
    if with_depth == 0:
        return Result("depth_submission_consistent", SKIP, "no depth layers submitted")
    if without:
        return Result("depth_submission_consistent", FAIL,
                      "depth attached to %d views but missing from %d" % (with_depth, without))
    return Result("depth_submission_consistent", PASS, "depth on all %d views" % with_depth)


CHECKS = [
    check_trace_is_readable,
    check_runtime_identified,
    check_no_silent_truncation,
    check_session_reached_focused,
    check_stereo_view_count_is_2,
    check_frame_contract,
    check_frame_loop_advances,
    check_never_submits_zero_layers,
    check_layer_budget,
    check_both_eyes_submitted,
    check_eye_order,
    check_ipd_plausible,
    check_no_vertical_disparity,
    check_eyes_parallel,
    check_submitted_fov_matches_located,
    check_submitted_pose_matches_located,
    check_eye_subimages_distinct,
    check_no_submit_with_invalid_pose,
    check_eye_pair_shares_display_time,
    check_depth_submission_consistent,
]

CHECK_NAMES = [fn.__name__.replace("check_", "") for fn in CHECKS]


def run_checks(trace, opts=None):
    opts = opts or {}
    return [fn(trace, opts) for fn in CHECKS]


# ---------------------------------------------------------------------- cli


def main(argv=None):
    parser = argparse.ArgumentParser(description="Check an xr-tape trace.")
    parser.add_argument("trace", help="path to a .ndjson trace")
    parser.add_argument("--expect-runtime", default=None,
                        help="fail unless the recorded runtime name contains this")
    parser.add_argument("--require", action="append", default=[], metavar="CHECK",
                        help="treat a SKIP of this check as a failure (repeatable, "
                             "'all' for every check)")
    parser.add_argument("--json", action="store_true", help="emit machine-readable results")
    parser.add_argument("--quiet", action="store_true", help="only print failures")
    args = parser.parse_args(argv)

    path = Path(args.trace)
    if not path.is_file():
        print("xr-tape: no such trace: %s" % path, file=sys.stderr)
        return 2

    trace = Trace(path)
    results = run_checks(trace, {"expect_runtime": args.expect_runtime})

    required = set(args.require)
    if "all" in required:
        required = set(CHECK_NAMES)
    for result in results:
        if result.status == SKIP and result.name in required:
            result.status = FAIL
            result.detail = "required but skipped: " + result.detail

    failed = [r for r in results if r.status == FAIL]
    skipped = [r for r in results if r.status == SKIP]

    if args.json:
        print(json.dumps({
            "trace": str(path),
            "runtime": (trace.runtime or {}).get("name"),
            "frames": len(trace.frames),
            "results": [{"name": r.name, "status": r.status, "detail": r.detail,
                         "atlas": r.atlas} for r in results],
            "passed": len(results) - len(failed) - len(skipped),
            "failed": len(failed),
            "skipped": len(skipped),
        }, indent=2))
        return 1 if failed else 0

    print("xr-tape %s" % path.name)
    if trace.header:
        print("  exe      %s" % trace.header.get("exe"))
    if trace.runtime:
        print("  runtime  %s" % trace.runtime.get("name"))
    if trace.sessions:
        print("  binding  %s" % trace.sessions[0].get("binding"))
    print("  frames   %d submitted" % len(trace.rendered_frames))
    print("")

    for result in results:
        if args.quiet and result.status == PASS:
            continue
        suffix = ("  [%s]" % result.atlas) if result.atlas else ""
        print("  %-4s %-32s %s%s" % (result.status, result.name, result.detail, suffix))

    print("")
    print("  %d passed, %d failed, %d skipped"
          % (len(results) - len(failed) - len(skipped), len(failed), len(skipped)))
    # Skips are printed, counted and can be promoted to failures with --require.
    # An earlier tool in this fleet let a missing field match no pattern, so
    # nothing objected, and a menu run passed: absence of evidence read as
    # evidence of validity.
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
