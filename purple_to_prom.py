#!/usr/bin/env python3

# MIT License
# Copyright (c) 2020 Will Bertelsen
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import time
import traceback
from typing import List, Optional

import aqi
import json
import requests
import argparse
import prometheus_client

aqi_g = prometheus_client.Gauge(
    'purpleair_pm_25_10m_iaqi', 'iAQI (10 min average)',
    ['parent_sensor_id', 'sensor_id', 'sensor_name']
)
aqi_AQandU_g = prometheus_client.Gauge(
    'purpleair_pm_25_10m_iaqi_AQandU',
    'iAQI (10 min average) w/ AQandU correction',
    ['parent_sensor_id', 'sensor_id', 'sensor_name']
)
aqi_LRAPA_g = prometheus_client.Gauge(
    'purpleair_pm_25_10m_iaqi_LRAPA',
    'iAQI (10 min average) w/ LRAPA correction',
    ['parent_sensor_id', 'sensor_id', 'sensor_name']
)
temp_g = prometheus_client.Gauge(
    'purpleair_temp_f', 'Sensor temp reading (degrees Fahrenheit)',
    ['parent_sensor_id', 'sensor_id', 'sensor_name']
)
humidity_g = prometheus_client.Gauge(
    'purpleair_humidity_pct', 'Sensor humidity reading (percent)',
    ['parent_sensor_id', 'sensor_id', 'sensor_name']
)
pressure_g = prometheus_client.Gauge(
    'purpleair_pressure_mb', 'Sensor pressure reading (millibars)',
    ['parent_sensor_id', 'sensor_id', 'sensor_name']
)


def clear_metrics():
  # NOTE: there's no official way to support it unless we convert this script
  # to a "custom collector".
  # See https://github.com/prometheus/client_python/issues/277
  for g in [aqi_g, aqi_AQandU_g, aqi_LRAPA_g, temp_g, pressure_g,
            humidity_g]:
    with g._lock():
      g._metrics.clear()


def check_sensor(read_api_key: str, parent_sensor_id: str,
    private_sensor_key: Optional[str] = None) -> None:
  resp = None
  if private_sensor_key is not None:
    resp = requests.get(
        "https://api.purpleair.com/v1/sensors/{}?read_key={}".format(
          parent_sensor_id, private_sensor_key),
        headers={"X-API-Key": read_api_key})
  else:
    resp = requests.get(
        "https://api.purpleair.com/v1/sensors/{}".format(parent_sensor_id),
        headers={"X-API-Key": read_api_key})
  if resp.status_code < 200 or resp.status_code > 299:
    clear_metrics()
    raise Exception(
        "got {} responde code from purpleair".format(resp.status_code))

  try:
    resp_json = resp.json()
  except ValueError:
    clear_metrics()
    raise
  for sensor in resp_json.get("sensor"):
    sensor_id = sensor.get("sensor_index")
    name = sensor.get("name")
    stats = sensor.get("stats")
    temp_f = sensor.get("temperature")
    humidity = sensor.get("humidity")
    pressure = sensor.get("pressure")
    try:
      if stats:
        stats = json.loads(stats)
        pm25_10min_raw = stats.get("pm2.5_10minute")
        if pm25_10min_raw:
          pm25_10min = max(float(pm25_10min_raw), 0)
          i_aqi = aqi.to_iaqi(aqi.POLLUTANT_PM25, str(pm25_10min),
                              algo=aqi.ALGO_EPA)
          aqi_g.labels(
              parent_sensor_id=parent_sensor_id, sensor_id=sensor_id,
              sensor_name=name
          ).set(i_aqi)

          # https://www.aqandu.org/airu_sensor#calibrationSection
          pm25_10min_AQandU = 0.778 * float(pm25_10min) + 2.65
          i_aqi_AQandU = aqi.to_iaqi(aqi.POLLUTANT_PM25, str(pm25_10min_AQandU),
                                     algo=aqi.ALGO_EPA)
          aqi_AQandU_g.labels(
              parent_sensor_id=parent_sensor_id, sensor_id=sensor_id,
              sensor_name=name
          ).set(i_aqi_AQandU)

          # https://www.lrapa.org/DocumentCenter/View/4147/PurpleAir-Correction-Summary
          pm25_10min_LRAPA = max(0.5 * float(pm25_10min) - 0.66, 0)
          i_aqi_LRAPA = aqi.to_iaqi(aqi.POLLUTANT_PM25, str(pm25_10min_LRAPA),
                                    algo=aqi.ALGO_EPA)
          aqi_LRAPA_g.labels(
              parent_sensor_id=parent_sensor_id, sensor_id=sensor_id,
              sensor_name=name
          ).set(i_aqi_LRAPA)

        if temp_f:
          temp_g.labels(
              parent_sensor_id=parent_sensor_id, sensor_id=sensor_id,
              sensor_name=name
          ).set(float(temp_f))
        if pressure:
          pressure_g.labels(
              parent_sensor_id=parent_sensor_id, sensor_id=sensor_id,
              sensor_name=name
          ).set(float(pressure))
        if humidity:
          humidity_g.labels(
              parent_sensor_id=parent_sensor_id, sensor_id=sensor_id,
              sensor_name=name
          ).set(float(humidity))
    except Exception:
      try:
        # Stop exporting metrics, instead of showing as a flat line.
        aqi_g.remove(parent_sensor_id, sensor_id, name)
        aqi_AQandU_g.remove(parent_sensor_id, sensor_id, name)
        aqi_LRAPA_g.remove(parent_sensor_id, sensor_id, name)
        temp_g.remove(parent_sensor_id, sensor_id, name)
        pressure_g.remove(parent_sensor_id, sensor_id, name)
        humidity_g.remove(parent_sensor_id, sensor_id, name)
      except KeyError:
        # No data produced yet. Silently ignore it.
        pass
      raise


def poll(sensor_ids: List[str], refresh_seconds: int) -> None:
  while True:
    print("refreshing sensors...", flush=True)
    for sensor_id in sensor_ids:
      try:
        check_sensor(sensor_id)
      except Exception:
        traceback.print_exc()
        print("Error fetching sensor data, sleeping till next poll")
        break
    time.sleep(refresh_seconds)


def main():
  parser = argparse.ArgumentParser(
      description="Gets sensor data from purple air, converts it to AQI, and exports it to prometheus"
  )
  parser.add_argument('--read-api-key', type=str, help="The API read key",
                      required=True)
  parser.add_argument('--sensor-ids', nargs="+", help="Sensors to collect from",
                      required=True)
  parser.add_argument('--private-sensor-ids', nargs="+",
                      help="Private sensor ids.  The number of private ids must correspond to the number of sensor ids.  Use 'None' if the corresponding sensor in sensor-ids is public.",
                      required=True)
  parser.add_argument("--port", type=int,
                      help="What port to serve prometheus metrics on",
                      default=9760)
  parser.add_argument("--refresh-seconds", type=int,
                      help="How often to refresh", default=60)
  args = parser.parse_args()

  prometheus_client.start_http_server(args.port)

  print("Serving prometheus metrics on {}/metrics".format(args.port))
  poll(args.sensor_ids, args.refresh_seconds)


if __name__ == "__main__":
  main()
