#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ПР-8 · Накал катода LaB6 — приложение управления источником ITECH IT6012D-300-150.

Запуск:   python pr8_cathode_app.py        (или двойной щелчок по «Запуск.bat»)
Нужно:    pip install pyvisa pyvisa-py matplotlib openpyxl
          (LAN работает сразу; для USB — NI-VISA или pyusb + libusb)

Что умеет
  * Подключение: LAN (Socket/VXI-11), любой VISA-ресурс из списка найденных,
    адрес вручную или модель нагревателя без прибора. «Проверить связь»
    спрашивает *IDN? и ничего не меняет в приборе.
  * При подключении программа только читает состояние. Если выход уже
    включён, она «подхватывает» текущую уставку без скачка.
  * ▲ / ▼ — движение по кривой из протоколов ПР-8 из ЛЮБОЙ точки, до
    заданного тока. Можно переключать направление на ходу.
  * ■ СТОП (Esc) — мгновенно зафиксировать ток. Ручная уставка с заданной
    скоростью, шаги ±1 А.
  * «Передать на панель» — программа отдаёт прибор (SYST:LOC) и молчит;
    «Вернуть управление» — заново читает состояние и продолжает с него.
  * Защиты: обрыв, окно R, скачок R, падение R при разогреве (как 05.10),
    P_max, dP/dt, предел напряжения, застой роста тока, самопроизвольное
    отключение выхода (подтверждается повторным опросом).
  * Целостность управления: уставка меняется только плавно — больше 0,5 А одной
    командой программа не отправит никогда; ток постоянно сверяется с уставкой,
    при расхождении уставка перечитывается из прибора и движение останавливается;
    после любого сбоя связи поток ответов выравнивается по *IDN? (запоздавший
    ответ не будет принят за ответ на следующий запрос).
  * Пока программа подключена, Windows не уходит в сон.
  * Журнал CSV каждые 0,5 с в папке logs рядом с программой; по окончании сессии
    он сам собирается в Excel (дата, время начала и окончания, итоги, журнал,
    события, графики). Кнопка «CSV → Excel…» пересоберёт любой старый журнал.
"""

import copy
import csv
import json
import math
import os
import queue
import random
import threading
import time
from datetime import datetime

__version__ = "0.4.0"

APP_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(APP_DIR, "pr8_cathode_settings.json")
LOG_DIR = os.path.join(APP_DIR, "logs")

# ============================== НАСТРОЙКИ ПО УМОЛЧАНИЮ ==============================
# Профиль: [до тока, А; скорость, А/мин; выдержка после, с]. Скорости — по ручным
# нагревам из протоколов ПР-8, ориентир 06.10.2026; остывание — зеркально.
DEFAULTS = dict(
    conn=dict(kind="lan", ip="192.168.1.100", port="30000", lan_proto="Socket",
              visa="", custom="", sim_speed="10", timeout_ms=3000),
    I_work=88.0,         # рабочий ток накала, А
    I_max=95.0,          # выше программа не выдаст никогда, А
    V_limit=8.0,         # VOLT:LIM в режиме CC, В (30.09: 6,06 В при 80 А)
    P_max=600.0,         # Вт (в протоколах максимум 485 Вт)
    heat_profile=[[20, 3.0, 0], [40, 1.8, 0], [60, 2.0, 0], [70, 2.0, 180],
                  [80, 1.1, 0], [88, 0.9, 0]],
    cool_profile=[[80, 0.9, 0], [70, 1.1, 0], [40, 2.0, 0], [0, 3.0, 0]],
    dPdt_max=1.0,        # Вт/с, страховка (в протоколах максимум 0,6)
    manual_rate=1.0,     # А/мин — по умолчанию для ручной уставки и ±1 А
    manual_rate_max=10.0,
    fault_rate=5.0,      # А/мин — аварийное снижение
    step_A=1.0,
    I_check=15.0,        # ниже R не проверяется
    R_min=0.010,         # Ом
    R_max=0.100,         # Ом
    R_jump=0.15,         # скачок R за 2 отсчёта → стоп
    R_drop=0.15,         # падение R при росте тока → стоп (05.10: −39 %)
    dt=0.5,              # период опроса, с
)

# Жёсткие границы настроек. Скорость ≤ 10 А/мин и период ≤ 1 с гарантируют, что
# одна команда меняет уставку не больше чем на 0,34 А (защита от скачка — 0,5 А).
RATE_MAX = 10.0          # А/мин — любая скорость в кривых и вручную
MAX_STEP_A = 0.5         # А — больше одной командой программа не изменит уставку никогда
DEVICE_I_MAX = 150.0     # А — паспорт IT6012D-300-150
LIMITS = dict(           # ключ: (минимум, максимум, подпись)
    I_work=(1.0, DEVICE_I_MAX, "Рабочий ток, А"),
    I_max=(1.0, DEVICE_I_MAX, "Максимум уставки, А"),
    V_limit=(0.5, 20.0, "Предел напряжения, В"),
    P_max=(1.0, 3000.0, "Предел мощности, Вт"),
    dPdt_max=(0.01, 20.0, "Макс. рост мощности, Вт/с"),
    manual_rate=(0.05, RATE_MAX, "Скорость ручная по умолчанию, А/мин"),
    manual_rate_max=(0.05, RATE_MAX, "Скорость ручная максимум, А/мин"),
    fault_rate=(0.1, RATE_MAX, "Аварийное снижение, А/мин"),
    step_A=(0.01, 5.0, "Шаг кнопок ±, А"),
    I_check=(1.0, DEVICE_I_MAX, "Контроль R от тока, А"),
    R_min=(1e-4, 1.0, "R минимум"),
    R_max=(1e-4, 1.0, "R максимум"),
    R_jump=(0.01, 0.9, "Скачок R"),
    R_drop=(0.01, 0.9, "Падение R при разогреве"),
    dt=(0.2, 1.0, "Период опроса, с"),
)

SCPI = dict(
    idn="*IDN?", cls="*CLS", remote="SYST:REM", local="SYST:LOC", err="SYST:ERR?",
    func_q="FUNC?", func_cc="FUNC CURR", curr="CURR {:.2f}", curr_q="CURR?",
    vlim="VOLT:LIM {:.2f}", vlim_q="VOLT:LIM?", out_on="OUTP ON", out_off="OUTP OFF", out_q="OUTP?",
    meas_v="MEAS:VOLT?", meas_i="MEAS:CURR?",
)

LOAD_WARNINGS = []       # замечания при чтении файла настроек — показываются в журнале


def validate_settings(s):
    """Список ошибок (по-русски); пустой — настройки годны."""
    errs = []
    for k, (lo, hi, cap) in LIMITS.items():
        v = s.get(k)
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v):
            errs.append("«%s»: не число" % cap)
        elif not lo <= v <= hi:
            errs.append("«%s» = %g вне допустимого диапазона %g…%g" % (cap, v, lo, hi))
    if not errs:
        if s["I_work"] > s["I_max"]:
            errs.append("Рабочий ток больше максимума уставки")
        if s["R_min"] >= s["R_max"]:
            errs.append("R минимум должен быть меньше R максимум")
        if s["manual_rate"] > s["manual_rate_max"]:
            errs.append("Ручная скорость по умолчанию больше максимальной")
    for key, name, asc in (("heat_profile", "Кривая разогрева", True),
                           ("cool_profile", "Кривая остывания", False)):
        prof = s.get(key)
        try:
            rows = [[float(x) for x in p] for p in prof]
            if not rows or any(len(r) != 3 for r in rows):
                raise ValueError
        except (TypeError, ValueError):
            errs.append("%s: неверный формат" % name)
            continue
        for n, (I, r, h) in enumerate(rows, 1):
            if not all(map(math.isfinite, (I, r, h))):
                errs.append("%s, точка %d: не число" % (name, n))
            elif I < 0 or (asc and I <= 0):
                errs.append("%s, точка %d: ток должен быть > 0" % (name, n))
            elif not 0 < r <= RATE_MAX:
                errs.append("%s, точка %d: скорость %g А/мин — допустимо от 0 до %g" % (name, n, r, RATE_MAX))
            elif not 0 <= h <= 36000:
                errs.append("%s, точка %d: выдержка 0…36000 с" % (name, n))
        seq = [r[0] for r in rows]
        if any((b <= a) if asc else (b >= a) for a, b in zip(seq, seq[1:])):
            errs.append("%s: токи должны %s" % (name, "возрастать" if asc else "убывать"))
        if not asc and seq and seq[-1] != 0:
            errs.append("Кривая остывания должна заканчиваться на 0 А")
        if asc and isinstance(s.get("I_max"), (int, float)) and seq and max(seq) > s["I_max"]:
            errs.append("Точки кривой разогрева выше максимума уставки")
    return errs


def load_settings():
    s = copy.deepcopy(DEFAULTS)
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            user = json.load(f)
    except FileNotFoundError:
        return s
    except Exception as e:
        LOAD_WARNINGS.append("Файл настроек не прочитан (%s) — взяты протокольные значения." % e)
        return s
    if not isinstance(user, dict):
        LOAD_WARNINGS.append("Файл настроек не содержит настроек — взяты протокольные значения.")
        return s
    if isinstance(user.get("conn"), dict):
        dc = DEFAULTS["conn"]
        for k, v in user["conn"].items():
            if k not in dc:
                continue
            if k == "timeout_ms":
                try:
                    v = int(float(v))
                except (TypeError, ValueError):
                    continue
                if not 500 <= v <= 30000:
                    continue
            elif k == "kind" and v not in ("lan", "visa", "custom", "sim"):
                continue
            elif k == "lan_proto" and v not in ("Socket", "VXI-11"):
                continue
            elif k == "sim_speed" and str(v) not in ("1", "10", "60"):
                continue
            else:
                v = str(v)
            s["conn"][k] = v
    for k in DEFAULTS:
        if k == "conn" or k not in user:
            continue
        v = user[k]
        if k in LIMITS and isinstance(v, str):
            try:
                v = float(v.replace(",", "."))
            except ValueError:
                pass
        s[k] = v
    errs = validate_settings(s)
    if errs:
        LOAD_WARNINGS.append("В файле настроек ошибки (%s) — кривая и пределы взяты протокольные."
                             % "; ".join(errs))
        conn = s["conn"]
        s = copy.deepcopy(DEFAULTS)
        s["conn"] = conn
    return s


def save_settings(s):
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SETTINGS_PATH)        # не оставить полузаписанный файл


def cfg_from_settings(s):
    c = {k: (float(v) if k in LIMITS else v) for k, v in s.items() if k != "conn"}
    c["heat_profile"] = [tuple(map(float, x)) for x in s["heat_profile"]]
    c["cool_profile"] = [tuple(map(float, x)) for x in s["cool_profile"]]
    return c


def log_dir():
    """Папка журналов: рядом с программой, иначе в «Документах», иначе во временной."""
    import tempfile
    for d in (LOG_DIR, os.path.join(os.path.expanduser("~"), "Documents", "PR8_nakal_logs"),
              os.path.join(tempfile.gettempdir(), "PR8_nakal_logs")):
        try:
            os.makedirs(d, exist_ok=True)
            probe = os.path.join(d, ".write_test")
            with open(probe, "w") as f:
                f.write("1")
            os.remove(probe)
            return d
        except OSError:
            continue
    raise OSError("нет папки, доступной для записи журналов")


def build_resource(conn):
    k = conn["kind"]
    if k == "lan":
        if conn["lan_proto"].startswith("VXI"):
            return "TCPIP0::%s::inst0::INSTR" % conn["ip"].strip()
        return "TCPIP0::%s::%s::SOCKET" % (conn["ip"].strip(), str(conn["port"]).strip())
    if k == "visa":
        return conn["visa"].strip()
    if k == "custom":
        return conn["custom"].strip()
    return "SIM"


def keep_awake(on):
    """Windows: не уходить в сон и не гасить экран, пока идёт сессия (для вызывающего потока)."""
    try:
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED, ES_DISPLAY_REQUIRED = 0x80000000, 0x1, 0x2
        # на ноутбуках с Modern Standby без ES_DISPLAY_REQUIRED сон после гашения экрана возможен
        ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ((ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED) if on else 0))
    except Exception:
        pass


# ================================ кривая ================================
def up_segments(profile, I_now, limit):
    """Участки вверх по кривой от текущего тока до limit: [(цель, А/с, выдержка)].
    Пусто, если limit не выше текущего тока (кривая вверх никогда не ведёт вниз)."""
    if not limit > I_now + 0.05:
        return []
    prof = sorted(profile, key=lambda s: s[0])
    segs = []
    for tgt, r, h in prof:
        if tgt <= I_now + 1e-3:          # точка пройдена (почти достигнутая — нет: её выдержка нужна)
            continue
        if tgt >= limit - 0.05:
            segs.append((limit, r / 60.0, h if abs(tgt - limit) < 0.05 else 0.0))
            return segs
        segs.append((tgt, r / 60.0, h))
    if prof:
        segs.append((limit, prof[-1][1] / 60.0, 0.0))
    return segs


def down_segments(profile, I_now, limit):
    """Участки вниз по кривой от текущего тока до limit. Пусто, если limit не ниже тока."""
    if not limit < I_now - 0.05:
        return []
    prof = sorted(profile, key=lambda s: -s[0])
    segs = []
    for tgt, r, h in prof:
        if tgt >= I_now - 1e-3:
            continue
        if tgt <= limit + 0.05:
            segs.append((limit, r / 60.0, h if abs(tgt - limit) < 0.05 else 0.0))
            return segs
        segs.append((tgt, r / 60.0, h))
    if prof:
        segs.append((limit, prof[-1][1] / 60.0, 0.0))
    return segs


def curve_points(t, I, segments):
    pts = [(t, I)]
    for tgt, r, h in segments:
        if r > 0:
            t += abs(tgt - I) / r
        I = tgt
        pts.append((t, I))
        if h:
            t += h
            pts.append((t, I))
    return pts


# ================================ транспорт ================================
class CommError(Exception):
    pass


class Clock:
    def __init__(self, speed=None):
        self.sim = speed is not None
        self.speed = float(speed or 1.0)
        self._t = 0.0
        self._t0 = time.monotonic()

    def now(self):
        return self._t if self.sim else time.monotonic() - self._t0

    def sleep(self, s):
        if s <= 0:
            return
        if self.sim:
            time.sleep(s / self.speed)
            self._t += s
        else:
            time.sleep(s)


def _resource_manager():
    import pyvisa
    try:
        return pyvisa.ResourceManager()
    except Exception:
        return pyvisa.ResourceManager("@py")


def list_visa_resources():
    import pyvisa
    found = []
    for backend in (None, "@py"):
        try:
            rm = pyvisa.ResourceManager(backend) if backend else pyvisa.ResourceManager()
            for r in rm.list_resources():
                if r not in found:
                    found.append(r)
        except Exception:
            pass
    return found


class ItechIO:
    def __init__(self, resource, timeout_ms=3000):
        rm = _resource_manager()
        try:
            self.inst = rm.open_resource(resource, open_timeout=timeout_ms)
        except TypeError:
            self.inst = rm.open_resource(resource)
        self.inst.timeout = timeout_ms
        self.inst.read_termination = "\n"
        self.inst.write_termination = "\n"
        try:
            self.inst.encoding = "latin-1"   # не падать на случайном не-ASCII байте
        except Exception:
            pass

    def write(self, cmd):
        self.inst.write(cmd)

    def read(self):
        return self.inst.read().strip()

    def query(self, cmd):
        return self.inst.query(cmd).strip()

    def drain(self, ms=150, limit=20):
        """Выбросить из буфера запоздавшие ответы (пришедшие после таймаута).
        Возвращает, сколько ответов выброшено."""
        old = self.inst.timeout
        n = 0
        try:
            self.inst.timeout = ms
            for _ in range(limit):
                try:
                    self.inst.read()
                    n += 1
                except Exception:
                    break
        finally:
            try:
                self.inst.timeout = old
            except Exception:
                pass
        return n

    def close(self):
        try:
            self.inst.close()
        except Exception:
            pass


class SimIO:
    """Модель нагревателя по протокольным данным: R ≈ 20 мОм холодный, ≈ 59 мОм при 88 А."""
    IDN = "МОДЕЛЬ,IT6012D-300-150 (без прибора),0,0"

    def __init__(self, clock):
        self.clock = clock
        self.T = 295.0
        self.I_set = 0.0
        self.vlim = 10.0
        self.on = False
        self.func = "VOLT"
        self.R0, self.R_leads = 20e-3, 0.5e-3
        self.C, self.A, self.G = 1.5, 5.15e-4, 0.02
        self.errs = []
        self.pending = []          # ответы на запросы, отправленные через write()
        self.t_last = clock.now()

    def _R(self):
        return self.R0 * (1 + 8.46e-4 * (self.T - 295.0))

    def _iv(self):
        if not self.on:
            return 0.0, 0.0
        Rt = self._R() + self.R_leads
        I, V = self.I_set, self.I_set * Rt
        if V > self.vlim:
            V, I = self.vlim, self.vlim / Rt
        return I, V

    def _advance(self):
        now = self.clock.now()
        dt, self.t_last = now - self.t_last, now
        n = max(1, int(dt / 0.05) + 1)
        for _ in range(n):
            I, _ = self._iv()
            eps = 0.035 + 1.05e-4 * self.T
            loss = 5.67e-8 * eps * self.A * (self.T ** 4 - 295.0 ** 4) + self.G * (self.T - 295.0)
            self.T += (I * I * self._R() - loss) / self.C * dt / n

    def write(self, cmd):
        self._advance()
        c = cmd.strip().upper()
        if c.endswith("?"):
            self.pending.append(self.query(cmd))
        elif c.startswith("CURR "):
            self.I_set = float(c.split()[1])
        elif c.startswith("VOLT:LIM "):
            self.vlim = float(c.split()[1])
        elif c == "OUTP ON":
            self.on = True
        elif c == "OUTP OFF":
            self.on = False
        elif c == "FUNC CURR":
            self.func = "CURR"
        elif c not in ("SYST:REM", "SYST:LOC", "*CLS"):
            self.errs.append('-113,"Undefined header"')

    def read(self):
        if not self.pending:
            raise TimeoutError("нет ответа")
        return self.pending.pop(0)

    def query(self, cmd):
        self._advance()
        c = cmd.strip().upper()
        I, V = self._iv()
        ans = {"*IDN?": self.IDN,
               "FUNC?": self.func, "CURR?": "%.2f" % self.I_set, "VOLT:LIM?": "%.2f" % self.vlim,
               "OUTP?": "1" if self.on else "0",
               "MEAS:VOLT?": "%.2f" % max(0.0, V + random.gauss(0, 0.003)),
               "MEAS:CURR?": "%.2f" % max(0.0, I + random.gauss(0, 0.01))}
        if c == "SYST:ERR?":
            return self.errs.pop(0) if self.errs else '0,"No error"'
        if c in ans:
            return ans[c]
        self.errs.append('-113,"Undefined header"')
        return "0"

    def drain(self, ms=150, limit=20):
        n, self.pending = len(self.pending), []
        return n

    def close(self):
        pass


# допустимые диапазоны числовых ответов: вне их ответ считается сдвинутым/битым
NUM_RANGE = dict(meas_v=(-1.0, 350.0), meas_i=(-1.0, 200.0), curr_q=(-0.5, 200.0),
                 vlim_q=(0.0, 350.0))


class Supply:
    """Команды источника с повторами. После любого сбоя поток ответов считается
    рассинхронизированным: пока он не выровнен (буфер пуст, *IDN? вернулся ровно
    один раз и за ним ничего), ни один запрос не отправляется — иначе запрос мог
    бы получить чужой запоздавший ответ (OUTP? → «5.21», MEAS:VOLT? → ток)."""

    def __init__(self, io):
        self.io = io
        self.idn = ""
        self.synced = True

    def _realign(self):
        try:
            self.io.drain()
        except Exception:
            pass
        if not self.idn:
            self.synced = True
            return
        for _ in range(3):
            try:
                self.io.write(SCPI["idn"])
                for _ in range(8):
                    if self.io.read().strip() == self.idn:
                        break
                else:
                    continue
                if not self.io.drain():        # за ответом на *IDN? больше ничего нет
                    self.synced = True
                    return
            except Exception:
                continue
        self.synced = False

    def _robust(self, fn, tries=3, delay=0.3):
        last = None
        for _ in range(tries):
            if not self.synced:
                self._realign()
                if not self.synced:
                    last = last or Exception("ответы прибора не удалось выровнять")
                    time.sleep(delay)
                    continue
            try:
                return fn()
            except Exception as e:
                last = e
                self.synced = False
                time.sleep(delay)
        raise CommError(str(last))

    def w(self, key, *args):
        cmd = SCPI[key].format(*args)
        self._robust(lambda: self.io.write(cmd))

    def q(self, key):
        return self._robust(lambda: self.io.query(SCPI[key]).strip())

    def qf(self, key):
        lo, hi = NUM_RANGE[key]

        def f():
            ans = self.io.query(SCPI[key]).strip()
            x = float(ans)
            if not (math.isfinite(x) and lo <= x <= hi):
                raise ValueError("неожиданный ответ на %s: %r" % (SCPI[key], ans))
            return x
        return self._robust(f)

    def measure(self):
        return self.qf("meas_v"), self.qf("meas_i")

    def setpoint(self):
        return max(0.0, self.qf("curr_q"))

    def output_is_on(self):
        def f():
            a = self.io.query(SCPI["out_q"]).strip().upper()
            if a in ("1", "ON"):
                return True
            if a in ("0", "OFF"):
                return False
            raise ValueError("неожиданный ответ на OUTP?: %r" % a)
        return self._robust(f)

    def func(self):
        def f():
            a = self.io.query(SCPI["func_q"]).strip().upper().strip('"')
            if not a or not a[0].isalpha():
                raise ValueError("неожиданный ответ на FUNC?: %r" % a)
            return a
        return self._robust(f)

    def errors(self):
        out = []
        for _ in range(20):
            e = self.q("err")
            if e.lstrip("+").startswith("0"):
                break
            out.append(e)
        return out


def explain_comm_error(e):
    """Понятная подсказка к типичным ошибкам связи."""
    t = str(e)
    low = t.lower()
    if "-1073807339" in t or "timeout" in low or "timed out" in low or "tmo" in low:
        hint = "прибор не ответил за отведённое время: проверьте IP-адрес, кабель и что прибор включён"
    elif "refused" in low or "111" in t or "10061" in t:
        hint = "по этому адресу нет приёма на указанном порту: проверьте порт (System → LAN)"
    elif "-1073807343" in t or "resource not found" in low or "rsrc_nfound" in low:
        hint = "ресурс не найден: обновите список («Найти устройства») или проверьте кабель/драйвер"
    elif "unreachable" in low or "no route" in low or "10065" in t or "10051" in t:
        hint = "сеть недоступна: ноутбук и прибор в разных подсетях?"
    elif "busy" in low or "locked" in low or "-1073807345" in t:
        hint = "ресурс занят другой программой (например, IT9000 или второй копией этой)"
    else:
        return t
    return "%s (%s)" % (hint, t)


def check_connection(resource, timeout_ms=3000):
    """*IDN? без каких-либо изменений в приборе."""
    if resource == "SIM":
        return SimIO.IDN
    io = ItechIO(resource, timeout_ms)
    try:
        return io.query("*IDN?")
    finally:
        io.close()


# ============================== экспорт в Excel ==============================
LEVEL_RU = {"info": "", "warn": "внимание", "alarm": "ТРЕВОГА"}


def _fmt_dur(sec):
    sec = int(round(sec))
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return ("%d ч %02d мин %02d с" % (h, m, s)) if h else ("%d мин %02d с" % (m, s))


def export_session_xlsx(csv_path, xlsx_path=None, meta=None, events=None):
    """Собрать журнал сессии (CSV) в Excel: лист «Сессия» (дата, время начала и
    окончания, итоги), «Журнал», «События», «Графики». Возвращает путь к .xlsx
    или None, если в журнале нет данных. meta/events — из движка; без них
    (пересборка старого CSV) всё восстанавливается из самого CSV."""
    from openpyxl import Workbook
    from openpyxl.chart import Reference, ScatterChart, Series
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    import io as _io
    meta = meta or {}
    rows = []
    with open(csv_path, encoding="utf-8-sig", errors="replace", newline="") as f:
        text = f.read().replace("\x00", "")      # после аварийного выключения бывают нули
    def num(r, k):
        try:
            return float(r.get(k) or "")
        except (TypeError, ValueError):
            return None
    for r in csv.DictReader(_io.StringIO(text)):
        try:
            ts = datetime.fromisoformat(r.get("time") or "")
        except (TypeError, ValueError):
            continue                      # обрезанная/битая строка
        rows.append(dict(ts=ts, t=num(r, "t_s"), phase=r.get("phase") or "",
                         I_set=num(r, "I_set_A"), I=num(r, "I_A"), U=num(r, "U_V"),
                         P=num(r, "P_W"), R=num(r, "R_mOhm"), event=r.get("event") or ""))
    if not rows:
        return None
    xlsx_path = xlsx_path or os.path.splitext(csv_path)[0] + ".xlsx"
    t_start = meta.get("start") or rows[0]["ts"]
    t_end = meta.get("end") or rows[-1]["ts"]
    if events is None:
        events = [(r["ts"], "", m) for r in rows if r["event"] for m in r["event"].split(" | ")]

    BOLD = Font(bold=True)
    HEAD = PatternFill("solid", fgColor="DDE5EE")
    RED = Font(bold=True, color="C62828")
    ORANGE = Font(color="B26A00")
    thin = Side(style="thin", color="B0B0B0")
    BOX = Border(left=thin, right=thin, top=thin, bottom=thin)
    wb = Workbook()

    # ---- Сессия ----
    ws = wb.active
    ws.title = "Сессия"
    ws.column_dimensions["A"].width = 38
    ws.column_dimensions["B"].width = 62
    ws["A1"] = "ПР-8 · Сессия управления накалом катода LaB₆"
    ws["A1"].font = Font(bold=True, size=14)

    def peak(key):
        best = max((r for r in rows if r[key] is not None), key=lambda r: r[key], default=None)
        return best
    pI, pU, pP = peak("I"), peak("U"), peak("P")
    Rs = [r["R"] for r in rows if r["R"] is not None and (r["I"] or 0) >= 15]
    n_alarm = sum(1 for e in events if e[1] == "alarm")
    same_day = t_start.date() == t_end.date()
    last = rows[-1]
    out_end = meta.get("out_on_end")
    end_state = "%s; уставка %.2f А" % (meta.get("phase_end", last["phase"]),
                                         meta.get("I_end", last["I_set"] or 0))
    if out_end is not None:
        end_state += "; выход %s" % ("ВКЛЮЧЁН (источник держит ток)" if out_end else "выключен")
    # при пересборке старого CSV прибор и версия берутся из строки «Прибор: … (программа vX)»
    dev_line = ("—", "—")
    for _, _, m in events:
        if m.startswith("Прибор: "):
            body = m[len("Прибор: "):]
            k = body.rfind("(программа v")
            dev_line = (body[:k].strip() if k >= 0 else body.strip(),
                        body[k + len("(программа v"):].rstrip(")").strip() if k >= 0 else "—")
            break
    info = [
        ("Дата", t_start.strftime("%d.%m.%Y") + ("" if same_day else " – " + t_end.strftime("%d.%m.%Y"))),
        ("Время начала", t_start.strftime("%H:%M:%S") if same_day else t_start.strftime("%d.%m.%Y %H:%M:%S")),
        ("Время окончания", t_end.strftime("%H:%M:%S") if same_day else t_end.strftime("%d.%m.%Y %H:%M:%S")),
        ("Длительность", _fmt_dur((t_end - t_start).total_seconds())),
        ("Адрес прибора", meta.get("resource", "—")),
        ("Прибор (*IDN?)", meta.get("idn") or dev_line[0]),
        ("Состояние в начале", "%s; уставка %.2f А" % (rows[0]["phase"], rows[0]["I_set"] or 0)),
        ("Состояние в конце", end_state),
        ("Максимальный ток", "%.2f А в %s" % (pI["I"], pI["ts"].strftime("%H:%M:%S")) if pI else "—"),
        ("Максимальное напряжение", "%.3f В в %s" % (pU["U"], pU["ts"].strftime("%H:%M:%S")) if pU else "—"),
        ("Максимальная мощность", "%.1f Вт в %s" % (pP["P"], pP["ts"].strftime("%H:%M:%S")) if pP else "—"),
        ("Сопротивление накала (I ≥ 15 А)", "%.1f – %.1f мОм" % (min(Rs), max(Rs)) if Rs else "—"),
        ("Тревог", str(n_alarm) if meta else "—"),
        ("Записей в журнале", "%d (период %.1f с)" % (len(rows), meta.get("dt", 0.5))),
        ("Файл CSV", os.path.basename(csv_path)),
        ("Версия программы", meta.get("version") or dev_line[1]),
    ]
    for n, (k, v) in enumerate(info, start=3):
        ws.cell(row=n, column=1, value=k).font = BOLD
        c = ws.cell(row=n, column=2, value=v)
        c.alignment = Alignment(wrap_text=True)
        for col in (1, 2):
            ws.cell(row=n, column=col).border = BOX
    if n_alarm:
        ws.cell(row=3 + [k for k, _ in info].index("Тревог"), column=2).font = RED

    # ---- Журнал ----
    wj = wb.create_sheet("Журнал")
    head = ["Дата и время", "t, мин", "Режим", "Уставка, А", "Ток, А", "Напряжение, В",
            "Мощность, Вт", "R, мОм", "События"]
    widths = [20, 9, 22, 11, 10, 13, 13, 10, 90]
    for col, (h, w) in enumerate(zip(head, widths), start=1):
        c = wj.cell(row=1, column=col, value=h)
        c.font, c.fill, c.border = BOLD, HEAD, BOX
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        wj.column_dimensions[c.column_letter].width = w
    t0 = rows[0]["ts"]
    fmts = [None, "0.00", None, "0.00", "0.00", "0.000", "0.0", "0.0", None]
    for n, r in enumerate(rows, start=2):
        tm = r["t"] / 60 if r["t"] is not None else (r["ts"] - t0).total_seconds() / 60
        vals = [r["ts"], tm, r["phase"], r["I_set"], r["I"],
                r["U"], r["P"], r["R"], r["event"] or None]
        for col, (v, fm) in enumerate(zip(vals, fmts), start=1):
            c = wj.cell(row=n, column=col, value=v)
            if fm:
                c.number_format = fm
        wj.cell(row=n, column=1).number_format = "DD.MM.YYYY HH:MM:SS"
    wj.freeze_panes = "B2"
    wj.auto_filter.ref = "A1:I%d" % (len(rows) + 1)

    # ---- События ----
    we = wb.create_sheet("События")
    for col, (h, w) in enumerate(zip(["Время", "Уровень", "Событие"], [20, 12, 120]), start=1):
        c = we.cell(row=1, column=col, value=h)
        c.font, c.fill, c.border = BOLD, HEAD, BOX
        we.column_dimensions[c.column_letter].width = w
    for n, (ts, lvl, msg) in enumerate(events, start=2):
        we.cell(row=n, column=1, value=ts).number_format = "DD.MM.YYYY HH:MM:SS"
        we.cell(row=n, column=2, value=LEVEL_RU.get(lvl, lvl))
        c = we.cell(row=n, column=3, value=msg)
        if lvl == "alarm":
            for col in (2, 3):
                we.cell(row=n, column=col).font = RED
        elif lvl == "warn":
            c.font = ORANGE
    we.freeze_panes = "A2"

    # ---- Графики (по прореженным данным: Excel тормозит на десятках тысяч точек) ----
    wg = wb.create_sheet("Графики")
    wd = wb.create_sheet("Данные графиков")
    step = max(1, -(-len(rows) // 4000))
    pick = rows[::step]
    if pick[-1] is not rows[-1]:
        pick.append(rows[-1])
    for col, h in enumerate(head[:8], start=1):
        wd.cell(row=1, column=col, value=h).font = BOLD
    for n, r in enumerate(pick, start=2):
        tm = r["t"] / 60 if r["t"] is not None else (r["ts"] - t0).total_seconds() / 60
        for col, v in ((2, tm), (4, r["I_set"]), (5, r["I"]), (7, r["P"]), (8, r["R"])):
            wd.cell(row=n, column=col, value=v)
    wd.cell(row=1, column=10, value="Каждая %d-я строка журнала — только для графиков" % step)
    N = len(pick) + 1
    x = Reference(wd, min_col=2, min_row=2, max_row=N)

    COLORS = {4: "1565C0", 5: "EF6C00", 7: "8E24AA", 8: "2E7D32"}

    def chart(title, ytitle, cols, anchor):
        ch = ScatterChart()
        ch.title, ch.style = title, 13
        ch.x_axis.title, ch.y_axis.title = "Время от начала, мин", ytitle
        ch.height, ch.width = 9, 26
        ch.x_axis.delete = False
        ch.y_axis.delete = False
        for col in cols:
            s = Series(Reference(wd, min_col=col, min_row=1, max_row=N), x, title_from_data=True)
            s.marker.symbol = "none"
            s.smooth = False
            s.graphicalProperties.line.width = 15000 if col != 5 else 9000
            s.graphicalProperties.line.solidFill = COLORS[col]
            ch.series.append(s)
        wg.add_chart(ch, anchor)
    chart("Ток накала", "Ток, А", [4, 5], "A1")
    chart("Сопротивление накала", "R, мОм", [8], "A20")
    chart("Мощность накала", "P, Вт", [7], "A39")

    # печать: «Сессия» и «Графики» — по одной странице, журнал — по ширине
    for sh, land, tall in ((ws, False, 1), (wg, False, 1), (wj, True, 0), (we, True, 0)):
        sh.page_setup.orientation = "landscape" if land else "portrait"
        sh.page_setup.paperSize = sh.PAPERSIZE_A4
        sh.sheet_properties.pageSetUpPr.fitToPage = True
        sh.page_setup.fitToWidth, sh.page_setup.fitToHeight = 1, tall
    wj.print_title_rows = we.print_title_rows = "1:1"

    try:
        wb.save(xlsx_path)
    except PermissionError:               # файл с таким именем открыт в Excel
        root, ext = os.path.splitext(xlsx_path)
        n = 2
        while os.path.exists("%s (%d)%s" % (root, n, ext)):
            n += 1
        xlsx_path = "%s (%d)%s" % (root, n, ext)
        wb.save(xlsx_path)
    return xlsx_path


# ================================= движок =================================
LIM_MARGIN = 0.5         # А — насколько уставка может быть выше тока, ограниченного источником
                         #     (= наибольший скачок при снятии ограничения; ≤ MAX_STEP_A)


class Engine:
    PH_NC = "НЕТ ПОДКЛЮЧЕНИЯ"
    PH_OFF = "ВЫХОД ВЫКЛЮЧЕН"
    PH_HOLD = "УДЕРЖАНИЕ"
    PH_UP = "ВВЕРХ ПО КРИВОЙ"
    PH_DOWN = "ВНИЗ ПО КРИВОЙ"
    PH_MAN = "РУЧНОЕ ДВИЖЕНИЕ"
    PH_FAULT = "АВАРИЙНОЕ СНИЖЕНИЕ"
    PH_PANEL = "УПРАВЛЕНИЕ С ПАНЕЛИ"
    PH_OBS = "ТОЛЬКО НАБЛЮДЕНИЕ"
    RAMPING = (PH_UP, PH_DOWN, PH_MAN, PH_FAULT)
    FALLING = (PH_DOWN, PH_FAULT)

    def __init__(self, cfg, on_event=None):
        self.cfg = cfg
        self.on_event = on_event or (lambda lvl, msg: print("[%s] %s" % (lvl, msg)))
        self.cmdq = queue.Queue()
        self.lock = threading.Lock()
        self.hist = []
        self.thread = None
        self.alive = False
        self.clock = Clock()
        self.log_path = ""
        self.exporting = False
        self._reset()
        self.snap = self._make_snap()

    # ---------- служебное ----------
    def _reset(self):
        self.I_set, self.I_sent, self.target, self.rate = 0.0, None, 0.0, 0.0
        self.seg_hold, self.hold_until, self.segments = 0.0, None, []
        self.phase, self.out_on, self.control, self.poll = self.PH_NC, False, False, False
        self.off_at_zero, self.ramp_limit, self.off_pending = False, None, False
        self._reset_guards()
        self.comm_fail = 0
        self.vlim_eff, self.vlim_q_ok, self.vlim_note, self.vlim_sync_t = None, True, "", 0.0
        self.vlim_pending = False      # приведение VOLT:LIM к настройке отложено (было небезопасно)
        self.last = dict(v=0.0, i=0.0, P=0.0, R=None)
        self.data_wall = 0.0                       # time.time() последнего удачного отсчёта
        self.idn, self.resource, self.notes = "", "", []
        self.sup = self.io = None
        self.logf = None
        self.log_broken, self.local_sent = False, False
        self.err_last = ("", 0.0)
        self.events, self.t_start, self.last_export = [], None, ""

    def _reset_guards(self):
        """Состояние защит: счётчики подтверждения (действие — только по 2 отсчётам подряд)."""
        self.cnt = dict(oc=0, rwin=0, rdrop=0, over=0, mis=0, bad=0)
        self.R_ema = self.R_peak = None
        self.jump_count, self.pmax_warned = 0, False
        self.limiting, self.up_blocked, self.block_reason = False, False, ""
        self.vlim_alarmed, self.over_alarmed, self.over_active = False, False, False
        self.untrusted = True          # первый отсчёт после подхвата/сбоя — только наблюдение

    def ev(self, msg, level="info"):
        self.notes.append(msg)
        self.events.append((datetime.now(), level, msg))
        self.on_event(level, msg)

    def send(self, *cmd):
        self.cmdq.put(cmd)

    def busy(self):
        return self.thread is not None and self.thread.is_alive()

    def connect(self, resource, sim_speed=10.0, timeout_ms=3000):
        if self.busy():
            return
        while not self.cmdq.empty():
            self.cmdq.get_nowait()
        self.alive = True
        self.thread = threading.Thread(target=self._run, daemon=True,
                                       args=(resource, sim_speed, timeout_ms))
        self.thread.start()

    def _vl(self):
        """Предел напряжения, действующий в приборе (если известен), иначе — из настроек."""
        return self.vlim_eff if self.vlim_eff else self.cfg["V_limit"]

    def _fail_connect(self, msg):
        self.ev(msg, "alarm")
        if self.sup is not None:          # не оставлять панель прибора заблокированной
            try:
                self.io.write(SCPI["local"])
            except Exception:
                pass
        self._close()
        self.alive = False
        self.phase = self.PH_NC
        self.snap = self._make_snap()

    def _run(self, resource, sim_speed, timeout_ms):
        self._reset()
        with self.lock:
            self.hist = []
        try:
            if resource == "SIM":
                self.clock = Clock(sim_speed)
                self.io = SimIO(self.clock)
            else:
                self.clock = Clock(None)
                self.io = ItechIO(resource, timeout_ms)
            self.sup = Supply(self.io)
            self.resource = resource
            self._open_log()
            self._takeover()
        except ModuleNotFoundError as e:
            self._fail_connect("Не установлен пакет %s. В командной строке: pip install pyvisa "
                               "pyvisa-py" % e.name)
            return
        except Exception as e:
            self._fail_connect("Не удалось подключиться к %s: %s" % (resource, explain_comm_error(e)))
            return
        keep_awake(True)
        try:
            self._loop()
        finally:
            keep_awake(False)
            if not self.local_sent:        # цикл прервался нештатно — отдать панель прибору
                try:
                    self.io.write(SCPI["local"])
                except Exception:
                    pass
            res = self.resource if self.resource != "SIM" else \
                "модель без прибора (время ускорено ×%g; t в журнале — модельное)" % self.clock.speed
            meta = dict(start=self.t_start, end=datetime.now(), resource=res, idn=self.idn,
                        out_on_end=self.out_on, I_end=self.I_set, phase_end=self.phase,
                        dt=self.cfg["dt"], version=__version__)
            self.alive = False
            self.exporting = True
            self._close()
            self.control = self.poll = False
            self.phase = self.PH_NC
            self.snap = self._make_snap()          # окно сразу видит: сессия закрыта
            self._export(meta)
            self.snap = self._make_snap()

    def _open_log(self):
        d = log_dir()
        base = datetime.now().strftime("pr8_nakal_%Y%m%d_%H%M%S")
        path, n = os.path.join(d, base + ".csv"), 2
        while os.path.exists(path):
            path, n = os.path.join(d, "%s_%d.csv" % (base, n)), n + 1
        self.log_path = path
        self.t_start = datetime.now()
        self.logf = open(self.log_path, "w", newline="", encoding="utf-8-sig")
        self.csv = csv.writer(self.logf)
        self.csv.writerow(["time", "t_s", "phase", "I_set_A", "I_A", "U_V", "P_W",
                           "R_mOhm", "event"])

    def _write_row(self, v, i, P, R):
        if not self.logf or self.log_broken:
            return
        try:
            self.csv.writerow([datetime.now().isoformat(timespec="milliseconds"),
                               "%.1f" % self.clock.now(), self.phase, "%.3f" % self.I_set,
                               "%.3f" % i, "%.3f" % v, "%.1f" % P,
                               "%.2f" % (R * 1e3) if R else "", " | ".join(self.notes)])
            self.notes = []
            self.logf.flush()
        except (OSError, ValueError) as e:
            self.log_broken = True
            self.on_event("alarm", "Журнал CSV больше не пишется (%s). Управление продолжается; "
                                   "события остаются в окне и попадут в Excel." % e)

    def _close(self):
        try:
            if self.io:
                self.io.close()
        except Exception:
            pass
        if self.logf:
            if self.notes:      # события после последнего отсчёта — отдельной строкой
                L = self.last
                self._write_row(L["v"], L["i"], L["P"], L["R"])
            try:
                self.logf.close()
            except Exception:
                pass
        self.logf = None

    def _export(self, meta):
        if not self.log_path or not self.t_start:
            self.exporting = False
            return
        self.exporting = True
        try:
            path = export_session_xlsx(self.log_path, meta=meta, events=list(self.events))
            if path:
                self.last_export = path
                self.on_event("info", "Журнал сессии сохранён в Excel: %s" % path)
        except ModuleNotFoundError:
            self.on_event("warn", "Не установлен openpyxl — Excel не создан, журнал остался в CSV. "
                                  "Установите: pip install openpyxl, затем «CSV → Excel…».")
        except Exception as e:
            self.on_event("alarm", "Не удалось сохранить Excel (%s). Журнал остался в CSV: %s"
                          % (e, self.log_path))
        finally:
            self.exporting = False

    # ---------- главный цикл ----------
    def _loop(self):
        t_prev = self.clock.now() - self.cfg["dt"]
        while self.alive:
            c = self.cfg
            t0 = self.clock.now()
            did_io = False
            try:
                self._handle_cmds()
                if not self.alive:
                    break
                if self.poll:
                    did_io = True
                    self._step(min(max(t0 - t_prev, 1e-3), 2 * c["dt"]))
                if did_io and self.comm_fail:
                    self.ev("Связь с источником восстановлена.")
                    self.comm_fail = 0
            except CommError as e:
                self.comm_fail += 1
                self.untrusted = True          # первый отсчёт после сбоя не используется
                if self.comm_fail in (1, 5) or self.comm_fail % 30 == 0:
                    self.ev("Нет связи с источником (%s). Он держит последнюю уставку %.2f А."
                            % (e, self.I_sent if self.I_sent is not None else self.I_set), "alarm")
            except Exception as e:
                msg = "Внутренняя ошибка: %r" % (e,)
                if msg != self.err_last[0] or time.time() - self.err_last[1] > 30:
                    self.ev(msg, "alarm")
                    self.err_last = (msg, time.time())
            t_prev = t0
            self.snap = self._make_snap()
            self.clock.sleep(c["dt"] - (self.clock.now() - t0))

    def _handle_cmds(self):
        while self.alive:                  # после «отключить» очередь не исполняется
            try:
                cmd = self.cmdq.get_nowait()
            except queue.Empty:
                return
            fn = getattr(self, "_cmd_" + cmd[0], None)
            if fn is None:
                continue
            try:
                fn(*cmd[1:])
            except CommError:
                raise
            except Exception as e:
                self.ev("Команда «%s» не выполнена: %s" % (cmd[0], e), "alarm")

    def _step(self, dt):
        v, i = self.sup.measure()
        on = self.sup.output_is_on()
        if self.out_on and not on:
            # засчитывать отключение только после повторной проверки: одиночный
            # сбойный ответ не должен «обнулить» уставку горячего катода
            on2 = self.sup.output_is_on()
            _, i2 = self.sup.measure()
            if on2 or i2 > 1.0:
                self.ev("Противоречивый ответ о состоянии выхода (OUTP? = 0, ток %.2f А) — "
                        "отсчёт пропущен." % i2, "warn")
                return
            if self.off_pending:
                self.ev("Выход выключен (команда выключения подтвердилась после сбоя связи).")
            else:
                self.ev("Выход источника отключился сам: сработала защита прибора (OVP/OCP/OTP) "
                        "или нажата кнопка на панели. Проверьте прибор.", "alarm")
            self.off_pending = False
            self._mark_off()
            if self.control:
                # чтобы повторное включение выхода с панели не дало прежний ток мгновенно
                self.sup.w("curr", 0.0)
                self.I_sent = 0.0
        elif on and not self.out_on:
            if not self.sup.output_is_on():        # одиночный сбойный ответ — пропустить
                return
            self.out_on = True
            self._reset_guards()
            if self.control:
                I = self.sup.setpoint()
                self.I_set = self.I_sent = self.target = I
                self.segments, self.hold_until, self.seg_hold = [], None, 0.0
                self.phase = self.PH_HOLD
                self.ev("Выход включён извне — удержание %.2f А." % I, "warn")
        self.data_wall = time.time()
        R = v / i if i >= 1.0 else None
        P = v * i
        self.last = dict(v=v, i=i, P=P, R=R)
        if self.control and self.out_on:
            if self.untrusted:
                self.untrusted = False
            elif self._plausible(v, i):
                self._safety(v, i, R)
                if self.out_on and self.control:
                    self._limits(v, i)
                if self.out_on and self.control:
                    self._overpower(i, R)
                if self.out_on and self.control and self.phase in self.RAMPING:
                    self._advance(dt, i, R)
                if self.out_on and self.control:
                    self._sync_vlim_periodic(v, i)
        self._record(v, i, P, R)

    def _plausible(self, v, i):
        """Фильтр одиночных неправдоподобных отсчётов (перепутанные/битые ответы).
        Устойчивое (3 отсчёта подряд) состояние считается настоящим и идёт в защиты."""
        c = self.cfg
        ok = v <= 1.05 * self._vl() + 0.1
        if ok and i >= 1.0:
            ok = 0.3 * c["R_min"] <= v / i <= 3.0 * c["R_max"]
        if ok:
            self.cnt["bad"] = 0
            return True
        self.cnt["bad"] += 1
        if self.cnt["bad"] == 1:
            self.ev("Неправдоподобный отсчёт (U = %.3f В, I = %.2f А) — защиты его пропускают; "
                    "если повторится 3 раза подряд, будет считаться настоящим." % (v, i), "warn")
        return self.cnt["bad"] >= 3

    def _record(self, v, i, P, R):
        with self.lock:
            self.hist.append((self.clock.now(), self.I_set, i, v, R, P))
        self._write_row(v, i, P, R)

    def _make_snap(self):
        now = self.clock.now()
        proj = self._projection(now) if self.phase in self.RAMPING else []
        hl = max(0.0, self.hold_until - now) if self.hold_until is not None else None
        return dict(
            connected=self.alive and self.sup is not None, control=self.control,
            poll=self.poll, out_on=self.out_on, phase=self.phase, I_set=self.I_set,
            I=self.last["i"], U=self.last["v"], P=self.last["P"], R=self.last["R"],
            target=self.target if self.phase in self.RAMPING else None,
            rate=self.rate * 60 if self.phase in self.RAMPING else None,
            hold_left=hl, eta=max(0.0, proj[-1][0] - now) if proj else None, proj=proj,
            t=now, data_wall=self.data_wall, idn=self.idn, resource=self.resource,
            comm_fail=self.comm_fail, log_path=self.log_path, exporting=self.exporting,
            limiting=self.limiting, up_blocked=self.up_blocked, vlim=self._vl())

    def _projection(self, now):
        if self.hold_until is not None:
            pts = [(now, self.I_set)]
            return pts + curve_points(self.hold_until, self.I_set, self.segments)
        return curve_points(now, self.I_set,
                            [(self.target, self.rate, self.seg_hold)] + self.segments)

    def history(self):
        with self.lock:
            return list(self.hist)

    # ---------- предел напряжения в приборе ----------
    def _read_vlim(self):
        """VOLT:LIM? прибора или None (не поддерживается/нет ответа — больше не спрашивать)."""
        if not self.vlim_q_ok:
            return None
        try:
            return self.sup.qf("vlim_q")
        except CommError:
            self.vlim_q_ok = False
            try:
                self.sup.w("cls")
            except CommError:
                pass
            self.ev("Прибор не отвечает на VOLT:LIM? — предел напряжения на включённом выходе "
                    "программа менять не будет.", "warn")
            return None

    def _sync_vlim(self, v, i, why=""):
        """Привести VOLT:LIM прибора к настройке, не вызвав скачка тока:
        повышение — только если уставка выше тока не больше чем на LIM_MARGIN;
        понижение — только если напряжение заметно ниже нового предела."""
        want, have = self.cfg["V_limit"], self.vlim_eff
        if have is not None and abs(have - want) < 0.005:
            self.vlim_note, self.vlim_pending = "", False
            return
        if not self.out_on:
            self.sup.w("vlim", want)
            self.vlim_eff = self._read_vlim() or want
            return
        if have is None:
            return
        if want > have:
            ok = (self.I_set - i) <= LIM_MARGIN + 0.05
            reason = "ток %.2f А ограничен при уставке %.2f А" % (i, self.I_set)
        else:
            ok = v < 0.85 * want
            reason = "напряжение %.2f В слишком близко к новому пределу" % v
        if ok:
            self.sup.w("vlim", want)
            self.vlim_eff = self._read_vlim() or want
            self.vlim_note, self.vlim_pending = "", False
            self.ev("Предел напряжения в приборе %.2f → %.2f В%s." % (have, want, why))
            return
        self.vlim_pending = True
        if self.vlim_note != reason[:12]:
            self.vlim_note = reason[:12]
            self.ev("Предел напряжения в приборе %.2f В, в настройках %.2f В — сейчас менять нельзя "
                    "без скачка тока (%s). Программа работает с пределом прибора." % (have, want, reason),
                    "warn")

    def _sync_vlim_cmd(self):
        if self.out_on and self.vlim_eff is not None and \
                abs(self.vlim_eff - self.cfg["V_limit"]) >= 0.005:
            L = self.last
            self.vlim_sync_t = self.clock.now()
            self._sync_vlim(L["v"], L["i"], " (по настройкам)")

    def _sync_vlim_periodic(self, v, i):
        if not self.vlim_pending or self.vlim_eff is None:
            return
        now = self.clock.now()
        if now - self.vlim_sync_t >= 5.0:
            self.vlim_sync_t = now
            self._sync_vlim(v, i)

    # ---------- подхват состояния ----------
    def _takeover(self):
        self.idn = self.sup.q("idn")
        self.sup.idn = self.idn
        self.ev("Прибор: %s  (программа v%s)" % (self.idn, __version__))
        self.sup.w("remote")
        self.local_sent = False
        on = self.sup.output_is_on()
        func = self.sup.func()
        I = self.sup.setpoint()
        self.segments, self.hold_until, self.seg_hold = [], None, 0.0
        self.off_pending, self.ramp_limit = False, None
        self._reset_guards()
        self.vlim_eff = self._read_vlim()
        self.poll = True
        if on:
            self.out_on = True
            v, i = self.sup.measure()

            def close(a, b):
                return abs(a - b) <= 0.3 + 0.01 * max(a, b)
            consistent = close(i, I)
            if func.startswith("CURR") and not consistent:
                I = self.sup.setpoint()             # перечитать: вдруг ответ был сбойным
                v, i = self.sup.measure()
                consistent = close(i, I)
            v_limited = self.vlim_eff is not None and v >= 0.97 * self.vlim_eff and i < I
            if func.startswith("CURR") and (consistent or v_limited):
                self.control = True
                self.I_set = self.I_sent = self.target = I
                self.phase = self.PH_HOLD
                self.ev("Выход включён, уставка %.2f А — удержание без изменений. "
                        "Дальше: ▲/▼ по кривой или ручная уставка." % I)
                if v_limited and not consistent:
                    self.ev("Источник сейчас ограничен напряжением %.2f В: ток %.2f А при уставке "
                            "%.2f А. Уставка будет подтянута к току, рост тока заблокирован."
                            % (self.vlim_eff, i, I), "alarm")
                if I > self.cfg["I_max"]:
                    self.ev("Уставка прибора %.2f А выше «Максимума уставки» %.1f А — вверх "
                            "программа не пойдёт, вниз — можно." % (I, self.cfg["I_max"]), "warn")
                if self.vlim_eff is None:
                    self.ev("Предел напряжения прибора неизвестен — программа его не меняет и "
                            "считает равным %.1f В." % self.cfg["V_limit"], "warn")
                elif abs(self.vlim_eff - self.cfg["V_limit"]) >= 0.005:
                    # при подключении прибор не меняется: предел приводится к настройке
                    # только по первой команде оператора и только без скачка тока
                    self.ev("Предел напряжения в приборе %.2f В, в настройках %.1f В — будет "
                            "приведён к настройке при первой команде движения, если это не "
                            "вызовет скачка тока." % (self.vlim_eff, self.cfg["V_limit"]), "warn")
            else:
                self.control = False
                self.I_set, self.target = I, I
                self.phase = self.PH_OBS
                why = ("режим приоритета напряжения (%s)" % func) if not func.startswith("CURR") \
                    else ("уставка %.2f А, а ток %.2f А — прибор ограничивает выход или ответы "
                          "сбоят" % (I, i))
                self.ev("Выход включён, но %s. Программа только наблюдает: проверьте прибор, при "
                        "необходимости снизьте накал на панели, затем нажмите «Вернуть управление "
                        "программе»." % why, "alarm")
        else:
            self.out_on = False
            self.control = True
            self.I_set, self.I_sent, self.target = 0.0, None, 0.0
            self.phase = self.PH_OFF
            self.ev("Выход выключен. «▲ Вверх по кривой» включит выход в режиме CC при 0 А.")
        errs = self.sup.errors()
        if errs:
            self.ev("Ошибки прибора: %s" % errs, "warn")

    def _output_on_safe(self):
        c = self.cfg
        if self.sup.output_is_on():
            # программа считала выход выключенным, а он включён — не трогать уставку
            self.ev("Выход прибора уже включён — уставка не сбрасывается, состояние перечитано.",
                    "warn")
            self._takeover()
            return False
        self.sup.w("cls")                  # старые ошибки в очереди не должны мешать проверке
        self.sup.w("curr", 0.0)
        self.sup.w("func_cc")
        self.sup.w("vlim", c["V_limit"])
        self.sup.w("curr", 0.0)
        errs = self.sup.errors()
        func, I0 = self.sup.func(), self.sup.setpoint()
        if errs or not func.startswith("CURR") or I0 > 0.05:
            self.ev("Прибор не перешёл в нужный режим (ошибки: %s; режим %s; уставка %.2f А). "
                    "Выход НЕ включён — проверьте словарь SCPI по IT6000D Programming Guide."
                    % (errs or "нет", func, I0), "alarm")
            return False
        self.vlim_eff = self._read_vlim() or c["V_limit"]
        self.sup.w("out_on")
        if not self.sup.output_is_on():
            self.ev("Команда включения выхода не подействовала (выход выключен). Проверьте "
                    "прибор: блокировка, защита, интерлок.", "alarm")
            return False
        self.out_on = True
        self.I_set, self.I_sent, self.target = 0.0, 0.0, 0.0
        self.off_pending = False
        self._reset_guards()
        self.ev("Режим CC, U_lim = %.1f В, выход включён при 0 А." % self.vlim_eff)
        return True

    def _mark_off(self):
        self.out_on = False
        self.I_set, self.I_sent, self.target = 0.0, 0.0, 0.0
        self.segments, self.hold_until, self.seg_hold = [], None, 0.0
        self.limiting, self.up_blocked = False, False
        if self.control:
            self.phase = self.PH_OFF

    def _output_off(self, msg, level="info"):
        self.sup.w("curr", 0.0)
        # уставка 0 принята — состояние программы сразу ей соответствует, чтобы при
        # сбое на OUTP OFF не остаться с «уставкой 30 А» при реальных 0 А
        self.I_set = self.I_sent = self.target = 0.0
        self.segments, self.hold_until, self.seg_hold, self.ramp_limit = [], None, 0.0, None
        self.phase, self.off_pending = self.PH_HOLD, True
        self.clock.sleep(0.5)
        self.sup.w("out_off")
        if self.sup.output_is_on():
            self.ev("%s Но выход остался ВКЛЮЧЁН при 0 А — команда OUTP OFF не подействовала, "
                    "выключите выход на панели прибора." % msg, "alarm")
            return
        self.off_pending = False
        self._mark_off()
        self.phase = self.PH_OFF
        self.ev(msg, level)

    # ---------- команды оператора ----------
    def _need_control(self):
        if not self.control:
            self.ev("Сейчас программа не управляет прибором (%s). Нажмите «Вернуть управление "
                    "программе»." % self.phase.lower(), "warn")
            return False
        return True

    def _refuse_up(self):
        """Рост тока невозможен, пока источник ограничивает ток или напряжение у предела."""
        if self.out_on and (self.limiting or self.up_blocked):
            self.ev("Рост тока заблокирован: %s." % (self.block_reason or "источник ограничивает ток"),
                    "alarm")
            return True
        return False

    def _start(self, phase, segs, msg):
        self.phase = phase
        self.segments = list(segs)
        self.hold_until, self.seg_hold = None, 0.0
        self.R_peak, self.jump_count, self.pmax_warned = None, 0, False
        self.cnt["rdrop"] = 0
        self.off_pending = False
        self.ev(msg)
        self._next_segment()

    def _cmd_up(self, limit):
        if not self._need_control():
            return
        c = self.cfg
        limit = float(limit)
        if not math.isfinite(limit):
            return
        limit = min(limit, c["I_max"])
        I_from = self.I_set if self.out_on else 0.0
        segs = up_segments(c["heat_profile"], I_from, limit)
        if not segs:               # в т. ч. «▲ до 0» и цель ниже текущего тока
            self.ev("Ток %.2f А уже не ниже %.1f А — движение вверх не нужно." % (I_from, limit), "warn")
            return
        if self._refuse_up():
            return
        if not self.out_on and not self._output_on_safe():
            return
        self._sync_vlim_cmd()
        self.off_at_zero, self.ramp_limit = False, limit
        self._start(self.PH_UP, segs, "▲ ВВЕРХ ПО КРИВОЙ: %.2f → %.1f А" % (self.I_set, limit))

    def _cmd_down(self, limit):
        if not self._need_control():
            return
        if not self.out_on:
            self.ev("Выход выключен — снижать нечего.", "warn")
            return
        limit = float(limit)
        if not math.isfinite(limit):
            return
        limit = max(0.0, limit)
        segs = down_segments(self.cfg["cool_profile"], self.I_set, limit)
        if not segs:               # в т. ч. цель выше текущего тока
            if limit <= 0.05 and self.I_set <= 0.05:
                self._output_off("Ток 0 — выход выключен.")
            else:
                self.ev("Ток %.2f А уже не выше %.1f А — движение вниз не нужно." % (self.I_set, limit),
                        "warn")
            return
        self._sync_vlim_cmd()
        self.off_at_zero, self.ramp_limit = limit <= 0.05, limit
        self._start(self.PH_DOWN, segs, "▼ ВНИЗ ПО КРИВОЙ: %.2f → %.1f А%s" % (
            self.I_set, limit, ", затем выключение выхода" if self.off_at_zero else ""))

    def _cmd_stop(self):
        if not self._need_control() or not self.out_on:
            return
        self._freeze("■ СТОП: ток зафиксирован на %.2f А." % self.I_set, "warn")

    def _freeze(self, msg, level):
        self.segments, self.hold_until, self.seg_hold = [], None, 0.0
        self.target = self.I_set
        self.phase = self.PH_HOLD
        self.ramp_limit = None
        self.ev(msg, level)

    def _cmd_manual(self, target, rate):
        if not self._need_control():
            return
        c = self.cfg
        target, rate = float(target), float(rate)
        if not (math.isfinite(target) and math.isfinite(rate)):
            return
        target = min(max(target, 0.0), c["I_max"])
        rate = min(max(rate, 0.05), c["manual_rate_max"])
        if not self.out_on:
            if target <= 0:
                return
            if not self._output_on_safe():
                return
        elif target > self.I_set + 1e-6 and self._refuse_up():
            return
        self._sync_vlim_cmd()
        self.off_at_zero, self.ramp_limit = False, None
        self._start(self.PH_MAN, [(target, rate / 60.0, 0.0)],
                    "РУЧНОЕ: %.2f → %.2f А, %.2f А/мин" % (self.I_set, target, rate))

    def _cmd_step(self, d):
        if not self._need_control():
            return
        if not self.out_on:
            self.ev("Выход выключен — шаг ±1 А не выполняется (включите выход через ▲ или "
                    "ручную уставку).", "warn")
            return
        d = float(d)
        # шаг в сторону текущего ручного движения продлевает его, в обратную — разворачивает
        if self.phase == self.PH_MAN and (d > 0) == (self.target > self.I_set):
            base = self.target
        else:
            base = self.I_set
        self._cmd_manual(base + d, self.cfg["manual_rate"])

    def _cmd_fault(self):
        if not self._need_control() or not self.out_on:
            return
        self.off_at_zero, self.ramp_limit = True, None
        self._start(self.PH_FAULT, [(0.0, self.cfg["fault_rate"] / 60.0, 0.0)],
                    "АВАРИЙНОЕ СНИЖЕНИЕ до 0 со скоростью %.1f А/мин" % self.cfg["fault_rate"])

    def _cmd_off(self):
        if not self._need_control():
            return
        self._output_off("ВЫХОД ВЫКЛЮЧЕН оператором (без плавного снижения).", "warn")

    def _cmd_local(self):
        if not (self.control or self.phase == self.PH_OBS):
            return
        self.segments, self.hold_until, self.seg_hold = [], None, 0.0
        self.target, self.ramp_limit = self.I_set, None
        sent = True
        try:
            self.sup.w("local")
        except CommError:
            sent = False
        self.local_sent = True
        self.control = self.poll = False
        self.phase = self.PH_PANEL
        self.ev("Управление передано на переднюю панель. Источник держит %.2f А; программа "
                "не отправляет команд и не опрашивает прибор.%s" % (
                    self.I_sent if self.I_sent is not None else self.I_set,
                    "" if sent else " Команда SYST:LOC не дошла — нажмите на приборе кнопку "
                                    "Local (обычно Shift + Local)."), "warn")

    def _cmd_remote(self):
        self.ev("Возврат управления программе — читаю текущее состояние прибора…")
        try:
            self._takeover()
        except CommError as e:
            try:
                self.sup.w("local")
            except CommError:
                pass
            self.local_sent = True
            self.control = self.poll = False
            self.phase = self.PH_PANEL
            self.ev("Не удалось прочитать прибор (%s) — управление оставлено на панели." % e, "alarm")

    def _cmd_cfg(self, cfg):
        self.cfg = cfg
        msg = "Профиль и пределы обновлены."
        if self.phase in (self.PH_UP, self.PH_DOWN) and self.ramp_limit is not None:
            if self.phase == self.PH_UP:
                self.ramp_limit = min(self.ramp_limit, cfg["I_max"])
                segs = up_segments(cfg["heat_profile"], self.I_set, self.ramp_limit)
            else:
                segs = down_segments(cfg["cool_profile"], self.I_set, self.ramp_limit)
            if self.hold_until is not None:
                self.segments = segs                       # идущая выдержка сохраняется
            elif segs:
                self.target, self.rate, self.seg_hold = segs[0]
                self.segments = segs[1:]
            else:
                self.segments, self.target, self.seg_hold = [], self.I_set, 0.0
            msg += " Текущее движение пересчитано по новой кривой от %.2f А." % self.I_set
        elif self.phase == self.PH_MAN:
            if self.target > self.I_set:
                self.target = max(self.I_set, min(self.target, cfg["I_max"]))
            self.rate = min(self.rate, cfg["manual_rate_max"] / 60.0)
        elif self.phase == self.PH_FAULT:
            self.rate = cfg["fault_rate"] / 60.0
        if self.I_set > cfg["I_max"] + 0.05:
            msg += " Ток %.2f А выше нового максимума уставки — вверх программа не пойдёт." % self.I_set
        self.ev(msg)
        if self.control and self.sup is not None and self.poll and self.out_on \
                and self.vlim_eff is not None:
            L = self.last
            self.vlim_sync_t = self.clock.now()
            self._sync_vlim(L["v"], L["i"], " (изменена настройка)")

    def _cmd_disconnect(self):
        if self.control or self.phase == self.PH_OBS:
            try:
                if self.comm_fail:
                    self.io.write(SCPI["local"])       # одна попытка, без повторов
                else:
                    self.sup.w("local")
                self.local_sent = True
            except Exception:
                pass
        self.ev("Отключено. Выход %s%s; управление — на передней панели." % (
            "ВКЛЮЧЁН" if self.out_on else "выключен",
            ", ток %.2f А" % self.I_set if self.out_on else ""),
            "warn" if self.out_on else "info")
        self.alive = False

    # ---------- уставка ----------
    def _next_segment(self):
        if not self.segments:
            return False
        self.target, self.rate, self.seg_hold = self.segments.pop(0)
        self.ev("→ %.1f А, %.2f А/мин%s" % (self.target, self.rate * 60,
                ", затем выдержка %.0f с" % self.seg_hold if self.seg_hold else ""))
        return True

    def _ramp_done(self):
        if self.phase in self.FALLING and self.off_at_zero and self.I_set <= 1e-6:
            self._output_off("Ток снижен до 0 — выход выключен.")
        else:
            self.phase, self.ramp_limit = self.PH_HOLD, None
            self.ev("Достигнуто %.2f А — удержание." % self.I_set)

    def _set_curr(self, new):
        """Единственное место, где программа меняет ток прибора. Скачок больше
        MAX_STEP_A одной командой не отправляется ни при каких обстоятельствах."""
        if self.I_sent is not None and abs(new - self.I_sent) > MAX_STEP_A:
            self._freeze("ЗАЩИТА ОТ СКАЧКА: попытка изменить уставку %.2f → %.2f А одной "
                         "командой. Команда не отправлена, ток зафиксирован."
                         % (self.I_sent, new), "alarm")
            return False
        if self.I_sent is None or round(new, 2) != round(self.I_sent, 2):
            self.sup.w("curr", new)
            self.I_sent = new
        self.I_set = new
        return True

    def _advance(self, dt, i, R):
        c = self.cfg
        now = self.clock.now()
        if self.hold_until is not None:
            if now < self.hold_until:
                return
            self.hold_until = None
            if not self._next_segment():
                self._ramp_done()
            return
        diff = self.target - self.I_set
        if abs(diff) < 1e-6:
            if self.seg_hold > 0:
                self.hold_until = now + self.seg_hold
                self.ev("выдержка %.0f с при %.2f А" % (self.seg_hold, self.I_set))
                self.seg_hold = 0.0
            elif not self._next_segment():
                self._ramp_done()
            return
        step = self.rate * dt
        if diff > 0:
            if self.up_blocked or self.limiting:
                self._freeze("Рост тока остановлен: %s." % (self.block_reason or
                                                            "источник ограничивает ток"), "alarm")
                return
            if self.I_set >= c["I_max"] - 1e-6:
                self._freeze("Достигнут максимум уставки %.1f А — рост остановлен." % c["I_max"],
                             "warn")
                return
            dI = min(step, diff)
            if R and R > 0:
                I_pm = math.sqrt(c["P_max"] / R)
                if I_pm <= self.I_set + 1e-6:
                    self._freeze("Достигнут предел мощности P_max = %.0f Вт при %.2f А — рост "
                                 "тока остановлен." % (c["P_max"], self.I_set), "alarm")
                    return
                # прирост мощности считается от уставки, а не от измеренного тока:
                # иначе погрешность измерения (ток чуть ниже уставки) останавливает рост
                I_dp = math.sqrt(self.I_set ** 2 + c["dPdt_max"] * dt / R)
                dI = min(dI, I_dp - self.I_set, I_pm - self.I_set)
            new = min(self.I_set + dI, c["I_max"])
        else:
            new = max(self.I_set + max(-step, diff), 0.0)
        if abs(new - self.I_set) > 1e-6:
            self._set_curr(new)

    def _overpower(self, i, R):
        """Мощность выше P_max: срабатывает выше 1,02·P_max (2 отсчёта), снижает ток
        до P ≤ P_max; сообщение — один раз, пока мощность не опустится ниже 0,95·P_max."""
        c = self.cfg
        P = i * i * R if R else 0.0
        if P < 0.95 * c["P_max"]:
            self.over_alarmed = False
        if not self.over_active:
            if P <= 1.02 * c["P_max"]:
                self.cnt["over"] = 0
                return
            self.cnt["over"] += 1
            if self.cnt["over"] < 2:
                return
            self.over_active = True
        if P <= c["P_max"]:
            self.over_active, self.cnt["over"] = False, 0
            return
        if self.phase in self.FALLING:
            return                          # снижение и так идёт — не ускорять его
        if self.phase == self.PH_UP or (self.phase == self.PH_MAN and self.target > self.I_set):
            self._freeze("Мощность %.0f Вт выше P_max = %.0f Вт — рост остановлен."
                         % (P, c["P_max"]), "alarm")
        if not self.over_alarmed:
            self.over_alarmed = True
            self.ev("Мощность %.0f Вт выше P_max = %.0f Вт — ток плавно снижается (%.1f А/мин) до "
                    "предела." % (P, c["P_max"], c["manual_rate"]), "alarm")
        new = max(0.0, self.I_set - c["manual_rate"] / 60.0 * c["dt"])
        if self._set_curr(new):
            self.target = min(self.target, new)

    # ---------- защиты ----------
    def _limits(self, v, i):
        """Каждый отсчёт: ограничивает ли источник ток и совпадает ли уставка с прибором.
        Пока ток ограничен — рост запрещён, а уставка подтягивается к реальному току
        (снижение уставки, пока она выше ограниченного тока, сам ток не меняет), так что
        снятие ограничения даёт скачок не больше LIM_MARGIN. Снижение (▼, авария) идёт."""
        vl = self._vl()
        at_v = v >= 0.97 * vl
        thr = 0.3 + 0.01 * max(self.I_set, i)
        # при упоре в предел напряжения ограничение известно наверняка — уставка не должна
        # оставаться выше тока больше чем на LIM_MARGIN (иначе снятие предела = скачок)
        below = self.I_set - i > ((LIM_MARGIN + 0.1) if at_v else thr)
        above = i - self.I_set > thr + 0.2
        if at_v:
            self.block_reason = "напряжение у предела U_lim = %.2f В" % vl
            if not self.vlim_alarmed:
                self.vlim_alarmed = True
                self.ev("Напряжение у предела U_lim = %.2f В (ток %.2f А, уставка %.2f А) — рост тока "
                        "заблокирован." % (vl, i, self.I_set), "alarm")
        elif v < 0.9 * vl:
            self.vlim_alarmed = False
        if self.limiting:
            if at_v or self.I_set - i > 0.3:
                self.block_reason = self.block_reason if at_v else "источник ограничивает ток"
                if self.I_set - i > LIM_MARGIN + 0.1:
                    new = max(i + LIM_MARGIN, self.I_set - 0.9 * MAX_STEP_A)
                    if self._set_curr(new) and self.phase not in self.FALLING:
                        self.target = min(self.target, self.I_set)
            else:
                self.limiting = False
                self.ev("Ограничение снято: ток %.2f А снова равен уставке." % i)
            self.up_blocked = self.limiting or at_v
            return
        self.up_blocked = at_v or below or above
        if not self.up_blocked:
            self.block_reason = ""
        if not (below or above):
            self.cnt["mis"] = 0
            return
        if not at_v:
            self.block_reason = "ток не следует за уставкой"
        self.cnt["mis"] += 1
        if self.cnt["mis"] < 2:
            return
        self.cnt["mis"] = 0
        # прежде чем что-то менять — сверить уставку в приборе
        dev = self.sup.setpoint()
        mine = self.I_sent if self.I_sent is not None else self.I_set
        if below and abs(dev - mine) <= 0.02:
            self.limiting = True
            self.block_reason = ("напряжение у предела U_lim = %.2f В" % vl) if at_v \
                else "источник ограничивает ток"
            self.ev("ИСТОЧНИК ОГРАНИЧИВАЕТ ТОК: %.2f А при уставке %.2f А (%s). Уставка "
                    "подтягивается к реальному току, рост заблокирован%s."
                    % (i, dev, "напряжение у предела" if at_v else "предел мощности/тока прибора?",
                       ", снижение продолжается" if self.phase in self.FALLING else ""), "alarm")
            if self.phase in (self.PH_UP, self.PH_MAN) and self.target > self.I_set:
                self._freeze("Движение вверх остановлено.", "alarm")
        elif abs(dev - i) <= thr + 0.2:
            self.I_set = self.I_sent = dev
            if self.phase in self.FALLING:
                self.ev("РАССИНХРОНИЗАЦИЯ: программа считала уставку %.2f А, в приборе %.2f А (ток "
                        "%.2f А). Уставка перечитана, снижение продолжается от неё." % (mine, dev, i),
                        "alarm")
            else:
                self.target = dev
                self._freeze("РАССИНХРОНИЗАЦИЯ: программа считала уставку %.2f А, в приборе %.2f А "
                             "(ток %.2f А). Уставка перечитана из прибора, движение остановлено."
                             % (mine, dev, i), "alarm")
        else:
            self.segments, self.hold_until, self.seg_hold = [], None, 0.0
            self.control = False
            self.phase = self.PH_OBS
            self.I_set = self.target = dev
            self.ev("Состояние прибора неясно: уставка %.2f А, ток %.2f А, программа считала "
                    "%.2f А. Управление остановлено — только наблюдение. Проверьте прибор и "
                    "нажмите «Вернуть управление программе»." % (dev, i, mine), "alarm")

    def _safety(self, v, i, R):
        c = self.cfg
        # обрыв: тока нет совсем, напряжение у предела — подтверждается двумя отсчётами
        if self.I_set > 2.0 and i < max(0.5, 0.05 * self.I_set) and v > 0.9 * self._vl():
            self.cnt["oc"] += 1
            if self.cnt["oc"] >= 2:
                self.cnt["oc"] = 0
                self._output_off("ОБРЫВ ЦЕПИ НАКАЛА: U = %.2f В, I = %.2f А при уставке %.2f А — "
                                 "выход выключен." % (v, i, self.I_set), "alarm")
            return
        self.cnt["oc"] = 0
        if R is None or i < c["I_check"] or self.phase == self.PH_FAULT:
            self.cnt["rwin"] = 0
            return
        if R < c["R_min"] or R > c["R_max"]:
            self.cnt["rwin"] += 1
            if self.cnt["rwin"] >= 2:
                self.cnt["rwin"] = 0
                self.off_at_zero, self.ramp_limit = True, None
                self.ev("ТРЕВОГА: R = %.1f мОм вне окна [%.0f; %.0f] мОм." % (
                    R * 1e3, c["R_min"] * 1e3, c["R_max"] * 1e3), "alarm")
                self._start(self.PH_FAULT, [(0.0, c["fault_rate"] / 60.0, 0.0)],
                            "АВАРИЙНОЕ СНИЖЕНИЕ до 0 со скоростью %.1f А/мин" % c["fault_rate"])
            return
        self.cnt["rwin"] = 0
        rising = self.phase == self.PH_UP or (self.phase == self.PH_MAN and self.target > self.I_set)
        if rising:
            if self.R_peak is None or R > self.R_peak:
                self.R_peak, self.cnt["rdrop"] = R, 0
            elif R < (1 - c["R_drop"]) * self.R_peak:
                self.cnt["rdrop"] += 1
                if self.cnt["rdrop"] >= 2:
                    self._freeze("R ПАДАЕТ ПРИ РОСТЕ ТОКА: %.1f → %.1f мОм (−%.0f %%). Похоже на "
                                 "контакт нагревателя с корпусом (как 05.10). Ток зафиксирован."
                                 % (self.R_peak * 1e3, R * 1e3, (1 - R / self.R_peak) * 100), "alarm")
                    self.R_peak, self.cnt["rdrop"] = None, 0
            else:
                self.cnt["rdrop"] = 0
        else:
            self.R_peak, self.cnt["rdrop"] = None, 0
        if self.R_ema is None:
            self.R_ema = R
            return
        rel = abs(R - self.R_ema) / self.R_ema
        self.jump_count = self.jump_count + 1 if rel > c["R_jump"] else 0
        if self.jump_count >= 2:
            msg = ("СКАЧОК R: %.1f → %.1f мОм (контакт с корпусом? давление? если только что "
                   "зажгли разряд — это ожидаемо)." % (self.R_ema * 1e3, R * 1e3))
            if self.phase in (self.PH_UP, self.PH_MAN, self.PH_DOWN):
                self._freeze(msg + " Ток зафиксирован.", "alarm")
            else:
                self.ev(msg, "alarm")
            self.jump_count = 0
            self.R_ema = R
            return
        self.R_ema = 0.7 * self.R_ema + 0.3 * R


# ================================ интерфейс ================================
def run_gui(test_hook=None):
    import tkinter as tk
    from tkinter import ttk, messagebox
    from tkinter.scrolledtext import ScrolledText
    import matplotlib
    matplotlib.use("TkAgg")
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

    C_UP, C_DOWN, C_STOP, C_RED = "#2e7d32", "#1565c0", "#e08a00", "#c62828"

    def fnum(s):
        try:
            x = float(str(s).strip().replace(",", "."))
        except ValueError:
            raise ValueError("«%s» — не число" % str(s).strip())
        if not math.isfinite(x):
            raise ValueError("«%s» — не конечное число" % str(s).strip())
        return x

    class App(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title("ПР-8 · Накал катода LaB₆ — ITECH IT6012D-300-150 · v%s" % __version__)
            self.geometry("1400x900")
            self.minsize(1100, 650)
            for z in (lambda: self.state("zoomed"), lambda: self.attributes("-zoomed", True)):
                try:
                    z()
                    break
                except tk.TclError:
                    pass
            self.settings = load_settings()
            self.evq = queue.Queue()
            self.uiq = queue.Queue()          # результаты фоновых задач → поток окна
            self._closing = False
            self._comm_bad = False
            for w in LOAD_WARNINGS:
                self.evq.put((datetime.now().strftime("%H:%M:%S"), "warn", w))
            self.engine = Engine(cfg_from_settings(self.settings),
                                 lambda lvl, msg: self.evq.put((datetime.now().strftime("%H:%M:%S"), lvl, msg)))
            self._style()
            self._build_connection()
            nb = ttk.Notebook(self)
            nb.pack(fill="both", expand=True, padx=8, pady=(0, 8))
            self.tab_ctl = ttk.Frame(nb)
            self.tab_prof = ttk.Frame(nb)
            nb.add(self.tab_ctl, text="  Управление  ")
            nb.add(self.tab_prof, text="  Кривая и пределы  ")
            self._build_control(self.tab_ctl)
            self._build_profile(self.tab_prof)
            self.bind_all("<Escape>", lambda e: self._stop())
            self.protocol("WM_DELETE_WINDOW", self._on_close)
            self._last_plot = 0.0
            self._was_connected = False
            self._connecting = False
            self._last_phase = None
            self.after(300, self._tick)

        # ---------------- оформление ----------------
        def _style(self):
            st = ttk.Style(self)
            try:
                st.theme_use("clam")
            except tk.TclError:
                pass
            base = ("Segoe UI", 10)
            self.option_add("*Font", base)
            st.configure(".", font=base)
            st.configure("TLabelframe.Label", font=("Segoe UI", 10, "bold"))
            for name, col in (("Up", C_UP), ("Down", C_DOWN), ("Stop", C_STOP), ("Red", C_RED)):
                st.configure(name + ".TButton", foreground="white", background=col,
                             font=("Segoe UI", 11, "bold"), padding=(10, 6))
                st.map(name + ".TButton", background=[("disabled", "#b0b0b0"), ("active", col)],
                       foreground=[("disabled", "#eeeeee")])
            st.configure("Big.TLabel", font=("Segoe UI", 17, "bold"))
            st.configure("Val.TLabel", font=("Consolas", 13, "bold"))
            st.configure("Cap.TLabel", foreground="#555555")
            st.configure("Res.TLabel", font=("Consolas", 10, "bold"))

        # ---------------- подключение ----------------
        def _build_connection(self):
            cs = self.settings["conn"]
            fr = ttk.LabelFrame(self, text="Подключение к источнику")
            fr.pack(fill="x", padx=8, pady=(6, 4))
            self.v_kind = tk.StringVar(value=cs["kind"])
            self.v_ip = tk.StringVar(value=cs["ip"])
            self.v_port = tk.StringVar(value=str(cs["port"]))
            self.v_proto = tk.StringVar(value=cs["lan_proto"])
            self.v_visa = tk.StringVar(value=cs["visa"])
            self.v_custom = tk.StringVar(value=cs["custom"])
            self.v_speed = tk.StringVar(value=str(cs["sim_speed"]))
            self.v_res = tk.StringVar()

            kinds = ttk.Frame(fr)
            kinds.grid(row=0, column=0, columnspan=3, sticky="w", padx=6, pady=(2, 0))
            self.kind_buttons = []
            for k, text in (("lan", "LAN (Ethernet)"), ("visa", "USB / GPIB / COM (VISA)"),
                            ("custom", "Адрес вручную"), ("sim", "Модель без прибора")):
                b = ttk.Radiobutton(kinds, text=text, value=k, variable=self.v_kind,
                                    command=self._kind_changed)
                b.pack(side="left", padx=(0, 18))
                self.kind_buttons.append(b)

            self.f_opts = ttk.Frame(fr)
            self.f_opts.grid(row=1, column=0, sticky="w", padx=6, pady=2)
            self.f_lan = ttk.Frame(self.f_opts)
            ttk.Label(self.f_lan, text="IP-адрес").pack(side="left")
            self.e_ip = ttk.Entry(self.f_lan, textvariable=self.v_ip, width=16)
            self.e_ip.pack(side="left", padx=(4, 12))
            ttk.Label(self.f_lan, text="Протокол").pack(side="left")
            self.c_proto = ttk.Combobox(self.f_lan, textvariable=self.v_proto, width=9,
                                        values=["Socket", "VXI-11"], state="readonly")
            self.c_proto.pack(side="left", padx=(4, 12))
            self.c_proto.bind("<<ComboboxSelected>>", lambda e: self._kind_changed())
            ttk.Label(self.f_lan, text="Порт").pack(side="left")
            self.e_port = ttk.Entry(self.f_lan, textvariable=self.v_port, width=7)
            self.e_port.pack(side="left", padx=4)
            ttk.Label(self.f_lan, text="IP и порт — в меню прибора System → LAN",
                      style="Cap.TLabel").pack(side="left", padx=12)

            self.f_visa = ttk.Frame(self.f_opts)
            self.c_visa = ttk.Combobox(self.f_visa, textvariable=self.v_visa, width=52)
            self.c_visa.pack(side="left")
            self.b_find = ttk.Button(self.f_visa, text="Найти устройства", command=self._find)
            self.b_find.pack(side="left", padx=6)
            ttk.Label(self.f_visa, text="ITECH по USB: USB0::0x2EC7::…::INSTR",
                      style="Cap.TLabel").pack(side="left", padx=8)

            self.f_custom = ttk.Frame(self.f_opts)
            self.e_custom = ttk.Entry(self.f_custom, textvariable=self.v_custom, width=58)
            self.e_custom.pack(side="left")
            ttk.Label(self.f_custom, text="любой VISA-адрес, например ASRL3::INSTR",
                      style="Cap.TLabel").pack(side="left", padx=8)

            self.f_sim = ttk.Frame(self.f_opts)
            ttk.Label(self.f_sim, text="Ускорение времени ×").pack(side="left")
            self.c_speed = ttk.Combobox(self.f_sim, textvariable=self.v_speed, width=5,
                                        values=["1", "10", "60"], state="readonly")
            self.c_speed.pack(side="left", padx=4)
            ttk.Label(self.f_sim, text="модель нагревателя по протоколам ПР-8 — для освоения кнопок",
                      style="Cap.TLabel").pack(side="left", padx=8)

            row = ttk.Frame(fr)
            row.grid(row=2, column=0, sticky="we", padx=6, pady=(2, 4))
            ttk.Label(row, text="Адрес:").pack(side="left")
            ttk.Label(row, textvariable=self.v_res, style="Res.TLabel").pack(side="left", padx=(4, 16))
            self.b_check = ttk.Button(row, text="Проверить связь", command=self._check)
            self.b_check.pack(side="left")
            self.b_conn = ttk.Button(row, text="Подключить", command=self._connect)
            self.b_conn.pack(side="left", padx=6)
            self.b_disc = ttk.Button(row, text="Отключить", command=self._disconnect)
            self.b_disc.pack(side="left")
            ttk.Button(row, text="Папка журналов", command=self._open_logs).pack(side="left", padx=(12, 0))
            ttk.Button(row, text="CSV → Excel…", command=self._csv_to_xlsx).pack(side="left", padx=6)
            self.l_status = tk.Label(row, text="Не подключено", anchor="w", fg="#555555",
                                     font=("Segoe UI", 10))
            self.l_status.pack(side="left", padx=12, fill="x", expand=True)
            fr.columnconfigure(0, weight=1)
            for v in (self.v_ip, self.v_port, self.v_visa, self.v_custom, self.v_proto):
                v.trace_add("write", lambda *a: self._update_res())
            self._kind_changed()

        def _conn_from_ui(self):
            c = dict(self.settings["conn"])
            c.update(kind=self.v_kind.get(), ip=self.v_ip.get(), port=self.v_port.get(),
                     lan_proto=self.v_proto.get(), visa=self.v_visa.get(),
                     custom=self.v_custom.get(), sim_speed=self.v_speed.get())
            return c

        def _update_res(self):
            self.v_res.set(build_resource(self._conn_from_ui()) or "—")

        def _kind_changed(self):
            for f in (self.f_lan, self.f_visa, self.f_custom, self.f_sim):
                f.pack_forget()
            {"lan": self.f_lan, "visa": self.f_visa, "custom": self.f_custom,
             "sim": self.f_sim}[self.v_kind.get()].pack(side="left")
            self.e_port.configure(state="normal" if self.v_proto.get() == "Socket" else "disabled")
            self._update_res()

        def _status(self, text, color="#555555"):
            self.l_status.configure(text=text, fg=color)

        def _bg(self, work, done):
            """Фоновая задача; итог обрабатывается в потоке окна (через uiq в _tick)."""
            def runner():
                try:
                    res = (True, work())
                except Exception as e:
                    res = (False, e)
                self.uiq.put((done, res))
            threading.Thread(target=runner, daemon=True).start()

        def _find(self):
            self._status("Поиск VISA-устройств…")
            self.b_find.configure(state="disabled")

            def done(ok, res):
                self.b_find.configure(state="normal")
                if ok is False and isinstance(res, ModuleNotFoundError):
                    self._status("Не установлен pyvisa: pip install pyvisa pyvisa-py", C_RED)
                    return
                if not ok:
                    self._status("Поиск не удался: %s. Установлен ли pyvisa? "
                                 "pip install pyvisa pyvisa-py" % res, C_RED)
                    return
                self.c_visa.configure(values=res)
                if res and not self.v_visa.get():
                    self.v_visa.set(res[0])
                self._status("Найдено устройств: %d. LAN-приборы с raw-socket в списке обычно "
                             "не видны — для них вкладка LAN." % len(res) if res else
                             "VISA-устройства не найдены. Проверьте кабель/драйвер или используйте LAN.",
                             "#555555" if res else C_STOP)
            self._bg(list_visa_resources, done)

        def _check(self):
            res = build_resource(self._conn_from_ui())
            if not res:
                self._status("Адрес не задан.", C_RED)
                return
            self._status("Проверка %s…" % res)
            self.b_check.configure(state="disabled")

            def done(ok, ans):
                self.b_check.configure(state="normal")
                if ok:
                    self._status("✓ Прибор ответил: %s" % ans, C_UP)
                elif isinstance(ans, ModuleNotFoundError):
                    self._status("✗ Не установлен pyvisa: pip install pyvisa pyvisa-py", C_RED)
                else:
                    self._status("✗ Нет ответа от %s: %s" % (res, explain_comm_error(ans)), C_RED)
            self._bg(lambda: check_connection(res, self.settings["conn"]["timeout_ms"]), done)

        def _connect(self):
            conn = self._conn_from_ui()
            res = build_resource(conn)
            if not res:
                self._status("Адрес не задан.", C_RED)
                return
            self.settings["conn"] = conn
            try:
                save_settings(self.settings)
            except Exception:
                pass
            speed = 10.0
            if conn["kind"] == "sim":
                try:
                    speed = fnum(conn["sim_speed"])
                except ValueError:
                    pass
            # настройки, применённые пока сохранялся журнал прошлой сессии, не теряются
            self.engine.cfg = cfg_from_settings(self.settings)
            self._status("Подключение к %s…" % res)
            self._connecting = True
            self.engine.connect(res, sim_speed=speed, timeout_ms=conn["timeout_ms"])

        def _open_logs(self):
            try:
                d = log_dir()
                if hasattr(os, "startfile"):
                    os.startfile(d)
                else:
                    import subprocess
                    subprocess.Popen(["xdg-open", d])
            except Exception as e:
                messagebox.showinfo("Папка журналов", "%s\n\n(%s)" % (LOG_DIR, e))

        def _csv_to_xlsx(self):
            from tkinter import filedialog
            try:
                d = log_dir()
            except OSError:
                d = APP_DIR
            path = filedialog.askopenfilename(title="Журнал CSV для экспорта в Excel",
                                              initialdir=d, filetypes=[("Журнал CSV", "*.csv")])
            if not path:
                return
            self._status("Экспорт %s в Excel…" % os.path.basename(path))

            def done(ok, res):
                if ok and res:
                    self._status("✓ Excel сохранён: %s" % res, C_UP)
                elif ok:
                    self._status("В журнале нет данных — Excel не создан.", C_STOP)
                elif isinstance(res, ModuleNotFoundError):
                    self._status("✗ Не установлен openpyxl: pip install openpyxl", C_RED)
                else:
                    self._status("✗ Экспорт не удался: %s" % res, C_RED)
            self._bg(lambda: export_session_xlsx(path), done)

        def _disconnect(self):
            s = self.engine.snap
            if s.get("out_on") and s.get("I_set", 0) > 1:
                if not messagebox.askyesno(
                        "Отключиться?",
                        "Выход включён, ток %.1f А.\n\nПосле отключения источник продолжит держать "
                        "этот ток, управление перейдёт на переднюю панель.\n\nОтключиться?" % s["I_set"]):
                    return
            self.engine.send("disconnect")

        # ---------------- управление ----------------
        def _build_control(self, parent):
            parent.columnconfigure(1, weight=1)
            parent.rowconfigure(0, weight=1)
            outer = ttk.Frame(parent)
            outer.grid(row=0, column=0, sticky="ns", padx=(6, 8), pady=6)
            bg = ttk.Style(self).lookup("TFrame", "background") or "#dcdad5"
            cnv = tk.Canvas(outer, highlightthickness=0, bg=bg)
            sb = ttk.Scrollbar(outer, orient="vertical", command=cnv.yview)
            left = ttk.Frame(cnv)
            cnv.create_window((0, 0), window=left, anchor="nw")
            cnv.configure(yscrollcommand=sb.set)
            cnv.pack(side="left", fill="y")
            sb.pack(side="right", fill="y")

            def _resize(e):
                cnv.configure(scrollregion=cnv.bbox("all"), width=left.winfo_reqwidth())
            left.bind("<Configure>", _resize)

            def _wheel(e):
                if left.winfo_reqheight() > cnv.winfo_height():
                    cnv.yview_scroll(-1 if (e.delta > 0 or e.num == 4) else 1, "units")
            wheel_seqs = ("<MouseWheel>", "<Button-4>", "<Button-5>")
            cnv.bind("<Enter>", lambda e: [self.bind_all(q, _wheel) for q in wheel_seqs])
            cnv.bind("<Leave>", lambda e: [self.unbind_all(q) for q in wheel_seqs])
            right = ttk.Frame(parent)
            right.grid(row=0, column=1, sticky="nsew", pady=6, padx=(0, 6))

            # состояние
            st = ttk.LabelFrame(left, text="Состояние")
            st.pack(fill="x")
            self.l_phase = tk.Label(st, text=Engine.PH_NC, font=("Segoe UI", 14, "bold"),
                                    fg="white", bg="#777777", padx=8, pady=4)
            self.l_phase.pack(fill="x", padx=6, pady=(4, 0))
            self.l_out = tk.Label(st, text="ВЫХОД: —", font=("Segoe UI", 11, "bold"))
            self.l_out.pack(fill="x", padx=6)
            grid = ttk.Frame(st)
            grid.pack(fill="x", padx=6, pady=(2, 4))
            self.vals = {}
            for n, (key, cap) in enumerate((("I_set", "Уставка, А"), ("target", "Цель, А"),
                                            ("I", "Ток, А"), ("rate", "Скорость, А/мин"),
                                            ("U", "Напряжение, В"), ("eta", "До цели, мин:с"),
                                            ("P", "Мощность, Вт"), ("R", "R, мОм"))):
                r, col = n // 2, (n % 2) * 2
                ttk.Label(grid, text=cap, style="Cap.TLabel").grid(row=r, column=col, sticky="w",
                                                                   padx=(0 if col == 0 else 14, 0))
                lab = ttk.Label(grid, text="—", style="Val.TLabel", width=7, anchor="e")
                lab.grid(row=r, column=col + 1, sticky="e", padx=(6, 0))
                self.vals[key] = lab

            # СТОП — всегда на виду, сразу под состоянием
            self.b_stop = ttk.Button(left, text="■  СТОП — зафиксировать ток   [Esc]",
                                     style="Stop.TButton", command=self._stop)
            self.b_stop.pack(fill="x", pady=(6, 0))

            # кривая
            cv = ttk.LabelFrame(left, text="Движение по кривой (из любой точки)")
            cv.pack(fill="x", pady=(6, 0))
            self.v_up = tk.StringVar(value="%g" % self.settings["I_work"])
            self.v_dn = tk.StringVar(value="0")
            r1 = ttk.Frame(cv)
            r1.pack(fill="x", padx=6, pady=(6, 3))
            self.b_up = ttk.Button(r1, text="▲ Вверх по кривой до", style="Up.TButton",
                                   command=self._up)
            self.b_up.pack(side="left")
            ttk.Entry(r1, textvariable=self.v_up, width=6, font=("Consolas", 12)).pack(side="left", padx=6)
            ttk.Label(r1, text="А").pack(side="left")
            r2 = ttk.Frame(cv)
            r2.pack(fill="x", padx=6, pady=(3, 2))
            self.b_dn = ttk.Button(r2, text="▼ Вниз по кривой до", style="Down.TButton",
                                   command=self._down)
            self.b_dn.pack(side="left")
            ttk.Entry(r2, textvariable=self.v_dn, width=6, font=("Consolas", 12)).pack(side="left", padx=6)
            ttk.Label(r2, text="А").pack(side="left")
            ttk.Label(cv, text="0 А — с выключением выхода. Направление меняется на ходу.",
                      style="Cap.TLabel").pack(fill="x", padx=6, pady=(0, 4))

            # перехват
            mn = ttk.LabelFrame(left, text="Перехват управления")
            mn.pack(fill="x", pady=(6, 0))
            r3 = ttk.Frame(mn)
            r3.pack(fill="x", padx=6, pady=(6, 0))
            ttk.Label(r3, text="Уставка").pack(side="left")
            self.v_man = tk.StringVar(value="")
            self.e_man = ttk.Entry(r3, textvariable=self.v_man, width=6, font=("Consolas", 12))
            self.e_man.pack(side="left", padx=4)
            ttk.Label(r3, text="А,").pack(side="left")
            self.v_manrate = tk.StringVar(value="%g" % self.settings["manual_rate"])
            ttk.Entry(r3, textvariable=self.v_manrate, width=5, font=("Consolas", 12)).pack(side="left", padx=4)
            ttk.Label(r3, text="А/мин").pack(side="left")
            self.b_man = ttk.Button(r3, text="Плавно", command=self._manual)
            self.b_man.pack(side="left", padx=(8, 0))
            r4 = ttk.Frame(mn)
            r4.pack(fill="x", padx=6, pady=4)
            ttk.Label(r4, text="Шаг ручной уставки:", style="Cap.TLabel").pack(side="left")
            self.b_minus = ttk.Button(r4, text="− 1 А", width=6, command=lambda: self._step(-1))
            self.b_minus.pack(side="left", padx=(12, 4))
            self.b_plus = ttk.Button(r4, text="+ 1 А", width=6, command=lambda: self._step(+1))
            self.b_plus.pack(side="left")
            r5 = ttk.Frame(mn)
            r5.pack(fill="x", padx=6, pady=(0, 4))
            self.b_local = ttk.Button(r5, text="Передать на панель прибора", command=self._local)
            self.b_local.pack(side="left")
            self.b_remote = ttk.Button(r5, text="Вернуть управление программе", command=self._remote)
            self.b_remote.pack(side="left", padx=6)

            # аварийно
            r6 = ttk.Frame(mn)
            r6.pack(fill="x", padx=6, pady=(2, 6))
            self.b_fault = ttk.Button(r6, text="Аварийно вниз до 0", style="Red.TButton",
                                      command=self._fault)
            self.b_fault.pack(side="left")
            self.b_off = ttk.Button(r6, text="Выключить выход сразу", command=self._off)
            self.b_off.pack(side="left", padx=6)

            # график
            self.fig = Figure(figsize=(8, 5), dpi=96)
            self.ax1 = self.fig.add_subplot(211)
            self.ax2 = self.fig.add_subplot(212, sharex=self.ax1)
            self.ax2b = self.ax2.twinx()
            self.ln_set, = self.ax1.plot([], [], color=C_DOWN, lw=2, label="уставка")
            self.ln_meas, = self.ax1.plot([], [], color="#ef6c00", lw=1, label="измеренный ток")
            self.ln_proj, = self.ax1.plot([], [], color="#888888", lw=1.6, ls="--", label="по кривой дальше")
            self.ax1.set_ylabel("Ток накала, А")
            self.ax1.legend(loc="upper left", fontsize=9)
            self.ax1.grid(alpha=0.3)
            self.ln_R, = self.ax2.plot([], [], color=C_UP, lw=1.3, label="R")
            self.ln_P, = self.ax2b.plot([], [], color="#8e24aa", lw=1.0, label="P")
            self.ax2.set_ylabel("R, мОм", color=C_UP)
            self.ax2b.set_ylabel("P, Вт", color="#8e24aa")
            self.ax2.set_xlabel("Время с подключения, мин")
            self.ax2.grid(alpha=0.3)
            self.fig.tight_layout()
            right.rowconfigure(0, weight=1)
            right.columnconfigure(0, weight=1)
            self.canvas = FigureCanvasTkAgg(self.fig, master=right)
            self.canvas.get_tk_widget().grid(row=0, column=0, sticky="nsew")

            # журнал — под графиком
            lg = ttk.LabelFrame(right, text="Журнал событий")
            lg.grid(row=1, column=0, sticky="nsew", pady=(6, 0))
            self.log = ScrolledText(lg, height=7, font=("Consolas", 10), state="disabled", wrap="word")
            self.log.pack(fill="both", expand=True, padx=4, pady=4)
            self.log.tag_configure("info", foreground="#222222")
            self.log.tag_configure("warn", foreground="#b26a00")
            self.log.tag_configure("alarm", foreground=C_RED, font=("Consolas", 10, "bold"))

        # кнопки → движок
        def _num(self, var, name):
            try:
                return fnum(var.get())
            except ValueError as e:
                messagebox.showerror("Ошибка ввода", "%s: %s. Введите число." % (name, e))
                return None

        def _up(self):
            v = self._num(self.v_up, "Вверх до")
            if v is None:
                return
            s = self.engine.snap
            if not s.get("out_on"):
                if not messagebox.askyesno("Включить выход?",
                                           "Выход выключен. Включить его в режиме CC при 0 А и "
                                           "начать разогрев по кривой до %.1f А?" % v):
                    return
            self.engine.send("up", v)

        def _down(self):
            v = self._num(self.v_dn, "Вниз до")
            if v is not None:
                self.engine.send("down", v)

        def _stop(self):
            self.engine.send("stop")

        def _manual(self):
            t = self._num(self.v_man, "Уставка")
            r = self._num(self.v_manrate, "Скорость")
            if t is None or r is None:
                return
            if not self.engine.snap.get("out_on") and t > 0:
                if not messagebox.askyesno("Включить выход?",
                                           "Выход выключен. Включить его в режиме CC при 0 А и "
                                           "плавно поднять ток до %.1f А со скоростью %.2f А/мин?"
                                           % (t, r)):
                    return
            rmax = self.engine.cfg["manual_rate_max"]
            if r > rmax:
                messagebox.showwarning("Скорость", "Скорость ограничена %.1f А/мин (вкладка "
                                                   "«Кривая и пределы»)." % rmax)
            self.engine.send("manual", t, r)

        def _step(self, d):
            self.engine.send("step", d * self.engine.cfg["step_A"])

        def _local(self):
            self.engine.send("local")

        def _remote(self):
            self.engine.send("remote")

        def _fault(self):
            self.engine.send("fault")

        def _off(self):
            s = self.engine.snap
            if messagebox.askyesno("Выключить выход сразу?",
                                   "Ток %.1f А будет снят мгновенно — для таблетки LaB₆ это "
                                   "термоудар.\nПлавный вариант — «Аварийно вниз до 0».\n\n"
                                   "Выключить сразу?" % s.get("I_set", 0), icon="warning"):
                self.engine.send("off")

        # ---------------- профиль ----------------
        def _build_profile(self, parent):
            parent.columnconfigure(2, weight=1)
            parent.rowconfigure(0, weight=1)
            pf = ttk.Frame(parent)
            pf.grid(row=0, column=0, sticky="ns", padx=8, pady=8)
            ttk.Label(pf, text="Кривая разогрева", font=("Segoe UI", 10, "bold")).pack(anchor="w")
            ttk.Label(pf, text="до тока, А   скорость, А/мин   выдержка, с", style="Cap.TLabel").pack(anchor="w")
            self.t_heat = tk.Text(pf, width=30, height=10, font=("Consolas", 11))
            self.t_heat.pack(fill="x", pady=(2, 10))
            ttk.Label(pf, text="Кривая остывания", font=("Segoe UI", 10, "bold")).pack(anchor="w")
            ttk.Label(pf, text="до тока, А   скорость, А/мин   выдержка, с", style="Cap.TLabel").pack(anchor="w")
            self.t_cool = tk.Text(pf, width=30, height=8, font=("Consolas", 11))
            self.t_cool.pack(fill="x", pady=(2, 10))
            ttk.Label(pf, text="Ориентир — ручные нагревы по протоколам\nПР-8 (06.10.2026); остывание — зеркально.",
                      style="Cap.TLabel").pack(anchor="w")

            lf = ttk.LabelFrame(parent, text="Пределы и защиты")
            lf.grid(row=0, column=1, sticky="n", padx=8, pady=8)
            self.lim_fields = [
                ("I_work", "Рабочий ток, А", 1), ("I_max", "Максимум уставки, А", 1),
                ("V_limit", "Предел напряжения (VOLT:LIM), В", 1), ("P_max", "Предел мощности, Вт", 1),
                ("dPdt_max", "Макс. рост мощности, Вт/с", 1),
                ("manual_rate", "Скорость ручная по умолчанию, А/мин", 1),
                ("manual_rate_max", "Скорость ручная максимум, А/мин", 1),
                ("fault_rate", "Аварийное снижение, А/мин", 1), ("step_A", "Шаг кнопок ±, А", 1),
                ("I_check", "Контроль R от тока, А", 1), ("R_min", "R минимум, мОм", 1e3),
                ("R_max", "R максимум, мОм", 1e3), ("R_jump", "Скачок R → стоп, %", 100),
                ("R_drop", "Падение R при разогреве → стоп, %", 100), ("dt", "Период опроса, с", 1)]
            self.lim_vars = {}
            for r, (k, cap, mul) in enumerate(self.lim_fields):
                ttk.Label(lf, text=cap).grid(row=r, column=0, sticky="w", padx=6, pady=2)
                v = tk.StringVar()
                ttk.Entry(lf, textvariable=v, width=9, font=("Consolas", 11)).grid(row=r, column=1, padx=6, pady=2)
                self.lim_vars[k] = v
            bf = ttk.Frame(lf)
            bf.grid(row=len(self.lim_fields), column=0, columnspan=2, pady=10)
            ttk.Button(bf, text="Применить", command=self._apply).pack(side="left", padx=4)
            ttk.Button(bf, text="Сбросить к протокольным", command=self._defaults).pack(side="left", padx=4)
            ttk.Label(lf, text="«Применить» пересчитывает текущее движение\nпо кривой и сохраняется на следующий запуск.\nСкорости — не более %g А/мин, опрос 0,2–1 с." % RATE_MAX,
                      style="Cap.TLabel").grid(row=len(self.lim_fields) + 1, column=0, columnspan=2, padx=6)

            pv = ttk.LabelFrame(parent, text="Полный цикл по кривой: 0 → рабочий ток → 10 мин → 0")
            pv.grid(row=0, column=2, sticky="nsew", padx=8, pady=8)
            self.pfig = Figure(figsize=(6, 4), dpi=96)
            self.pax = self.pfig.add_subplot(111)
            self.l_pv = ttk.Label(pv, text="", font=("Segoe UI", 10, "bold"))
            self.l_pv.pack(anchor="w", padx=6, pady=4)
            self.pcanvas = FigureCanvasTkAgg(self.pfig, master=pv)
            self.pcanvas.get_tk_widget().pack(fill="both", expand=True)
            self._fill_profile(self.settings)

        def _fill_profile(self, s):
            for txt, prof in ((self.t_heat, s["heat_profile"]), (self.t_cool, s["cool_profile"])):
                txt.delete("1.0", "end")
                txt.insert("1.0", "\n".join("%-6g  %-6g  %g" % tuple(p) for p in prof))
            for k, cap, mul in self.lim_fields:
                self.lim_vars[k].set("%g" % (s[k] * mul))
            self._preview(cfg_from_settings(s))

        def _parse_prof(self, txt, name, ascending):
            rows = []
            for n, line in enumerate(txt.get("1.0", "end").splitlines(), 1):
                line = line.split("#")[0].strip()
                if not line:
                    continue
                parts = line.replace(";", " ").replace("\t", " ").split()
                if len(parts) < 2:
                    raise ValueError("%s, строка %d: нужно «ток скорость [выдержка]»" % (name, n))
                try:
                    a = [fnum(x) for x in parts[:3]] + ([0.0] if len(parts) == 2 else [])
                except ValueError as e:
                    raise ValueError("%s, строка %d: %s" % (name, n, e))
                if a[1] <= 0 or a[2] < 0 or a[0] < 0:
                    raise ValueError("%s, строка %d: скорость > 0, выдержка ≥ 0" % (name, n))
                rows.append(a)
            if not rows:
                raise ValueError("%s: пусто" % name)
            seq = [r[0] for r in rows]
            if any((b <= a) if ascending else (b >= a) for a, b in zip(seq, seq[1:])):
                raise ValueError("%s: токи должны %s" % (name, "возрастать" if ascending else "убывать"))
            if not ascending and seq[-1] != 0:
                raise ValueError("Кривая остывания должна заканчиваться на 0 А")
            return rows

        def _apply(self):
            try:
                s = copy.deepcopy(self.settings)
                s["heat_profile"] = self._parse_prof(self.t_heat, "Кривая разогрева", True)
                s["cool_profile"] = self._parse_prof(self.t_cool, "Кривая остывания", False)
                for k, cap, mul in self.lim_fields:
                    s[k] = fnum(self.lim_vars[k].get()) / mul
            except ValueError as e:
                messagebox.showerror("Кривая и пределы", str(e))
                return
            errs = validate_settings(s)
            if errs:
                messagebox.showerror("Кривая и пределы", "Не применено:\n\n• " + "\n• ".join(errs))
                return
            self.settings = s
            try:
                save_settings(s)
            except Exception as e:
                messagebox.showwarning("Сохранение", "Не удалось сохранить настройки: %s" % e)
            cfg = cfg_from_settings(s)
            if self.engine.busy() and self.engine.alive:
                self.engine.send("cfg", cfg)
            else:
                self.engine.cfg = cfg      # и при следующем подключении берутся из self.settings
            self.v_up.set("%g" % s["I_work"])
            self._preview(cfg)

        def _defaults(self):
            d = copy.deepcopy(DEFAULTS)
            d["conn"] = self.settings["conn"]
            self._fill_profile(d)

        def _preview(self, cfg):
            up = up_segments(cfg["heat_profile"], 0.0, cfg["I_work"])
            pts = curve_points(0.0, 0.0, up)
            t_up = pts[-1][0]
            down = down_segments(cfg["cool_profile"], cfg["I_work"], 0.0)
            pts2 = curve_points(t_up + 600, cfg["I_work"], down)
            t_dn = pts2[-1][0] - pts2[0][0]
            ax = self.pax
            ax.clear()
            ax.plot([p[0] / 60 for p in pts], [p[1] for p in pts], color=C_UP, lw=2, label="разогрев")
            ax.plot([t_up / 60, (t_up + 600) / 60], [cfg["I_work"]] * 2, color="#888888", lw=2, ls=":")
            ax.plot([p[0] / 60 for p in pts2], [p[1] for p in pts2], color=C_DOWN, lw=2, label="остывание")
            seen = set()
            for t, I in pts[1:-1]:
                if I in seen:
                    continue
                seen.add(I)
                ax.annotate("%g" % I, (t / 60, I), textcoords="offset points", xytext=(-4, 6),
                            fontsize=8, ha="right", color="#555555")
            ax.set_xlabel("Время, мин")
            ax.set_ylabel("Ток накала, А")
            ax.grid(alpha=0.3)
            ax.legend(loc="upper right", fontsize=9)
            self.pfig.tight_layout()
            self.pcanvas.draw_idle()
            self.l_pv.configure(text="Разогрев до %.0f А: %.0f мин   ·   Остывание: %.0f мин" % (
                cfg["I_work"], t_up / 60, t_dn / 60))

        # ---------------- обновление экрана ----------------
        def _tick(self):
            try:
                while True:
                    try:
                        done, res = self.uiq.get_nowait()
                    except queue.Empty:
                        break
                    done(*res)
                self._drain_events()
                self._refresh()
            except Exception as e:      # не дать одной ошибке остановить обновление окна
                if getattr(self, "_tick_err", "") != repr(e):
                    self._tick_err = repr(e)
                    self.evq.put((datetime.now().strftime("%H:%M:%S"), "alarm",
                                  "Ошибка обновления окна: %r" % (e,)))
            finally:
                if not self._closing:
                    self.after(300, self._tick)

        def _drain_events(self):
            got = False
            while True:
                try:
                    ts, lvl, msg = self.evq.get_nowait()
                except queue.Empty:
                    break
                got = True
                self.log.configure(state="normal")
                self.log.insert("end", "%s  %s\n" % (ts, msg), lvl)
                self.log.configure(state="disabled")
                if lvl == "alarm":
                    self.bell()
            if got:
                lines = int(self.log.index("end-1c").split(".")[0])
                if lines > 3000:
                    self.log.configure(state="normal")
                    self.log.delete("1.0", "%d.0" % (lines - 2500))
                    self.log.configure(state="disabled")
                self.log.see("end")

        def _refresh(self):
            e = self.engine
            s = e.snap
            busy = e.busy()
            conn = bool(s.get("connected"))
            ctl = conn and s.get("control")
            on = conn and s.get("out_on")
            ph = s.get("phase", Engine.PH_NC)
            if conn and not self._was_connected:
                self._status("✓ Подключено: %s  ·  %s  ·  журнал: %s" % (
                    s.get("resource"), s.get("idn"), os.path.basename(s.get("log_path", ""))), C_UP)
            if not busy and self._was_connected:
                self._status("Отключено." + (" Журнал в Excel: %s" % os.path.basename(e.last_export)
                                              if e.last_export else ""), "#555555")
            if not busy and not conn and self._connecting:
                self._status("✗ Не удалось подключиться — подробности в журнале событий.", C_RED)
            if busy and conn:
                self._connecting = False
            if not busy:
                self._connecting = False
            self._was_connected = conn
            if conn and s.get("comm_fail"):
                self._comm_bad = True
                self._status("✗ НЕТ СВЯЗИ с источником (попыток %d). Он держит последнюю уставку."
                             % s["comm_fail"], C_RED)
            elif conn and self._comm_bad:
                self._comm_bad = False
                self._status("✓ Связь восстановлена: %s  ·  журнал: %s" % (
                    s.get("resource"), os.path.basename(s.get("log_path", ""))), C_UP)
            if e.exporting:
                self._status("Сохраняю журнал сессии в Excel…", C_STOP)

            colors = {Engine.PH_UP: C_UP, Engine.PH_DOWN: C_DOWN, Engine.PH_MAN: "#00838f",
                      Engine.PH_HOLD: "#455a64", Engine.PH_FAULT: C_RED, Engine.PH_PANEL: C_STOP,
                      Engine.PH_OBS: C_STOP, Engine.PH_OFF: "#777777", Engine.PH_NC: "#9e9e9e"}
            txt = ph
            bg = colors.get(ph, "#777777")
            if s.get("hold_left"):
                txt += "  ·  выдержка %d с" % s["hold_left"]
            if conn and on and s.get("limiting"):
                txt += "  ·  ТОК ОГРАНИЧЕН ИСТОЧНИКОМ"
                bg = C_RED
            elif conn and on and s.get("up_blocked"):
                txt += "  ·  рост заблокирован"
            self.l_phase.configure(text=txt, bg=bg)
            age = time.time() - s.get("data_wall", 0) if s.get("data_wall") else None
            if conn and ph != Engine.PH_PANEL:
                txt = "ВЫХОД: ВКЛЮЧЁН" if on else "ВЫХОД: выключен"
                stale = age is not None and age > 3.0
                if age is not None:
                    txt += "   ·   данные %s" % ("%.0f с назад!" % age if stale else "свежие")
                self.l_out.configure(text=txt, fg=C_RED if (on or stale) else "#555555")
            else:
                self.l_out.configure(text="ВЫХОД: —", fg="#555555")

            def fmt(v, f="%.2f"):
                return "—" if v is None else f % v
            fresh = conn and s.get("poll")
            self.vals["I_set"].configure(text=fmt(s.get("I_set") if (conn and ph != Engine.PH_PANEL) else None))
            self.vals["I"].configure(text=fmt(s.get("I") if fresh else None))
            self.vals["U"].configure(text=fmt(s.get("U") if fresh else None, "%.3f"))
            self.vals["P"].configure(text=fmt(s.get("P") if fresh else None, "%.1f"))
            R = s.get("R") if fresh else None
            self.vals["R"].configure(text=fmt(R * 1e3 if R else None, "%.1f"))
            self.vals["target"].configure(text=fmt(s.get("target"), "%.1f"))
            self.vals["rate"].configure(text=fmt(s.get("rate"), "%.2f"))
            eta = s.get("eta")
            self.vals["eta"].configure(text="—" if eta is None else "%d:%02d" % (eta // 60, eta % 60))

            def en(w, flag):
                w.configure(state="normal" if flag else "disabled")
            for w in (self.b_up, self.b_man):
                en(w, ctl)
            for w in (self.b_dn, self.b_stop, self.b_fault, self.b_off, self.b_minus, self.b_plus):
                en(w, ctl and on)
            en(self.b_local, conn and (ctl or ph == Engine.PH_OBS))
            en(self.b_remote, conn and ph in (Engine.PH_PANEL, Engine.PH_OBS))
            en(self.b_conn, not busy)
            en(self.b_disc, busy)
            en(self.b_check, not busy)
            for w in self.kind_buttons + [self.e_ip, self.e_custom, self.b_find]:
                en(w, not busy)
            self.c_visa.configure(state="normal" if not busy else "disabled")
            self.c_proto.configure(state="readonly" if not busy else "disabled")
            self.c_speed.configure(state="readonly" if not busy else "disabled")
            self.e_port.configure(state="normal" if (not busy and self.v_proto.get() == "Socket") else "disabled")
            # поле ручной уставки: текущий ток при каждом переходе в удержание (если его не правят)
            if conn and on and (self.v_man.get() == "" or
                                (ph == Engine.PH_HOLD and self._last_phase != ph
                                 and self.focus_get() is not self.e_man)):
                self.v_man.set("%.1f" % s.get("I_set", 0))
            self._last_phase = ph

            now = time.monotonic()
            if now - self._last_plot > 1.0:
                self._last_plot = now
                self._plot(s)

        def _plot(self, s):
            h = self.engine.history()
            if len(h) > 4000:
                h = h[::len(h) // 4000 + 1]
            t = [x[0] / 60 for x in h]
            self.ln_set.set_data(t, [x[1] for x in h])
            self.ln_meas.set_data(t, [x[2] for x in h])
            self.ln_R.set_data(t, [x[4] * 1e3 if (x[4] and x[2] >= 5) else float("nan") for x in h])
            self.ln_P.set_data(t, [x[5] for x in h])
            proj = s.get("proj") or []
            self.ln_proj.set_data([p[0] / 60 for p in proj], [p[1] for p in proj])
            tmax = max([0.5] + t[-1:] + [p[0] / 60 for p in proj[-1:]])
            self.ax1.set_xlim(0, tmax * 1.03 + 0.1)
            ymax = max([10.0] + [x[1] for x in h] + [p[1] for p in proj])
            self.ax1.set_ylim(0, ymax * 1.1)
            for ax in (self.ax2, self.ax2b):
                ax.relim()
                ax.autoscale_view(scalex=False)
            self.canvas.draw_idle()

        def _on_close(self):
            if self._closing:
                return
            e = self.engine
            if e.busy():
                s = e.snap
                if s.get("out_on") and s.get("I_set", 0) > 1:
                    if not messagebox.askyesno(
                            "Закрыть программу?",
                            "Выход включён, ток %.1f А.\nИсточник продолжит держать этот ток, "
                            "управление перейдёт на переднюю панель.\n\nЗакрыть?" % s["I_set"]):
                        return
                self._closing = True
                e.send("disconnect")
                self._status("Сохраняю журнал сессии в Excel…", C_STOP)
                self.update_idletasks()
                t0 = time.monotonic()
                while e.busy():
                    self.update()
                    time.sleep(0.05)
                    if time.monotonic() - t0 > 60:
                        if messagebox.askyesno(
                                "Закрыть программу?",
                                "Программа ещё завершает сессию (нет связи с прибором или идёт "
                                "сохранение журнала в Excel).\n\nПодождать ещё минуту?\n«Нет» — "
                                "закрыть сразу (CSV-журнал уже сохранён, Excel можно собрать позже "
                                "кнопкой «CSV → Excel…»)."):
                            t0 = time.monotonic()
                        else:
                            break
            self._closing = True
            self.destroy()

    app = App()
    if test_hook:
        test_hook(app)
    app.mainloop()


if __name__ == "__main__":
    run_gui()
