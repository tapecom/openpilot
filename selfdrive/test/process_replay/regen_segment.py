#!/usr/bin/env python3
import os
import time
import multiprocessing

# run DM procs
os.environ["USE_WEBCAM"] = "1"

import cereal.messaging as messaging
from cereal.services import service_list
from cereal.visionipc.visionipc_pyx import VisionIpcServer, VisionStreamType  # pylint: disable=no-name-in-module, import-error
from common.params import Params
from common.realtime import Ratekeeper
from common.transformations.camera import eon_f_frame_size, eon_d_frame_size
from selfdrive.car.fingerprints import FW_VERSIONS
from selfdrive.manager.process import ensure_running
from selfdrive.manager.process_config import managed_processes
from tools.lib.route import Route
from tools.lib.logreader import LogReader

def replay_service(s, msgs):
  pm = messaging.PubMaster([s, ])
  rk = Ratekeeper(service_list[s].frequency, print_delay_threshold=None)
  s_msgs = [m for m in msgs if m.which() == s]
  for m in s_msgs:
    pm.send(s, m.as_builder())
    rk.keep_time()

def replay_cameras():
  pm = messaging.PubMaster(["roadCameraState", "driverCameraState"])
  cameras = [
    ("roadCameraState", eon_f_frame_size, VisionStreamType.VISION_STREAM_YUV_BACK, 1),
    ("driverCameraState", eon_d_frame_size, VisionStreamType.VISION_STREAM_YUV_FRONT, 2),
  ]
  vipc_server = VisionIpcServer("camerad")
  for (_, size, stream, __) in cameras:
    vipc_server.create_buffers(stream, 40, False, size[0], size[1])
  vipc_server.start_listener()

  rk = Ratekeeper(1 / 0.05, print_delay_threshold=None)
  while True:
    for (pkt, size, stream, decimation) in cameras:
      if rk.frame % decimation != 0:
        continue

      # TODO: send real frames from log
      m = messaging.new_message(pkt)
      msg = getattr(m, pkt)
      msg.frameId = rk.frame // decimation
      pm.send(pkt, m)

      img = b"\x00" * int(size[0]*size[1]*3/2)  # yuv img
      vipc_server.send(stream, img, msg.frameId, msg.timestampSof, msg.timestampEof)
    rk.keep_time()

def regen_segment(route, seg):

  r = Route(route)
  lr = list(LogReader(r.log_paths()[seg]))

  # setup env
  params = Params()
  params.clear_all()
  params.put_bool("Passive", False)
  params.put_bool("OpenpilotEnabledToggle", True)
  params.put_bool("CommunityFeaturesToggle", True)
  params.put_bool("CommunityFeaturesToggle", True)
  cal = messaging.new_message('liveCalibration')
  cal.liveCalibration.validBlocks = 20
  cal.liveCalibration.rpyCalib = [0.0, 0.0, 0.0]
  params.put("CalibrationParams", cal.to_bytes())

  process_replay_dir = os.path.dirname(os.path.abspath(__file__))
  os.environ["LOG_ROOT"] = process_replay_dir
  os.environ["SIMULATION"] = "1"

  os.environ['SKIP_FW_QUERY'] = ""
  os.environ['FINGERPRINT'] = ""
  for msg in lr:
    if msg.which() == 'carParams':
      car_fingerprint = msg.carParams.carFingerprint
      if len(msg.carParams.carFw) and (car_fingerprint in FW_VERSIONS):
        params.put("CarParamsCache", msg.carParams.as_builder().to_bytes())
      else:
        os.environ['SKIP_FW_QUERY'] = "1"
        os.environ['FINGERPRINT'] = car_fingerprint

  fake_daemons = {
    'sensord': [
      multiprocessing.Process(target=replay_service, args=('sensorEvents', lr)),
    ],
    'pandad': [
      multiprocessing.Process(target=replay_service, args=('can', lr)),
      multiprocessing.Process(target=replay_service, args=('pandaState', lr)),
    ],
    'managerState': [
      multiprocessing.Process(target=replay_service, args=('managerState', lr)),
    ],
    'camerad': [
      multiprocessing.Process(target=replay_cameras),
    ],

    # TODO: fix these and run them
    'paramsd': [
      multiprocessing.Process(target=replay_service, args=('liveParameters', lr)),
    ],
    'locationd': [
      multiprocessing.Process(target=replay_service, args=('liveLocationKalman', lr)),
    ],
  }

  # startup procs
  ignore = list(fake_daemons.keys()) + ['ui', 'manage_athenad', 'uploader']
  ensure_running(managed_processes.values(), started=True, not_run=ignore)
  for threads in fake_daemons.values():
    for t in threads:
      t.start()

  # TODO: ensure all procs are running
  time.sleep(60)

  # kill everything
  for p in managed_processes.values():
    p.stop()
  for procs in fake_daemons.values():
    for p in procs:
      p.terminate()

if __name__ == "__main__":
  regen_segment("ef895f46af5fd73f|2021-05-22--14-06-35", 10)
