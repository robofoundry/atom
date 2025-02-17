#!/usr/bin/env python3

"""
Reads the calibration results from a json file and computes the evaluation metrics
"""

# -------------------------------------------------------------------------------
# --- IMPORTS
# -------------------------------------------------------------------------------

# Standard imports
import json
import os

import argparse
from collections import OrderedDict
import sys
import numpy as np
import cv2
import ros_numpy
from scipy.spatial import distance
from colorama import Fore

# Ros imports
from rospy_message_converter import message_converter
from image_geometry import PinholeCameraModel

# Atom imports
from atom_core.naming import generateKey
from atom_calibration.collect.label_messages import convertDepthImage32FC1to16UC1, convertDepthImage16UC1to32FC1
from atom_core.vision import projectToCamera
from atom_core.dataset_io import getPointCloudMessageFromDictionary, read_pcd
from atom_core.atom import getTransform

# -------------------------------------------------------------------------------
# --- FUNCTIONS
# -------------------------------------------------------------------------------


def rangeToImage(collection, json_file, ss, ts, tf):
    filename = os.path.dirname(json_file) + '/' + collection['data'][ss]['data_file']
    msg = read_pcd(filename)
    collection['data'][ss].update(message_converter.convert_ros_message_to_dictionary(msg))

    cloud_msg = getPointCloudMessageFromDictionary(collection['data'][ss])
    idxs = collection['labels'][ss]['idxs_limit_points']

    pc = ros_numpy.numpify(cloud_msg)[idxs]
    points_in_vel = np.zeros((4, pc.shape[0]))
    points_in_vel[0, :] = pc['x']
    points_in_vel[1, :] = pc['y']
    points_in_vel[2, :] = pc['z']
    points_in_vel[3, :] = 1

    points_in_cam = np.dot(tf, points_in_vel)

    # -- Project them to the image
    w, h = collection['data'][ts]['width'], collection['data'][ts]['height']
    K = np.ndarray((3, 3), buffer=np.array(test_dataset['sensors'][ts]['camera_info']['K']), dtype=float)
    D = np.ndarray((5, 1), buffer=np.array(test_dataset['sensors'][ts]['camera_info']['D']), dtype=float)

    lidar_pts_in_img, _, _ = projectToCamera(K, D, w, h, points_in_cam[0:3, :])

    return lidar_pts_in_img


def depthInImage(collection, json_file, ss, pinhole_camera_model):
    filename = os.path.dirname(json_file) + '/' + collection['data'][ss]['data_file']

    cv_image_int16_tenths_of_millimeters = cv2.imread(filename, cv2.IMREAD_UNCHANGED)
    img = convertDepthImage16UC1to32FC1(cv_image_int16_tenths_of_millimeters,
                                        scale=10000.0)

    idxs = collection['labels'][ss]['idxs_limit_points']
    w = pinhole_camera_model.fullResolution()[0]
    # w = size[0]

    # initialize lists
    xs = []
    ys = []
    for idx in idxs:  # iterate all points
        # convert from linear idx to x_pix and y_pix indices.
        y_pix = int(idx / w)
        x_pix = int(idx - y_pix * w)
        xs.append(x_pix)
        ys.append(y_pix)

    points_in_depth = np.array((xs, ys), dtype=float)
    # print(points_in_depth)
    return points_in_depth


# -------------------------------------------------------------------------------
# --- MAIN
# -------------------------------------------------------------------------------

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("-train_json", "--train_json_file", help="Json file containing input training dataset.", type=str,
                    required=True)
    ap.add_argument("-test_json", "--test_json_file", help="Json file containing input testing dataset.", type=str,
                    required=True)
    ap.add_argument("-ld", "--lidar_sensor", help="Source transformation sensor.", type=str, required=True)
    ap.add_argument("-cs", "--depth_sensor", help="Target transformation sensor.", type=str, required=True)
    ap.add_argument("-si", "--show_images", help="If true the script shows images.", action='store_true', default=False)
    ap.add_argument("-bt", "--border_tolerance", help="Define the percentage of pixels to use to create a border. Lidar points outside that border will not count for the error calculations",
                                                 type=float, default=0.025)

    # - Save args
    args = vars(ap.parse_args())
    lidar_sensor = args['lidar_sensor']
    depth_sensor = args['depth_sensor']
    show_images = args['show_images']
    border_tolerance = args['border_tolerance']

    # ---------------------------------------
    # --- INITIALIZATION Read calibration data from file
    # ---------------------------------------
    # Loads a json file containing the calibration
    train_json_file = args['train_json_file']
    f = open(train_json_file, 'r')
    train_dataset = json.load(f)
    test_json_file = args['test_json_file']
    f = open(test_json_file, 'r')
    test_dataset = json.load(f)

    # ---------------------------------------
    # --- Get mixed json (calibrated transforms from train and the rest from test)
    # ---------------------------------------
    # test_dataset = test_dataset
    # I am just using the test dataset for everything. If we need an original test dataset we can copy here.

    # Replace optimized transformations in the test dataset copying from the train dataset
    for sensor_key, sensor in train_dataset['sensors'].items():
        calibration_parent = sensor['calibration_parent']
        calibration_child = sensor['calibration_child']
        transform_name = generateKey(calibration_parent, calibration_child)

        # We can only optimized fixed transformations, so the optimized transform should be the same for all
        # collections. We select the first collection (selected_collection_key) and retrieve the optimized
        # transformation for that.
        selected_collection_key = list(train_dataset['collections'].keys())[0]
        optimized_transform = train_dataset['collections'][selected_collection_key]['transforms'][transform_name]

        # iterate all collections of the test dataset and replace the optimized transformation
        for collection_key, collection in test_dataset['collections'].items():
            collection['transforms'][transform_name]['quat'] = optimized_transform['quat']
            collection['transforms'][transform_name]['trans'] = optimized_transform['trans']

    # Copy intrinsic parameters for cameras from train to test dataset.
    for train_sensor_key, train_sensor in train_dataset['sensors'].items():
        if train_sensor['msg_type'] == 'Image':
            test_dataset['sensors'][train_sensor_key]['camera_info']['D'] = train_sensor['camera_info']['D']
            test_dataset['sensors'][train_sensor_key]['camera_info']['K'] = train_sensor['camera_info']['K']
            test_dataset['sensors'][train_sensor_key]['camera_info']['P'] = train_sensor['camera_info']['P']
            test_dataset['sensors'][train_sensor_key]['camera_info']['R'] = train_sensor['camera_info']['R']

    # ---------------------------------------
    # --- INITIALIZATION Read evaluation data from file ---> if desired <---
    # ---------------------------------------
    print(Fore.BLUE + '\nStarting evalutation...')
    print(Fore.WHITE)
    print(
        '-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------')
    print(
        '{:^25s}{:^25s}{:^25s}{:^25s}{:^25s}{:^25s}{:^25s}'.format('#', 'RMS', 'Avg Error', 'X Error', 'Y Error',
                                                                   'X Standard Deviation',
                                                                   'Y Standard Deviation'))
    print(
        '-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------')

    # Declare output dict to save the evaluation data if desire
    delta_total = []
    output_dict = {}
    output_dict['ground_truth_pts'] = {}

    pinhole_camera_model = PinholeCameraModel()
    pinhole_camera_model.fromCameraInfo(
        message_converter.convert_dictionary_to_ros_message('sensor_msgs/CameraInfo',
                                                            train_dataset['sensors'][depth_sensor]['camera_info']))

    from_frame = test_dataset['calibration_config']['sensors'][depth_sensor]['link']
    to_frame = test_dataset['calibration_config']['sensors'][lidar_sensor]['link']
    od = OrderedDict(sorted(test_dataset['collections'].items(), key=lambda t: int(t[0])))

    for collection_key, collection in od.items():
        # ---------------------------------------
        # --- Range to image projection
        # ---------------------------------------
        vel2cam = getTransform(from_frame, to_frame, test_dataset['collections'][collection_key]['transforms'])
        lidar_pts_in_img = rangeToImage(collection, test_json_file, lidar_sensor, depth_sensor, vel2cam)

        # ---------------------------------------
        # --- Get evaluation data for current collection
        # ---------------------------------------
        filename = os.path.dirname(test_json_file) + '/' + collection['data'][depth_sensor]['data_file']
        # print(filename)
        image = cv2.imread(filename)
        depth_pts_in_depth_img = depthInImage(collection, test_json_file, depth_sensor, pinhole_camera_model)

        # Clear image annotations
        image = cv2.imread(filename)

        delta_pts = []
        distances = []
        w, h = collection['data'][depth_sensor]['width'], collection['data'][depth_sensor]['height']
        for idx in range(lidar_pts_in_img.shape[1]):
            x = lidar_pts_in_img[0][idx]
            y = lidar_pts_in_img[1][idx]
            
            # If the points are near or surpassing the image limits, do not count them for the errors
            if x > w * (1 - border_tolerance) or x < w * border_tolerance or \
                y > h * (1 - border_tolerance) or y < h * border_tolerance:
                continue

            lidar_pt = np.reshape(lidar_pts_in_img[0:2, idx], (1, 2))
            delta_pts.append(np.min(distance.cdist(lidar_pt, depth_pts_in_depth_img.transpose()[:, :2], 'euclidean')))
            coords = np.where(distance.cdist(lidar_pt, depth_pts_in_depth_img.transpose()[:, :2], 'euclidean') == np.min(
                distance.cdist(lidar_pt, depth_pts_in_depth_img.transpose()[:, :2], 'euclidean')))
            # if len(depth_pts_in_depth_img.transpose()[coords[1]])>1:
            #     min_dist_pt=depth_pts_in_depth_img.transpose()[coords[1],0]
            #     print(depth_pts_in_depth_img.transpose()[coords[1][0]])
            #     print(depth_pts_in_depth_img.transpose()[coords[1]],min_dist_pt)
            # else:
            min_dist_pt = depth_pts_in_depth_img.transpose()[coords[1]][0]
            # print(min_dist_pt)
            dist = abs(lidar_pt - min_dist_pt)
            distances.append(dist)
            delta_total.append(dist)

            if show_images:
                image = cv2.line(image, (int(lidar_pt.transpose()[0]), int(lidar_pt.transpose()[1])),
                                 (int(min_dist_pt[0]), int(min_dist_pt[1])), (0, 255, 255), 2)

        if len(delta_pts) == 0:
            print('No LiDAR point mapped into the image for collection ' + str(collection_key))
            continue

        # ---------------------------------------
        # --- Compute error metrics
        # ---------------------------------------
        total_pts = len(delta_pts)
        delta_pts = np.array(delta_pts, np.float32)
        avg_error = np.sum(np.abs(delta_pts)) / total_pts
        rms = np.sqrt((delta_pts ** 2).mean())

        delta_xy = np.array(distances, np.float32)
        delta_xy = delta_xy[:, 0]
        avg_error_x = np.sum(np.abs(delta_xy[:, 0])) / total_pts
        avg_error_y = np.sum(np.abs(delta_xy[:, 1])) / total_pts
        stdev_xy = np.std(delta_xy, axis=0)

        print(
            '{:^25s}{:^25f}{:^25.4f}{:^25.4f}{:^25.4f}{:^25.4f}{:^25.4f}'.format(collection_key, rms,
                                                                                 avg_error, avg_error_x,
                                                                                 avg_error_y,
                                                                                 stdev_xy[0], stdev_xy[1]))

        # ---------------------------------------
        # --- Drawing ...
        # ---------------------------------------
        if show_images is True:

            for idx in range(0, lidar_pts_in_img.shape[1]):
                image = cv2.circle(image, (int(lidar_pts_in_img[0, idx]), int(
                    lidar_pts_in_img[1, idx])), 5, (255, 0, 0), -1)
            for idx in range(0, depth_pts_in_depth_img.shape[1]):
                image = cv2.circle(image, (int(depth_pts_in_depth_img[0, idx]), int(
                    depth_pts_in_depth_img[1, idx])), 5, (0, 0, 255), -1)
            win_name = "Lidar to Camera reprojection - collection " + str(collection_key)
            cv2.imshow(win_name, image)
            cv2.waitKey()
            cv2.destroyWindow(winname=win_name)

    total_pts = len(delta_total)
    delta_total = np.array(delta_total, np.float32)
    avg_error = np.sum(np.abs(delta_total)) / total_pts
    rms = np.sqrt((delta_total ** 2).mean())

    delta_xy = np.array(delta_total, np.float32)
    delta_xy = delta_xy[:, 0]
    avg_error_x = np.sum(np.abs(delta_xy[:, 0])) / total_pts
    avg_error_y = np.sum(np.abs(delta_xy[:, 1])) / total_pts
    stdev_xy = np.std(delta_xy, axis=0)

    print('-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------')
    print(
        '{:^25s}{:^25f}{:^25.4f}{:^25.4f}{:^25.4f}{:^25.4f}{:^25.4f}'.format('All', rms,
                                                                             avg_error, avg_error_x,
                                                                             avg_error_y,
                                                                             stdev_xy[0], stdev_xy[1]))
    print(
        '-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------')
    print('Ending script...')
    sys.exit()

    # print("Press ESC to quit and close all open windows.")

    # while True:
    #     k = cv2.waitKey(0) & 0xFF
    #     if k == 27:
    #         cv2.destroyAllWindows()
    #         break