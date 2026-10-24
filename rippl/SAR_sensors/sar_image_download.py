# Shared search/download helpers for SAR sensors (Sentinel-1, RADARSAT-2, ...).
from typing import Optional, List, Union, Callable, Iterable, Any
import datetime
import hashlib
import logging
import os
import re
from multiprocessing import get_context

import numpy as np
from shapely.geometry import Polygon, MultiPolygon
from shapely.ops import unary_union
import cartopy.crs as ccrs
import cartopy.feature as cfeature
import cartopy.mpl.ticker as cticker
import matplotlib.pyplot as plt
from tqdm.auto import tqdm

from rippl.user_settings import UserSettings
from rippl.orbit_geometry.read_write_shapes import ReadWriteShapes


def filename_from_content_disposition(header: str) -> str:
    """Parse a Content-Disposition header into a bare filename."""
    if not header:
        return ''
    match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', header, flags=re.IGNORECASE)
    return os.path.basename(match.group(1).strip().strip('"')) if match else ''


def stream_response_to_file(response, file_path: str, position: int = 1,
                            checksum: str = '', chunk_size: int = 1024 * 1024) -> str:
    """
    Stream an HTTP response to disk with a tqdm progress bar.

    Writes to ``file_path + '.part'`` first, then renames on success. Optional
    SHA-256 ``checksum`` (hex, optionally prefixed with ``sha256:``) is verified
    after download.
    """
    tmp_path = file_path + '.part'
    if os.path.exists(tmp_path):
        os.remove(tmp_path)

    total = int(response.headers.get('content-length', 0) or 0)
    sha = hashlib.sha256() if checksum else None
    with open(tmp_path, 'wb') as file, \
            tqdm(desc=os.path.basename(file_path), total=total or None, unit='iB',
                 unit_scale=True, unit_divisor=1024, position=position, leave=True,
                 miniters=1) as bar:
        for chunk in response.iter_content(chunk_size=chunk_size):
            if not chunk:
                continue
            file.write(chunk)
            if sha is not None:
                sha.update(chunk)
            bar.update(len(chunk))

    if sha is not None:
        digest = sha.hexdigest()
        expected = str(checksum).lower().replace('sha256:', '')
        if digest != expected:
            os.remove(tmp_path)
            raise ValueError(
                f'Checksum mismatch for {os.path.basename(file_path)}: '
                f'got {digest}, expected {expected}')

    os.replace(tmp_path, file_path)
    return file_path


def run_parallel_downloads(downloader: Callable, download_dat: Iterable[Any],
                           n_processes: int = 1, block_size: int = 25) -> None:
    """
    Run a callable downloader over jobs, matching the Sentinel-1 pattern.

    Sequential when ``n_processes <= 1``, otherwise a spawn process pool in
    blocks of ``block_size``.
    """
    jobs = list(download_dat)
    if not jobs:
        logging.info('All images are already downloaded. Skipping download')
        return

    n_processes = max(1, int(n_processes))
    logging.info('Downloading %s product(s) with %s process(es)', len(jobs), n_processes)

    if n_processes > 1:
        with get_context('spawn').Pool(processes=n_processes, maxtasksperchild=5) as pool:
            for i in range(int(np.ceil(len(jobs) / block_size))):
                last = min((i + 1) * block_size, len(jobs))
                pool.map(downloader, jobs[i * block_size:last])
    else:
        for job in jobs:
            downloader(job)


class DownloadSar(object):
    """
    Base class for SAR catalogue search and download.

    Holds the common search filters (AOI, dates, track, polarisation, orbit
    direction), result containers and the coverage summary/plot logic.
    Sensor-specific catalogue queries and download backends live in subclasses.
    """

    sensor_name = 'SAR'

    ASCENDING_ALIASES = {
        'a', 'A', 'ASC', 'Asc', 'ASCENDING', 'Ascending', 'ascending', 'asc'}
    DESCENDING_ALIASES = {
        'D', 'd', 'DESC', 'desc', 'Desc', 'DESCENDING', 'Descending',
        'descending', 'DSC', 'dsc'}

    def __init__(self, start_date='', end_date='', start_dates='', end_dates='',
                 date='', dates='', time_window='', shape='', track='',
                 polarisation='', orbit_direction='', sensor_mode='',
                 product='SLC', max_num_products=1000, n_processes=1):
        self.n_processes = n_processes
        self.settings = UserSettings()
        self.settings.load_settings()

        self.products = []
        self.product_names = []
        self.footprints = []
        self.tracks = []
        self.orbit_directions = []
        self.ids = []
        self.dates = []
        self.polarisations = []
        self.sizes = []

        self.valid_files = []
        self.invalid_files = []

        self.shape = self._load_shape(shape)
        self.shape_string = ''
        self.create_shape_str()

        self.sensor_mode = sensor_mode
        self.product = product
        self.track = str(track) if track != '' and track is not None else ''
        self.polarisation = polarisation
        self.max_num_products = max_num_products
        self.orbit_direction = self.normalize_orbit_direction(orbit_direction)

        self.start_dates, self.end_dates = self._parse_date_windows(
            start_date=start_date, end_date=end_date,
            start_dates=start_dates, end_dates=end_dates,
            date=date, dates=dates, time_window=time_window)

    # ------------------------------------------------------------------
    # Shared setup helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _load_shape(shape) -> Polygon:
        if isinstance(shape, Polygon):
            return shape
        shape_data = ReadWriteShapes()
        shape_data(shape)
        if not isinstance(shape_data.shape, Polygon):
            raise TypeError('Shape type should be Polygon (no Point, MultiPolygon etc..)')
        return shape_data.shape

    @classmethod
    def normalize_orbit_direction(cls, orbit_direction: str) -> str:
        if orbit_direction in cls.ASCENDING_ALIASES:
            return 'ascending'
        if orbit_direction in cls.DESCENDING_ALIASES:
            return 'descending'
        return ''

    @staticmethod
    def direction_short(direction: str) -> str:
        direction = (direction or '').lower()
        if direction.startswith('asc'):
            return 'asc'
        if direction.startswith('desc'):
            return 'dsc'
        return direction or 'unknown'

    @classmethod
    def _parse_date_windows(cls, start_date='', end_date='', start_dates='', end_dates='',
                            date='', dates='', time_window=''):
        if isinstance(date, datetime.datetime):
            dates = [date]
        elif isinstance(dates, datetime.datetime):
            dates = [dates]

        if isinstance(dates, list):
            for d in dates:
                if not isinstance(d, datetime.datetime):
                    raise TypeError('Input dates should be datetime objects.')

            if isinstance(time_window, datetime.timedelta):
                start_list = [d - time_window for d in dates]
                end_list = [d + time_window for d in dates]
            else:
                start_list = [d.replace(hour=0, minute=0, second=0, microsecond=0) for d in dates]
                end_list = [d + datetime.timedelta(days=1) for d in start_list]

        elif isinstance(start_date, datetime.datetime) and isinstance(end_date, datetime.datetime):
            start_list = [start_date]
            end_list = [end_date]
        elif isinstance(start_dates, list) and isinstance(end_dates, list):
            start_list = start_dates
            end_list = end_dates
            valid_dates = [isinstance(s, datetime.datetime) and isinstance(e, datetime.datetime)
                           for s, e in zip(start_dates, end_dates)]
            if np.sum(valid_dates) < len(valid_dates):
                raise TypeError('Input dates should be datetime objects.')
        else:
            raise TypeError(
                'You should define a start/end date or a list of dates to search for '
                'SAR data! Dates should be datetime objects')

        return np.array(start_list), np.array(end_list)

    def create_shape_str(self):
        """Create WKT-like coordinate string used by ASF / ESA queries."""
        if not isinstance(self.shape, Polygon):
            raise TypeError('Shape type should be Polygon (no Point, MultiPolygon etc..)')

        dat = self.shape.exterior.coords
        self.shape_string = '('
        for p in dat:
            self.shape_string = self.shape_string + str(p[0]) + ' ' + str(p[1]) + ','
        self.shape_string = self.shape_string[:-1] + ')'

    def shape_to_geojson(self) -> dict:
        """GeoJSON Polygon geometry for STAC / other GeoJSON APIs."""
        coords = list(self.shape.exterior.coords)
        return {'type': 'Polygon', 'coordinates': [coords]}

    def reset_search_results(self):
        self.products = []
        self.product_names = []
        self.footprints = []
        self.tracks = []
        self.orbit_directions = []
        self.ids = []
        self.dates = []
        self.polarisations = []
        self.sizes = []
        # Optional fields used by some sensors
        if hasattr(self, 'beam_modes'):
            self.beam_modes = []
        if hasattr(self, 'assets'):
            self.assets = []
        if hasattr(self, 'asset_keys'):
            self.asset_keys = []
        if hasattr(self, 'product_types'):
            self.product_types = []
        if hasattr(self, 'frames'):
            self.frames = []
        if hasattr(self, 'swath_ids'):
            self.swath_ids = []
        if hasattr(self, 'frame_overlaps'):
            self.frame_overlaps = []
        if hasattr(self, 'checksums'):
            self.checksums = []

    def get_account_credentials(self, account_name: str, username='', password=''):
        if not username:
            account = self.settings.settings.get('accounts', {}).get(account_name, {})
            username = account.get('username')
        if not password:
            account = self.settings.settings.get('accounts', {}).get(account_name, {})
            password = account.get('password')
        if not username or not password:
            raise ValueError(
                f'{account_name} username/password missing. Add them via UserSettings '
                f'or user_setup.py.')
        return username, password

    def get_radar_database_folder(self, sensor_path_key: str) -> str:
        radar_database = self.settings.settings['paths']['radar_database']
        folder = self.settings.settings['path_names']['SAR'][sensor_path_key]
        return os.path.join(radar_database, folder)

    # ------------------------------------------------------------------
    # Coverage summary / plotting
    # ------------------------------------------------------------------

    def _format_product_label(self, index: int) -> str:
        return str(self.product_names[index])

    def _stack_group_values(self):
        """
        Values used to aggregate stacks for the coverage summary.

        Prefer relative orbit / track when available; otherwise group by orbit
        direction only (e.g. RADARSAT-2 STAC currently omits track metadata).
        """
        tracks = list(self.tracks)
        has_tracks = any(t not in (None, '', -1) for t in tracks)
        if has_tracks:
            return [t if t not in (None, '') else -1 for t in tracks], 'track'
        directions = [d or 'unknown' for d in self.orbit_directions]
        return directions, 'direction'

    @staticmethod
    def _geometries_for_plot(geometry) -> List[Polygon]:
        if isinstance(geometry, Polygon):
            return [geometry]
        if isinstance(geometry, MultiPolygon):
            return list(geometry.geoms)
        return [pol for pol in geometry]

    def summarize_search_results(self, plot=True, plot_cartopy=True, buffer=3):
        """
        Plot overlap between search results and the AOI and summarize coverage.
        """

        if len(self.dates) == 0:
            logging.info('No images found to visualize')
            return

        valid = [i for i, (d, f) in enumerate(zip(self.dates, self.footprints))
                 if d is not None and f is not None]
        if not valid:
            logging.info('Search returned items but no usable date/footprint metadata.')
            for i in range(len(self.product_names)):
                logging.info('  - %s', self._format_product_label(i))
            return

        dates = np.array([self.dates[i] for i in valid])
        directions = np.array([self.orbit_directions[i] or 'unknown' for i in valid])
        footprints = [self.footprints[i] for i in valid]
        group_values_all, group_kind = self._stack_group_values()
        group_values = np.array([group_values_all[i] for i in valid])

        date_group = np.array([
            d.date().isoformat() + '_' + str(g) for d, g in zip(dates, group_values)])
        unique, unique_ids = np.unique(date_group, return_index=True)

        dates_u = dates[unique_ids]
        groups_u = group_values[unique_ids]
        direction_u = directions[unique_ids]
        ids_list = []
        polygons = []
        coverage = []
        AOI_area = self.shape.area

        for u_val in unique:
            ids = np.ravel(np.argwhere(date_group == u_val))
            ids_list.append(ids)
            polygons.append(unary_union([footprints[i] for i in ids]))
            coverage.append(polygons[-1].intersection(self.shape).area / AOI_area)

        polygons = np.array(polygons, dtype=object)
        coverage = np.array(coverage)
        unique_groups, unique_ids = np.unique(groups_u, return_index=True)
        direction_u = direction_u[unique_ids]

        stack_polygons = []
        average_coverages = []
        image_nos = []
        stack_directions = []

        for group in unique_groups:
            ids = np.ravel(np.argwhere(groups_u == group))
            stack_polygons.append(unary_union(polygons[ids].tolist()))
            image_nos.append(len(ids))
            average_coverages.append(np.mean(coverage[ids]))
            stack_directions.append(direction_u[np.where(unique_groups == group)[0][0]])

        bb_buffer = np.array(
            unary_union(stack_polygons + [self.shape]).buffer(buffer).bounds)[np.array([0, 2, 1, 3])]

        for n, polygon in enumerate(stack_polygons):
            if group_kind == 'track':
                title = (str(int(average_coverages[n] * 100)) + '% coverage for ' +
                         str(stack_directions[n]).lower() + ' track ' + str(unique_groups[n]))
            else:
                title = (str(int(average_coverages[n] * 100)) + '% coverage for ' +
                         str(unique_groups[n]).lower() + ' ' + self.sensor_name)
            polygons_plot = self._geometries_for_plot(polygon)

            if plot_cartopy:
                ax = plt.axes(projection=ccrs.PlateCarree())
                ax.set_title(title)
                ax.add_feature(cfeature.LAND, zorder=0, edgecolor='k')
                ax.add_feature(cfeature.BORDERS, linestyle='-', alpha=.5)
                ax.add_geometries(polygons_plot, ccrs.PlateCarree(), facecolor='b', alpha=0.5)
                ax.add_geometries([self.shape], ccrs.PlateCarree(), facecolor='r', alpha=0.5)
                ax.set_xticks(np.arange(-180, 181, 2), crs=ccrs.PlateCarree())
                ax.xaxis.set_major_formatter(cticker.LongitudeFormatter())
                ax.set_yticks(np.arange(-90, 91, 2), crs=ccrs.PlateCarree())
                ax.yaxis.set_major_formatter(cticker.LatitudeFormatter())
                ax.set_extent(list(bb_buffer), ccrs.PlateCarree())
                plt.show()
            elif plot:
                ax = plt.axes()
                shape_x, shape_y = self.shape.exterior.xy
                for polygon in polygons_plot:
                    dat_x, dat_y = polygon.exterior.xy
                    ax.fill(dat_x, dat_y, alpha=0.5, fc='blue', ec='none')
                ax.fill(shape_x, shape_y, alpha=0.5, fc='r', ec='none')
                ax.set_title(title)
                ax.set_xlim([bb_buffer[0], bb_buffer[1]])
                ax.set_ylim([bb_buffer[2], bb_buffer[3]])
                plt.show()

        logging.info('Summary statistics for %s search:', self.sensor_name)
        for group, average_coverage, image_no, direc in zip(
                unique_groups, average_coverages, image_nos, stack_directions):
            if group_kind == 'track':
                logging.info(
                    'Stack for ' + str(direc).lower() + ' track ' + str(group).zfill(3) +
                    ' contains ' + str(image_no) + ' images with ' +
                    str(int(average_coverage * 100)) + '% coverage of area of interest')
            else:
                logging.info(
                    'Stack for ' + str(group).lower() + ' contains ' + str(image_no) +
                    ' image dates with ' + str(int(average_coverage * 100)) +
                    '% average coverage of area of interest')

        for group in unique_groups:
            logging.info('')
            if group_kind == 'track':
                logging.info('Full list of images for track ' + str(group).zfill(3) + ':')
            else:
                logging.info('Full list of images for ' + str(group).lower() + ':')

            id_dates = np.ravel(np.argwhere(groups_u == group))
            sorted_ids = id_dates[np.ravel(np.argsort(dates_u[id_dates]))]
            for idx in sorted_ids:
                logging.info(dates_u[idx].isoformat() + ' with a coverage of ' +
                             str(int(coverage[idx] * 100)) + '% consists of SAR products:')
                for id_image in ids_list[idx]:
                    # ids_list indexes into the filtered/valid arrays
                    original_index = valid[id_image]
                    logging.info('          ' + self._format_product_label(original_index))

    @staticmethod
    def ensure_database_path(database_folder, sensor_prefix, date, track, polarisation,
                             direction, type_name, product_name):
        """
        Create `<database>/<prefix>_<asc|dsc>_tXXX/<type>_<pol>/<YYYYMMDD>/<product>.zip`.
        """
        direction = DownloadSar.direction_short(direction)
        track_str = str(track).zfill(3) if track not in (None, '') else '000'
        polarisation = polarisation or 'unknown'

        track_folder = os.path.join(database_folder, f'{sensor_prefix}_{direction}_t{track_str}')
        if not os.path.exists(track_folder):
            os.mkdir(track_folder)

        type_folder = os.path.join(track_folder, f'{type_name}_{polarisation}')
        if not os.path.exists(type_folder):
            os.mkdir(type_folder)

        date_folder = os.path.join(type_folder, date.strftime('%Y%m%d'))
        if not os.path.exists(date_folder):
            os.mkdir(date_folder)

        if not product_name.endswith('.zip'):
            product_name = product_name + '.zip'
        return os.path.join(date_folder, product_name)
