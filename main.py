"""Forecast a game launch day's hourly Twitch viewers, hours 2..24.

Design (original, written for this entry):
  * Work in log space.  For every comparable launch we know the full
    release-day curve, so we form per-hour log-ratios r_h = ln(v_h) - ln(v_1)
    against that launch's own hour 1.
  * Comparables are weighted by similarity to the target case: hour-1
    scale, pre-release baseline level, pre-release ramp into launch,
    viewers-per-channel at hour 1, and unlock style (rolling midnight
    local vs one simultaneous global unlock vs early-access contaminated).
  * Each forecast hour's five quantiles are taken empirically from the
    weighted distribution of comparable log-ratios for that hour, shrunk
    toward the pooled (unweighted) quantiles, winsorized, lightly smoothed
    across neighbouring hours, then mapped back through the target's
    hour-1 print.
  * The pseudo "comparable" that is really the target category's own
    pre-release history (its unlock text says it is not a launch) is
    excluded, as is any comparable without a usable hour 1.

Standard library only, deterministic, no network, no model calls.
"""

import json
import math
import os
import sys

# ---------------------------------------------------------------- tunables
# Feature weights for the similarity distance (squared log-scale units).
W_SCALE = 2.00    # log10 hour-1 viewers
W_BASE = 0.45     # log10 pre-release baseline (days -7..-2)
W_RAMP = 0.60     # pre-release ramp: last-24h median vs baseline
W_VPC = 0.35      # log10 viewers per channel at hour 1
W_CLASS = 0.70    # unlock-class mismatch penalty (added in quadrature)
BANDWIDTH = 0.85  # Gaussian kernel width on the total distance
SHRINK = 1.00     # weight on the similarity-weighted quantiles vs pooled
SPREAD = 1.08     # multiplier on (quantile - median) in log-ratio space
CLIP_LO = -8.00   # safety winsorize on comparable log-ratios (rarely binds)
CLIP_HI = 8.00
SMOOTH = 0.22     # neighbour mixing when smoothing quantiles across hours
MIN_H1 = 20.0     # comparable hour-1 viewers below this are unusable
Z = [-1.6448536269514722, -0.6744897501960817, 0.0,
     0.6744897501960817, 1.6448536269514722]

EPOCH_ORD = None  # set lazily


def parse_ts(s):
    """ISO-8601 UTC string -> epoch seconds (float), or None."""
    if not s or not isinstance(s, str):
        return None
    try:
        t = s.strip()
        if t.endswith("Z"):
            t = t[:-1] + "+00:00"
        # datetime is stdlib; imported lazily to keep startup lean.
        from datetime import datetime
        dt = datetime.fromisoformat(t)
        if dt.tzinfo is None:
            from datetime import timezone
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def hour_offset(ts, base_ts):
    """Whole-hour offset of ts from base_ts, or None if not hour-aligned."""
    if ts is None or base_ts is None:
        return None
    d = ts - base_ts
    h = int(round(d / 3600.0))
    if abs(d - h * 3600.0) > 120.0:
        return None
    return h


def median(vals):
    if not vals:
        return None
    s = sorted(vals)
    n = len(s)
    m = n // 2
    if n % 2:
        return s[m]
    return 0.5 * (s[m - 1] + s[m])


def unlock_class(text):
    """0 = rolling midnight local, 1 = simultaneous/global, 2 = other/unknown.
    Returns (cls, early_access_flag)."""
    t = (text or "").lower()
    early = False
    if "no early access" not in t and "no early-access" not in t:
        if "early access" in t or "early-access" in t:
            early = True
    if "midnight local" in t or "rolling" in t or "midnight in" in t:
        cls = 0
    elif ("simultaneous" in t or "global unlock" in t or "single global" in t
          or "one global" in t or "same time" in t):
        cls = 1
    else:
        cls = 2
    return cls, early


def features_from_series(pre_points, h1_viewers, h1_channels):
    """pre_points: list of (offset_hours_from_day_start, viewers) with
    offset < 0.  Returns feature tuple or None."""
    base_vals = [v for (o, v) in pre_points if -168 <= o < -24 and v is not None]
    ramp_vals = [v for (o, v) in pre_points if -24 <= o < 0 and v is not None]
    if len(base_vals) < 6:
        base_vals = [v for (o, v) in pre_points if -336 <= o < -24 and v is not None]
    base = median(base_vals)
    ramp = median(ramp_vals)
    f_scale = math.log10(max(h1_viewers, 1.0))
    f_base = math.log10((base if base is not None else 0.0) + 1.0)
    if ramp is not None and base is not None:
        f_ramp = math.log10(ramp + 1.0) - math.log10(base + 1.0)
    elif ramp is not None:
        f_ramp = math.log10(ramp + 1.0) - f_base
    else:
        f_ramp = 0.0
    if h1_channels and h1_channels > 0:
        f_vpc = math.log10(max(h1_viewers, 1.0) / float(h1_channels))
    else:
        f_vpc = 1.5
    return (f_scale, f_base, f_ramp, f_vpc)


def weighted_quantile(pairs, tau):
    """pairs: list of (value, weight>0).  Linear-interpolation quantile."""
    if not pairs:
        return None
    pairs = sorted(pairs)
    total = 0.0
    for _, w in pairs:
        total += w
    if total <= 0:
        return pairs[len(pairs) // 2][0]
    target = tau * total
    cum = 0.0
    prev_v = pairs[0][0]
    prev_c = 0.0
    for v, w in pairs:
        cum += w
        if cum >= target:
            if cum == prev_c:
                return v
            frac = (target - prev_c) / (cum - prev_c)
            return prev_v + frac * (v - prev_v)
        prev_v = v
        prev_c = cum
    return pairs[-1][0]


def extract_comparable(comp):
    """Return dict with day curve {hour_index0: viewers}, features, class."""
    if not isinstance(comp, dict):
        return None
    unlock_text = comp.get("unlock") or ""
    name = comp.get("name") or ""
    if "not a launch" in unlock_text.lower():
        return None
    if "category before release" in name.lower():
        return None
    base_ts = parse_ts(comp.get("day_start"))
    if base_ts is None:
        return None
    hourly = comp.get("hourly") or []
    day = {}
    pre = []
    h1_ch = None
    for row in hourly:
        if not isinstance(row, dict):
            continue
        off = hour_offset(parse_ts(row.get("from")), base_ts)
        if off is None:
            continue
        v = row.get("viewers")
        if v is None:
            continue
        try:
            v = float(v)
        except Exception:
            continue
        if off == 0:
            h1_ch = row.get("channels")
        if 0 <= off < 24:
            if v > 0:
                day[off] = v
        elif off < 0:
            pre.append((off, v))
    h1 = day.get(0)
    if h1 is None or h1 < MIN_H1:
        return None
    try:
        h1_ch = float(h1_ch) if h1_ch is not None else None
    except Exception:
        h1_ch = None
    feats = features_from_series(pre, h1, h1_ch)
    cls, early = unlock_class(unlock_text + " " + (comp.get("platforms") or ""))
    return {"day": day, "h1": h1, "feats": feats, "cls": cls,
            "early": early, "name": name}


def target_features(case, p, p_ch):
    base_ts = parse_ts(case.get("day_start"))
    pre = []
    hist = case.get("history_hourly") or case.get("history") or []
    for row in hist:
        if not isinstance(row, dict):
            continue
        ts = parse_ts(row.get("from") or row.get("at"))
        off = hour_offset(ts, base_ts)
        if off is None or off >= 0:
            continue
        v = row.get("viewers")
        if v is None:
            continue
        try:
            pre.append((off, float(v)))
        except Exception:
            continue
    feats = features_from_series(pre, p, p_ch)
    cls, early = unlock_class(case.get("notes") or "")
    return feats, cls, early


def sim_weight(tf, tcls, tearly, comp):
    cf = comp["feats"]
    if tf is None or cf is None:
        d2 = 1.0
    else:
        d2 = (W_SCALE * (tf[0] - cf[0]) ** 2
              + W_BASE * (tf[1] - cf[1]) ** 2
              + W_RAMP * (tf[2] - cf[2]) ** 2
              + W_VPC * (tf[3] - cf[3]) ** 2)
    if tcls != comp["cls"]:
        d2 += W_CLASS
        if tcls == 2 or comp["cls"] == 2:
            d2 -= 0.45 * W_CLASS  # unknown vs known is a smaller mismatch
    if tearly != comp["early"]:
        d2 += 0.55
    return math.exp(-d2 / (2.0 * BANDWIDTH * BANDWIDTH))


def baseline_row(p, hour):
    s = 0.15 * math.sqrt(max(hour - 1, 1))
    return [p * math.exp(z * s) for z in Z]


def forecast_case(case):
    """Return list of (hour, [q05..q95]) for the case's forecast hours."""
    levels = case.get("levels") or [0.05, 0.25, 0.5, 0.75, 0.95]
    levels = [float(x) for x in levels]
    hours = []
    for row in case.get("forecast") or []:
        try:
            hours.append(int(row.get("hour")))
        except Exception:
            continue
    if not hours:
        hours = list(range(2, 25))
    hours = sorted(set(hours))

    p = None
    p_ch = None
    known = case.get("known") or []
    for row in known:
        if isinstance(row, dict) and row.get("viewers") is not None:
            try:
                p = float(row["viewers"])
                p_ch = row.get("channels")
            except Exception:
                p = None
    if p is None or not (p > 0) or not math.isfinite(p):
        # fall back to the last history point, then to a neutral level
        hist = case.get("history") or []
        for row in reversed(hist):
            if isinstance(row, dict) and row.get("viewers"):
                try:
                    p = float(row["viewers"])
                    break
                except Exception:
                    pass
    if p is None or not (p > 0) or not math.isfinite(p):
        p = 1000.0
    p = max(p, 1.0)
    try:
        p_ch = float(p_ch) if p_ch is not None else None
    except Exception:
        p_ch = None

    comps = []
    for comp in case.get("comparables") or []:
        ec = extract_comparable(comp)
        if ec is not None:
            comps.append(ec)

    if len(comps) < 5:
        return [(h, baseline_row(p, h)) for h in hours]

    tf, tcls, tearly = target_features(case, p, p_ch)
    weights = [sim_weight(tf, tcls, tearly, c) for c in comps]
    logp = math.log(p)

    # Per forecast hour: weighted + pooled quantiles of log-ratios.
    raw = {}  # hour -> list of log-ratio quantiles (in levels order)
    for h in hours:
        idx = h - 1  # hour 2 -> offset 1 from day_start
        wpairs = []
        ppairs = []
        for c, w in zip(comps, weights):
            v = c["day"].get(idx)
            if v is None or v <= 0:
                continue
            r = math.log(v) - math.log(c["h1"])
            if r < CLIP_LO:
                r = CLIP_LO
            elif r > CLIP_HI:
                r = CLIP_HI
            wpairs.append((r, w))
            ppairs.append((r, 1.0))
        if len(ppairs) < 5:
            raw[h] = None
            continue
        qs = []
        for tau in levels:
            qw = weighted_quantile(wpairs, tau)
            qp = weighted_quantile(ppairs, tau)
            qs.append(SHRINK * qw + (1.0 - SHRINK) * qp)
        raw[h] = qs

    # Fill any missing hour from the baseline shape in log-ratio space.
    for h in hours:
        if raw.get(h) is None:
            s = 0.15 * math.sqrt(max(h - 1, 1))
            raw[h] = [z * s for z in Z]

    # Spread scaling around each hour's median (index of level closest .5).
    mid_i = min(range(len(levels)), key=lambda i: abs(levels[i] - 0.5))
    for h in hours:
        qs = raw[h]
        med = qs[mid_i]
        raw[h] = [med + SPREAD * (q - med) for q in qs]

    # Light smoothing across adjacent hours, per quantile level.
    hs = sorted(raw.keys())
    if SMOOTH > 0 and len(hs) >= 3:
        sm = {}
        for i, h in enumerate(hs):
            cur = raw[h]
            if i == 0 or i == len(hs) - 1:
                sm[h] = cur
                continue
            prev = raw[hs[i - 1]]
            nxt = raw[hs[i + 1]]
            sm[h] = [cur[j] * (1.0 - SMOOTH)
                     + 0.5 * SMOOTH * (prev[j] + nxt[j])
                     for j in range(len(cur))]
        raw = sm

    out = []
    for h in hours:
        qs = []
        for r in raw[h]:
            v = math.exp(logp + r)
            if not math.isfinite(v) or v <= 0:
                v = p
            v = min(max(v, 1.0), 1e9)
            qs.append(v)
        qs.sort()
        # round for stable, compact output
        qs = [round(v, 2) for v in qs]
        out.append((h, qs))
    return out


def main():
    in_dir = os.environ.get("MAKEFUN_INPUT_DIR") or "/input"
    out_dir = os.environ.get("MAKEFUN_OUTPUT_DIR") or "/output"
    try:
        names = sorted(os.listdir(in_dir))
    except Exception:
        return 0
    try:
        os.makedirs(out_dir, exist_ok=True)
    except Exception:
        pass
    for name in names:
        if not name.endswith(".in"):
            continue
        path = os.path.join(in_dir, name)
        if not os.path.isfile(path):
            continue
        cid = name[:-3]
        try:
            with open(path, "r", encoding="utf-8") as fh:
                case = json.load(fh)
            rows = forecast_case(case)
            payload = {"hours": [{"hour": h, "q": qs} for h, qs in rows]}
            text = json.dumps(payload, separators=(",", ":"), sort_keys=False)
        except Exception:
            # Last-resort valid output: flat at 1000 viewers.
            payload = {"hours": [{"hour": h, "q": [1000.0] * 5}
                                 for h in range(2, 25)]}
            text = json.dumps(payload, separators=(",", ":"))
        try:
            with open(os.path.join(out_dir, cid + ".out"), "w",
                      encoding="utf-8") as fh:
                fh.write(text)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
