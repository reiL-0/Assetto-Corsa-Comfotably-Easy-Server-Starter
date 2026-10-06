"""A weather plan and the conditions it produces each second (what `protocol.weather_update` carries).

`Plan` is a list of (start second, WeatherFX type id) steps; each change is a smooth transition that ends at the step's start (CSP blends
`current` -> `upcoming` by `transition`). `Conditions.step` turns the current weather into rain intensity and integrates wetness and
puddles the way a track would: they build up while it rains and dry slowly afterwards, which is what the physics (grip) follows on the client.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# rain intensity per WeatherFX type id (0 = dry)
RAIN = {0: 0.6, 1: 0.8, 2: 1.0, 3: 0.12, 4: 0.25, 5: 0.4, 6: 0.35, 7: 0.6, 8: 0.9}


@dataclass
class Plan:
    steps: list[tuple[float, int]]      # (seconds from the start, type id), by time
    transition_s: float = 30.0

    def at(self, t: float) -> tuple[int, int, float]:
        """(current type, upcoming type, transition 0..1) at second `t`."""
        cur = next((ty for s, ty in reversed(self.steps) if s <= t), self.steps[0][1])
        i = next((k for k, (s, _) in enumerate(self.steps) if s > t), None)
        if i is None:
            return cur, cur, 0.0
        start, nxt = self.steps[i]
        begin = start - self.transition_s
        return (cur, nxt, max(0.0, (t - begin) / self.transition_s)) if t >= begin else (cur, cur, 0.0)


@dataclass
class Conditions:
    plan: Plan
    ambient: float = 20.0
    t: float = 0.0
    wetness: float = 0.0
    water: float = 0.0
    wet_rate: float = 1 / 60      # per second while raining
    dry_rate: float = 1 / 600
    water_rate: float = 1 / 180
    grip_loss: float = 0.2        # grip lost at full wetness
    loop: float | None = None     # repeat the plan every this many seconds
    state: dict = field(default_factory=dict)

    def step(self, dt: float) -> dict:
        self.t += dt
        cur, up, tr = self.plan.at(self.t % self.loop if self.loop else self.t)
        rain = RAIN.get(cur, 0.0) * (1 - tr) + RAIN.get(up, 0.0) * tr
        k = 1 if rain > 0.05 else -1
        self.wetness = min(1.0, max(0.0, self.wetness + k * (self.wet_rate if k > 0 else self.dry_rate) * dt * max(rain, 0.3 if k < 0 else 0)))
        self.water = min(1.0, max(0.0, self.water + (self.water_rate * rain if k > 0 else -self.water_rate / 4) * dt))
        self.state = dict(current=cur, upcoming=up, transition=tr, ambient=self.ambient, road=self.ambient + (3 if rain == 0 else -2),
                          grip=1.0 - self.grip_loss * self.wetness, rain=rain, wetness=self.wetness, water=self.water)
        return self.state
