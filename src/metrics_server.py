#!/usr/bin/env python3
import json
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

# SEN0501 (I2C 0x22)
from dfrobot_environmental_sensor import EnvironmentalSensor, Units, UVSensor

# VOL-23 / SCD41 (I2C 0x62)
import board
import adafruit_scd4x

I2C_BUS = 1
SEN0501_ADDR = 0x22
HTTP_PORT = 8124

# SEN0501: UV/照度/気圧は使わないが、初期化にUVバリアント指定が必要な場合がある
# （V2系はLTR390UVが一般的）
_sen = EnvironmentalSensor.i2c(bus=I2C_BUS, address=SEN0501_ADDR, uv_sensor=UVSensor.LTR390UV)

_i2c = board.I2C()  # /dev/i2c-1 を使用
_scd4x = adafruit_scd4x.SCD4X(_i2c)
_scd4x.start_periodic_measurement()


def read_sen0501_temp_hum():
    # 温湿度のみ取得（他は呼ばない）
    t = float(_sen.read_temperature(Units.C))
    h = float(_sen.read_humidity())
    return {"temperature_c": round(t, 2), "humidity_rh": round(h, 2)}


def read_scd41():
    # data_readyで取れる時だけ返す（起動直後はNoneになり得る）
    if not _scd4x.data_ready:
        return None
    return {
        "co2_ppm": int(_scd4x.CO2),
        "temperature_c": round(float(_scd4x.temperature), 2),
        "humidity_rh": round(float(_scd4x.relative_humidity), 2),
    }


def read_all():
    return {
        "timestamp": int(time.time()),
        "sen0501": read_sen0501_temp_hum(),
        "scd41": read_scd41(),
    }


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/", "/metrics"):
            self.send_response(404)
            self.end_headers()
            return

        payload = read_all()
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        return  # ログ不要


def main():
    HTTPServer(("0.0.0.0", HTTP_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
