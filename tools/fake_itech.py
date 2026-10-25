#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Эмулятор ITECH IT6012D-300-150 по LAN (raw socket SCPI) — проверка программы
через настоящий pyvisa без прибора. За «прибором» стоит модель нагревателя
из программы (SimIO: R ≈ 20 мОм холодный, ≈ 59 мОм при 88 А).

Запуск:   python tools/fake_itech.py                 (порт 30000, время ×1)
          python tools/fake_itech.py --port 30001 --speed 60
В программе: LAN, IP 127.0.0.1, протокол Socket, порт 30000.

Отвечает на те же команды, что модель в программе; на *IDN? — строкой в формате
ITECH. Одновременно обслуживает одного клиента (как и настоящий прибор по Socket).
"""

import argparse
import os
import socket
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pr8_cathode_app as A  # noqa: E402

IDN = "ITECH Ltd.,IT6012D-300-150,EMULATOR,0.0"


class FakeItech:
    """SCPI-сервер на 127.0.0.1:<port>. start() — в фоне, stop() — остановить."""

    def __init__(self, port=30000, speed=1.0, host="127.0.0.1"):
        self.host, self.port = host, port
        self.clock = A.Clock(speed)
        self.sim = A.SimIO(self.clock)
        self._stop = threading.Event()
        self._srv = None
        self._threads = []

    # модельное время идёт само, даже когда клиент молчит
    def _tick(self):
        while not self._stop.is_set():
            self.clock.sleep(0.5)

    def _handle(self, conn):
        buf = b""
        conn.settimeout(0.5)
        with conn:
            while not self._stop.is_set():
                try:
                    data = conn.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    return
                if not data:
                    return
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    cmd = line.decode("latin-1").strip()
                    if not cmd:
                        continue
                    if cmd.endswith("?"):
                        ans = IDN if cmd.upper() == "*IDN?" else self.sim.query(cmd)
                        try:
                            conn.sendall((ans + "\n").encode("latin-1", "replace"))
                        except OSError:
                            return
                    else:
                        self.sim.write(cmd)

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self._handle(conn)

    def start(self):
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind((self.host, self.port))
        self.port = self._srv.getsockname()[1]          # port=0 → свободный порт
        self._srv.listen(1)
        self._srv.settimeout(0.5)
        for fn in (self._tick, self._serve):
            t = threading.Thread(target=fn, daemon=True)
            t.start()
            self._threads.append(t)
        return self

    def stop(self):
        self._stop.set()
        try:
            self._srv.close()
        except Exception:
            pass
        for t in self._threads:
            t.join(timeout=2)

    @property
    def resource(self):
        return "TCPIP0::%s::%d::SOCKET" % (self.host, self.port)


def main():
    ap = argparse.ArgumentParser(description="Эмулятор ITECH IT6012D по LAN")
    ap.add_argument("--port", type=int, default=30000)
    ap.add_argument("--speed", type=float, default=1.0, help="ускорение модельного времени")
    args = ap.parse_args()
    srv = FakeItech(args.port, args.speed).start()
    print("Эмулятор IT6012D слушает %s (время ×%g). Ctrl+C — остановить." % (srv.resource, args.speed))
    try:
        while True:
            threading.Event().wait(1.0)
    except KeyboardInterrupt:
        srv.stop()


if __name__ == "__main__":
    main()
