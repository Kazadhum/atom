#!/usr/bin/env python3

"""
ICP calibration from open3d
"""

# -------------------------------------------------------------------------------
# --- IMPORTS
# -------------------------------------------------------------------------------

import argparse
import copy
import json
import math
import os
from collections import OrderedDict

import numpy as np
import open3d as o3d
import cv2
import tf
from colorama import Style, Fore
from atom_evaluation.utilities import atomicTfFromCalibration
from atom_core.atom import getTransform
from atom_core.dataset_io import addNoiseToInitialGuess, saveResultsJSON
from atom_core.vision import depthToPointCloud

def drawRegistrationResults(source, target, transformation, initial_transformation):
    """
    Visualization tool for open3d
    """
    source_temp = copy.deepcopy(source)
    source_initial_temp = copy.deepcopy(source)
    target_temp = copy.deepcopy(target)
    source_temp.paint_uniform_color([0, 1, 1])
    source_initial_temp.paint_uniform_color([0, 0.5, 0.5])
    target_temp.paint_uniform_color([1, 0, 0])
    source_temp.transform(transformation)
    source_initial_temp.transform(initial_transformation)
    o3d.visualization.draw_geometries([source_initial_temp, source_temp, target_temp])

def pickPoints(pcd):
    """
    Open a window to pick points in order to align two pointclouds
    """
    print("")
    print(
        "1) Please pick at least three correspondences using [shift + left click]"
    )
    print("   Press [shift + right click] to undo point picking")
    print("2) After picking points, press q for close the window")
    print("3) Press [shift + '] or [shift + «] to increase or decrease the size of the picked points")
    print("4) If you desire to not use this collection, please don't choose any points in both pointclouds")
    vis = o3d.visualization.VisualizerWithEditing()
    vis.create_window()
    vis.add_geometry(pcd)
    vis.run()  # user picks points
    vis.destroy_window()
    print("")
    return vis.get_picked_points()

def ICPCalibration(source_point_cloud, target_point_cloud, threshold, T_init, show_images):
    """
    Use ICP to estimate the transformation between two range sensors
    """
    reg_p2p = o3d.pipelines.registration.registration_icp(
        source_point_cloud, target_point_cloud, threshold, T_init,
        o3d.pipelines.registration.TransformationEstimationPointToPoint(),
        o3d.pipelines.registration.ICPConvergenceCriteria(max_iteration=200000))
    print(reg_p2p)
    print("Transformation is:")
    print(reg_p2p.transformation)

    if show_images:
        drawRegistrationResults(source_point_cloud, target_point_cloud, reg_p2p.transformation, T_init)

    return reg_p2p


def saveICPCalibration(dataset, sensor_source, sensor_target, transform, json_file, descriptor):
    """
    Save a JSON file with the data of the ICP calibration
    """
    res = atomicTfFromCalibration(dataset, sensor_target, sensor_source, transform)
    child_link = dataset['calibration_config']['sensors'][sensor_source]['child_link']
    parent_link = dataset['calibration_config']['sensors'][sensor_source]['parent_link']
    frame = parent_link + '-' + child_link
    quat = tf.transformations.quaternion_from_matrix(res)
    for collection_key, collection in dataset['collections'].items():
        dataset['collections'][collection_key]['transforms'][frame]['quat'] = quat
        dataset['collections'][collection_key]['transforms'][frame]['trans'] = res[0:3, 3]

    # Save results to a json file
    filename_results_json = os.path.dirname(json_file) + '/ICPCalibration_' + descriptor + '.json'
    saveResultsJSON(filename_results_json, dataset)



def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-json", "--json_file", help="Json file containing train input dataset.", type=str,
                    required=True)
    ap.add_argument("-ss", "--sensor_source", help="Name of the sensor to be aligned.", type=str, required=True)
    ap.add_argument("-st", "--sensor_target", help="Name of the anchored sensor.", type=str, required=True)
    ap.add_argument("-si", "--show_images", help="If true the script shows images.", action='store_true', default=False)
    ap.add_argument("-ma", "--manual_alignment", help="If true, the user has the possibility to align the pointclouds", action='store_true', default=False)
    ap.add_argument("-ssd", "--sub_sample_depth", help="If a depth sensor is used, subsample its pixels by this amount.", type=int, default=1)
    ap.add_argument("-seed", "--sample_seed", help="Sampling seed", type=int)
    ap.add_argument("-nig", "--noisy_initial_guess", nargs=2, metavar=("translation", "rotation"),
                    help="Percentage of noise to add to the initial guess atomic transformations set before.",
                    type=float, default=[0.0, 0.0],)

    # Save args
    args = vars(ap.parse_args())
    json_file = args['json_file']
    show_images = args['show_images']

    # Read json file
    f = open(json_file, 'r')
    dataset = json.load(f)

    # Define variables
    threshold = 0.1
    transforms = np.zeros((4, 4), np.float32)
    min_rmse = math.inf
    min_transform = None
    source_frame = dataset['calibration_config']['sensors'][args['sensor_source']]['link']
    target_frame = dataset['calibration_config']['sensors'][args['sensor_target']]['link']
    collections_list = list(OrderedDict(sorted(dataset['collections'].items(), key=lambda t: int(t[0]))))
    used_datasets = 0
    pointclouds = {}

    # Add noise to initial guess
    addNoiseToInitialGuess(dataset, args, list(dataset["collections"].keys())[0])

    for list_idx, selected_collection_key in enumerate(collections_list):
        print(Fore.YELLOW + '\nCalibrating collection ' + str(selected_collection_key) + '\n' + Style.RESET_ALL)
        # Retrieve pointclouds
        for sensor in [args['sensor_source'], args['sensor_target']]:
            if dataset['calibration_config']['sensors'][sensor]['modality'] == 'lidar3d':
                filename = os.path.dirname(
                    args['json_file']) + '/' + dataset['collections'][selected_collection_key]['data'][sensor]['data_file']
                print('Reading point cloud from ' + filename)
                point_cloud = o3d.io.read_point_cloud(filename)
                pointclouds[sensor] = point_cloud
            elif dataset['calibration_config']['sensors'][sensor]['modality'] == 'depth':
                filename = os.path.dirname(json_file) + '/' + \
                            dataset['collections'][selected_collection_key]['data'][sensor]['data_file']
                img = cv2.imread(filename, cv2.IMREAD_UNCHANGED)
                pixels = img.shape[0] * img.shape[1]
                idxs_to_trim = range(pixels)
                idxs = [idx for idx in idxs_to_trim if not idx % args['sub_sample_depth']]
                point_cloud = depthToPointCloud(dataset, selected_collection_key, args['json_file'], sensor, idxs)
                print('Reading point cloud from idxs')
                pointclouds[sensor] = point_cloud
            else:
                print('The sensor ' + sensor +  ' does not have a pointcloud to retrieve, so ICP would not work.\nShutting down...')
                exit(0)


        source_point_cloud = pointclouds[args['sensor_source']]
        target_point_cloud = pointclouds[args['sensor_target']]

        # Get the transformation from target to frame
        if not args['manual_alignment']:
            T_target_to_source = getTransform(target_frame, source_frame,
                                            dataset['collections'][selected_collection_key]['transforms'])
        else:
            # Align the pointclouds
            print('\nAligning pointclouds for a better calibration')
            source_picked_points = pickPoints(source_point_cloud)
            target_picked_points = pickPoints(target_point_cloud)

            # Conditions of alignment
            if len(source_picked_points) == 0 and len(target_picked_points) == 0:
                print('\nYou have not chosen any points, so this collection will be ignored')
                continue
            elif not (len(source_picked_points) >= 3 and len(target_picked_points) >= 3):
                print('\nYou have chosen less than 3 points in at least one of the last two pointclouds, please redo them.')
                collections_list.insert(list_idx, selected_collection_key)
                continue            
            if not (len(source_picked_points) == len(target_picked_points)):
                print(f'\nYou have chosen {len(source_picked_points)} and {len(target_picked_points)} points, which needed to be equal, please redo them.')
                collections_list.insert(list_idx, selected_collection_key)
                continue           

            corr = np.zeros((len(source_picked_points), 2))
            corr[:, 0] = source_picked_points
            corr[:, 1] = target_picked_points

            # Estimate rough transformation using correspondences
            print("Compute a rough transform using the correspondences given by user")
            p2p = o3d.pipelines.registration.TransformationEstimationPointToPoint()
            T_target_to_source = p2p.compute_transformation(source_point_cloud, target_point_cloud,
                                                    o3d.utility.Vector2iVector(corr))
        
        # Calibrate using ICP
        print('Using ICP to calibrate')
        print('T_target_to_source = \n' + str(T_target_to_source))
        reg_p2p = ICPCalibration(source_point_cloud, target_point_cloud, threshold, T_target_to_source, show_images)

        # Append transforms
        transforms += reg_p2p.transformation
        if reg_p2p.inlier_rmse < min_rmse:
            min_transform = reg_p2p.transformation
            min_rmse = reg_p2p.inlier_rmse
        
        # Define dataset as used
        used_datasets += 1

    # Average the initial transforms
    transform = transforms / used_datasets 

    # Saving Json files
    print(Fore.YELLOW + '\nSaving two calibrations, one with the average transformation and other with the transformation with the least error.')
    saveICPCalibration(dataset, args['sensor_source'], args['sensor_target'], transform, json_file, 'average')
    saveICPCalibration(dataset, args['sensor_source'], args['sensor_target'], min_transform, json_file, 'best')



if __name__ == '__main__':
   main() 