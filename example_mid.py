import numpy as np


def volume_integration(image, threshold=100, sigma=2):
    """
    Detects the first and last significant intensity changes in pixel values
    for each column of the image using a Gaussian filter and a squared difference threshold.

    Parameters:
    - image (numpy.ndarray): Input 2D image array.
    - threshold (float): Threshold for detecting significant intensity changes.
    - sigma (float): Standard deviation for Gaussian filtering.

    Returns:
    - X (numpy.ndarray): Row indices of detected edges.
    - Y (numpy.ndarray): Column indices of detected edges.
    """
    from scipy import ndimage
    
    # Apply Gaussian filtering
    image_filtered = ndimage.gaussian_filter(image, sigma)

    # Compute squared intensity differences along each column
    gap = np.diff(image_filtered, axis=0) ** 2  # Equivalent to manual differencing

    # Find first significant change (top-down) per column
    top_edges = np.argmax(gap > threshold, axis=0)  # Returns first index where condition is True

    # Find first significant change (bottom-up) per column
    bottom_edges = gap.shape[0] - np.argmax(gap[::-1, :] > threshold, axis=0) - 1

    # Mask out columns where no threshold crossing was found
    valid_top = gap[top_edges, np.arange(gap.shape[1])] > threshold
    valid_bottom = gap[bottom_edges, np.arange(gap.shape[1])] > threshold

    # Filter out invalid results
    X = np.concatenate((top_edges[valid_top], bottom_edges[valid_bottom]))
    Y = np.concatenate((np.arange(gap.shape[1])[valid_top], np.arange(gap.shape[1])[valid_bottom]))

    return X, Y

def fit_ellipse(x, y):
    """
    Fits an ellipse to a set of 2D points (x, y) using least squares minimization.

    The general quadratic equation of an ellipse is:

    $$
    A x^2 + B x y + C y^2 + D x + E y + F = 0
    $$

    This function finds the best-fitting ellipse by solving the eigenvalue problem:

    $$
    S^{-1} C v = \lambda v
    $$

    where:
    - \( S = D^T D \) is the scatter matrix,
    - \( C \) is the constraint matrix that enforces the ellipse condition.

    Parameters:
    - x, y (numpy.ndarray): Arrays of x and y coordinates.

    Returns:
    - a (numpy.ndarray): Coefficients \([A, B, C, D, E, F]\) of the fitted ellipse equation.
    """
    from scipy.linalg import eig
    
    x, y = x[:, np.newaxis], y[:, np.newaxis]

    # Construct design matrix D
    D = np.hstack((x*x, x*y, y*y, x, y, np.ones_like(x)))

    # Compute scatter matrix S
    S = np.dot(D.T, D)

    # Constraint matrix to enforce ellipse shape
    C = np.zeros((6, 6))
    C[0, 2] = C[2, 0] = 2  # Enforce conic constraints
    C[1, 1] = -1

    # Solve generalized eigenvalue problem
    E, V = eig(S, C)
    mask = np.isfinite(E) & (E.real > 0)   # exactly one such eigenvalue for a real ellipse fit
    a = V[:, mask][:, 0]

    return a.real  # Ensure real output

def ellipse_center(a):
    """
    Computes the center \((x_0, y_0)\) of the ellipse given its quadratic equation coefficients.

    The center of the ellipse is computed using:

    $$
    x_0 = \frac{C D - B E}{B^2 - A C}
    $$

    $$
    y_0 = \frac{A E - B D}{B^2 - A C}
    $$

    where \( A, B, C, D, E, F \) are the coefficients of the ellipse equation.

    Parameters:
    - a (numpy.ndarray): Coefficients \([A, B, C, D, E, F]\) of the fitted ellipse.

    Returns:
    - center (numpy.ndarray): \((x_0, y_0)\) coordinates of the ellipse center.
    """
    A, B, C, D, E, _ = a
    B /= 2
    D /= 2
    E /= 2

    denominator = B*B - A*C
    x0 = (C*D - B*E) / denominator
    y0 = (A*E - B*D) / denominator

    return np.array([x0, y0])

def ellipse_angle_of_rotation(a):
    """
    Computes the rotation angle \(\phi\) of the ellipse with respect to the x-axis.

    The angle is given by:

    $$
    \phi = \frac{1}{2} \tan^{-1} \left(\frac{2B}{A - C}\right)
    $$

    where:
    - \( A, B, C \) are the quadratic terms of the ellipse equation.

    Parameters:
    - a (numpy.ndarray): Coefficients \([A, B, C, D, E, F]\) of the fitted ellipse.

    Returns:
    - phi (float): Rotation angle in radians.
    """
    A, B, C, _, _, _ = a
    B /= 2

    return 0.5 * np.arctan2(2 * B, (A - C))

def ellipse_axis_length(a):
    """
    Computes the lengths of the major and minor axes of the ellipse.

    The formula for the axis lengths is:

    $$
    \text{numerator} = 2 \left(A E^2 + C D^2 + F B^2 - 2 B D E - A C F \right)
    $$

    $$
    \text{denominator}_1 = (B^2 - A C) \left( (C - A) \sqrt{1 + \frac{4B^2}{(A - C)^2}} - (C + A) \right)
    $$

    $$
    \text{denominator}_2 = (B^2 - A C) \left( (A - C) \sqrt{1 + \frac{4B^2}{(A - C)^2}} - (C + A) \right)
    $$

    The major and minor axis lengths are then computed as:

    $$
    \text{major\_axis} = \sqrt{\frac{\text{numerator}}{\text{denominator}_1}}
    $$

    $$
    \text{minor\_axis} = \sqrt{\frac{\text{numerator}}{\text{denominator}_2}}
    $$

    Parameters:
    - a (numpy.ndarray): Coefficients \([A, B, C, D, E, F]\) of the fitted ellipse.

    Returns:
    - axes (numpy.ndarray): \([\text{major\_axis}, \text{minor\_axis}]\) lengths.
    """
    A, B, C, D, E, F = a
    B /= 2
    D /= 2
    E /= 2

    denominator = B*B - A*C

    # Compute the numerator term
    num = 2 * (A*E**2 + C*D**2 + F*B**2 - 2*B*D*E - A*C*F)

    # Compute denominator terms for axis length
    term = np.sqrt(1 + (4*B**2) / ((A - C)**2))
    denom1 = denominator * ((C - A) * term - (C + A))
    denom2 = denominator * ((A - C) * term - (C + A))

    # Compute axis lengths
    major_axis = np.sqrt(num / denom1)
    minor_axis = np.sqrt(num / denom2)

    return np.array([major_axis, minor_axis])


def fit_droplet(x, y):
    """
    Fit an ellipse to edge points.
    x = horizontal (columns), y = vertical (rows)

    Returns
    -------
    center : np.ndarray  (x0, y0)
    phi    : float        rotation of the radius[0] semi-axis from +x, in radians
    radius : np.ndarray  (a, b) semi-axes in pixels; a lies along phi, b perpendicular
    """
    from skimage.measure import EllipseModel
    pts = np.column_stack([np.asarray(x, float), np.asarray(y, float)])
    model = EllipseModel()
    if not model.estimate(pts) or model.params is None:
        raise ValueError("ellipse fit did not converge")   # batch worker already catches ValueError
    xc, yc, a, b, theta = model.params
    return np.array([xc, yc]), float(theta), np.array([a, b])

def volume_estimate(radius, px):
    """
    Estimates the volume of an ellipsoid given its radii in pixels and the pixel size in micrometers.

    The volume of an ellipsoid is given by:

    $$
    V = \frac{4}{3} \pi a b c
    $$

    where:
    - \( a, b, c \) are the semi-axes (radii).
    - The radii are first converted from pixels to micrometers using the given pixel size \( px \).

    Parameters:
    - radius (tuple or list of floats): The two measured radii in pixels.
    - px (float): The pixel size in micrometers (um).

    Returns:
    - volume_est (float): Estimated volume in cubic millimeters (mm³).
    """

    # Convert radii from pixels to micrometers
    r0_um = radius[0] * px  # Convert first radius
    r1_um = radius[1] * px  # Convert second radius

    # Compute volume based on the larger radius being the major axis
    if r0_um > r1_um:
        volume_est = (4/3) * np.pi * r1_um * (r0_um**2) * 1e-9  # Convert um^3 to mm^3
    else:
        volume_est = (4/3) * np.pi * (r1_um**2) * r0_um * 1e-9  # Convert um^3 to mm^3

    return volume_est

def process_single_droplet(image, px, threshold = 100, sigma = 2,
                           plot = False, all_messages = False):
    """
    Combined ellipsoid fitting and volume estimation levitated droplet tracking
    """

    ### can add different volume integration methods in the future
    Y, X = volume_integration(image, threshold, sigma)
        
    center, phi, radius = fit_droplet(X, Y)

    # if plot:
    #     plot_ellipse(image, center, phi, radius)

    droplet_volume = volume_estimate(radius, px)
    
    if all_messages:
        print(r"Droplet Volume: {:.2f} $mm^3$".format(droplet_volume))

    return center, phi, radius, droplet_volume

def process_droplet_batch(kd, roi=None, px=1, threshold = 100, sigma = 2, save=True):
    """
    Batch process of droplet fitting for 3D image data

    Parameters:
    - images (numpy.ndarray): 3D array of greyscale droplet images [t, y, x]- 
    - threshold (float): Edge detection threshold.
    - sigma (float): Standard deviation for Gaussian filtering.
    """
    kd = kd.drop_empty_trains()
    tids = np.array(kd.train_ids)
    nt = len(tids)
    ctx = pasha.ProcessContext(num_workers=4)

    centers = ctx.alloc((nt, 2), dtype=np.float32, fill=np.nan)
    phis = ctx.alloc((nt,), dtype=np.float32, fill=np.nan)
    radii = ctx.alloc((nt, 2), dtype=np.float32, fill=np.nan)
    volumes = ctx.alloc((nt,), dtype=np.float32, fill=np.nan)

    def worker(worker_id, index, tid):
        image = kd[index]['data.image.pixels'].ndarray(roi=roi).squeeze()

        try:
            center, phi, radius, volume = process_single_droplet(image, px=px,
                                                                 threshold=threshold,
                                                                 sigma=sigma,
                                                                 plot = False, all_messages = False)
        except ValueError:
            return

        # if volume > 100:
        #     return
        
        centers[index] = center
        phis[index] = phi
        radii[index] = radius
        volumes[index] = volume

    # Big hammer to hide lots of LinAlgWarnings
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        ctx.map(worker, tids)

    centers = xr.DataArray(centers, 
                           dims=("trainId", "dim"),
                           coords=dict(trainId=tids,
                                       dim=["x", "y"]))
    phis = xr.DataArray(phis,
                        dims=("trainId",),
                        coords=dict(trainId=tids))
    radii = xr.DataArray(radii, 
                         dims=("trainId", "dim"),
                         coords=dict(trainId=tids,
                                     dim=["x", "y"]))
    volumes = xr.DataArray(volumes,
                           dims=("trainId",),
                           coords=dict(trainId=tids))

    result = xr.Dataset(dict(center=centers, phi=phis, radius=radii, volume=volumes))
    result.attrs["pixel_size_um"] = px
    
    # saving
    if save:
        run_no = kd.run_metadata()['runNumber']
        out_dir = f'/gpfs/exfel/exp/MID/202601/p010400/scratch/xpcs/r{run_no:04d}'
        filename = out_dir + "/droplet_tracking.nc"
        result.to_netcdf(filename)
    
    return result