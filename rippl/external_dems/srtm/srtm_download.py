# Function created by Gert Mulder
# Institute TU Delft
# Date 9-11-2016
# Part of RIPPL
# Updated to use USGS M2M API (https://m2m.cr.usgs.gov/) for SRTM downloads.

# This function creates a dem based on either a shape/kml file or a given bounding box. If a shape/kml file is given a
# minimum offset of about 0.1 degrees is used.
# All grids are based on the WGS84 projection.
# Downloaded data is based on USGS EarthExplorer SRTM void-filled products via the M2M API:
# https://www.usgs.gov/centers/eros/science/usgs-eros-archive-digital-elevation-shuttle-radar-topography-mission-srtm
# API docs: https://m2m.cr.usgs.gov/

import os
import re
import time
import uuid
import logging
from typing import Optional, List, Any, Tuple

import numpy as np
import requests
from multiprocessing import get_context
from tqdm import tqdm
from osgeo import gdal
gdal.DontUseExceptions()

from rippl.orbit_geometry.coordinate_system import CoordinateSystem
from rippl.user_settings import UserSettings

"""
# Test download of SRTM download
from rippl.external_dems.srtm.srtm_download import SrtmDownload
from rippl.orbit_geometry.coordinate_system import CoordinateSystem

for srtm_type, arc_sec in zip(['SRTM1', 'SRTM3'], [1, 3]):
    download_folder = '/mnt/external/rippl_tutorial_test/DEM_database/srtm'
    download = SrtmDownload(n_processes=1, srtm_folder=download_folder, srtm_type=srtm_type)

    coordinates = CoordinateSystem()
    coordinates.create_geographic(dlat=arc_sec/3600, dlon=arc_sec/3600, lat0=45, lon0=4, shape=(1000, 1000))
    download(coordinates=coordinates)

"""

# USGS M2M dataset aliases and preferred GeoTIFF product names
M2M_API_URL = 'https://m2m.cr.usgs.gov/api/api/json/stable/'
SRTM_DATASETS = {
    # Global 1 arc-second void-filled product
    'SRTM1': {
        'datasetName': 'srtm_v3',
        'productNames': ['GeoTIFF', 'GeoTIFF 1 Arc-second'],
    },
    # Void-filled: 3 arc-second globally (1 arc-second mainly over the US)
    'SRTM3': {
        'datasetName': 'srtm_v2',
        'productNames': ['GeoTIFF 3 Arc-second', 'GeoTIFF'],
    },
}

# SRTM coverage limits (degrees)
SRTM_LAT_MIN = -56
SRTM_LAT_MAX = 60

DISPLAY_ID_RE = re.compile(
    r'^SRTM(?:1|3)?([NS])(\d{2})([EW])(\d{3})',
    re.IGNORECASE
)


class M2MClient:
    """Minimal USGS Machine-to-Machine API client (login-token auth)."""

    def __init__(self, username: str, token: str, timeout: int = 120):
        if not username or not token:
            raise ValueError(
                'USGS M2M username and application token are required. '
                'Create an ERS account at https://ers.cr.usgs.gov/, request MACHINE access, '
                'and generate an application token under your profile.'
            )
        self.timeout = timeout
        self.session = requests.Session()
        self.auth_token = self._login(username, token)

    def _login(self, username: str, token: str) -> str:
        response = self.session.post(
            M2M_API_URL + 'login-token',
            json={'username': username, 'token': token},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get('errorCode'):
            raise ConnectionError(
                'USGS M2M login failed: {} - {}'.format(
                    payload.get('errorCode'), payload.get('errorMessage'))
            )
        api_key = payload.get('data')
        if not api_key:
            raise ConnectionError('USGS M2M login did not return an API key.')
        return api_key

    def request(self, endpoint: str, data: Optional[dict] = None) -> Any:
        response = self.session.post(
            M2M_API_URL + endpoint,
            headers={'X-Auth-Token': self.auth_token},
            json=data or {},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get('errorCode') or payload.get('errorMessage'):
            raise ConnectionError(
                'USGS M2M {} failed: {} - {}'.format(
                    endpoint, payload.get('errorCode'), payload.get('errorMessage'))
            )
        return payload.get('data')

    def logout(self):
        try:
            self.request('logout')
        except Exception:
            pass
        self.session.close()

    def scene_search(self, dataset_name: str, bbox: Tuple[float, float, float, float],
                     max_results: int = 1000) -> List[dict]:
        """Search scenes intersecting bbox = (min_lon, min_lat, max_lon, max_lat)."""
        min_lon, min_lat, max_lon, max_lat = bbox
        base = {
            'datasetName': dataset_name,
            'maxResults': max_results,
            'metadataType': 'summary',
            'sceneFilter': {
                'spatialFilter': {
                    'filterType': 'mbr',
                    'lowerLeft': {'longitude': min_lon, 'latitude': min_lat},
                    'upperRight': {'longitude': max_lon, 'latitude': max_lat},
                }
            },
        }

        results = []
        starting_number = 1
        while True:
            payload = dict(base)
            payload['startingNumber'] = starting_number
            data = self.request('scene-search', payload) or {}
            batch = data.get('results') or []
            results.extend(batch)
            returned = data.get('recordsReturned') or len(batch)
            total = data.get('totalHits') or len(results)
            if returned == 0 or len(results) >= total:
                break
            starting_number += returned
        return results

    def get_download_url(self, dataset_name: str, entity_id: str,
                         product_names: List[str]) -> str:
        options = self.request(
            'download-options',
            {'datasetName': dataset_name, 'entityIds': [entity_id]},
        ) or []

        available = {}
        for product in options:
            if product.get('available') and product.get('id'):
                available[product.get('productName', '')] = product['id']

        product_id = None
        for name in product_names:
            if name in available:
                product_id = available[name]
                break
        if product_id is None:
            # Fall back to any available GeoTIFF product
            for name, pid in available.items():
                if 'geotiff' in name.lower():
                    product_id = pid
                    break
        if product_id is None:
            raise ValueError(
                'No GeoTIFF download product for entity {}. Available: {}'.format(
                    entity_id, list(available.keys()))
            )

        label = str(uuid.uuid4())
        request_data = self.request(
            'download-request',
            {'downloads': [{'label': label, 'entityId': entity_id, 'productId': product_id}]},
        ) or {}

        available_downloads = request_data.get('availableDownloads') or []
        if available_downloads and available_downloads[0].get('url'):
            return available_downloads[0]['url']

        # Poll until the download is staged
        for _ in range(30):
            time.sleep(5)
            retrieve = self.request('download-retrieve', {'label': label}) or {}
            ready = retrieve.get('available') or []
            if ready and ready[0].get('url'):
                return ready[0]['url']
            pending = retrieve.get('requested') or []
            if pending and pending[0].get('url'):
                return pending[0]['url']

        raise ConnectionError('Timed out waiting for USGS M2M download URL for ' + entity_id)


def parse_display_id(display_id: str) -> Optional[Tuple[int, int]]:
    """Parse SRTM tile latitude/longitude from an EarthExplorer displayId."""
    match = DISPLAY_ID_RE.match(display_id or '')
    if not match:
        return None
    lat_sign, lat_str, lon_sign, lon_str = match.groups()
    lat = int(lat_str) if lat_sign.upper() == 'N' else -int(lat_str)
    lon = int(lon_str) if lon_sign.upper() == 'E' else -int(lon_str)
    return lat, lon


def latlon_strings(lat: int, lon: int) -> Tuple[str, str]:
    latstr = ('S' if lat < 0 else 'N') + str(abs(lat)).zfill(2)
    lonstr = ('W' if lon < 0 else 'E') + str(abs(lon)).zfill(3)
    return latstr, lonstr


def geotiff_to_hgt(tif_path: str, hgt_path: str, expected_shape: Optional[Tuple[int, int]] = None):
    """Convert a downloaded SRTM GeoTIFF to RIPPL's big-endian int16 .hgt format."""
    dataset = gdal.Open(tif_path)
    if dataset is None:
        raise IOError('Could not open GeoTIFF ' + tif_path)
    band = dataset.GetRasterBand(1)
    data = band.ReadAsArray()
    nodata = band.GetNoDataValue()
    del dataset

    if data is None:
        raise IOError('Empty GeoTIFF raster in ' + tif_path)

    data = np.asarray(data)
    if nodata is not None:
        data = np.where(data == nodata, -32768, data)
    data = np.rint(data).astype(np.int16)

    if expected_shape and data.shape != expected_shape:
        logging.warning(
            'Unexpected SRTM tile shape {} for {} (expected {}). Saving anyway.'.format(
                data.shape, tif_path, expected_shape)
        )

    os.makedirs(os.path.dirname(hgt_path), exist_ok=True)
    data.astype('>i2').tofile(hgt_path)


class SrtmDownloadTile:
    """Download a single SRTM tile given a prepared download URL."""

    def __call__(self, input_data):
        url = input_data[0]
        tif_path = input_data[1]
        hgt_path = input_data[2]
        expected_shape = input_data[3]

        if os.path.exists(hgt_path):
            return

        try:
            self.download_file(url, tif_path)
            geotiff_to_hgt(tif_path, hgt_path, expected_shape)
        except Exception as e:
            for path in (tif_path, hgt_path):
                if os.path.exists(path):
                    os.remove(path)
            raise ConnectionError('Failed to download/convert SRTM tile from {}. {}'.format(url, e))

    @staticmethod
    def download_file(url: str, file_path: str):
        if os.path.exists(file_path) and os.stat(file_path).st_size == 0:
            os.remove(file_path)

        if os.path.exists(file_path):
            return

        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        logging.info('Downloading: ' + file_path)
        with requests.Session() as session:
            response = session.get(url, stream=True, timeout=120)
            response.raise_for_status()
            total_size_in_bytes = int(response.headers.get('content-length', 0))
            block_size = 1024
            progress_bar = tqdm(total=total_size_in_bytes, unit='iB', unit_scale=True)
            with open(file_path, 'wb') as file:
                for data in response.iter_content(block_size):
                    progress_bar.update(len(data))
                    file.write(data)
            progress_bar.close()


class SrtmDownload:

    def __init__(self, srtm_folder='', username='', token='', password='', srtm_type='SRTM3',
                 quality=False, n_processes=4, filelist_folder=''):
        # password is accepted as an alias for the M2M application token (legacy callers).

        settings = UserSettings()
        settings.load_settings()

        if not srtm_folder:
            self.srtm_folder = os.path.join(
                settings.settings['paths']['DEM_database'],
                settings.settings['path_names']['DEM']['SRTM'],
            )
        else:
            self.srtm_folder = srtm_folder

        if not os.path.exists(self.srtm_folder):
            os.mkdir(self.srtm_folder)

        for srtm_type_folder in ['srtm1', 'srtm3']:
            path = os.path.join(self.srtm_folder, srtm_type_folder)
            if not os.path.isdir(path):
                os.mkdir(path)
            orig_path = os.path.join(path, 'orig_data')
            if not os.path.isdir(orig_path):
                os.mkdir(orig_path)

        usgs_account = settings.settings.get('accounts', {}).get('USGS', {})
        if not username:
            username = usgs_account.get('username') or ''
        if not token:
            token = usgs_account.get('token') or password or ''

        self.username = username
        self.token = token
        self.quality = quality
        self.srtm_type = srtm_type
        self.n_processes = n_processes
        self.egm96_interp = []

        if self.srtm_type not in SRTM_DATASETS:
            raise ValueError('Unknown SRTM type {}. Use SRTM1 or SRTM3.'.format(self.srtm_type))

        if self.quality:
            logging.warning(
                'Quality (.q) layers are not provided by the USGS M2M GeoTIFF products and will be skipped.'
            )

    def __call__(self, coordinates, parallel=True):

        if self.srtm_type == 'SRTM3':
            step = 1.0 / 3600 * 3
            expected_shape = (1201, 1201)
        elif self.srtm_type == 'SRTM1':
            step = 1.0 / 3600
            expected_shape = (3601, 3601)
        else:
            logging.info('Unknown SRTM type ' + self.srtm_type)
            return

        self.coordinates = coordinates  # type: CoordinateSystem
        if not np.abs(self.coordinates.dlat - step) < 0.0000000001 or not np.abs(self.coordinates.dlon - step) < 0.0000000001:
            raise ValueError('Value of dlat and dlon of input coordinate system should be ' + str(step))
        if self.coordinates.shape == '' or self.coordinates.shape == [0, 0]:
            raise ValueError('Coordinate shape size is missing to determine DEM download')

        tiles, download_tiles, [tile_lats, tile_lons], _, tiles_zip = self.select_tiles(
            self.coordinates, self.srtm_folder, self.srtm_type, self.quality, download=True
        )
        if len(download_tiles) == 0:
            logging.info('All needed SRTM DEM files already downloaded.')
            return

        bbox = self.coordinates_bbox(self.coordinates)
        dataset_name = SRTM_DATASETS[self.srtm_type]['datasetName']
        product_names = SRTM_DATASETS[self.srtm_type]['productNames']

        logging.info(
            'Searching USGS M2M ({}) for SRTM tiles over AOI {}'.format(dataset_name, bbox)
        )
        client = M2MClient(self.username, self.token)
        try:
            scenes = client.scene_search(dataset_name, bbox)
            logging.info('M2M returned {} SRTM scene(s) for the area of interest.'.format(len(scenes)))

            needed = {(int(lat), int(lon)): (hgt, tif)
                      for lat, lon, hgt, tif in zip(tile_lats, tile_lons, download_tiles, tiles_zip)}
            download_dat = []
            matched = set()

            for scene in scenes:
                parsed = parse_display_id(scene.get('displayId', ''))
                if parsed is None:
                    continue
                lat, lon = parsed
                key = (lat, lon)
                if key not in needed or key in matched:
                    continue

                hgt_path, tif_path = needed[key]
                if os.path.exists(hgt_path):
                    matched.add(key)
                    continue

                url = client.get_download_url(dataset_name, scene['entityId'], product_names)
                download_dat.append([url, tif_path, hgt_path, expected_shape])
                matched.add(key)
                logging.info('Queued SRTM tile {} ({})'.format(scene.get('displayId'), url))

            missing = [key for key in needed if key not in matched and not os.path.exists(needed[key][0])]
            if missing:
                logging.warning(
                    'No M2M SRTM tiles found for {} degree cell(s); these are often ocean or outside coverage: {}'.format(
                        len(missing), missing[:10])
                )

            if len(download_dat) == 0:
                logging.info('All needed SRTM DEM files already downloaded.')
                return

            logging.info('SRTM tiles to be downloaded to ' + os.path.dirname(download_dat[0][2]))
            tile_download = SrtmDownloadTile()
            if self.n_processes > 1 and parallel:
                with get_context('spawn').Pool(processes=self.n_processes, maxtasksperchild=5) as pool:
                    block_size = 25
                    for i in range(int(np.ceil(len(download_dat) / block_size))):
                        last_dat = np.minimum((i + 1) * block_size, len(download_dat))
                        pool.map(tile_download, list(download_dat[i * block_size:last_dat]))
            else:
                for download_info in download_dat:
                    tile_download(download_info)
        finally:
            client.logout()

    @staticmethod
    def coordinates_bbox(coordinates: CoordinateSystem) -> Tuple[float, float, float, float]:
        lat0 = coordinates.lat0 + coordinates.dlat * coordinates.first_line
        lon0 = coordinates.lon0 + coordinates.dlon * coordinates.first_pixel
        lat1 = lat0 + (coordinates.shape[0] - 1) * coordinates.dlat
        lon1 = lon0 + (coordinates.shape[1] - 1) * coordinates.dlon
        return (
            float(min(lon0, lon1)),
            float(min(lat0, lat1)),
            float(max(lon0, lon1)),
            float(max(lat0, lat1)),
        )

    @staticmethod
    def select_tiles(coordinates, srtm_folder, srtm_type='SRTM3', quality=False, download=True,
                     filelist=None):
        """
        Select SRTM tiles covering a geographic CoordinateSystem AOI.

        The unused ``filelist`` argument is kept for backward compatibility with older callers.
        With download=True, missing local tiles are returned for M2M download.
        With download=False, only tiles that already exist on disk are returned (used by ImportDem).
        """

        if not isinstance(coordinates, CoordinateSystem):
            logging.info('coordinates should be an CoordinateSystem object')
            return [], [], [[], []], [], []
        elif coordinates.grid_type != 'geographic':
            logging.info('only geographic coordinate systems can be used to download SRTM data')
            return [], [], [[], []], [], []

        tiles_zip = []
        tiles = []
        download_tiles = []
        tile_lats = []
        tile_lons = []
        url = []

        lat0 = coordinates.lat0 + coordinates.dlat * coordinates.first_line
        lon0 = coordinates.lon0 + coordinates.dlon * coordinates.first_pixel
        # Small epsilon avoids floating-point edges adding an extra degree tile.
        d_small_lat = 0.0001 * np.sign(coordinates.dlat)
        lats = np.arange(
            np.floor(lat0 + d_small_lat),
            np.ceil(lat0 + (coordinates.shape[0] - 1) * coordinates.dlat - d_small_lat),
        ).astype(np.int32)
        d_small_lon = 0.0001 * np.sign(coordinates.dlon)
        lons = np.arange(
            np.floor(lon0 + d_small_lon),
            np.ceil(lon0 + (coordinates.shape[1] - 1) * coordinates.dlon - d_small_lon),
        ).astype(np.int32)

        type_folder = os.path.join(srtm_folder, srtm_type.lower())
        orig_folder = os.path.join(type_folder, 'orig_data')

        for lat in lats:
            for lon in lons:
                lat = int(lat)
                lon = int(lon)

                if lat < SRTM_LAT_MIN or lat >= SRTM_LAT_MAX:
                    continue

                latstr, lonstr = latlon_strings(lat, lon)
                unzip = os.path.join(type_folder, srtm_type + '__' + latstr + lonstr + '.hgt')
                tif = os.path.join(orig_folder, latstr + lonstr + '.tif')

                if download:
                    tiles.append(unzip)
                    if not os.path.exists(unzip):
                        tiles_zip.append(tif)
                        download_tiles.append(unzip)
                        tile_lats.append(lat)
                        tile_lons.append(lon)
                        url.append('')
                else:
                    if os.path.exists(unzip):
                        tiles.append(unzip)
                        tiles_zip.append(tif)
                        download_tiles.append(unzip)
                        tile_lats.append(lat)
                        tile_lons.append(lon)
                        url.append('')

                if quality:
                    logging.debug('Skipping quality tile request for {}{}'.format(latstr, lonstr))

        return tiles, download_tiles, [tile_lats, tile_lons], url, tiles_zip

    @staticmethod
    def srtm_listing(filelist_folder='', username='', password='', token=''):
        """
        Deprecated. Tile availability is determined by M2M scene-search for the AOI.
        Kept so older callers that expect a dict do not break; returns an empty catalog.
        """
        logging.info(
            'srtm_listing() is deprecated: SRTM tiles are discovered via the USGS M2M API for each AOI.'
        )
        return {'SRTM1': {}, 'SRTM3': {}, 'SRTM30': {}}
