"""
Download global DEMs via the OpenTopography API for an area of interest.

API docs: https://portal.opentopography.org/apidocs/#/Public/getGlobalDem
Developer info: https://opentopography.org/developers

Supported dem_type values (RIPPL aliases and native OpenTopography names):
  SRTM1 / SRTMGL1, SRTM3 / SRTMGL3, SRTMGL1_E,
  AW3D30, AW3D30_E, NASADEM, COP30, COP90, EU_DTM,
  SRTM15Plus, GEDI_L3, GEBCOIceTopo, GEBCOSubIceTopo

The full AOI is downloaded in one request, then split into 1x1 degree tiles
for reuse by ImportDem (.hgt for classic SRTM aliases, float32 .raw otherwise).
"""

import os
import logging
from typing import Tuple, Dict, Any, Optional

import numpy as np
import requests
from tqdm import tqdm
from osgeo import gdal
gdal.DontUseExceptions()

from rippl.orbit_geometry.coordinate_system import CoordinateSystem
from rippl.user_settings import UserSettings

OPENTOPO_GLOBALDEM_URL = 'https://portal.opentopography.org/API/globaldem'

# Catalog of OpenTopography global DEM products.
# arc_sec: native posting used for RIPPL geographic grids / 1° tiles
# ellipsoidal: True -> heights are ellipsoidal (skip EGM96 in ImportDem)
# lat_min/lat_max: approximate coverage used to skip out-of-range tiles
OPENTOPO_DEM_TYPES: Dict[str, Dict[str, Any]] = {
    # Classic RIPPL aliases
    'SRTM1': {
        'demtype': 'SRTMGL1', 'arc_sec': 1, 'ellipsoidal': False,
        'lat_min': -56, 'lat_max': 60, 'tile_format': 'hgt',
    },
    'SRTM3': {
        'demtype': 'SRTMGL3', 'arc_sec': 3, 'ellipsoidal': False,
        'lat_min': -56, 'lat_max': 60, 'tile_format': 'hgt',
    },
    # Native OpenTopography names
    'SRTMGL1': {
        'demtype': 'SRTMGL1', 'arc_sec': 1, 'ellipsoidal': False,
        'lat_min': -56, 'lat_max': 60, 'tile_format': 'raw',
    },
    'SRTMGL3': {
        'demtype': 'SRTMGL3', 'arc_sec': 3, 'ellipsoidal': False,
        'lat_min': -56, 'lat_max': 60, 'tile_format': 'raw',
    },
    'SRTMGL1_E': {
        'demtype': 'SRTMGL1_E', 'arc_sec': 1, 'ellipsoidal': True,
        'lat_min': -56, 'lat_max': 60, 'tile_format': 'raw',
    },
    'AW3D30': {
        'demtype': 'AW3D30', 'arc_sec': 1, 'ellipsoidal': False,
        'lat_min': -60, 'lat_max': 60, 'tile_format': 'raw',
    },
    'AW3D30_E': {
        'demtype': 'AW3D30_E', 'arc_sec': 1, 'ellipsoidal': True,
        'lat_min': -60, 'lat_max': 60, 'tile_format': 'raw',
    },
    'NASADEM': {
        'demtype': 'NASADEM', 'arc_sec': 1, 'ellipsoidal': False,
        'lat_min': -56, 'lat_max': 60, 'tile_format': 'raw',
    },
    'COP30': {
        'demtype': 'COP30', 'arc_sec': 1, 'ellipsoidal': False,
        'lat_min': -90, 'lat_max': 90, 'tile_format': 'raw',
    },
    'COP90': {
        'demtype': 'COP90', 'arc_sec': 3, 'ellipsoidal': False,
        'lat_min': -90, 'lat_max': 90, 'tile_format': 'raw',
    },
    'EU_DTM': {
        'demtype': 'EU_DTM', 'arc_sec': 1, 'ellipsoidal': False,
        'lat_min': 34, 'lat_max': 72, 'tile_format': 'raw',
    },
    'SRTM15Plus': {
        'demtype': 'SRTM15Plus', 'arc_sec': 15, 'ellipsoidal': False,
        'lat_min': -90, 'lat_max': 90, 'tile_format': 'raw',
    },
    'GEDI_L3': {
        'demtype': 'GEDI_L3', 'arc_sec': 30, 'ellipsoidal': False,
        'lat_min': -90, 'lat_max': 90, 'tile_format': 'raw',
    },
    'GEBCOIceTopo': {
        'demtype': 'GEBCOIceTopo', 'arc_sec': 15, 'ellipsoidal': False,
        'lat_min': -90, 'lat_max': 90, 'tile_format': 'raw',
    },
    'GEBCOSubIceTopo': {
        'demtype': 'GEBCOSubIceTopo', 'arc_sec': 15, 'ellipsoidal': False,
        'lat_min': -90, 'lat_max': 90, 'tile_format': 'raw',
    },
}


def is_opentopo_dem(dem_type: str) -> bool:
    return dem_type in OPENTOPO_DEM_TYPES


def get_dem_info(dem_type: str) -> Dict[str, Any]:
    if dem_type not in OPENTOPO_DEM_TYPES:
        raise ValueError(
            'Unknown OpenTopography DEM type "{}". Supported: {}'.format(
                dem_type, ', '.join(sorted(OPENTOPO_DEM_TYPES.keys())))
        )
    return OPENTOPO_DEM_TYPES[dem_type]


def latlon_strings(lat: int, lon: int) -> Tuple[str, str]:
    latstr = ('S' if lat < 0 else 'N') + str(abs(lat)).zfill(2)
    lonstr = ('W' if lon < 0 else 'E') + str(abs(lon)).zfill(3)
    return latstr, lonstr


def tile_basename(dem_type: str, lat: int, lon: int, tile_format: str) -> str:
    latstr, lonstr = latlon_strings(lat, lon)
    ext = '.hgt' if tile_format == 'hgt' else '.raw'
    return dem_type + '__' + latstr + lonstr + ext


def geotiff_tile_to_disk(tif_path: str, out_path: str, lat: int, lon: int,
                         arc_sec: float, tile_format: str = 'raw') -> bool:
    """
    Extract one 1x1 degree tile from an AOI GeoTIFF onto a regular geographic grid
    and write it as classic SRTM .hgt (int16 BE) or float32 .raw.
    """
    size = int(round(3600.0 / arc_sec)) + 1
    warp_opts = gdal.WarpOptions(
        format='MEM',
        outputBounds=[lon, lat, lon + 1, lat + 1],
        width=size,
        height=size,
        dstSRS='EPSG:4326',
        resampleAlg=gdal.GRA_Bilinear,
    )
    dataset = gdal.Warp('', tif_path, options=warp_opts)
    if dataset is None:
        raise IOError('Could not warp GeoTIFF ' + tif_path)

    band = dataset.GetRasterBand(1)
    data = band.ReadAsArray()
    nodata = band.GetNoDataValue()
    del dataset

    if data is None:
        raise IOError('Empty GeoTIFF raster in ' + tif_path)

    data = np.asarray(data, dtype=np.float64)
    if nodata is not None:
        data = np.where(data == nodata, np.nan, data)
    # OpenTopography often uses large negative fill over voids/ocean
    data = np.where(data < -1000, np.nan, data)

    if data.shape != (size, size):
        raise IOError(
            'Unexpected DEM tile shape {} for {} (expected ({}, {})).'.format(
                data.shape, tif_path, size, size)
        )

    if np.all(~np.isfinite(data)):
        logging.info('Skipping empty ocean tile lat={}, lon={}'.format(lat, lon))
        return False

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    if tile_format == 'hgt':
        out = np.where(np.isfinite(data), np.rint(data), -32768).astype(np.int16)
        out.astype('>i2').tofile(out_path)
    else:
        out = np.where(np.isfinite(data), data, -99999).astype(np.float32)
        out.tofile(out_path)
    return True


class OpenTopographyDownload:
    """Download and tile any supported OpenTopography global DEM for an AOI."""

    def __init__(self, dem_folder='', api_key='', dem_type='SRTM3', quality=False,
                 n_processes=4, timeout=600, username='', token='', password=''):
        # username/token/password kept for legacy callers; api_key is preferred.
        settings = UserSettings()
        settings.load_settings()

        self.dem_type = dem_type
        self.info = get_dem_info(dem_type)
        self.quality = quality
        self.n_processes = n_processes
        self.timeout = timeout

        if not dem_folder:
            self.dem_folder = self.default_dem_folder(dem_type, settings)
        else:
            self.dem_folder = dem_folder

        type_folder = os.path.join(self.dem_folder, dem_type.lower())
        orig_folder = os.path.join(type_folder, 'orig_data')
        for path in (self.dem_folder, type_folder, orig_folder):
            if not os.path.isdir(path):
                os.makedirs(path, exist_ok=True)

        ot_account = settings.settings.get('accounts', {}).get('OpenTopography', {})
        if not api_key:
            api_key = ot_account.get('api_key') or token or password or ''
        self.api_key = api_key

        if not self.api_key:
            raise ValueError(
                'OpenTopography API key is required. Request one at '
                'https://portal.opentopography.org/ and store it under '
                'accounts["OpenTopography"]["api_key"].'
            )

        if self.quality:
            logging.warning(
                'Quality layers are not provided by OpenTopography global DEM products and will be skipped.'
            )

    @staticmethod
    def default_dem_folder(dem_type: str, settings: Optional[UserSettings] = None) -> str:
        if settings is None:
            settings = UserSettings()
            settings.load_settings()
        dem_db = settings.settings['paths']['DEM_database']
        path_names = settings.settings.get('path_names', {}).get('DEM', {})
        if dem_type in ('SRTM1', 'SRTM3'):
            return os.path.join(dem_db, path_names.get('SRTM', 'srtm'))
        return os.path.join(dem_db, path_names.get('OpenTopography', 'opentopography'))

    def __call__(self, coordinates, parallel=True):
        arc_sec = float(self.info['arc_sec'])
        step = arc_sec / 3600.0
        tile_format = self.info['tile_format']

        self.coordinates = coordinates  # type: CoordinateSystem
        if not np.abs(self.coordinates.dlat - step) < 1e-10 or not np.abs(self.coordinates.dlon - step) < 1e-10:
            raise ValueError(
                'Value of dlat and dlon of input coordinate system should be {} '
                'for dem_type {}.'.format(step, self.dem_type)
            )
        if self.coordinates.shape == '' or self.coordinates.shape == [0, 0]:
            raise ValueError('Coordinate shape size is missing to determine DEM download')

        tiles, download_tiles, [tile_lats, tile_lons], _, _ = self.select_tiles(
            self.coordinates, self.dem_folder, self.dem_type, self.quality, download=True
        )
        if len(download_tiles) == 0:
            logging.info('All needed {} DEM files already downloaded.'.format(self.dem_type))
            return

        south = int(min(tile_lats))
        north = int(max(tile_lats)) + 1
        west = int(min(tile_lons))
        east = int(max(tile_lons)) + 1
        api_demtype = self.info['demtype']

        type_folder = os.path.join(self.dem_folder, self.dem_type.lower())
        orig_folder = os.path.join(type_folder, 'orig_data')
        aoi_tif = os.path.join(
            orig_folder,
            '{}_{}_{}_{}_{}.tif'.format(api_demtype, south, north, west, east),
        )

        logging.info(
            'Downloading OpenTopography {} for full AOI south={}, north={}, west={}, east={}'.format(
                api_demtype, south, north, west, east)
        )
        self.download_aoi(api_demtype, south, north, west, east, aoi_tif)

        logging.info(
            'Splitting AOI GeoTIFF into {} {} tile(s)'.format(len(download_tiles), tile_format)
        )
        for lat, lon, tile_path in zip(tile_lats, tile_lons, download_tiles):
            if os.path.exists(tile_path):
                continue
            geotiff_tile_to_disk(aoi_tif, tile_path, int(lat), int(lon), arc_sec, tile_format)

    def download_aoi(self, demtype: str, south: float, north: float, west: float, east: float,
                     file_path: str):
        """Download the full AOI as a single GeoTIFF from OpenTopography."""
        if os.path.exists(file_path) and os.stat(file_path).st_size == 0:
            os.remove(file_path)
        if os.path.exists(file_path):
            logging.info('Using existing AOI GeoTIFF ' + file_path)
            return

        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        params = {
            'demtype': demtype,
            'south': south,
            'north': north,
            'west': west,
            'east': east,
            'outputFormat': 'GTiff',
            'API_Key': self.api_key,
        }

        with requests.Session() as session:
            response = session.get(
                OPENTOPO_GLOBALDEM_URL, params=params, stream=True, timeout=self.timeout
            )
            if response.status_code == 204:
                raise FileNotFoundError(
                    'No OpenTopography data for AOI south={}, north={}, west={}, east={}.'.format(
                        south, north, west, east)
                )
            if response.status_code == 401:
                raise ConnectionError('OpenTopography API key is invalid or unauthorized.')
            response.raise_for_status()

            content_type = (response.headers.get('content-type') or '').lower()
            if 'json' in content_type or 'text' in content_type or 'html' in content_type:
                raise ConnectionError(
                    'OpenTopography returned an error instead of a GeoTIFF: ' + response.text[:500]
                )

            total_size_in_bytes = int(response.headers.get('content-length', 0))
            block_size = 1024 * 64
            progress_bar = tqdm(total=total_size_in_bytes, unit='iB', unit_scale=True)
            try:
                with open(file_path, 'wb') as file:
                    for data in response.iter_content(block_size):
                        progress_bar.update(len(data))
                        file.write(data)
            except Exception:
                if os.path.exists(file_path):
                    os.remove(file_path)
                raise
            finally:
                progress_bar.close()

        logging.info('Saved AOI GeoTIFF to ' + file_path)

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
    def select_tiles(coordinates, dem_folder, dem_type='SRTM3', quality=False, download=True,
                     filelist=None):
        """
        Select 1x1 degree tiles covering a geographic CoordinateSystem AOI.

        With download=True, missing local tiles are returned for download.
        With download=False, only tiles that already exist on disk are returned.
        """
        if not isinstance(coordinates, CoordinateSystem):
            logging.info('coordinates should be an CoordinateSystem object')
            return [], [], [[], []], [], []
        elif coordinates.grid_type != 'geographic':
            logging.info('only geographic coordinate systems can be used to download OpenTopography data')
            return [], [], [[], []], [], []

        info = get_dem_info(dem_type)
        tile_format = info['tile_format']
        lat_min = info['lat_min']
        lat_max = info['lat_max']

        tiles_tif = []
        tiles = []
        download_tiles = []
        tile_lats = []
        tile_lons = []
        url = []

        lat0 = coordinates.lat0 + coordinates.dlat * coordinates.first_line
        lon0 = coordinates.lon0 + coordinates.dlon * coordinates.first_pixel
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

        type_folder = os.path.join(dem_folder, dem_type.lower())
        orig_folder = os.path.join(type_folder, 'orig_data')

        for lat in lats:
            for lon in lons:
                lat = int(lat)
                lon = int(lon)

                if lat < lat_min or lat >= lat_max:
                    continue

                tile_path = os.path.join(type_folder, tile_basename(dem_type, lat, lon, tile_format))
                tif = os.path.join(orig_folder, latlon_strings(lat, lon)[0] + latlon_strings(lat, lon)[1] + '.tif')

                if download:
                    tiles.append(tile_path)
                    if not os.path.exists(tile_path):
                        tiles_tif.append(tif)
                        download_tiles.append(tile_path)
                        tile_lats.append(lat)
                        tile_lons.append(lon)
                        url.append('')
                else:
                    if os.path.exists(tile_path):
                        tiles.append(tile_path)
                        tiles_tif.append(tif)
                        download_tiles.append(tile_path)
                        tile_lats.append(lat)
                        tile_lons.append(lon)
                        url.append('')

                if quality:
                    logging.debug('Skipping quality tile request for {}'.format(tile_path))

        return tiles, download_tiles, [tile_lats, tile_lons], url, tiles_tif
