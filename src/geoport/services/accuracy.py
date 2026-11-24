"""Smooth GPS uncertainty.

The iOS location channel used by GeoPort
(``simulateLocationWithLatitude:longitude:``) accepts only a latitude and a
longitude. It has no horizontal-accuracy argument, so uncertainty is modeled
by moving the fix itself.

The offset is a 2D Ornstein-Uhlenbeck process: it wanders, it is correlated
from one tick to the next, and it stays near the requested radius instead of
jumping by a fresh random amount every update. A radius of 0 pins the fix
exactly on the requested point.
"""
from __future__ import annotations

import math
import random
from typing import Optional

MAX_ACCURACY_M = 100.0
# How long a typical error takes to forget itself. Several seconds, so a
# 0.25s drive tick cannot look like white noise.
CORRELATION_TIME_S = 8.0


class GpsAccuracyModel:
    """North/east offset in meters around the true simulated point."""

    def __init__(
        self,
        accuracy_m: float = 0.0,
        tau_s: float = CORRELATION_TIME_S,
        rng: Optional[random.Random] = None,
    ) -> None:
        self.tau_s = max(0.5, float(tau_s))
        self._rng = rng or random.Random()
        self.north_m = 0.0
        self.east_m = 0.0
        self.accuracy_m = 0.0
        self.set_meters(accuracy_m)

    def set_meters(self, meters: float) -> float:
        clamped = min(MAX_ACCURACY_M, max(0.0, float(meters)))
        self.accuracy_m = clamped
        if clamped <= 0.0:
            self.north_m = 0.0
            self.east_m = 0.0
        return clamped

    def step(self, dt_s: float) -> tuple[float, float]:
        """Advance the wander by ``dt_s`` seconds and return ``(north_m, east_m)``."""
        if self.accuracy_m <= 0.0 or dt_s <= 0.0:
            self.north_m = 0.0
            self.east_m = 0.0
            return 0.0, 0.0
        theta = 1.0 / self.tau_s
        # Stationary radial RMS equals accuracy_m: each axis has variance accuracy^2 / 2.
        variance = (self.accuracy_m ** 2) / 2.0
        sigma = math.sqrt(2.0 * theta * variance)
        shock = sigma * math.sqrt(dt_s)
        self.north_m = self._relax(self.north_m, theta, dt_s, shock)
        self.east_m = self._relax(self.east_m, theta, dt_s, shock)
        self._limit_radius()
        return self.north_m, self.east_m

    def _relax(self, value: float, theta: float, dt_s: float, shock: float) -> float:
        decay = math.exp(-theta * dt_s)
        innovation = self._rng.gauss(0.0, 1.0) * shock
        return value * decay + innovation

    def _limit_radius(self) -> None:
        cap = 3.0 * self.accuracy_m
        radius = math.hypot(self.north_m, self.east_m)
        if radius > cap > 0.0:
            scale = cap / radius
            self.north_m *= scale
            self.east_m *= scale

    def offset(self) -> tuple[float, float]:
        return self.north_m, self.east_m
