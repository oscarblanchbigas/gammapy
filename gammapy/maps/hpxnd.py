# Licensed under a 3-clause BSD style license - see LICENSE.rst
from __future__ import absolute_import, division, print_function, unicode_literals
import numpy as np
from astropy.io import fits
from .utils import unpack_seq
from .geom import MapCoords, pix_tuple_to_idx, coord_to_idx
from .hpxmap import HpxMap
from .hpx import HpxGeom, HpxToWcsMapping, nside_to_order

__all__ = [
    'HpxNDMap',
]


class HpxNDMap(HpxMap):
    """Representation of a N+2D map using HEALPix with two spatial
    dimensions and N non-spatial dimensions.

    This class uses a N+1D numpy array to represent the sequence of
    HEALPix image planes.  Following the convention of WCS-based maps
    this class uses a column-wise ordering for the data array with the
    spatial dimension being tied to the last index of the array.

    Parameters
    ----------
    geom : `~gammapy.maps.HpxGeom`
        HEALPIX geometry object.
    data : `~numpy.ndarray`
        HEALPIX data array.
        If none then an empty array will be allocated.
    """

    def __init__(self, geom, data=None, dtype='float32'):

        shape = tuple([np.max(geom.npix)] + [ax.nbin for ax in geom.axes])
        if data is None:

            if geom.npix.size > 1:
                data = np.nan * np.ones(shape, dtype=dtype).T
                idx = geom.get_idx(local=True)
                data[idx[::-1]] = 0.0
            else:
                data = np.zeros(shape, dtype=dtype).T

        elif data.shape != shape[::-1]:
            raise ValueError('Wrong shape for input data array. Expected {} '
                             'but got {}'.format(shape, data.shape))

        super(HpxNDMap, self).__init__(geom, data)
        self._wcs2d = None
        self._hpx2wcs = None

    @classmethod
    def from_hdu(cls, hdu, hdu_bands=None):
        """Make a HpxNDMap object from a FITS HDU.

        Parameters
        ----------
        hdu : `~astropy.io.fits.BinTableHDU`
            The FITS HDU
        hdu_bands  : `~astropy.io.fits.BinTableHDU`
            The BANDS table HDU
        """
        hpx = HpxGeom.from_header(hdu.header, hdu_bands)
        shape = tuple([ax.nbin for ax in hpx.axes[::-1]])
        shape_data = shape + tuple([np.max(hpx.npix)])

        # TODO: Should we support extracting slices?

        map_out = cls(hpx)

        colnames = hdu.columns.names
        cnames = []
        if hdu.header.get('INDXSCHM', None) == 'SPARSE':
            pix = hdu.data.field('PIX')
            vals = hdu.data.field('VALUE')
            if 'CHANNEL' in hdu.data.columns.names:
                chan = hdu.data.field('CHANNEL')
                chan = np.unravel_index(chan, shape)
                idx = chan + (pix,)
            else:
                idx = (pix,)

            map_out.set_by_idx(idx[::-1], vals)
        else:
            for c in colnames:
                if c.find(hpx.conv.colstring) == 0:
                    cnames.append(c)
            nbin = len(cnames)
            if len(cnames) == 1:
                map_out.data = hdu.data.field(cnames[0])
            else:
                for i, cname in enumerate(cnames):
                    idx = np.unravel_index(i, shape)
                    map_out.data[idx + (slice(None),)] = hdu.data.field(cname)

        return map_out

    def make_wcs_mapping(self, sum_bands=False, proj='AIT', oversample=2):
        """Make a HEALPix to WCS mapping object.

        Parameters
        ----------
        sum_bands : bool
           sum over non-spatial dimensions before reprojecting
        proj  : str
           WCS-projection
        oversample : int
           Oversampling factor for WCS map
        normalize : bool
           True -> preserve integral by splitting HEALPIX values between bins

        Returns
        -------
        wcs : `~astropy.wcs.WCS`
            WCS object
        data : `~numpy.ndarray`
            Reprojected data
        """
        self._wcs_proj = proj
        self._wcs_oversample = oversample
        self._wcs2d = self.geom.make_wcs(proj=proj, oversample=oversample,
                                         drop_axes=True)
        self._hpx2wcs = HpxToWcsMapping.create(self.geom, self._wcs2d)

    def to_wcs(self, sum_bands=False, normalize=True, proj='AIT', oversample=2):

        from .wcsnd import WcsNDMap

        if sum_bands and self.geom.nside.size > 1:
            map_sum = self.sum_over_axes()
            return map_sum.to_wcs(sum_bands=False, normalize=normalize, proj=proj,
                                  oversample=oversample)

        # FIXME: Check whether the old mapping is still valid and reuse it
        self.make_wcs_mapping(oversample=oversample, proj=proj)

        # FIXME: Need a function to extract a valid shape from npix property

        if sum_bands:
            hpx_data = np.apply_over_axes(np.sum, self.data,
                                          axes=np.arange(self.data.ndim - 1))
            hpx_data = np.squeeze(hpx_data)
            wcs_shape = tuple([t.flat[0] for t in self._hpx2wcs.npix])
            wcs_data = np.zeros(wcs_shape).T
            wcs = self.geom.make_wcs(proj=proj,
                                     oversample=oversample,
                                     drop_axes=True)
        else:
            hpx_data = self.data
            wcs_shape = tuple([t.flat[0] for t in
                               self._hpx2wcs.npix]) + self.geom._shape
            wcs_data = np.zeros(wcs_shape).T
            wcs = self.geom.make_wcs(proj=proj,
                                     oversample=oversample,
                                     drop_axes=False)

        # FIXME: Should reimplement instantiating map first and fill data array

        self._hpx2wcs.fill_wcs_map_from_hpx_data(hpx_data, wcs_data, normalize)
        return WcsNDMap(wcs, wcs_data)

    def iter_by_image(self):
        for idx in np.ndindex(self.geom.shape):
            yield self.data[idx[::-1]], idx

    def iter_by_pix(self, buffersize=1):
        idx = list(self.geom.get_idx(flat=True))
        vals = self.data[np.isfinite(self.data)]
        return unpack_seq(np.nditer([vals] + idx,
                                    flags=['external_loop', 'buffered'],
                                    buffersize=buffersize))

    def iter_by_coords(self, buffersize=1):
        coords = list(self.geom.get_coords(flat=True))
        vals = self.data[np.isfinite(self.data)]
        return unpack_seq(np.nditer([vals] + coords,
                                    flags=['external_loop', 'buffered'],
                                    buffersize=buffersize))

    def sum_over_axes(self):
        """Sum over all non-spatial dimensions.

        Returns
        -------
        map_out : `~HpxNDMap`
            Summed map.
        """
        hpx_out = self.geom.to_image()
        map_out = self.__class__(hpx_out)

        if self.geom.nside.size > 1:
            vals = self.get_by_idx(self.geom.get_idx(flat=True))
            map_out.fill_by_coords(self.geom.get_coords(flat=True)[:2], vals)
        else:
            axes = np.arange(self.data.ndim - 1).tolist()
            data = np.apply_over_axes(np.sum, self.data, axes=axes)
            map_out.data = np.squeeze(data, axis=axes)

        return map_out

    def _reproject_wcs(self, geom, order=1, mode='interp'):

        from reproject import reproject_from_healpix
        from .wcsnd import WcsNDMap

        map_out = WcsNDMap(geom)
        coordsys = 'galactic' if geom.coordsys == 'GAL' else 'icrs'
        axes_eq = np.all([ax0 == ax1 for ax0, ax1 in
                          zip(geom.axes, self.geom.axes)])

        for vals, idx in map_out.iter_by_image():

            if self.geom.ndim == 2 or axes_eq:
                img = self.data[idx[::-1]]
            else:
                raise NotImplementedError

            # TODO: Create WCS object for image plane if
            # multi-resolution geom
            shape_out = geom.get_image_shape(idx)[::-1]

            data, footprint = reproject_from_healpix((img, coordsys),
                                                     geom.wcs,
                                                     shape_out=shape_out)
            vals[...] = data

        return map_out

    def _reproject_hpx(self, geom, order=1, mode='interp'):
        map_out = HpxNDMap(geom)
        axes_eq = np.all([ax0 == ax1 for ax0, ax1 in
                          zip(geom.axes, self.geom.axes)])

        for vals, idx in map_out.iter_by_image():
            pass

        return map_out

    def pad(self, pad_width):
        geom = self.geom.pad(pad_width)
        map_out = self.__class__(geom)
        map_out.coadd(self)
        return map_out

    def crop(self, crop_width):
        geom = self.geom.crop(crop_width)
        map_out = self.__class__(geom)
        map_out.coadd(self)
        return map_out

    def upsample(self, factor):
        raise NotImplementedError

    def downsample(self, factor):
        raise NotImplementedError

    def interp_by_coords(self, coords, interp=None):
        if interp == 'linear':
            return self._interp_by_coords(coords, interp)
        else:
            raise ValueError('Invalid interpolation method: {}'.format(interp))

    def interp_by_pix(self, pix, interp=None):
        """Interpolate map values at the given pixel coordinates.
        """
        raise NotImplementedError

    def get_by_idx(self, idx):
        idx = pix_tuple_to_idx(idx)
        idx = self.geom.global_to_local(idx)
        return self.data.T[idx]

    def _get_interp_weights(self, coords, idxs):

        import healpy as hp

        c = MapCoords.create(coords)
        coords_ctr = list(coords[:2])
        coords_ctr += [ax.pix_to_coord(t)
                       for ax, t in zip(self.geom.axes, idxs)]
        pix_ctr = pix_tuple_to_idx(self.geom.coord_to_pix(coords_ctr))
        pix_ctr = self.geom.global_to_local(pix_ctr)

        if np.any(pix_ctr[0] == -1):
            raise ValueError('HPX pixel index out of map bounds.')

        theta = np.array(np.pi / 2. - np.radians(c.lat), ndmin=1)
        phi = np.array(np.radians(c.lon), ndmin=1)

        if self.geom.nside.size > 1:
            nside = self.geom.nside[idxs]
        else:
            nside = self.geom.nside

        pix, wts = hp.get_interp_weights(nside, theta,
                                         phi, nest=self.geom.nest)

        if self.geom.nside.size > 1:
            pix_local = [self.geom.global_to_local([pix] + list(idxs))[0]]
        else:
            pix_local = [self.geom[pix]]

        m = pix_local[0] == -1
        pix_local[0][m] = (pix_ctr[0] * np.ones(pix.shape, dtype=int))[m]

        return pix_local + list(idxs), wts

    def _interp_by_coords(self, coords, interp):
        """Linearly interpolate map values."""
        c = MapCoords.create(coords)
        idx_ax = self.geom.coord_to_idx(c, clip=True)[1:]
        pix, wts = self._get_interp_weights(coords, idx_ax)

        if self.geom.ndim == 2:
            return np.sum(self.data.T[pix] * wts, axis=0)

        val = np.zeros(pix[0].shape[1:])
        # Loop over function values at corners
        for i, t in enumerate(range(2 ** len(self.geom.axes))):

            pix_i = []
            wt = np.ones(pix[0].shape[1:])[None, ...]
            for j, ax in enumerate(self.geom.axes):

                idx = coord_to_idx(ax.center[:-1],
                                   c[2 + j], clip=True)  # [None, ...]

                w = ax.center[idx + 1] - ax.center[idx]
                if i & (1 << j):
                    wt *= (c[2 + j] - ax.center[idx]) / w
                    pix_i += [1 + idx]
                else:
                    wt *= (1.0 - (c[2 + j] - ax.center[idx]) / w)
                    pix_i += [idx]

            if self.geom.nside.size > 1:
                pix, wts = self._get_interp_weights(coords, pix_i)

            val += np.sum(wts * wt * self.data.T[pix[:1] + pix_i], axis=0)

        return val

    def fill_by_idx(self, idx, weights=None):
        idx = pix_tuple_to_idx(idx)
        msk = np.all(np.stack([t != -1 for t in idx]), axis=0)
        if weights is not None:
            weights = weights[msk]
        idx = [t[msk] for t in idx]

        idx_local = list(self.geom.global_to_local(idx))
        msk = idx_local[0] >= 0
        idx_local = [t[msk] for t in idx_local]
        if weights is not None:
            weights = weights[msk]

        idx_local = np.ravel_multi_index(idx_local, self.data.T.shape)
        idx_local, idx_inv = np.unique(idx_local, return_inverse=True)
        weights = np.bincount(idx_inv, weights=weights)
        self.data.T.flat[idx_local] += weights

    def set_by_idx(self, idx, vals):
        idx = pix_tuple_to_idx(idx)
        idx_local = self.geom.global_to_local(idx)
        self.data.T[idx_local] = vals

    def _make_cols(self, header, conv):
        shape = self.data.shape
        cols = []
        if header['INDXSCHM'] == 'SPARSE':
            data = self.data.copy()
            data[~np.isfinite(data)] = 0
            nonzero = np.where(data > 0)
            pix = self.geom.local_to_global(nonzero[::-1])[0]
            if len(shape) == 1:
                cols.append(fits.Column('PIX', 'J', array=pix))
                cols.append(fits.Column('VALUE', 'E',
                                        array=data[nonzero].astype(float)))

            else:
                channel = np.ravel_multi_index(nonzero[:-1], shape[:-1])
                cols.append(fits.Column('PIX', 'J', array=pix))
                cols.append(fits.Column('CHANNEL', 'I', array=channel))
                cols.append(fits.Column('VALUE', 'E',
                                        array=data[nonzero].astype(float)))

        elif len(shape) == 1:
            cols.append(fits.Column(conv.colname(indx=conv.firstcol),
                                    'E', array=self.data.astype(float)))
        else:
            for i, idx in enumerate(np.ndindex(shape[:-1])):
                cols.append(fits.Column(conv.colname(indx=i + conv.firstcol), 'E',
                                        array=self.data[idx].astype(float)))
        return cols

    def to_swapped_scheme(self):
        import healpy as hp
        hpx_out = self.geom.to_swapped()
        map_out = self.__class__(hpx_out)
        idx = self.geom.get_idx(flat=True)
        vals = self.get_by_idx(idx)
        if self.geom.nside.size > 1:
            nside = self.geom.nside[idx[1:]]
        else:
            nside = self.geom.nside

        if self.geom.nest:
            idx_new = tuple([hp.nest2ring(nside, idx[0])]) + idx[1:]
        else:
            idx_new = tuple([hp.ring2nest(nside, idx[0])]) + idx[1:]

        map_out.set_by_idx(idx_new, vals)
        return map_out

    def to_ud_graded(self, nside, preserve_counts=False):
        # FIXME: For partial sky maps we should ensure that a higher
        # order map fully encompasses the lower order map

        # FIXME: For higher order maps we may want the option to split
        # the pixel amplitude among all subpixels

        import healpy as hp
        order = nside_to_order(nside)
        new_hpx = self.geom.ud_graded_hpx(order)
        map_out = self.__class__(new_hpx)

        idx = list(self.geom.get_idx())
        coords = self.geom.get_coords()
        vals = self.get_by_idx(idx)
        msk = vals > 0
        coords = [t[msk] for t in coords]
        vals = vals[msk]

        map_out.fill_by_coords(coords, vals)

        if not preserve_counts:
            fact = (2 ** order) ** 2 / (2 ** self.geom.order) ** 2
            if self.geom.nside.size > 1:
                fact = fact[..., None]
            map_out.data *= fact

        return map_out

    def plot(self, ax=None, idx=None, normalize=False, proj='AIT', oversample=4, method='raster'):
        """Quickplot method.

        This will generate a visualization of the map by converting to
        a rasterized WCS image (method='raster') or drawing polygons
        for each pixel (method='poly').

        Parameters
        ----------
        proj : string, optional
            Any valid WCS projection type.
        oversample : int
            Oversampling factor to use when generating a rasterized
            image.
        method : str
            Method for mapping HEALPix pixels to a two-dimensional
            image.  Can be set to 'raster' (rasterization to cartesian
            image plane) or 'poly' (explicit polygons for each pixel).
            WARNING: The 'poly' method is much slower than 'raster'
            and only suitable for maps with less than ~10k pixels.
        idx : tuple
            Set the image slice to plot if this map has non-spatial
            dimensions.

        Returns
        -------
        fig : `~matplotlib.figure.Figure`
            Figure object.

        ax : `~astropy.visualization.wcsaxes.WCSAxes`
            WCS axis object

        im : `~matplotlib.image.AxesImage` or `~matplotlib.collections.PatchCollection`
            Image object.
        """
        if method == 'raster':
            m = self.to_wcs(sum_bands=True,
                            normalize=normalize,
                            proj=proj, oversample=oversample)
            return m.plot(ax)
        elif method == 'poly':
            return self._plot_poly(proj=proj, ax=ax)
        else:
            raise ValueError('Invalid method: {}'.format(method))

    def _plot_poly(self, proj='AIT', step=1, ax=None):
        """Plot the map using a collection of polygons.

        Parameters
        ----------
        proj : string, optional
            Any valid WCS projection type.
        step : int
            Set the number vertices that will be computed for each
            pixel in multiples of 4.
        """
        # FIXME: At the moment this only works for all-sky maps if the
        # projection is centered at (0,0)

        # FIXME: Figure out how to force a square aspect-ratio like imshow

        import matplotlib.pyplot as plt
        from matplotlib.patches import Polygon
        from matplotlib.collections import PatchCollection
        import healpy as hp

        wcs = self.geom.make_wcs(proj=proj, oversample=1)
        if ax is None:
            fig = plt.gcf()
            ax = fig.add_subplot(111, projection=wcs.wcs)

        wcs_lonlat = wcs.center_coord[:2]
        idx = self.geom.get_idx()
        vtx = hp.boundaries(self.geom.nside, idx[0],
                            nest=self.geom.nest, step=step)
        theta, phi = hp.vec2ang(np.rollaxis(vtx, 2))
        theta = theta.reshape((4 * step, -1)).T
        phi = phi.reshape((4 * step, -1)).T

        patches = []
        data = []

        def get_angle(x, t):
            return 180. - (180. - x + t) % 360.

        for i, (x, y) in enumerate(zip(phi, theta)):

            lon, lat = np.degrees(x), np.degrees(np.pi / 2. - y)
            # Add a small ofset to avoid vertices wrapping to the
            # other size of the projection
            if get_angle(np.median(lon), wcs_lonlat[0]) > 0.0:
                idx = wcs.coord_to_pix((lon - 1E-4, lat))
            else:
                idx = wcs.coord_to_pix((lon + 1E-4, lat))

            dist = np.max(np.abs(idx[0][0] - idx[0]))

            # Split pixels that wrap around the edges of the projection
            if (dist > wcs.npix[0] / 1.5):

                lon, lat = np.degrees(x), np.degrees(np.pi / 2. - y)
                lon0 = lon - 1E-4
                lon1 = lon + 1E-4
                pix0 = wcs.coord_to_pix((lon0, lat))
                pix1 = wcs.coord_to_pix((lon1, lat))

                idx0 = np.argsort(pix0[0])
                idx1 = np.argsort(pix1[0])

                pix0 = (pix0[0][idx0][:3], pix0[1][idx0][:3])
                pix1 = (pix1[0][idx1][1:], pix1[1][idx1][1:])

                patches.append(Polygon(np.vstack((pix0[0], pix0[1])).T, True))
                patches.append(Polygon(np.vstack((pix1[0], pix1[1])).T, True))
                data.append(self.data[i])
                data.append(self.data[i])

            else:
                polygon = Polygon(np.vstack((idx[0], idx[1])).T, True)
                patches.append(polygon)
                data.append(self.data[i])

        p = PatchCollection(patches, linewidths=0, edgecolors='None')
        p.set_array(np.array(data))
        ax.add_collection(p)
        ax.autoscale_view()
        ax.coords.grid(color='w', linestyle=':', linewidth=0.5)

        return fig, ax, p
