import viser
import viser.transforms as tf

import matplotlib
import os

import time
import numpy as np

import open3d as o3d
import trimesh as tr

from graspnetAPI import GraspGroup, Grasp

from autolab_core import RigidTransform

from graspnet_baseline.graspnet_module import GraspNetModule

from robot_lerf.capture_utils import _generate_hemi

from typing import List

import torch

def get_grasps(
    graspnet: GraspNetModule,
    world_pointcloud: tr.PointCloud,
    hemisphere: List[RigidTransform],
    graspnet_batch_size: int = 15,
    ) -> GraspGroup:
    """Get grasps from graspnet, as images taken from the hemisphere
    
    Args: 
        graspnet (GraspNetModule): graspnet module
        world_pointcloud (tr.PointCloud): world pointcloud
        hemisphere (List[RigidTransform]): list of camera poses
    
    Returns:
        GraspGroup: grasps
    """
    torch.cuda.empty_cache()
    gg_all = None
    for i in range(0, len(hemisphere), graspnet_batch_size):
        start = time.time()
        ind_range = range(i, min(i+graspnet_batch_size, len(hemisphere)))
        rgbd_cropped_list = []
        for j in ind_range:
            c2w = hemisphere[j].matrix[:3,:]
            rgbd_cropped = world_pointcloud.copy()
            rgbd_cropped.vertices = tr.transformations.transform_points(
                rgbd_cropped.vertices,
                np.linalg.inv(np.concatenate([c2w, [[0, 0, 0, 1]]], axis=0))
            )
            rgbd_cropped_list.append(rgbd_cropped)
        print("Transform time: ", time.time() - start)

        gg_list = graspnet(rgbd_cropped_list)
        for g_ind, gg in enumerate(gg_list):
            c2w = hemisphere[i + g_ind].matrix[:3,:]
            gg.transform(np.concatenate([c2w, [[0, 0, 0, 1]]], axis=0))
        print(f"Grasp pred time: {time.time() - start:.2f}s")
        start = time.time()

        gg_all_curr = gg_list[0]
        for gg in gg_list[1:]:
            gg_all_curr.add(gg)
        gg = gg_all_curr

        # If the grasps are too close to the ground, then lift them a bit.
        # This is hardcoded though, so it might not work for all scenes
        gg_translations = gg.translations
        gg_translations[gg_translations[:, 2] < -0.16] += np.tile(np.array([0, 0, 0.01]), ((gg_translations[:, 2] < -0.16).sum(), 1))
        gg.translations = gg_translations
        # gg[gg.translations[:, 2] < -0.16].translations += np.tile(np.array([0, 0, 0.04]), ((gg.translations[:, 2] < -0.16).sum(), 1))
        gg = gg[(gg.translations[:, 0] > 0.22)] #& (gg.translations[:, 2] < 0.05)]

        gg = gg[np.abs(gg.rotation_matrices[:, :, 1][:, 2]) < 0.5]

        # gg = gg[gg.scores > 0.6]
        if len(gg) == 0:
            continue

        gg = gg.nms(translation_thresh=0.01, rotation_thresh=30.0/180.0*np.pi)

        # select grasps that are not too close to the table
        # Currently, this function does general grasp filtering (using collision detection, grasp includes non-table components, ...)
        gg = graspnet.local_collision_detection(gg)

        print(f"Collision detection time: {time.time() - start:.2f}s")
        print(f"Post proc time: {time.time() - start:.2f}s")
        if gg_all is None:
            gg_all = gg
        else:
            gg_all.add(gg)

    if gg_all is None:
        return GraspGroup()
    
    gg_all = gg_all.nms(translation_thresh=0.01, rotation_thresh=30.0/180.0*np.pi)
    gg_all.sort_by_score()
    torch.cuda.empty_cache()

    return gg_all


def main() -> None:
    server = viser.ViserServer()

    # add global point cloud
    original_o3d_pc = None
    grasps = None

    with server.add_gui_folder("Pointclouds") as folder:
        gui_pointcloud_url = server.add_gui_text(
            "Pointcloud URL",
            initial_value="test_pc",
        )

        gui_load_pointcloud = server.add_gui_button("Load Pointcloud")

        gui_generate_grasp= server.add_gui_button("Generate Grasps")

        gui_load_grasp = server.add_gui_button("Load Grasp")

    @gui_load_pointcloud.on_click
    def _(_) -> None:
        nonlocal original_o3d_pc
        pointcloud_url = "pointclouds/" + gui_pointcloud_url.value + "/pointcloud.ply"
        print('Loading point cloud from:', pointcloud_url)
        # load point cloud
        if not os.path.exists(pointcloud_url):
            print('Pointcloud file not found')
            return
        original_o3d_pc = o3d.io.read_point_cloud(pointcloud_url)
        original_pointcloud = np.asarray(original_o3d_pc.points)
        original_colors = np.asarray(original_o3d_pc.colors)
        print('Point cloud loaded')
        # add point cloud to server
        server.add_point_cloud(
            "full_pointcloud",
            points=original_pointcloud,
            colors=original_colors,
            point_size=0.002,
            position=(0, 0, 0)
        )

    @gui_load_grasp.on_click
    def _(_) -> None:
        nonlocal grasps
        grasp_url = "grasps/" + gui_pointcloud_url.value + "/grasps.npy"
        print('Loading grasp from:', grasp_url)
        # load grasp
        if not os.path.exists(grasp_url):
            print('Grasp file not found')
            return
        grasps = GraspGroup(np.load(grasp_url))
        print('Grasp loaded')
        for i, grasp in enumerate(grasps):
            # add grasp to server
            default_grasp = Grasp()
            default_grasp.depth = grasp.depth
            default_grasp.width = grasp.width
            default_grasp.height = grasp.height
            default_grasp = default_grasp.to_open3d_geometry()

            robot_frame_R = RigidTransform(
                rotation=RigidTransform.y_axis_rotation(np.pi/2) @ RigidTransform.z_axis_rotation(np.pi/2)
            )

            frame_handle = server.add_frame(
                name=f'/grasps_{i}',
                wxyz=tf.SO3.from_matrix(grasp.rotation_matrix).wxyz,
                position=grasp.translation,
                show_axes=False
            )
            grasp_handle = server.add_mesh(
                name=f'/grasps_{i}/mesh',
                vertices=np.asarray(default_grasp.vertices),
                faces=np.asarray(default_grasp.triangles),
                color=np.array([grasp.score, 0, 0]),
            )
            ur5_handle = server.add_frame(
                name=f'/grasps_{i}/ur5',
                wxyz=robot_frame_R.quaternion,
                position=np.array([grasp.depth-0.015, 0, 0]),
                axes_length=0.05,
                axes_radius=0.002,
                show_axes=True,
                visible=False
            )

    @gui_generate_grasp.on_click
    def _(_) -> None:
        graspnet = GraspNetModule()
        graspnet_ckpt = "./graspnet_baseline/logs/log_kn/checkpoint.tar"
        # from o3d to tr.PointCloud
        original_pc = tr.PointCloud(np.asarray(original_o3d_pc.points))
        original_pc.colors = np.asarray(original_o3d_pc.colors)

        graspnet.init_net(graspnet_ckpt, original_pc, cylinder_radius=0.04, floor_height=-0.16)
        server.add_point_cloud(
            name=f"/coll_pointcloud",
            points=graspnet.pointcloud_vertices,
            colors=np.repeat(np.array([[0, 1, 0]]), len(graspnet.pointcloud_vertices), axis=0),
            point_size=0.002,
            visible=False
        )

        hemi_radius = 2
        hemi_theta_N = 15
        hemi_phi_N = 15
        hemi_th_range = 90
        hemi_phi_down = 0
        hemi_phi_up = 70

        # adjust according to the hardware
        table_center = np.array((.45,0,-.18))

        grasp_hemisphere = _generate_hemi(
            hemi_radius,hemi_theta_N,hemi_phi_N,
            (np.deg2rad(-hemi_th_range),np.deg2rad(hemi_th_range)),
            (np.deg2rad(hemi_phi_down),np.deg2rad(hemi_phi_up)),
            center_pos=table_center,look_pos=table_center
            )
        grasps = get_grasps(graspnet, original_pc, grasp_hemisphere)
        grasps.save_npy(f"grasps/{gui_pointcloud_url.value}/grasps.npy")

        print(f"Grasps generated: {len(grasps)}")

    while True:
        time.sleep(0.01)

if __name__ == "__main__":
    main()