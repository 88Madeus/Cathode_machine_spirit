# -*- coding: utf-8 -*-
"""Настоящий транспорт: pyvisa-py по TCP-сокету к эмулятору прибора."""
import os
import time

import pytest

import pr8_cathode_app as A
from faults import BaseSim, hold_at, stop, wait

pytest.importorskip("pyvisa")
pytest.importorskip("pyvisa_py")

pytestmark = pytest.mark.lan


@pytest.fixture
def emulator(monkeypatch):
    from tools.fake_itech import FakeItech
    monkeypatch.setattr(A, "SimIO", BaseSim)        # эмулятор — обычная модель, без RecSim
    srv = FakeItech(port=0, speed=60).start()
    yield srv
    srv.stop()


def test_check_connection_reads_idn_only(emulator):
    idn = A.check_connection(emulator.resource, 3000)
    assert "IT6012D-300-150" in idn
    assert not emulator.sim.on


def test_session_over_lan(emulator):
    ev = []
    E = A.Engine(A.cfg_from_settings(A.load_settings()), lambda lvl, msg: ev.append((lvl, msg)))
    E.connect(emulator.resource, timeout_ms=3000)
    try:
        assert wait(E, lambda s: s["connected"] and s["phase"] == A.Engine.PH_OFF, 15), ev
        E.send("manual", 5, 10)
        assert hold_at(E, 5, 120)
        assert abs(emulator.sim.I_set - 5) < 0.02 and emulator.sim.func == "CURR"
        E.send("local")
        assert wait(E, lambda s: s["phase"] == A.Engine.PH_PANEL, 10)
        E.send("remote")
        assert wait(E, lambda s: s["control"] and s["phase"] == A.Engine.PH_HOLD, 15)
        E.send("down", 0)
        assert wait(E, lambda s: s["phase"] == A.Engine.PH_OFF, 120)
        assert not emulator.sim.on
    finally:
        stop(E)
    assert E.last_export and os.path.exists(E.last_export)
    assert not [m for lvl, m in ev if lvl == "alarm"]
