# #!/usr/bin/env python
# # -*- coding: utf-8 -*-

import logging
import os
from pathlib import Path

import click
import cv2
import numpy as np
import open3d
from colmap_wrapper.colmap import COLMAP, generate_colmap_sparse_pc
from scipy.spatial.transform import Rotation

from aruco_estimator.colmap.read_write_model import (
    Image,
    Point3D,
    qvec2rotmat,
    read_model,
    rotmat2qvec,
    write_model,
)
from aruco_estimator.colmap.visualize_model import Model
from aruco_estimator.localizers import ArucoLocalizer


def get_normalization_transform(aruco_corners_3d: np.ndarray) -> np.ndarray:
    """Calculate transformation matrix to normalize coordinates to ArUco marker plane."""
    if len(aruco_corners_3d) != 4:
        raise ValueError(f"Expected 4 ArUco corners, got {len(aruco_corners_3d)}")
    
    # Calculate ArUco center
    aruco_center = np.mean(aruco_corners_3d, axis=0)
    
    # Calculate vectors defining the ArUco marker orientation
    y_vec = aruco_corners_3d[0] - aruco_corners_3d[3]
    y_vec = y_vec / np.linalg.norm(y_vec)
    
    x_vec = aruco_corners_3d[0] - aruco_corners_3d[1]
    x_vec = x_vec / np.linalg.norm(x_vec)
    
    # Calculate z-axis ensuring right-handed coordinate system
    z_vec = np.cross(x_vec, y_vec)
    z_vec = z_vec / np.linalg.norm(z_vec)
    
    # Create source vectors from ArUco orientation
    source_vectors = np.array([x_vec, y_vec, z_vec])
    
    # Define target vectors (unit vectors)
    # Patch For Nerfstudio Alignment
    target_vectors = np.array([
        [1, 0, 0],  # Unit x
        [0, 0, 1],  # Unit y
        [0, 1, 0]   # Unit z
    ])
    
    # Find rotation to align ArUco vectors with unit vectors
    rot, rmsd = Rotation.align_vectors(target_vectors, source_vectors)
    
    # Create full transform
    transform = np.eye(4)
    transform[:3, :3] = rot.as_matrix()
    transform[:3, 3] = -rot.as_matrix() @ aruco_center
    
    return transform
def normalize_poses_and_points(cameras, images, points3D, transform: np.ndarray):
    """Apply normalization transform to camera poses and 3D points"""
    # Transform camera poses
    transformed_images = {}
    for image_id, image in images.items():
        # Get current camera pose as 4x4 matrix
        R = qvec2rotmat(image.qvec)
        t = image.tvec
        pose = np.eye(4)
        pose[:3, :3] = R
        pose[:3, 3] = t

        # For camera poses, we need to apply the inverse transformation
        # Compute inverse of transform matrix
        transform_inv = np.linalg.inv(transform)
        
        # Apply transformation
        new_pose = pose @ transform_inv
        
        # Extract new rotation and translation
        new_R = new_pose[:3, :3]
        new_t = new_pose[:3, 3]

        # Create new image with transformed pose
        transformed_images[image_id] = Image(
            id=image.id,
            qvec=rotmat2qvec(new_R),
            tvec=new_t,
            camera_id=image.camera_id,
            name=image.name,
            xys=image.xys,
            point3D_ids=image.point3D_ids
        )
    
    # Transform 3D points using full homogeneous transformation
    transformed_points3D = {}
    for point3D_id, point3D in points3D.items():
        # Convert to homogeneous coordinates
        point_h = np.append(point3D.xyz, 1)
        # Apply full 4x4 transformation
        transformed_h = transform @ point_h
        # Convert back to 3D coordinates (divide by w)
        new_xyz = transformed_h[:3] / transformed_h[3]
        transformed_points3D[point3D_id] = Point3D(
            id=point3D.id,
            xyz=new_xyz,
            rgb=point3D.rgb,
            error=point3D.error,
            image_ids=point3D.image_ids,
            point2D_idxs=point3D.point2D_idxs
        )
    
    return cameras, transformed_images, transformed_points3D

def save_normalized_data(cameras, images, points3D, output_path: Path) -> None:
    """Save normalized poses and points using COLMAP structure"""
    # Create normalized/sparse directory
    output_dir = output_path / "normalized" / "sparse"
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Write transformed data
    write_model(cameras, images, points3D, str(output_dir))

def reassign_origin(colmap_project: str, aruco_size: float = 0.2,
                   dict_type: int = cv2.aruco.DICT_4X4_50,
                   show_original: bool = False, visualize: bool = False,
                   target_id: int = 0, export_tags: bool = False,
                   export_path: str = None):
    """
    Normalize COLMAP poses relative to ArUco marker.
    
    Args:
        colmap_project: Path to COLMAP project
        aruco_size: Size of ArUco marker in meters
        dict_type: ArUco dictionary type
        show_original: Whether to show original poses in visualization
        visualize: Whether to visualize the result
        target_id: ID of ArUco marker to use as origin (default: 0)
        export_tags: Whether to export tag positions (default: False)
        export_path: Path to export tag positions (default: project_path/aruco_tags.json)
    """
    project_path = Path(colmap_project)
    logging.basicConfig(level=logging.INFO)
    
    # Set default export path if not provided
    if export_tags and export_path is None:
        export_path = os.path.join(project_path, "aruco_tags.json")
    
    # Load COLMAP data
    logging.info("Loading COLMAP data...")
    sparse_dir = os.path.join(project_path, "sparse")
    cameras, images, points3D = read_model(sparse_dir)
    
    # Use COLMAP project only for ArUco detection
    logging.info("Detecting ArUco markers...")
    project = COLMAP(project_path=str(project_path))
    aruco_localizer = ArucoLocalizer(
        photogrammetry_software=project,
        aruco_size=aruco_size,
        dict_type=dict_type,
        target_id=target_id
    )
    aruco_distance, aruco_corners_3d = aruco_localizer.run()
    logging.info(f"Target ArUco ID: {target_id}")
    logging.info(f"ArUco 3d points: {aruco_corners_3d}")
    logging.info(f"ArUco marker distance: {aruco_distance}")
    
    # Calculate 3D positions for all detected ArUco markers
    if export_tags:
        logging.info("Calculating positions for all detected ArUco markers...")
        all_aruco_positions = aruco_localizer.get_all_aruco_positions()
        logging.info(f"Found {len(all_aruco_positions)} ArUco markers")
    
    # Calculate normalization transform
    transform = get_normalization_transform(aruco_corners_3d)
    
    # Apply normalization to loaded data
    logging.info("Normalizing poses and 3D points...")
    cameras_norm, images_norm, points3D_norm = normalize_poses_and_points(cameras, images, points3D, transform)
    
    if visualize:
        # Create visualization model
        model = Model()
        model.create_window()
        
        # Add point clouds
        if show_original:
            model.points3D = points3D
            model.add_points(color=[0.7, 0.7, 0.7])  # Gray for original points
        
        model.points3D = points3D_norm
        model.add_points()  # Light blue for transformed points
        
        # Add coordinate frames
        if show_original:
            model.add_coordinate_frame(size=1.0, transform=transform)  # Transformed coordinate frame
        model.add_coordinate_frame(size=2.0)  # True coordinate frame

        
        # Add ArUco markers
        if show_original:
            model.add_aruco_marker(aruco_corners_3d, color=[1, 0, 1])  # Magenta for original marker
        
        # Transform ArUco corners to new coordinate system using homogeneous coordinates
        transformed_corners = np.array([
            (transform @ np.append(corner, 1))[:3] / (transform @ np.append(corner, 1))[3]
            for corner in aruco_corners_3d
        ])
        model.add_aruco_marker(transformed_corners, color=[0, 1, 1])  # Cyan for transformed marker
        
        # Add all detected ArUco markers
        if export_tags:  # We only have all markers if export_tags was True
            logging.info("Visualizing all detected ArUco markers...")
            all_aruco_positions = aruco_localizer.get_all_aruco_positions()
            
            # Define a list of colors for different ArUco markers
            colors = [
                [1, 0, 0],    # Red
                [0, 1, 0],    # Green
                [0, 0, 1],    # Blue
                [1, 1, 0],    # Yellow
                [1, 0, 1],    # Magenta
                [0, 1, 1],    # Cyan
                [0.5, 0.5, 0],  # Olive
                [0.5, 0, 0.5],  # Purple
                [0, 0.5, 0.5],  # Teal
                [0.7, 0.3, 0.3]  # Brown
            ]
            
            # Loop through all detected ArUco markers
            for i, (aruco_id, corners) in enumerate(all_aruco_positions.items()):
                # Skip the target marker as it's already visualized
                if aruco_id == target_id:
                    continue
                
                # Choose a color based on the index
                color_idx = i % len(colors)
                
                if show_original:
                    # Add original marker
                    model.add_aruco_marker(corners, color=colors[color_idx])
                
                # Transform marker corners to new coordinate system
                transformed_marker_corners = np.array([
                    (transform @ np.append(corner, 1))[:3] / (transform @ np.append(corner, 1))[3]
                    for corner in corners
                ])
                
                # Add transformed marker with color indicating the ArUco ID
                model.add_aruco_marker(transformed_marker_corners, color=colors[color_idx])
                
                # Note: We're using different colors to distinguish different ArUco markers
                # Color mapping is based on the index in the colors list
        
        # Add cameras
        if show_original:
            model.cameras = cameras
            model.images = images
            model.add_cameras(scale=0.25, color=[0.7, 0.7, 0.7])  # Dark yellow for original cameras
        
        model.cameras = cameras_norm
        model.images = images_norm
        model.add_cameras(scale=0.25, color=[1, 0, 0])  # Orange for transformed cameras
        
        # Show visualization
        model.show()
    
    # Export tag positions if requested
    if export_tags:
        logging.info("Exporting ArUco tag positions...")
        # Transform ArUco corners to new coordinate system
        transformed_aruco_positions = {}
        for aruco_id, corners in all_aruco_positions.items():
            # Transform each corner using homogeneous coordinates
            transformed_corners = np.array([
                (transform @ np.append(corner, 1))[:3] / (transform @ np.append(corner, 1))[3]
                for corner in corners
            ])
            transformed_aruco_positions[int(aruco_id)] = transformed_corners.tolist()
        
        # Save to JSON file
        with open(export_path, 'w') as f:
            import json
            json.dump({
                "aruco_tags": transformed_aruco_positions,
                "aruco_size": aruco_size,
                "target_id": target_id
            }, f, indent=2)
        logging.info(f"ArUco tag positions exported to {export_path}")
    
    # Save transformed data
    logging.info("Saving normalized data...")
    save_normalized_data(cameras_norm, images_norm, points3D_norm, project_path)
    
    logging.info("Done! Normalized data saved to normalized/sparse/")
