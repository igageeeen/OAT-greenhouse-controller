#!/usr/bin/env python3
import json
import os
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

import RPi.GPIO as GPIO

# ==== GPIO/PWM settings ====
DIR_GPIO = 23          # BCM
PWM_CHIP = "/sys/class/pwm/pwmchip0"
PWM_CH = 0             # PWM0 -> GPIO18
PWM_PATH = f"{PWM_CHIP}/pwm{PWM_CH}"

# 20kHz（MDD10推奨帯域）
PWM_FREQ_HZ = 20000
PWM_PERIOD_NS = int(1e9 / PWM_FREQ_HZ)

# フェイルセーフ
MAX_SECONDS = 900      # 暴走防止（最大15分）
DEFAULT_DUTY = 100     # 0-100

LOCK_FILE = "/run/house-motor.lock"


def _write(path: str, s: str) -> None:
    with open(path, "w") as f:
        f.write(s)


def pwm_export_if_needed():
    if not os.path.isdir(PWM_PATH):
        _write(f"{PWM_CHIP}/export", str(PWM_CH))
        # udev反映待ち
        for _ in range(50):
            if os.path.isdir(PWM_PATH):
                break
            time.sleep(0.05)


def pwm_setup():
    pwm_export_if_needed()
    _write(f"{PWM_PATH}/period", str(PWM_PERIOD_NS))
    _write(f"{PWM_PATH}/duty_cycle", "0")
    _write(f"{PWM_PATH}/enable", "0")


def pwm_set_duty(duty_percent: int):
    duty_percent = max(0, min(100, duty_percent))
    duty_ns = int(PWM_PERIOD_NS * duty_percent / 100)
    _write(f"{PWM_PATH}/duty_cycle", str(duty_ns))


def pwm_enable(enable: bool):
    _write(f"{PWM_PATH}/enable", "1" if enable else "0")


def motor_stop():
    pwm_set_duty(0)
    pwm_enable(False)


def acquire_lock() -> bool:
    try:
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False


def release_lock():
    try:
        os.unlink(LOCK_FILE)
    except FileNotFoundError:
        pass


def motor_run(direction: int, seconds: int, duty: int):
    """
    direction: 0/1
    seconds: run time
    duty: 0-100
    """
    seconds = max(0, min(MAX_SECONDS, seconds))
    duty = max(0, min(100, duty))

    if not acquire_lock():
        return {"ok": False, "error": "busy"}

    try:
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        GPIO.setup(DIR_GPIO, GPIO.OUT)
        GPIO.output(DIR_GPIO, GPIO.HIGH if direction else GPIO.LOW)

        pwm_setup()
        pwm_set_duty(duty)
        pwm_enable(True)

        t0 = time.time()
        while time.time() - t0 < seconds:
            time.sleep(0.05)

        motor_stop()
        return {"ok": True, "direction": direction, "seconds": seconds, "duty": duty}
    finally:
        try:
            motor_stop()
        except Exception:
            pass
        GPIO.cleanup(DIR_GPIO)
        release_lock()


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)

        # /health
        if u.path == "/health":
            self._send(200, {"ok": True})
            return

        # /stop
        if u.path == "/stop":
            try:
                motor_stop()
            except Exception:
                pass
            release_lock()
            self._send(200, {"ok": True})
            return

        # /run?dir=0|1&sec=10&duty=100
        if u.path == "/run":
            try:
                direction = int(qs.get("dir", ["0"])[0])
                seconds = int(qs.get("sec", ["0"])[0])
                duty = int(qs.get("duty", [str(DEFAULT_DUTY)])[0])
            except Exception:
                self._send(400, {"ok": False, "error": "bad_params"})
                return

            if direction not in (0, 1):
                self._send(400, {"ok": False, "error": "dir_must_be_0_or_1"})
                return

            result = motor_run(direction, seconds, duty)
            code = 200 if result.get("ok") else 409
            self._send(code, result)
            return

        self._send(404, {"ok": False, "error": "not_found"})

    def log_message(self, fmt, *args):
        # systemd/journaldに任せる（静音化）
        return


def main():
    server = HTTPServer(("0.0.0.0", 8125), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
