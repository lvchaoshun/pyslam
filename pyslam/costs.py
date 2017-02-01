import numpy as np
import scipy.sparse as sparse

from liegroups import SE3

from pyslam.utils import bilinear_interpolate


class QuadraticCost:
    """A simple quadratic cost for fitting a parabola to data."""

    def __init__(self, x, y, stiffness):
        self.x = x
        self.y = y
        self.stiffness = stiffness

    def evaluate(self, params, compute_jacobians=None):
        residual = self.stiffness * np.array([params[0] * self.x * self.x
                                              + params[1] * self.x
                                              + params[2]
                                              - self.y])

        if compute_jacobians:
            jacobians = [None for _ in enumerate(params)]

            if compute_jacobians[0]:
                jacobians[0] = self.stiffness * self.x * self.x

            if compute_jacobians[1]:
                jacobians[1] = self.stiffness * self.x

            if compute_jacobians[2]:
                jacobians[2] = self.stiffness * 1.

            return residual, jacobians

        return residual


class PoseCost:
    """Unary pose cost given absolute pose measurement in SE2/SE3."""

    def __init__(self, T_obs, stiffness):
        self.T_obs = T_obs
        self.stiffness = stiffness
        self.obstype = type(T_obs)

    def evaluate(self, params, compute_jacobians=None):
        T_est = params[0]

        residual = np.dot(self.stiffness,
                          self.obstype.log(T_est * self.T_obs.inv()))

        if compute_jacobians:
            jacobians = [None for _ in enumerate(params)]

            if compute_jacobians[0]:
                jacobians[0] = np.dot(self.stiffness,
                                      np.identity(self.obstype.dof))

            return residual, jacobians

        return residual


class PoseToPoseCost:
    """Binary pose-to-pose cost given relative pose mesurement in SE2/SE3."""

    def __init__(self, T_2_1_obs, stiffness):
        self.T_2_1_obs = T_2_1_obs
        self.stiffness = stiffness
        self.obstype = type(T_2_1_obs)

    def evaluate(self, params, compute_jacobians=None):
        T_1_0_est = params[0]
        T_2_0_est = params[1]

        residual = np.dot(self.stiffness,
                          self.obstype.log(
                              T_2_0_est * T_1_0_est.inv() * self.T_2_1_obs.inv()))

        if compute_jacobians:
            jacobians = [None for _ in enumerate(params)]

            if compute_jacobians[0]:
                jacobians[0] = np.dot(self.stiffness, -T_2_0_est.adjoint())

            if compute_jacobians[1]:
                jacobians[1] = np.dot(
                    self.stiffness, np.identity(self.obstype.dof))

            return residual, jacobians

        return residual


class ReprojectionCost:
    """Reprojection error for any kind of camera."""

    def __init__(self, camera, obs, stiffness):
        self.camera = camera
        self.obs = obs
        self.stiffness = stiffness

    def evaluate(self, params, compute_jacobians=None):
        T_cam_w = params[0]
        pt_w = params[1]
        pt_cam = T_cam_w * pt_w

        if compute_jacobians:
            jacobians = [None for _ in enumerate(params)]

            predicted_obs, cam_jacobian = self.camera.project(
                pt_cam, compute_jacobians=True)
            residual = np.dot(self.stiffness, predicted_obs - self.obs)

            if compute_jacobians[0]:
                jacobians[0] = np.dot(self.stiffness,
                                      cam_jacobian.dot(SE3.odot(pt_cam)))

            if compute_jacobians[1]:
                jacobians[1] = np.dot(self.stiffness,
                                      cam_jacobian.dot(T_cam_w.rot.as_matrix()))

            return residual, jacobians

        residual = np.dot(self.stiffness,
                          self.camera.project(pt_cam) - self.obs)
        return residual


class PhotometricCost:
    """Photometric cost for greyscale images. Uses the pre-computed reference image jacobian as an approximation to the tracking image jacobian."""

    def __init__(self, camera, im_ref, disp_ref, jac_ref, im_track, stiffness):
        self.camera = camera
        self.im_ref = im_ref
        self.disp_ref = disp_ref
        self.im_track = im_track
        self.jac_ref = jac_ref
        self.stiffness = stiffness
        self.u, self.v = np.meshgrid(list(range(0, camera.w)),
                                     list(range(0, camera.h)),
                                     indexing='xy')

    def evaluate(self, params, compute_jacobians=None):
        T_track_ref = params[0]

        uvd_ref = np.array([self.u.flatten(), self.v.flatten(),
                            self.disp_ref.flatten()]).T
        im_ref_true = self.im_ref.flatten()
        if compute_jacobians:
            im_jac = np.array([self.jac_ref[0, :, :].flatten(),
                               self.jac_ref[1, :, :].flatten()]).T

        # Filter out bad measurements (NaN disparity)
        valid_ref = self.camera.is_valid_measurement(uvd_ref)
        uvd_ref = uvd_ref[valid_ref, :]
        im_ref_true = im_ref_true[valid_ref]
        if compute_jacobians:
            im_jac = im_jac[valid_ref, :]

        # Reproject reference image pixels into tracking image to predict the
        # reference image based on the tracking image
        pt_ref = self.camera.triangulate(np.array(uvd_ref))
        pt_track = T_track_ref * pt_ref
        if compute_jacobians:
            uvd_track, project_jac = self.camera.project(
                pt_track, compute_jacobians=True)
        else:
            uvd_track = self.camera.project(pt_track)

        # Filter out bad measurements (out of bounds coordinates, nonpositive
        # disparity)
        valid_track = self.camera.is_valid_measurement(uvd_track)
        uvd_track = uvd_track[valid_track, :]
        pt_track = pt_track[valid_track, :]
        im_ref_true = im_ref_true[valid_track]
        if compute_jacobians:
            im_jac = im_jac[valid_track, :]
            project_jac = project_jac[valid_track, :, :]

        # The residual is the intensity difference between the estimated
        # reference image pixels and the true reference image pixels
        im_ref_est = bilinear_interpolate(
            self.im_track, uvd_track[:, 0], uvd_track[:, 1])
        residual = self.stiffness * (im_ref_est - im_ref_true)

        # Jacobian time!
        if compute_jacobians:
            jacobians = [None for _ in enumerate(params)]

            if compute_jacobians[0]:
                jac_blocks = [[None] for _ in enumerate(residual)]
                for i, block in enumerate(jac_blocks):
                    jac_blocks[i][0] = np.atleast_2d(
                        im_jac[i, :].dot(
                            project_jac[i, 0:2, :].dot(
                                SE3.odot(pt_track[i, :])))
                    )

                jacobians[0] = self.stiffness * np.bmat(jac_blocks).A

            return residual, jacobians

        # DEBUG: Rebuild the residual image as a sanity check
        # uvd_ref = uvd_ref[valid_track]
        # residual_image = np.empty(self.im_ref.shape)
        # residual_image.fill(np.nan)
        # residual_image[uvd_ref.astype(int)[:, 1],
        #                uvd_ref.astype(int)[:, 0]] = residual
        # return residual_image

        return residual
