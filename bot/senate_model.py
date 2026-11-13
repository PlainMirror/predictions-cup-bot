"""Senate control from race-level probabilities, with correlated (national-swing) errors.

One-factor Gaussian copula: race i goes Republican when  sqrt(rho)*Z + sqrt(1-rho)*e_i < Phi^-1(pR_i).
Z is the shared national environment; rho is how much races move together. Real election-forecast
models imply rho around 0.4-0.7 for Senate races. We back out the rho that reproduces a target control
price (e.g. Kalshi's), then use it to price anything that depends on the joint outcome.

Seat arithmetic (checked against my race-by-race research notes on 2026-10-10):
  119th Congress: 53 R, 45 D + 2 I who caucus with D. 35 seats are up in 2026 (33 Class II + OH and FL
  specials): 22 R-held, 13 D-held. Not up: 31 R, 34 D/I.
  VP is a Republican, so R controls at 50 seats; D needs 51.
"""
from __future__ import annotations

import random
from statistics import NormalDist

N = NormalDist()
NOT_UP = {"R": 31, "D": 34}
R_CONTROL_AT = 50
D_CONTROL_AT = 51


def simulate(races: dict[str, dict], rho: float, sims: int = 40000, seed: int = 7,
             independents_caucus_with: str | None = None) -> dict:
    """races: state -> {"R": pR, "D": pD, "OTHER": pOther} (needn't sum exactly to 1).

    Returns P(R control), P(D control), P(neither), seat distribution, and per-race win rates
    conditional on each control outcome (handy for scenario baskets)."""
    rng = random.Random(seed)
    states = sorted(races)
    thresholds, nonr_label = [], []
    for s in states:
        r = races[s]
        tot = (r.get("R") or 0) + (r.get("D") or 0) + (r.get("OTHER") or 0)
        pr = min(max((r.get("R") or 0) / tot if tot else 0.5, 1e-4), 1 - 1e-4)
        thresholds.append(N.inv_cdf(pr))
        nonr_label.append("D" if (r.get("D") or 0) >= (r.get("OTHER") or 0) else "OTHER")
    a, b = rho ** 0.5, (1 - rho) ** 0.5
    counts = {"R": 0, "D": 0, "NEITHER": 0}
    seat_hist: dict[int, int] = {}
    cond = {k: [0] * len(states) for k in ("R", "D", "NEITHER")}
    for _ in range(sims):
        z = rng.gauss(0, 1)
        r_seats = d_seats = 0
        wins_r = []
        for t, lab in zip(thresholds, nonr_label):
            is_r = a * z + b * rng.gauss(0, 1) < t
            wins_r.append(is_r)
            if is_r:
                r_seats += 1
            elif lab == "D" or independents_caucus_with == "D":
                d_seats += 1
            elif independents_caucus_with == "R":
                r_seats += 1
        r_total, d_total = NOT_UP["R"] + r_seats, NOT_UP["D"] + d_seats
        outcome = "R" if r_total >= R_CONTROL_AT else "D" if d_total >= D_CONTROL_AT else "NEITHER"
        counts[outcome] += 1
        seat_hist[r_total] = seat_hist.get(r_total, 0) + 1
        for i, w in enumerate(wins_r):
            cond[outcome][i] += w
    p = {k: v / sims for k, v in counts.items()}
    cond_pr = {k: {s: (cond[k][i] / counts[k] if counts[k] else None) for i, s in enumerate(states)} for k in cond}
    return {"p": p, "rho": rho, "r_seat_hist": dict(sorted(seat_hist.items())),
            "p_r_win_given": cond_pr}


def implied_rho(races: dict[str, dict], target_p_r_control: float, sims: int = 20000) -> tuple[float, float]:
    """Bisection on rho so the model's P(R control) matches a market price. Returns (rho, achieved)."""
    lo, hi = 0.0, 0.95
    f = lambda rho: simulate(races, rho, sims)["p"]["R"]
    flo, fhi = f(lo), f(hi)
    # P(R control) moves monotonically toward 0.5-ish tails as rho grows; pick the direction from the ends.
    if (flo - target_p_r_control) * (fhi - target_p_r_control) > 0:
        best = min((lo, flo), (hi, fhi), key=lambda x: abs(x[1] - target_p_r_control))
        return best
    for _ in range(14):
        mid = (lo + hi) / 2
        fm = f(mid)
        if (flo - target_p_r_control) * (fm - target_p_r_control) <= 0:
            hi, fhi = mid, fm
        else:
            lo, flo = mid, fm
    return (lo + hi) / 2, f((lo + hi) / 2)


def races_from_kalshi(lookup: dict) -> dict[str, dict]:
    out = {}
    for (office, place), race in lookup.items():
        if office != "senate":
            continue
        out[place] = {"R": (race.get("R") or {}).get("mid"), "D": (race.get("D") or {}).get("mid"),
                      "OTHER": race.get("other_mid", 0.0)}
    return out


if __name__ == "__main__":
    from pathlib import Path
    import kalshi
    lookup = kalshi.as_lookup(kalshi.fetch_reference(Path(__file__).parent.parent / "data" / "kalshi_reference.json"))
    races = races_from_kalshi(lookup)
    print(f"{len(races)} Senate races priced")
    target = lookup[("control", "senate")]["R"]["mid"]
    for rho in (0.0, 0.3, 0.5, 0.7):
        res = simulate(races, rho)
        print(f"rho={rho:.1f}  P(R control)={res['p']['R']:.3f}  P(D)={res['p']['D']:.3f}  P(neither)={res['p']['NEITHER']:.3f}")
    rho, got = implied_rho(races, target)
    print(f"Kalshi P(R Senate)={target:.3f} -> implied rho={rho:.2f} (model {got:.3f})")
    res = simulate(races, rho)
    print("R seat distribution:", {k: round(v / 40000, 3) for k, v in res["r_seat_hist"].items() if v > 200})
