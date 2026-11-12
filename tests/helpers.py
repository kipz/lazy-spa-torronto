"""Shared fixtures for scheduler tests.

The scheduler's window logic depends on the *local* wall clock
(`datetime.fromtimestamp`). Tests pin TZ=UTC so epoch timestamps map to
predictable weekdays/hours regardless of the host's timezone.
"""
import datetime as dt
import os
import time

try:
    os.environ["TZ"] = "UTC"
    time.tzset()
except AttributeError:  # pragma: no cover - non-POSIX (e.g. Windows)
    pass

import scheduler  # noqa: E402

# Monday 2026-01-05 00:00 UTC. With TZ=UTC this is a stable anchor for
# constructing series whose wall-clock weekday/hour we control.
ANCHOR = dt.datetime(2026, 1, 5, 0, 0, tzinfo=dt.timezone.utc)
ANCHOR_TS = int(ANCHOR.timestamp())


def ts_at(hour, minute=0, day_offset=0):
    """Epoch seconds for `hour:minute` on Monday(+day_offset), in TZ=UTC."""
    d = ANCHOR + dt.timedelta(days=day_offset, hours=hour, minutes=minute)
    return int(d.timestamp())


def make_cfg(**overrides):
    """A config dict based on DEFAULTS with overrides applied (and coerced)."""
    cfg = dict(scheduler.DEFAULTS)
    cfg.update(overrides)
    for k in ("TARGET_TEMP", "ECO_TEMP", "FLOOR_TEMP", "MARGIN",
              "C_KWH_PER_C", "P_HEAT_KW", "UA_KW_PER_C", "WIND_SENSITIVITY",
              "SOLAR_GAIN_KW", "HORIZON_H", "STEP_H"):
        try:
            cfg[k] = float(cfg[k])
        except (TypeError, ValueError):
            pass
    cfg["HORIZON_H"] = int(cfg["HORIZON_H"])
    return cfg


def make_series(start_hour, hours=48, t_amb=5.0, step_h=1):
    """Hourly [(ts, t_amb)] series starting at `start_hour` on the anchor day."""
    return [(ts_at(start_hour + i * step_h), t_amb) for i in range(hours)]
