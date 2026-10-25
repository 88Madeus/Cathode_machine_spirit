# -*- coding: utf-8 -*-
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pytest  # noqa: E402

import pr8_cathode_app as A  # noqa: E402
from faults import RecSim, stop, wait  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Каждый тест — со своими настройками и журналами во временной папке
    и с моделью прибора RecSim вместо SimIO."""
    monkeypatch.setattr(A, "SETTINGS_PATH", str(tmp_path / "pr8_cathode_settings.json"))
    monkeypatch.setattr(A, "LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(A, "SimIO", RecSim)
    A.LOAD_WARNINGS.clear()
    RecSim.preset = None
    yield
    RecSim.preset = None


@pytest.fixture
def bench(monkeypatch):
    """bench(speed=400, preset=None, sim_cls=None, cfg=None) → (Engine, события, модель, ok).
    Все запущенные сессии отключаются в конце теста, даже если он упал."""
    started = []

    def connect(speed=400, preset=None, sim_cls=None, cfg=None):
        RecSim.preset = preset
        if sim_cls is not None:
            monkeypatch.setattr(A, "SimIO", sim_cls)
        ev = []
        c = A.cfg_from_settings(A.load_settings())
        if cfg:
            c.update(cfg)
        E = A.Engine(c, lambda lvl, msg: ev.append((lvl, msg)))
        E.connect("SIM", sim_speed=speed)
        started.append(E)
        ok = wait(E, lambda s: s["connected"] and s["phase"] != A.Engine.PH_NC, 10)
        if sim_cls is not None:
            monkeypatch.setattr(A, "SimIO", RecSim)
        return E, ev, RecSim.instances[-1], ok

    yield connect
    for E in started:
        stop(E)
