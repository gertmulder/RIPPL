# This processing file is a template for other processing steps.
# Do not change the already given steps in this function to prevent problems with creating pipeline processing
# later on.

# Try to do all calculations using numpy/scipy functions.
import numpy as np
from collections import OrderedDict
import os
import logging
import re

# Import the parent class Process for processing steps.
from rippl.resampling.coor_new_extend import CoorNewExtend
from rippl.external_dems.geoid import GeoidInterp
from rippl.meta_data.process import Process
from rippl.external_dems.opentopography.opentopography_download import (
    OpenTopographyDownload, is_opentopo_dem, get_dem_info)
from rippl.external_dems.tandem_x.tandem_x_download import TandemXDownload
from rippl.meta_data.image_processing_data import ImageProcessingData
from rippl.orbit_geometry.coordinate_system import CoordinateSystem
from rippl.user_settings import UserSettings


class ImportDem(Process):  # Change this name to the one of your processing step.

    def __init__(self, data_id='', in_coor='', out_coor='', reference_slc='reference_slc', dem_folder=None, quality=False,
                 buffer=0, rounding=0, expected_min_height=0, expected_max_height=500,
                 dem_type='SRTM3', lon_resolution=3, geoid_file=None, overwrite=False):

        """
        This function creates a dem. Supported options include OpenTopography products (SRTM1/SRTM3, COP30,
        AW3D30, NASADEM, ...) and TanDEM-X (TDM30/TDM90).

        :param str data_id: Data ID of image. Only used in specific cases where the processing chain contains 2 times
                    the same process.
        :param CoordinateSystem in_coor: Coordinate system of the input radar grid for which we create a grid. An output
                    grid will be generated in this function. (Not needed, only when output grid is not defined)
        :param CoordinateSystem out_coor: Coordinate system of the input radar grid for which we create a grid. An output
                    grid will be generated in this function. (Not needed if input grid is defined)
        :param ImageProcessingData reference_slc: Image used to coregister the secondary_slc image for resampling etc.
        """

        # Dummy input info because we do not have inputs, but needed for correct processing
        self.input_info = {'process_names': [], 'image_types': [], 'polarisations': [], 'data_ids': [], 'coor_types': [],
                           'file_names': [], 'data_types': [], 'in_coor_types': [], 'aliases_processing': []}

        # Output data information
        self.output_info = dict()
        self.output_info['process_name'] = 'dem'
        self.output_info['image_type'] = 'reference_slc'
        self.output_info['polarisation'] = ''
        self.output_info['data_id'] = data_id
        self.output_info['coor_type'] = 'out_coor'
        if not quality:
            self.output_info['file_names'] = ['dem']
            self.output_info['data_types'] = ['real4']
        else:
            self.output_info['file_names'] = ['dem', 'dem_quality']
            self.output_info['data_types'] = ['real4', 'int8']

        # Save the settings for dem generation
        settings = UserSettings()
        settings.load_settings()
        if not dem_folder:
            self.dem_folder = self.default_dem_folder(dem_type, settings)
        else:
            self.dem_folder = dem_folder
        if not geoid_file:
            self.geoid_file = os.path.join(settings.settings['paths']['DEM_database'], 'geoid', 'egm96.dat')
        else:
            self.geoid_file = geoid_file

        if not os.path.exists(self.dem_folder):
            raise FileExistsError('Path to DEM folder does not exist. Enter valid path for DEM data.')
        self.dem_type = dem_type
        self.quality = quality
        self.settings = OrderedDict({'in_coor': dict(), 'out_coor': dict()})
        self.settings['dem_folder'] = self.dem_folder
        self.settings['quality'] = self.quality
        self.settings['out_coor']['buffer'] = buffer
        self.settings['out_coor']['rounding'] = rounding
        self.settings['out_coor']['min_height'] = expected_min_height
        self.settings['out_coor']['max_height'] = expected_max_height
        self.settings['dem_type'] = dem_type
        self.settings['lon_resolution'] = lon_resolution

        self.overwrite = overwrite

        # Coordinate systems
        if isinstance(in_coor, CoordinateSystem):
            logging.info('Generating DEM input size from input grid')
            out_coor = self.create_dem_coor(dem_type, in_coor, True, buffer, rounding, lon_resolution, expected_min_height, expected_max_height)
        self.coordinate_systems = dict()
        self.coordinate_systems['out_coor'] = out_coor

        # image data processing
        self.processing_images = dict()
        self.processing_images['reference_slc'] = reference_slc

    @staticmethod
    def default_dem_folder(dem_type, settings=None):
        if settings is None:
            settings = UserSettings()
            settings.load_settings()
        dem_db = settings.settings['paths']['DEM_database']
        path_names = settings.settings.get('path_names', {}).get('DEM', {})
        if dem_type in ('SRTM1', 'SRTM3'):
            return os.path.join(dem_db, path_names.get('SRTM', 'srtm'))
        if dem_type.startswith('TDM'):
            return os.path.join(dem_db, path_names.get('TanDEM-X', 'tdx'))
        if is_opentopo_dem(dem_type):
            return os.path.join(dem_db, path_names.get('OpenTopography', 'opentopography'))
        return os.path.join(dem_db, dem_type)

    @staticmethod
    def create_dem_coor(dem_type, in_coor, calc_shape=True, buffer=0, rounding=0, lon_resolution=3, min_height=0, max_height=500):

        out_coor = CoordinateSystem()
        if is_opentopo_dem(dem_type):
            arc_sec = float(get_dem_info(dem_type)['arc_sec'])
            out_coor.create_geographic(arc_sec / 3600.0, arc_sec / 3600.0)
        elif dem_type == 'TDM30':
            out_coor.create_geographic(1.0 / 3600.0, lon_resolution / 3600.0)
        elif dem_type == 'TDM90':
            out_coor.create_geographic(3.0 / 3600.0, lon_resolution / 3600.0)
        else:
            raise ValueError(
                'dem type "{}" not supported. Options are OpenTopography products '
                '(SRTM1, SRTM3, COP30, AW3D30, NASADEM, ...) and TanDEM-X (TDM30, TDM90).'.format(dem_type)
            )

        # Calculate the extent of the output grid.
        if calc_shape:
            out_coor = CoorNewExtend(in_coor=in_coor, out_coor=out_coor, buffer=buffer, rounding=rounding,
                                     min_height=min_height, max_height=max_height).out_coor
            return out_coor
        else:
            return out_coor

    def process_calculations(self):
        """
        Find the already downloaded dem files and subtract the needed regions. Save these files in the right format
        to disk.

        :return:
        """

        # Because there is no input coordinate system we have to give the chunk coordinate system if the dem is loaded
        # in chunks.
        geoid = GeoidInterp.create_geoid(self.coordinate_systems['out_coor_chunk'], self.geoid_file)
        dem_type = self.settings['dem_type']

        if is_opentopo_dem(dem_type):
            info = get_dem_info(dem_type)
            self['dem'] = np.flipud(self.create_dem(
                self.coordinate_systems['out_coor_chunk'], self.dem_folder, dem_type, quality=False))
            if self.quality:
                if info['tile_format'] != 'hgt':
                    logging.warning('Quality layers are not available for OpenTopography product {}.'.format(dem_type))
                else:
                    self['dem_quality_' + dem_type] = np.flipud(self.create_dem(
                        self.coordinate_systems['out_coor_chunk'], self.dem_folder, dem_type, quality=True))
            # Convert to ellipsoidal height; fill voids/water with the geoid (ellipsoid height).
            self['dem'] = self.to_ellipsoid_heights(
                self['dem'], geoid, ellipsoidal=info.get('ellipsoidal', False))
        elif dem_type in ['TDM30', 'TDM90']:
            # TDM90 gives incorrect values over the ocean. Use ellipsoid height there instead.
            self['dem'] = np.flipud(self.create_dem(
                self.coordinate_systems['out_coor_chunk'], self.dem_folder, dem_type,
                lon_resolution=self.settings['lon_resolution']))
            self['dem'] = self.to_ellipsoid_heights(self['dem'], geoid, ellipsoidal=True)
        else:
            raise ValueError('Unsupported dem_type for ImportDem: ' + dem_type)

        self['dem'] = self['dem'].astype(np.float32)

    @staticmethod
    def to_ellipsoid_heights(dem, geoid, ellipsoidal=False):
        """
        Convert a DEM to ellipsoidal heights and fill missing/water voids with the geoid.

        Orthometric products: h = H + N for valid pixels; voids use h = N.
        Ellipsoidal products: valid pixels kept as-is; voids use h = N (approx. sea surface).

        Nodata conventions: OpenTopography .hgt voids (-32768), .raw / TanDEM-X voids (-99999),
        and non-finite values.
        """
        dem = np.asarray(dem, dtype=np.float32).copy()
        geoid = np.asarray(geoid, dtype=np.float32)
        missing = ~np.isfinite(dem) | (dem <= -1000)

        if ellipsoidal:
            dem[missing] = geoid[missing]
        else:
            # Treat voids as orthometric height 0 (sea level), then add geoid undulation.
            dem = np.where(missing, 0.0, dem) + geoid
        return dem

    @staticmethod
    def create_dem(coordinates, dem_folder, dem_type='SRTM3', quality=False, lon_resolution=3):
        """
        Create a DEM grid from downloaded tiles.

        :param CoordinateSystem coordinates:
        :param str dem_folder: Folder where downloaded DEM data is stored
        :param str dem_type: Type of DEM data (OpenTopography product or TanDEM-X)
        :param bool quality: Defines whether we create a quality or regular dem grid
        :return: dem or quality grid
        :rtype: np.ndarray
        """

        if not dem_folder:
            dem_folder = ImportDem.default_dem_folder(dem_type)

        if is_opentopo_dem(dem_type):
            info = get_dem_info(dem_type)
            tiles = OpenTopographyDownload.select_tiles(
                coordinates, dem_folder, dem_type, quality=quality, download=False)[0]
            if info['tile_format'] == 'hgt':
                tiles = [tile[:-4] for tile in tiles if tile.endswith('.hgt')]
                dat_type = '.q' if quality else '.hgt'
            else:
                if quality:
                    raise TypeError('No quality files available for OpenTopography product {}.'.format(dem_type))
                tiles = [tile[:-4] for tile in tiles if tile.endswith('.raw')]
                dat_type = '.raw'
            arc_sec = float(info['arc_sec'])
            size = int(round(3600.0 / arc_sec)) + 1
            tile_shape = (size, size)
            fill_value = 0 if dat_type == '.hgt' else -99999
            outputdata = (
                np.zeros(coordinates.shape, dtype=np.int8) if quality
                else np.ones(coordinates.shape, dtype=np.float32) * fill_value
            )
        elif dem_type.startswith('TDM'):
            filelist = TandemXDownload.tandem_x_listing()
            tiles = TandemXDownload.select_tiles(
                filelist, coordinates, dem_folder, tandem_x_type=dem_type,
                lon_resolution=lon_resolution)[0]
            if quality:
                raise TypeError('No quality files available for Tandem-x data.')
            outputdata = np.ones(coordinates.shape, dtype=np.float32) * -99999
            dat_type = ''
            if dem_type == 'TDM30':
                tile_shape = (3601, int(np.round(3600 / lon_resolution + 1)))
            else:
                tile_shape = (1201, int(np.round(3600 / lon_resolution + 1)))
        else:
            raise ValueError('Unsupported dem_type: ' + dem_type)

        lat_size = coordinates.dlat
        lon_size = coordinates.dlon
        step_lat = 1
        step_lon = 1

        for tile in tiles:
            tile_name = tile + dat_type
            if not os.path.exists(tile_name):
                raise FileExistsError('Tile ' + os.path.basename(tile_name) + ' does not exist!')

            if dat_type == '.q':
                image = np.fromfile(tile + dat_type, dtype='>u1').reshape(tile_shape)
            elif dat_type == '.hgt':
                image = np.fromfile(tile + dat_type, dtype='>i2').reshape(tile_shape)
            elif dat_type in ('.raw', '') or dem_type.startswith('TDM'):
                image = np.fromfile(tile + dat_type, dtype=np.float32).reshape(tile_shape)
            else:
                raise TypeError('images type should be ".q, .raw or .hgt"')

            lat, lon = ImportDem._parse_tile_latlon(os.path.basename(tile), dem_type)

            logging.info('adding ' + tile)
            lat0 = coordinates.lat0 + coordinates.dlat * coordinates.first_line
            lon0 = coordinates.lon0 + coordinates.dlon * coordinates.first_pixel

            latlim = [lat0, lat0 + coordinates.dlat * (coordinates.shape[0] - 1)]
            lonlim = [lon0, lon0 + coordinates.dlon * (coordinates.shape[1] - 1)]

            # Find the coordinates of the part of the tile that should be written to the output data.
            t_latlim = [max(lat, latlim[0]), min(lat + step_lat, latlim[1])]
            t_lonlim = [max(lon, lonlim[0]), min(lon + step_lon, lonlim[1])]
            t_latid = [tile_shape[0] - int(round((t_latlim[0] - lat) / lat_size)),
                       tile_shape[0] - (int(round((t_latlim[1] - lat) / lat_size)) + 1)]
            t_lonid = [int(round((t_lonlim[0] - lon) / lon_size)),
                       int(round((t_lonlim[1] - lon) / lon_size)) + 1]

            lat_total_size = int(round((latlim[1] - latlim[0]) / lat_size)) + 1
            latid = [lat_total_size - int(round((t_latlim[0] - latlim[0]) / lat_size)),
                     lat_total_size - (int(round((t_latlim[1] - latlim[0]) / lat_size)) + 1)]
            lonid = [int(round((t_lonlim[0] - lonlim[0]) / lon_size)),
                     int(round((t_lonlim[1] - lonlim[0]) / lon_size)) + 1]

            logging.info('Adding tile lat ' + str(t_latid[1] + 1) + ' to ' + str(t_latid[0]) + ' into dem file ' +
                  str(latid[1] + 1) + ' to ' + str(latid[0]))
            logging.info('Adding tile lon ' + str(t_lonid[0] + 1) + ' to ' + str(t_lonid[1]) + ' into dem file ' +
                  str(lonid[0] + 1) + ' to ' + str(lonid[1]))

            # Assign values from tiles to outputdata
            outputdata[latid[1]: latid[0], lonid[0]: lonid[1]] = image[t_latid[1]: t_latid[0], t_lonid[0]: t_lonid[1]]

        return outputdata

    @staticmethod
    def _parse_tile_latlon(basename, dem_type):
        """Parse latitude/longitude from tile basename like SRTM3__N51E004 or TDM_DEM_N51E004."""
        match = re.search(r'([NS])(\d{2})([EW])(\d{3})', basename)
        if not match:
            raise ValueError('Could not parse lat/lon from DEM tile name: ' + basename)
        lat_sign, lat_str, lon_sign, lon_str = match.groups()
        lat = float(lat_str) if lat_sign == 'N' else -float(lat_str)
        lon = float(lon_str) if lon_sign == 'E' else -float(lon_str)
        return lat, lon
