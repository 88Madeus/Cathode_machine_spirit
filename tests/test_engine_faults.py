# -*- coding: utf-8 -*-
"""Сбои связи и прибора: программа не теряет истинное состояние и не трогает ток."""
import time

import pytest

import pr8_cathode_app as A
from faults import StallSim, StuckOff, alarms, hold_at, max_jump, wait

E_ = A.Engine


def test_late_reply_does_not_shift_answers(bench):
    E, ev, sim, ok = bench()
    E.send("manual", 70, 10)
    assert hold_at(E, 70)
    n0 = len(sim.curr_log)
    for q in ("OUTP?", "MEAS:VOLT?", "CURR?", "MEAS:CURR?"):
        sim.late_next = q
        time.sleep(0.4)
    time.sleep(0.5)
    assert sim.on and E.snap["out_on"]
    assert abs(sim.I_set - 70) < 0.01 and abs(E.snap["I_set"] - 70) < 0.01
    assert len(sim.curr_log) == n0
    assert not sim.pending


def test_single_glitched_outp_ignored(bench):
    E, ev, sim, ok = bench()
    E.send("manual", 30, 10)
    assert hold_at(E, 30)
    sim.glitch_outp = 1
    time.sleep(0.4)
    assert E.snap["out_on"] and abs(E.snap["I_set"] - 30) < 0.01
    assert any("Противоречивый" in m for _, m in ev)


def test_setpoint_changed_behind_back_is_resynced(bench):
    E, ev, sim, ok = bench()
    E.send("manual", 60, 10)
    assert hold_at(E, 60)
    sim.I_set = 40.0
    assert wait(E, lambda s: abs(s["I_set"] - 40) < 0.01, 20)
    assert E.snap["phase"] == E_.PH_HOLD and any("РАССИНХРОНИЗАЦИЯ" in m for m in alarms(ev))
    E.send("down", 0)
    assert wait(E, lambda s: s["phase"] == E_.PH_DOWN)
    time.sleep(0.5)
    assert max_jump(sim) <= 0.5 and sim.I_set <= 40.0


def test_instrument_trip_detected_and_setpoint_zeroed(bench):
    E, ev, sim, ok = bench()
    E.send("manual", 20, 10)
    assert hold_at(E, 20)
    sim.on = False
    assert wait(E, lambda s: s["phase"] == E_.PH_OFF, 10)
    assert any("отключился сам" in m for m in alarms(ev))
    assert abs(sim.I_set) < 1e-9, "после срабатывания защиты в прибор записывается уставка 0"


def test_heater_short_then_open_circuit(bench):
    E, ev, sim, ok = bench()
    E.send("manual", 40, 10)
    assert wait(E, lambda s: s["I_set"] > 30, 60)
    sim.R0 *= 0.55                                   # контакт с корпусом: R падает на 45 %
    assert wait(E, lambda s: s["phase"] == E_.PH_HOLD, 10)
    assert any("ПАДАЕТ" in m or "СКАЧОК" in m for m in alarms(ev))
    sim.R0 = 1e6                                     # обрыв
    assert wait(E, lambda s: s["phase"] == E_.PH_OFF, 10)
    assert any("ОБРЫВ" in m for m in alarms(ev))


def test_lost_outp_off_keeps_state_consistent(bench):
    E, ev, sim, ok = bench()
    E.send("manual", 30, 10)
    assert hold_at(E, 30)
    sim.fail_writes["OUTP OFF"] = 3
    E.send("off")
    # признак comm_fail при ускоренном времени держится ~1 мс — ждём событие, а не состояние
    t0 = time.time()
    while not any("Нет связи" in m for m in alarms(ev)) and time.time() - t0 < 10:
        time.sleep(0.01)
    assert any("Нет связи" in m for m in alarms(ev))
    time.sleep(0.3)
    assert abs(E.snap["I_set"]) < 1e-9 and abs(sim.I_set) < 1e-9
    assert not any("ограничивает" in m.lower() for m in alarms(ev))
    E.send("off")
    assert wait(E, lambda s: s["phase"] == E_.PH_OFF, 10) and not sim.on


def test_executed_outp_off_with_lost_reply_is_not_a_trip(bench):
    E, ev, sim, ok = bench()
    E.send("manual", 20, 10)
    assert hold_at(E, 20)
    orig = sim.write

    def lossy(cmd):
        orig(cmd)
        if cmd.strip().upper() == "OUTP OFF" and not sim.on and not getattr(sim, "_lost", False):
            sim._lost = True
            raise TimeoutError("ответ потерян")

    sim.write = lossy
    E.send("off")
    assert wait(E, lambda s: s["phase"] == E_.PH_OFF, 10)
    assert not any("отключился сам" in m for m in alarms(ev))


def test_measurement_offset_does_not_stall_ramp(bench):
    E, ev, sim, ok = bench(speed=1500)
    sim.meas_offset = 0.25                           # в пределах паспортной точности прибора
    E.send("up", 88)
    assert wait(E, lambda s: s["phase"] == E_.PH_HOLD and s["I_set"] > 87.99, 120)
    assert not alarms(ev)


@pytest.mark.parametrize("end_at", [1, 2, 3, 4, 6])
def test_long_outage_with_backlog_no_false_fault(bench, end_at):
    StallSim.end_at = end_at
    E, ev, sim, ok = bench(sim_cls=StallSim)
    E.send("manual", 60, 10)
    assert hold_at(E, 60)
    sim.arm = "MEAS:CURR?"
    time.sleep(3.0)
    assert E.snap["phase"] == E_.PH_HOLD and abs(sim.I_set - 60) < 0.01 and sim.on, \
        [m[:80] for m in alarms(ev)]


def test_stale_off_pending_does_not_mask_real_trip(bench):
    E, ev, sim, ok = bench(sim_cls=StuckOff)
    E.send("manual", 10, 10)
    assert hold_at(E, 10)
    E.send("off")
    time.sleep(0.5)
    sim.ignore_off = False
    E.send("manual", 30, 10)
    assert hold_at(E, 30)
    n = len(ev)
    sim.on = False
    assert wait(E, lambda s: not s["out_on"], 10)
    time.sleep(0.2)
    assert any(lvl == "alarm" and "отключился сам" in m for lvl, m in ev[n:])
