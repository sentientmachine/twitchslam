#!/usr/bin/env python3
import os
import sys

sys.path.append("lib/macosx")
sys.path.append("lib/linux")

import time
import cv2
from display import Display2D, Display3D
from frame import Frame, match_frames
import numpy as np
import g2o
from pointmap import Map, Point
from helpers import hamming_distance, triangulate

np.set_printoptions(suppress=True)

# main classes
mapp = Map()
disp2d = None
disp3d = None

def process_frame(img, pose=None):
  start_time = time.time()
  img = cv2.resize(img, (W,H))
  frame = Frame(mapp, img, K)
  if frame.id == 0:
    return

  f1 = mapp.frames[-1]
  f2 = mapp.frames[-2]

  idx1, idx2, Rt = match_frames(f1, f2)

  # add new observations if the point is already observed in the previous frame
  # TODO: consider tradeoff doing this before/after search by projection
  for i,idx in enumerate(idx2):
    if f2.pts[idx] is not None and f1.pts[idx1[i]] is None:
      f2.pts[idx].add_observation(f1, idx1[i])

  if frame.id < 5:
    # get initial positions from fundamental matrix
    f1.pose = np.dot(Rt, f2.pose)
  else:
    # kinematic model
    velocity = np.dot(f2.pose, np.linalg.inv(mapp.frames[-3].pose))
    f1.pose = np.dot(velocity, f2.pose)

  # pose optimization
  if pose is None:
    #print(f1.pose)
    pose_opt = mapp.optimize(local_window=1, fix_points=True)
    print("Pose:     %f" % pose_opt)
    #print(f1.pose)
  else:
    # have ground truth for pose
    f1.pose = pose

  # search by projection
  sbp_pts_count = 0
  if len(mapp.points) > 0:
    map_points = np.array([p.homogeneous() for p in mapp.points])
    projs = np.dot(np.dot(K, f1.pose[:3]), map_points.T).T
    projs = projs[:, 0:2] / projs[:, 2:]
    good_pts = (projs[:, 0] > 0) & (projs[:, 0] < W) & \
               (projs[:, 1] > 0) & (projs[:, 1] < H)
    for i, p in enumerate(mapp.points):
      if not good_pts[i]:
        continue
      q = f1.kd.query_ball_point(projs[i], 5)
      for m_idx in q:
        if f1.pts[m_idx] is None:
          # if any descriptors within 32
          for o in p.orb():
            o_dist = hamming_distance(o, f1.des[m_idx])
            if o_dist < 32.0:
              p.add_observation(f1, m_idx)
              sbp_pts_count += 1
              break

  # triangulate the points we don't have matches for
  good_pts4d = np.array([f1.pts[i] is None for i in idx1])

  # do triangulation in local frame
  lpose = np.dot(f1.pose, np.linalg.inv(f2.pose))
  pts_local = triangulate(lpose, np.eye(4), f1.kps[idx1], f2.kps[idx2])
  good_pts4d &= np.abs(pts_local[:, 3]) > 0.01
  pts_local /= pts_local[:, 3:]       # homogeneous 3-D coords
  good_pts4d &= pts_local[:, 2] > 0   # locally in front of camera
  pts4d = np.dot(np.linalg.inv(f2.pose), pts_local.T).T

  print("Adding:   %d new points, %d search by projection" % (np.sum(good_pts4d), sbp_pts_count))

  # adding new points to the map from pairwise matches
  for i,p in enumerate(pts4d):
    if not good_pts4d[i]:
      continue
    u,v = int(round(f1.kpus[idx1[i],0])), int(round(f1.kpus[idx1[i],1]))
    pt = Point(mapp, p[0:3], img[v,u])
    pt.add_observation(f1, idx1[i])
    pt.add_observation(f2, idx2[i])

  # 2-D display
  if disp2d is not None:
    # paint annotations on the image
    for i1, i2 in zip(idx1, idx2):
      u1, v1 = int(round(f1.kpus[i1][0])), int(round(f1.kpus[i1][1]))
      u2, v2 = int(round(f2.kpus[i2][0])), int(round(f2.kpus[i2][1]))
      if f1.pts[i1] is not None:
        if len(f1.pts[i1].frames) >= 5:
          cv2.circle(img, (u1, v1), color=(0,255,0), radius=3)
        else:
          cv2.circle(img, (u1, v1), color=(0,128,0), radius=3)
      else:
        cv2.circle(img, (u1, v1), color=(0,0,0), radius=3)
      cv2.line(img, (u1, v1), (u2, v2), color=(255,0,0))
    disp2d.paint(img)

  # optimize the map
  if frame.id >= 4 and frame.id%5 == 0:
    err = mapp.optimize()
    print("Optimize: %f units of error" % err)

  # 3-D display
  if disp3d is not None:
    disp3d.paint(mapp)

  print("Map:      %d points, %d frames" % (len(mapp.points), len(mapp.frames)))
  print("Time:     %.2f ms" % ((time.time()-start_time)*1000.0))
  print(np.linalg.inv(f1.pose))

if __name__ == "__main__":
  if len(sys.argv) < 2:
    print("%s <video.mp4>" % sys.argv[0])
    exit(-1)
    
  # create displays and open file
  disp3d = Display3D()
  cap = cv2.VideoCapture(sys.argv[1])

  # camera parameters
  W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
  H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
  CNT = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
  F = float(os.getenv("F", "525"))
  if os.getenv("SEEK") is not None:
    cap.set(cv2.CAP_PROP_POS_FRAMES, int(os.getenv("SEEK")))

  if W > 1024:
    downscale = 1024.0/W
    F *= downscale
    H = int(H * downscale)
    W = 1024
  print("using camera %dx%d with F %f" % (W,H,F))

  # camera intrinsics
  K = np.array([[F,0,W//2],[0,F,H//2],[0,0,1]])
  Kinv = np.linalg.inv(K)

  disp2d = Display2D(W, H)

  """
  mapp.deserialize(open('map.json').read())
  while 1:
    disp3d.paint(mapp)
    time.sleep(1)
  """

  gt_pose = None
  if len(sys.argv) >= 3:
    gt_pose = np.load(sys.argv[2])['pose']
    # add scale param?
    #gt_pose[:, :3, 3] *= 20

  i = 0
  while cap.isOpened():
    ret, frame = cap.read()
    print("\n*** frame %d/%d ***" % (i, CNT))
    if ret == True:
      process_frame(frame, None if gt_pose is None else np.linalg.inv(gt_pose[i]))
    else:
      break
    i += 1
    """
    if i == 10:
      with open('map.json', 'w') as f:
        f.write(mapp.serialize())
        exit(0)
    """

