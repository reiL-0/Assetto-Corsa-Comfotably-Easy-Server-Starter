"""Knockout qualifying like Formula 1's (Q1, Q2, Q3): pure logic, no I/O.

How the 2026 F1 format works (FIA rules as the press reports them: Q1 18 min with every car, the slowest 6 are out; Q2 15 min with 16, the slowest 6 are out; Q3 13 min with 10, for pole; with 20 cars the cuts are 5 and 5, with
24 cars 7 and 7): every stage is a qualifying session; the cars that did not make the cut stay in the pits for the next one and keep the place their own stage gave them; the grid is the Q3 order, then the Q2 casualties by
their Q2 times, then the Q1 casualties by their Q1 times.

What this module does for a server with a sequence like practice, Q1, Q2, Q3, practice (to arrange the grid), race:
- `field_sizes`: how many cars run each stage (the F1 numbers, scaled for a small server);
- `run`: from the best lap each driver set in every stage, who is out after each stage (they are held in the pits for the next one) and the final grid;
- the manager reads the best laps from the result files of the stages (clean laps, as the site classifies them) and the grid goes into the times of the practice before the race (app/gridtimes.py).

ponytail: a tie keeps the driver who ranked better in the stage before (F1 gives it to who set the time first; the result files do not tell that here); a driver with no time in a stage ranks behind everybody with a time,
in the order of the stage before (a rule of this server, not checked against the FIA text); a driver who did not run Q1 is not an entrant: he goes last and never runs a later stage.
"""

from __future__ import annotations

from dataclasses import dataclass, field

F1_Q3_CARS = 10


def field_sizes(n: int, advance: list[int] | None = None, cuts: list[int | None] | None = None) -> list[int]:
    """Cars that RUN each stage: [n, through to Q2, through to Q3]. The F1 numbers (22 cars: 22, 16, 10; 20: 20, 15, 10; 24: 24, 17, 10) and, for a small server, half the entrants reach Q3 (16: 16, 12, 8; 8: 8, 6, 4);
    fewer than 4 entrants: nobody is cut. `advance` = [to Q2, to Q3] fixes them by hand (each at least 1 and no more than the stage before). `cuts` = [cars cut after Q1, cars cut after Q2] (None for one = the automatic
    cut): the director's own numbers for a grid bigger or smaller than F1's; a cut that would leave nobody in the next stage is held back to leave one."""
    if advance is not None:
        sizes = [n, *advance]
        if len(advance) != 2 or any(a < 1 for a in advance) or any(b > a for a, b in zip(sizes, sizes[1:])):
            raise ValueError(f"advance {advance} does not fit {n} entrants")
        return sizes
    if n < 4:
        auto = [n, n, n]
    else:
        q3 = min(F1_Q3_CARS, -(-n // 2))   # ceil(n / 2), at most 10
        cut_first = -(-(n - q3) // 2)       # the first cut takes the larger half when it is odd
        auto = [n, n - cut_first, q3]
    if cuts is None:
        return auto
    if len(cuts) != 2 or any(c is not None and c < 0 for c in cuts):
        raise ValueError(f"cuts {cuts}: two numbers (or None), none negative")
    c1 = auto[0] - auto[1] if cuts[0] is None else cuts[0]
    c2 = auto[1] - auto[2] if cuts[1] is None else cuts[1]
    c1 = max(0, min(c1, n - 1))
    c2 = max(0, min(c2, n - c1 - 1))
    return [n, n - c1, n - c1 - c2]


@dataclass
class Stage:
    name: str
    ran: list[str]                                  # the Steam IDs that ran it
    order: list[str]                                # its classification, best first (no time last)
    times: dict[str, int | None]                    # best lap in ms of each (None: no valid lap)
    out: list[str] = field(default_factory=list)    # cut after it, in the order they take on the grid
    held: list[str] = field(default_factory=list)   # who must be held in the pits for it (cut in an earlier stage)


def classify(ran: list[str], times: dict[str, int | None], before: dict[str, int]) -> list[str]:
    """Best lap first; no valid lap last; a tie (or two without a time) by where they ranked in the stage before (`before`: guid -> place), else as given."""
    inf = float("inf")
    return sorted(ran, key=lambda g: ((times.get(g) or inf), before.get(g, len(before)), ran.index(g)))


def per_group(order: list[str], groups: dict[str, int], keep: int) -> set[str]:
    """Who goes through from a stage whose cars ran in groups (a road or street course of IndyCar: two groups, the best six of each reach the round of twelve): `keep` places split evenly among the groups (the first
    ones take the odd one); a group that has fewer cars than its places leaves them to the best of the others."""
    gs = sorted({groups[g] for g in order if g in groups})
    base, extra = divmod(keep, len(gs)) if gs else (0, 0)
    quota = {g: base + (1 if k < extra else 0) for k, g in enumerate(gs)}
    taken = dict.fromkeys(gs, 0)
    chosen: set[str] = set()
    for g in order:   # best first
        gr = groups.get(g)
        if gr in quota and taken[gr] < quota[gr]:
            chosen.add(g)
            taken[gr] += 1
    for g in order:   # the places a short group left
        if len(chosen) >= keep:
            break
        chosen.add(g)
    return chosen


def run(entrants: list[str], stages: list[dict[str, int | None]], advance: list[int] | None = None, late: list[str] | None = None,
        names: tuple[str, ...] = ("Q1", "Q2", "Q3"), cuts: list[int | None] | None = None, groups: dict[str, int] | None = None) -> dict:
    """The knockout, from the times of the stages played so far (1 to 3 dicts guid -> best lap in ms or None).
    `entrants`: who ran Q1 (the order is only the last tie-break). `late`: who joined after Q1 (they go behind everybody).
    Returns {"sizes", "stages": [Stage], "held": guids to hold in the pits for the NEXT stage, "grid": the grid so far}: with every stage played it is the full grid; with fewer, the cars still in the fight are
    not placed yet (the grid is the casualties so far, last place first)."""
    if not 1 <= len(stages) <= len(names):
        raise ValueError("one to three stages")
    sizes = field_sizes(len(entrants), advance, cuts)
    done: list[Stage] = []
    ran, before = list(entrants), {g: i for i, g in enumerate(entrants)}
    for i, times in enumerate(stages):
        held = [g for g in entrants if g not in ran]
        order = classify(ran, times, before)
        last = i == len(names) - 1
        keep = len(order) if last else sizes[i + 1]
        if i == 0 and groups and not last:   # the first stage was run in groups: the best of EACH group go through, the casualties of all of them ranked together by their times
            through = per_group(order, groups, keep)
            ran_next, out_now = [g for g in order if g in through], [g for g in order if g not in through]
        else:
            ran_next, out_now = order[:keep], order[keep:]
        stage = Stage(names[i], list(ran), order, {g: times.get(g) for g in ran}, out_now, held)
        done.append(stage)
        before = {g: j for j, g in enumerate(order)}
        ran = ran_next
    complete = len(done) == len(names)
    casualties = [g for st in reversed(done) for g in st.out]   # the last stage's casualties first: they start ahead of the earlier ones
    grid = (done[-1].order + casualties) if complete else casualties   # the Q3 order, then the Q2 casualties, then the Q1 ones; before Q3 is played the cars still in the fight are not placed
    return {"sizes": sizes, "stages": done, "held": [] if complete else [g for g in entrants if g not in ran], "grid": grid + list(late or [])}
