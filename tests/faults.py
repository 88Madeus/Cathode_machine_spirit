# -*- coding: utf-8 -*-
"""Модели неисправного прибора для тестов движка.

RecSim — SimIO, который записывает каждую команду и умеет вредить:
запоздавший ответ (late_next), сбойный OUTP? (glitch_outp), ограничение тока
прибором (i_cap), смещение измерения тока (meas_offset), потеря команды
(fail_writes). StallSim — затянувшийся обрыв связи, после которого приходит
пачка запоздавших ответов. StuckOff — OUTP OFF не действует.
"""

import time

import pr8_cathode_app as A

BaseSim = A.SimIO          # исходная модель (до подмены в conftest)


class RecSim(BaseSim):
    preset = None          # dict: состояние прибора до подключения (on, func, I_set, T, vlim, i_cap…)
    instances = []

    def __init__(self, clock):
        super().__init__(clock)
        self.curr_log = []         # (t, значение) каждой команды CURR
        self.jumps = []            # |новая уставка − уставка в приборе| при включённом выходе
        self.writes = []
        self.late_next = None
        self.glitch_outp = 0
        self.i_cap = None
        self.meas_offset = 0.0
        self.fail_writes = {}
        if RecSim.preset:
            for k, v in RecSim.preset.items():
                setattr(self, k, v)
        RecSim.instances.append(self)

    def write(self, cmd):
        c = cmd.strip().upper()
        if self.fail_writes.get(c, 0) > 0:
            self.fail_writes[c] -= 1
            raise TimeoutError("имитация потери команды %s" % c)
        self.writes.append(cmd)
        if c.startswith("CURR "):
            new = float(c.split()[1])
            self.jumps.append(abs(new - self.I_set) if self.on else 0.0)
            self.curr_log.append((self.clock.now(), new))
        super().write(cmd)

    def _iv(self):
        I, V = super()._iv()
        if self.i_cap is not None and I > self.i_cap:
            I = self.i_cap
            V = I * (self._R() + self.R_leads)
        return I, V

    def query(self, cmd):
        c = cmd.strip().upper()
        if self.late_next and c == self.late_next:
            self.late_next = None
            self.pending.append(super().query(cmd))   # ответ придёт позже…
            raise TimeoutError("имитация таймаута")      # …а сейчас — таймаут
        if self.pending and not c.startswith("*IDN"):
            stale = self.pending.pop(0)                  # опоздавший ответ стоит в буфере первым
            self.pending.append(super().query(cmd))
            return stale
        if c == "OUTP?" and self.glitch_outp and self.on:
            self.glitch_outp -= 1
            return "0"
        if c == "MEAS:CURR?" and self.meas_offset:
            return "%.2f" % max(0.0, float(super().query(cmd)) - self.meas_offset)
        return super().query(cmd)

    def drain(self, ms=150, limit=20):
        return 0        # худший случай: опоздавший ответ приходит уже после очистки буфера


class StallSim(RecSim):
    """Связь пропадает на запросе `arm`; через `end_at` попыток чтения возвращается,
    и все накопленные ответы приходят пачкой."""
    end_at = 3

    def __init__(self, clock):
        super().__init__(clock)
        self.stalled, self.arm, self.reads_left, self.delayed = False, None, 0, []

    def write(self, cmd):
        if self.stalled and cmd.strip().endswith("?"):
            self.delayed.append(BaseSim.query(self, cmd))
            return
        return super().write(cmd)

    def read(self):
        if self.stalled:
            self.reads_left -= 1
            if self.reads_left > 0:
                raise TimeoutError("stall")
            self.stalled = False
            self.pending.extend(self.delayed)
            self.delayed = []
        return super().read()

    def query(self, cmd):
        if self.arm and cmd.strip().upper() == self.arm:
            self.arm, self.stalled, self.reads_left = None, True, self.end_at
        if self.stalled:
            self.delayed.append(BaseSim.query(self, cmd))
            raise TimeoutError("stall")
        return super().query(cmd)

    def drain(self, ms=150, limit=20):
        if self.stalled:
            return 0
        n, self.pending = len(self.pending), []
        return n


class StuckOff(RecSim):
    """Команда OUTP OFF принимается, но не действует."""
    ignore_off = True

    def write(self, cmd):
        if self.ignore_off and cmd.strip().upper() == "OUTP OFF":
            self.writes.append(cmd)
            return
        return super().write(cmd)


# ------------------------------ помощники ------------------------------
def wait(E, cond, tmax=40):
    t0 = time.time()
    while time.time() - t0 < tmax:
        if cond(E.snap):
            return True
        time.sleep(0.005)
    return False


def stop(E):
    if not E.busy():
        return
    E.send("disconnect")
    wait(E, lambda s: not s["connected"], 30)
    t0 = time.time()
    while E.busy() and time.time() - t0 < 30:
        time.sleep(0.01)


def max_jump(sim):
    return max(sim.jumps, default=0.0)


def alarms(ev):
    return [m for lvl, m in ev if lvl == "alarm"]


def hold_at(E, value, tmax=60):
    return wait(E, lambda s: s["phase"] == E.PH_HOLD and abs(s["I_set"] - value) < 0.1, tmax)
