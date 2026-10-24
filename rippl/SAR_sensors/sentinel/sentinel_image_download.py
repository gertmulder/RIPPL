# This file contains a function to check which files for sentinel are available, which ones are downloaded and a quality
# check for the files which are downloaded.
from typing import Optional

import requests
import numpy as np
import logging
import os
import datetime
import base64
import shapely

from rippl.SAR_sensors.sar_image_download import DownloadSar, stream_response_to_file, run_parallel_downloads
from rippl.SAR_sensors.sentinel.sentinel_burst_id import SentinelBurstId

"""
# Test 
import rippl
from rippl.orbit_geometry.read_write_shapes import ReadWriteShapes
from rippl.SAR_sensors.sentinel.sentinel_image_download import DownloadSentinel
import datetime

start = datetime.datetime(year=2017, month=7, day=16)
end = datetime.datetime(year=2017, month=7, day=28)

Benelux_shape = [[7.218017578125001, 53.27178347923819],
                 [5.218505859375, 53.50111704294316],
                 [4.713134765624999, 53.20603255157844],
                 [3.3508300781249996, 51.60437164681676],
                 [3.8452148437499996, 50.127621728300475],
                 [4.493408203125, 49.809631563563094],
                 [6.35009765625, 49.36806633482156],
                 [6.83349609375, 52.5897007687178],
                 [7.218017578125001, 53.27178347923819]]
study_area = ReadWriteShapes()
study_area(Benelux_shape)
shape = study_area.shape.buffer(0.2)

mode = 'IW'
product = 'SLC'
orbit_direction = 'DSC'
n_processes = 4
track = 37

self = DownloadSentinel(start_date=start, end_date=end, track=track,
                        shape=shape, orbit_direction=orbit_direction, 
                        sensor_mode='IW', product=product, n_processes=n_processes)

# First test ASF normal search and download
self.sentinel_search_ASF()
self.summarize_search_results()
self.sentinel_download_ASF()

# Then test ESA search and download
self.sentinel_search_ESA()
self.summarize_search_results()
self.sentinel_download_ESA()

# Finally test ASF download burstwise (not working jet, most likely because not implemented at ASF. Possibly because
# only the database with extracted bursts is checked. However, over most areas they are not extracted yet.)
self = DownloadSentinel(start_date=start, end_date=end, use_burst_id=True,
                        shape=shape, orbit_direction=orbit_direction, 
                        sensor_mode='IW', product=product, n_processes=n_processes)
self.sentinel_search_ASF()
self.summarize_search_results()
self.sentinel_download_ASF()

"""


class ASFDownloader():
    """Download one Sentinel-1 product from the ASF data pool."""

    def __init__(self, username, password):
        self.username = username
        self.password = password

    def __call__(self, download_data):
        [product_name, date, track, polarisation, direction, size, file_path, pos] = download_data

        url = 'https://datapool.asf.alaska.edu/SLC/SA/' + os.path.basename(file_path)
        edl_host = 'urs.earthdata.nasa.gov'
        edl_client_id = 'BO_n7nTIlMljdvU6kRRB3g'
        asf_auth_host = 'auth.asf.alaska.edu'
        login_url = (f'https://{edl_host}/oauth/authorize?client_id={edl_client_id}'
                     f'&response_type=code&redirect_uri=https://{asf_auth_host}/login')

        req = requests.Session()
        req.auth = (self.username, self.password)
        req.get(login_url)
        response = req.get(url, stream=True)
        response.raise_for_status()
        stream_response_to_file(response, file_path, position=pos or 1)


class ESADownloader():
    """Download one Sentinel-1 product from the Copernicus CDSE zipper."""

    def __init__(self, username, password):
        self.username = username
        self.password = password

    def __call__(self, download_data):
        [uuid, product_name, date, track, polarisation, direction, size, file_path, pos] = download_data

        r = requests.post(
            'https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token',
            data={'client_id': 'cdse-public', 'username': self.username,
                  'password': self.password, 'grant_type': 'password'})
        r.raise_for_status()
        token = r.json()['access_token']

        url = f'https://zipper.dataspace.copernicus.eu/odata/v1/Products({uuid})/$value'
        response = requests.get(url, headers={'Authorization': f'Bearer {token}'}, stream=True)
        response.raise_for_status()
        stream_response_to_file(response, file_path, position=pos or 1)


class DownloadSentinel(DownloadSar):

    sensor_name = 'Sentinel-1'

    def __init__(self, start_date='', end_date='', start_dates='', end_dates='', date='', dates='', time_window='',
                 shape='', track='', polarisation='', n_processes=1, use_burst_id=False,
                 orbit_direction='', sensor_mode='IW', product='SLC', instrument_name='', max_num_products=1000):
        # Following variables can be used to make a selection.
        # shape > defining shape file or .kml
        # start_date > first day for downloads (default one month before now) [yyyymmdd]
        # end_date > last day for downloads (default today)
        # track > the tracks we want to check (default all)
        # polarisation > which polarisation will be used. (default all)

        super().__init__(
            start_date=start_date, end_date=end_date, start_dates=start_dates, end_dates=end_dates,
            date=date, dates=dates, time_window=time_window, shape=shape, track=track,
            polarisation=polarisation, orbit_direction=orbit_direction, sensor_mode=sensor_mode,
            product=product, max_num_products=max_num_products, n_processes=n_processes)

        self.use_burst_id = use_burst_id
        if instrument_name not in ['1A', '1B', '']:
            logging.error('Instrument name should either be 1A, 1B or left empty')
        self.instrument_name = instrument_name
        self.orbit = ''
        self.data_collection = 'SENTINEL-1'
        # ESA / ASF catalogue spellings derived from the shared normalized direction.
        # CDSE expects uppercase ASCENDING/DESCENDING for orbitDirection.
        if self.orbit_direction == 'ascending':
            self.orbit_direction_ESA = 'ASCENDING'
            self.orbit_direction_ASF = 'ASC'
        elif self.orbit_direction == 'descending':
            self.orbit_direction_ESA = 'DESCENDING'
            self.orbit_direction_ASF = 'DESC'
        else:
            self.orbit_direction_ESA = ''
            self.orbit_direction_ASF = ''
        # ESA OData $top expects a string
        self.max_num_products = str(max_num_products)

    def search_string_ESA(self,
                          start_date: Optional[datetime.datetime] = None,
                          end_date: Optional[datetime.datetime] = None) -> str:
        """"Get querying URL"""

        url_base = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products?$filter="
        if self.instrument_name:
            satellite_platform_letter = self.instrument_name[-1]
        else:
            satellite_platform_letter = ''

        # Initialize
        q = {}
        q_keys = ['name', 'area', 'date_start', 'date_end', 'orbit_direction',
                  'product_type', 'operational_mode', 'satellite_platform_letter',
                  'relative_orbit_number', 'orbit_number', 'polarisation_mode',
                  'include_string', 'exclude_string', 'limit']

        date_start_str = start_date.strftime('%Y-%m-%d')
        date_end_str = end_date.strftime('%Y-%m-%d')

        # Set
        q['name'] = f"Collection/Name eq '{self.data_collection}'" if self.data_collection else ""
        q['area'] = f"OData.CSC.Intersects(area=geography'SRID=4326;POLYGON({self.shape_string})')" if self.shape_string else ""
        q['date_start'] = f"ContentDate/Start gt {date_start_str}T00:00:00.000Z" if date_start_str else ""
        q['date_end'] = f"ContentDate/Start lt {date_end_str}T23:59:59.000Z" if date_end_str else ""
        q['orbit_direction'] = f"Attributes/OData.CSC.StringAttribute/any(att:att/Name eq 'orbitDirection' and att/OData.CSC.StringAttribute/Value eq '{self.orbit_direction_ESA}')" if self.orbit_direction_ESA else ""
        # CDSE productType values look like 'IW_SLC__1S'; allow short aliases such as 'SLC'
        if self.product:
            if '_' in self.product:
                q['product_type'] = (
                    "Attributes/OData.CSC.StringAttribute/any(att:att/Name eq 'productType' and "
                    f"att/OData.CSC.StringAttribute/Value eq '{self.product}')")
            else:
                q['product_type'] = f"contains(Name,'{self.product}')"
        else:
            q['product_type'] = ""
        q['operational_mode'] = f"Attributes/OData.CSC.StringAttribute/any(att:att/Name eq 'operationalMode' and att/OData.CSC.StringAttribute/Value eq '{self.sensor_mode}')" if self.sensor_mode else ""
        q['satellite_platform_letter'] = f"Attributes/OData.CSC.StringAttribute/any(att:att/Name eq 'platformSerialIdentifier' and att/OData.CSC.StringAttribute/Value eq '{satellite_platform_letter}')" if satellite_platform_letter else ""
        q['relative_orbit_number'] = f"Attributes/OData.CSC.IntegerAttribute/any(att:att/Name eq 'relativeOrbitNumber' and att/OData.CSC.IntegerAttribute/Value eq {self.track})" if self.track else ""
        q['orbit_number'] = f"Attributes/OData.CSC.IntegerAttribute/any(att:att/Name eq 'orbitNumber' and att/OData.CSC.IntegerAttribute/Value eq {self.orbit})" if self.orbit else ""
        q['polarisation_mode'] = f"Attributes/OData.CSC.StringAttribute/any(att:att/Name eq 'polarisationChannels' and att/OData.CSC.StringAttribute/Value eq '{self.polarisation.replace(' ', '%26')}')" if self.polarisation else ""

        # limit is added separately
        q_limit = f"&$top={self.max_num_products}" if self.max_num_products else ""

        # Get all query items
        query_list = []
        for key, item in q.items():
            if item:
                query_list.append(item)

        # Join all query items
        query = " and ".join(query_list)

        # Prepend url
        url_req = f'{url_base}{query}{q_limit}&$expand=Attributes'

        return url_req

    @staticmethod
    def _esa_attribute_value(attributes, name, default=None):
        for attribute in attributes or []:
            if attribute.get('Name') == name:
                return attribute.get('Value', default)
        return default

    def sentinel_search_ESA(self):
        # All available sentinel-1 images are detected and printed on screen.

        self.reset_search_results()

        for start_date, end_date in zip(self.start_dates, self.end_dates):

            Copernicus_string = self.search_string_ESA(start_date, end_date)
            products = []
            # Finally we do the query to get the search result.
            # connect to server. Hopefully this works at once
            try:
                r = requests.get(Copernicus_string)

                # Check status code
                if r.status_code != 200:
                    logging.error(f'Response code was {r.status_code}. Output: {r.text}. '
                                  f'Please verify.', stacklevel=2)
                else:
                    products = r.json()['value']
            except Exception as e:
                logging.error("Could not query CDSE.", exc_info=e, stacklevel=2)

            self.products.extend(products)

            for product in products:
                attributes = product.get('Attributes', [])
                self.sizes.append(None)
                pol = self._esa_attribute_value(attributes, 'polarisationChannels', '')
                self.polarisations.append(str(pol).replace('&', ''))
                self.footprints.append(shapely.Polygon(product['GeoFootprint']['coordinates'][0]))
                track = self._esa_attribute_value(attributes, 'relativeOrbitNumber', None)
                self.tracks.append(int(track) if track is not None else None)
                direction = self._esa_attribute_value(attributes, 'orbitDirection', '')
                self.orbit_directions.append(direction)
                self.ids.append(product['Id'])
                # Strip .SAFE suffix for consistency with ASF product names
                name = product['Name']
                if name.endswith('.SAFE'):
                    name = name[:-5]
                self.product_names.append(name)
                if product['ContentDate']['Start'].endswith('Z'):
                    self.dates.append(
                        datetime.datetime.strptime(product['ContentDate']['Start'], '%Y-%m-%dT%H:%M:%S.%fZ'))
                else:
                    self.dates.append(
                        datetime.datetime.strptime(product['ContentDate']['Start'], '%Y-%m-%dT%H:%M:%S.%f'))

        if len(self.dates) == 0:
            logging.info('No images found!')

    def sentinel_download_ESA(self, database_folder='', username='', password=''):
        # Download the files which are found by the sentinel_available script.

        if not username or not password:
            username, password = self.get_account_credentials('Copernicus', username, password)
        if not database_folder:
            database_folder = self.get_radar_database_folder('Sentinel-1')

        if not self.products:
            raise FileNotFoundError('No images found!')

        downloader = ESADownloader(username, password)

        # Loop over all images
        download_dat = []
        pos = 0
        for uuid, product_name, date, track, polarisation, direction, size in zip(self.ids, self.product_names,
                                                                                  self.dates,
                                                                                  self.tracks, self.polarisations,
                                                                                  self.orbit_directions, self.sizes):
            file_path = DownloadSentinel.find_database_folder(database_folder, product_name, date, track,
                                                              polarisation, direction)
            if not os.path.exists(file_path):
                download_dat.append([uuid, product_name, date, track, polarisation, direction, size, file_path, pos])
                pos += 1

        run_parallel_downloads(downloader, download_dat, n_processes=self.n_processes)

    def search_string_ASF(self, start, end):

        ASF_string = ''
        if not isinstance(start, datetime.datetime) or not isinstance(end, datetime.datetime):
            logging.info('Start and end date should be datetime objects!')

        if self.instrument_name:
            ASF_string += '&platform=' + self.instrument_name
        if self.sensor_mode:
            ASF_string += '&beamMode=' + self.sensor_mode
        if self.product:
            ASF_string += '&processingLevel=' + self.product
        if self.orbit_direction_ASF:
            ASF_string += '&flightDirection=' + self.orbit_direction_ASF
        if self.track:
            ASF_string += '&relativeOrbit=' + str(self.track)
        if self.polarisation:
            if self.polarisation == 'HH':
                pol_str = 'HH,HH+HV'
            elif self.polarisation == 'HV':
                pol_str = 'HH+HV'
            elif self.polarisation == 'VV':
                pol_str = 'VV,VV+VH'
            elif self.polarisation == 'VH':
                pol_str = 'VV+VH'
            elif self.polarisation == ['HH', 'HV']:
                pol_str = 'HH+HV'
            elif self.polarisation == ['VV', 'VH']:
                pol_str = 'VV+VH'
            else:
                pol_str = self.polarisation
            ASF_string += '&polarization=' + pol_str
        # Get the burst IDs
        if self.use_burst_id:
            logging.info('Looking for burst IDs to search for')
            burst_ids_func = SentinelBurstId()
            self.burst_ids = burst_ids_func(aoi=self.shape, track=self.track)
            self.burst_id_str = ''
            for track in self.burst_ids.keys():
                for id in self.burst_ids[track]:
                    self.burst_id_str = self.burst_id_str + id.replace(' ', '_') + ','
            self.burst_id_str = self.burst_id_str[:-1]
            ASF_string += '&fullBurstID=' + self.burst_id_str
        elif self.shape:
            ASF_string += '&intersectsWith=polygon(' + self.shape_string + ')'
        ASF_string += '&output=JSON'

        ASF_string += '&start=' + start.strftime('%Y-%m-%dT%H:%M:%S') + 'UTC' + \
                      '&end=' + end.strftime('%Y-%m-%dT%H:%M:%S') + 'UTC'
        ASF_string = ASF_string[1:]

        return ASF_string

    def sentinel_search_ASF(self, username='', password=''):
        # All available sentinel-1 images are detected and printed on screen.

        if not username or not password:
            username, password = self.get_account_credentials('EarthData', username, password)
        self.reset_search_results()

        for start, end in zip(self.start_dates, self.end_dates):

            ASF_string = self.search_string_ASF(start, end)

            # Finally we do the query to get the search result.
            url = f'https://api.daac.asf.alaska.edu/services/search/param?' + ASF_string

            base64string = base64.b64encode(bytes('%s:%s' % (username, password), "utf-8")).decode()
            headers = {"Authorization": "Basic " + base64string}

            # connect to server. Hopefully this works at once
            try:
                dat = requests.get(url, headers=headers)
            except Exception as e:
                raise ConnectionError('Not possible to connect to ASF server. ' + str(e))

            if len(dat.json()) == 0:
                raise ConnectionError('No images found!')

            products = dat.json()[-1]
            self.products.extend(products)

            for product in products:
                self.sizes.append(int(float(product['sizeMB'])))
                self.polarisations.append(product['polarization'].replace('+', ''))
                self.footprints.append(shapely.wkt.loads(product['stringFootprint']))
                self.tracks.append(int(product['track']))
                self.orbit_directions.append(product['flightDirection'])
                self.ids.append(None)
                self.product_names.append(product['productName'])
                if product['sceneDate'].endswith('Z'):
                    self.dates.append(datetime.datetime.strptime(product['sceneDate'][:-1], '%Y-%m-%dT%H:%M:%S.%f'))
                else:
                    self.dates.append(datetime.datetime.strptime(product['sceneDate'], '%Y-%m-%dT%H:%M:%S.%f'))

        if len(self.dates) == 0:
            logging.info('No images found!')

    def sentinel_download_ASF(self, database_folder='', username='', password=''):
        # Download data from ASF (Generally easier and much faster to download from this platform.)

        if not username or not password:
            username, password = self.get_account_credentials('EarthData', username, password)
        if not database_folder:
            database_folder = self.get_radar_database_folder('Sentinel-1')

        if not self.products:
            raise FileNotFoundError('No images found!')

        downloader = ASFDownloader(username, password)

        # Loop over all images
        download_dat = []
        pos = 0
        for product_name, date, track, polarisation, direction, size in zip(self.product_names, self.dates, self.tracks,
                                                                            self.polarisations, self.orbit_directions,
                                                                            self.sizes):
            file_path = DownloadSentinel.find_database_folder(database_folder, product_name, date, track,
                                                              polarisation, direction)
            if not os.path.exists(file_path):
                download_dat.append([product_name, date, track, polarisation, direction, size, file_path, pos])
                pos += 1

        run_parallel_downloads(downloader, download_dat, n_processes=self.n_processes)

    @staticmethod
    def find_database_folder(database_folder, product_name, date, track, polarisation, direction):
        # Find the destination folder in the database.
        data_type = product_name[4:16]
        return DownloadSar.ensure_database_path(
            database_folder=database_folder,
            sensor_prefix='s1',
            date=date,
            track=track,
            polarisation=polarisation,
            direction=direction,
            type_name=data_type,
            product_name=product_name)
