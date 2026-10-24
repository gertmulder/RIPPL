# Search and download RADARSAT-2 SLC data via Satellietdataportaal.
# Catalogue: https://api.satellietdataportaal.nl/v2/stac/docs
# Download: STAC HTTPS (same zip naming as MDA FTP BRMSAR)
#   RS2_<OrderID>_<ProductID>_<DeliveryID>_XF0W#_<YYYYMMDD>_<HHMMSS>_<pol>_SLC.zip
#   Folders: /<relativeOrbit>-<frame>/  (e.g. /201-1/)
#
# STAC assets encode pol / orbit in the key, e.g. RS2_XF0W2_3702_HH_Descending.
from typing import Optional, List, Tuple
import datetime
import logging
import os
import re

import requests
from shapely.geometry import shape as shapely_shape

from rippl.SAR_sensors.sar_image_download import (
    DownloadSar, stream_response_to_file, run_parallel_downloads,
    filename_from_content_disposition)
from rippl.SAR_sensors.radarsat.radarsat_match_frame import RadarsatMatchFrame

"""
# Test RADARSAT-2 SLC search and download
import datetime
from rippl.orbit_geometry.read_write_shapes import ReadWriteShapes
from rippl.SAR_sensors.radarsat.radarsat_image_download import DownloadRadarsat

start = datetime.datetime(year=2024, month=1, day=1)
end = datetime.datetime(year=2024, month=3, day=1)

# Rough outline of the province of Utrecht (lon/lat)
Utrecht_shape = [[5.45, 52.30], [5.20, 52.20], [4.85, 52.05], [4.90, 51.90],
                 [5.15, 51.85], [5.45, 51.90], [5.65, 52.05], [5.60, 52.25],
                 [5.45, 52.30]]
study_area = ReadWriteShapes()
study_area(Utrecht_shape)

rs2 = DownloadRadarsat(start_date=start, end_date=end, shape=study_area.shape,
                       orbit_direction='DSC', track='202')  # n_processes=3 by default
rs2.radarsat_search_SDP()
rs2.summarize_search_results()
rs2.radarsat_download_FTP()
"""


class SDPDownloader(object):
    """Download one RADARSAT-2 SLC asset over HTTPS (same pattern as ASF/ESA)."""

    def __init__(self, username, password, verify_checksum=True):
        self.username = username
        self.password = password
        self.verify_checksum = verify_checksum

    def __call__(self, download_data):
        product_name, asset_key, href, checksum, file_path, pos = download_data
        if not href:
            logging.error('No download URL for %s / %s', product_name, asset_key)
            return

        url = href.replace('http://', 'https://')
        with requests.Session() as session:
            session.auth = (self.username, self.password)
            response = session.get(url, stream=True, timeout=300)
            if response.status_code == 401:
                raise PermissionError('Satellietdataportaal authentication failed (401).')
            response.raise_for_status()

            remote_name = filename_from_content_disposition(
                response.headers.get('content-disposition', ''))
            if remote_name.endswith('.zip'):
                file_path = os.path.join(os.path.dirname(file_path), remote_name)
                if os.path.exists(file_path):
                    logging.info('Already downloaded: %s', os.path.basename(file_path))
                    response.close()
                    return

            check = checksum if self.verify_checksum else ''
            stream_response_to_file(response, file_path, position=pos or 1, checksum=check)


class DownloadRadarsat(DownloadSar):
    """Search and download RADARSAT-2 SLC products via Satellietdataportaal STAC."""

    sensor_name = 'RADARSAT-2'
    STAC_BASE_URL = 'https://api.satellietdataportaal.nl/v2/stac'
    DEFAULT_COLLECTION = 'RadarSat-2_Nederland'
    ASSET_KEY_RE = re.compile(
        r'^RS2_(?P<beam>[A-Z0-9]+)_(?P<code>\d+)_(?P<pol>HH|HV|VH|VV)_'
        r'(?P<direction>Ascending|Descending)$',
        re.IGNORECASE)

    def __init__(self, start_date='', end_date='', start_dates='', end_dates='', date='', dates='',
                 time_window='', shape='', track='', polarisation='', orbit_direction='',
                 sensor_mode='', collection='', max_num_products=1000, n_processes=3,
                 verify_checksum=True, coverage_gpkg='', min_frame_overlap=0.9,
                 require_frame_match=True):
        super().__init__(
            start_date=start_date, end_date=end_date, start_dates=start_dates, end_dates=end_dates,
            date=date, dates=dates, time_window=time_window, shape=shape, track=track,
            polarisation=polarisation, orbit_direction=orbit_direction, sensor_mode=sensor_mode,
            product='SLC', max_num_products=max_num_products, n_processes=n_processes)

        self.beam_modes = []
        self.assets = []
        self.asset_keys = []
        self.checksums = []
        self.frames = []
        self.swath_ids = []
        self.frame_overlaps = []
        self.collection = collection or self.DEFAULT_COLLECTION
        self.verify_checksum = verify_checksum
        self.min_frame_overlap = float(min_frame_overlap)
        self.require_frame_match = require_frame_match
        self.frame_matcher = RadarsatMatchFrame(
            coverage_gpkg=coverage_gpkg, min_overlap=self.min_frame_overlap, track=self.track)

    def _session(self, username='', password=''):
        username, password = self.get_account_credentials(
            'Satellietdataportaal', username, password)
        session = requests.Session()
        session.auth = (username, password)
        session.headers.update({'Accept': 'application/json, application/geo+json'})
        return session

    def match_footprint_to_frame(self, footprint, orbit_direction: str = '') -> Optional[dict]:
        return self.frame_matcher.match_footprint(
            footprint, orbit_direction=orbit_direction, track=self.track,
            min_overlap=self.min_frame_overlap)

    @staticmethod
    def _datetime_interval(start: datetime.datetime, end: datetime.datetime) -> str:
        return start.strftime('%Y-%m-%dT%H:%M:%SZ') + '/' + end.strftime('%Y-%m-%dT%H:%M:%SZ')

    def _search_page(self, session: requests.Session, body: dict) -> dict:
        r = session.post(f'{self.STAC_BASE_URL}/search', json=body, timeout=120)
        if r.status_code == 401:
            raise PermissionError('Satellietdataportaal authentication failed (401).')
        if r.status_code >= 400:
            logging.error('STAC search failed (%s): %s', r.status_code, r.text)
            r.raise_for_status()
        return r.json()

    @staticmethod
    def _parse_datetime(value: Optional[str]) -> Optional[datetime.datetime]:
        if not value:
            return None
        value = value.strip().replace('Z', '').replace('T', ' ')
        if '.' in value:
            date_part, frac = value.split('.', 1)
            value = f'{date_part}.{re.sub(r"[^0-9]", "", frac)[:6].ljust(6, "0")}'
        for fmt in ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
            try:
                return datetime.datetime.strptime(value, fmt)
            except ValueError:
                continue
        logging.warning('Could not parse datetime: %s', value)
        return None

    @classmethod
    def _parse_slc_asset(cls, asset_key: str, asset: dict) -> Optional[dict]:
        match = cls.ASSET_KEY_RE.match(asset_key or '')
        if not match:
            return None
        direction = match.group('direction').lower()
        return {
            'asset_key': asset_key,
            'beam_mode': match.group('beam').upper(),
            'polarisation': match.group('pol').upper(),
            'orbit_direction': 'ascending' if direction.startswith('asc') else 'descending',
            'href': (asset or {}).get('href') or '',
            'checksum': (asset or {}).get('checksum') or '',
        }

    def _select_slc_asset(self, assets: dict) -> Optional[Tuple[str, dict, dict]]:
        for key, asset in (assets or {}).items():
            meta = self._parse_slc_asset(key, asset or {})
            if meta is None:
                continue
            if self.orbit_direction and meta['orbit_direction'] != self.orbit_direction:
                continue
            if self.polarisation:
                wanted = self.polarisation.replace('+', '').replace(' ', '').upper()
                pol = meta['polarisation']
                if wanted not in pol and pol not in wanted:
                    continue
            if self.sensor_mode and self.sensor_mode.upper() not in meta['beam_mode']:
                continue
            return key, asset or {}, meta
        return None

    def _append_feature(self, feature: dict, asset_key: str, asset: dict, meta: dict) -> bool:
        product_id = feature.get('id') or asset_key
        footprint = shapely_shape(feature['geometry']) if feature.get('geometry') else None
        props = feature.get('properties') or {}
        date = self._parse_datetime(props.get('datetime') or props.get('start_datetime'))
        direction = meta['orbit_direction']

        match = self.match_footprint_to_frame(footprint, direction)
        if match is None:
            if self.require_frame_match and self.frame_matcher.available:
                return False
            track = int(self.track) if self.track else None
            frame, swath_id, overlap = None, '', None
        else:
            track, frame = match['track'], match['frame']
            swath_id, overlap = match['swath_id'], match['overlap']
            if self.track and int(self.track) != int(track):
                return False

        self.products.append(feature)
        self.product_names.append(product_id)
        self.ids.append(feature.get('id'))
        self.dates.append(date)
        self.tracks.append(track)
        self.frames.append(frame)
        self.swath_ids.append(swath_id)
        self.frame_overlaps.append(overlap)
        self.orbit_directions.append(direction)
        self.polarisations.append(meta['polarisation'])
        self.beam_modes.append(meta['beam_mode'])
        self.sizes.append(None)
        self.checksums.append(meta['checksum'])
        self.footprints.append(footprint)
        self.assets.append({asset_key: asset})
        self.asset_keys.append(asset_key)
        return True

    def reset_search_results(self):
        super().reset_search_results()
        self.checksums = []
        self.frames = []
        self.swath_ids = []
        self.frame_overlaps = []

    def _sort_search_results(self):
        if not self.products:
            return
        order = sorted(
            range(len(self.products)),
            key=lambda i: (
                self.tracks[i] if self.tracks[i] is not None else 10 ** 9,
                self.frames[i] if self.frames[i] is not None else 10 ** 9,
                self.dates[i] or datetime.datetime.min))
        for name in ('products', 'product_names', 'ids', 'dates', 'tracks', 'frames',
                     'swath_ids', 'frame_overlaps', 'orbit_directions', 'polarisations',
                     'beam_modes', 'sizes', 'checksums', 'footprints', 'assets', 'asset_keys'):
            values = getattr(self, name)
            setattr(self, name, [values[i] for i in order])

    def _search_interval(self, session, start, end, intersects, limit) -> List[dict]:
        body = {
            'collections': [self.collection],
            'datetime': self._datetime_interval(start, end),
            'limit': limit,
        }
        if intersects is not None:
            body['intersects'] = intersects

        data = self._search_page(session, body)
        features = data.get('features') or []
        logging.info('STAC search returned %s items (%s → %s)',
                     len(features), start.date().isoformat(), end.date().isoformat())

        all_features = list(features)
        next_body = None
        for link in data.get('links') or []:
            if link.get('rel') == 'next' and link.get('body'):
                next_body = link['body']
                break
        while next_body is not None:
            data = self._search_page(session, next_body)
            page = data.get('features') or []
            all_features.extend(page)
            next_body = None
            for link in data.get('links') or []:
                if link.get('rel') == 'next' and link.get('body'):
                    next_body = link['body']
                    break
            if not page:
                break

        if (next_body is None and limit >= 100 and len(features) >= limit
                and (end - start) > datetime.timedelta(days=1)):
            mid = start + (end - start) / 2
            left = self._search_interval(session, start, mid, intersects, limit)
            right = self._search_interval(session, mid, end, intersects, limit)
            seen, merged = set(), []
            for feature in left + right:
                fid = feature.get('id')
                if fid not in seen:
                    seen.add(fid)
                    merged.append(feature)
            return merged
        return all_features

    def radarsat_search_SDP(self, username='', password='', collection: Optional[str] = None):
        """Search RADARSAT-2 SLC products in the Satellietdataportaal STAC API."""
        if collection:
            self.collection = collection
        session = self._session(username, password)
        self.reset_search_results()
        intersects = self.shape_to_geojson() if self.shape is not None else None

        skipped = 0
        for start, end in zip(self.start_dates, self.end_dates):
            for feature in self._search_interval(session, start, end, intersects, 1000):
                selected = self._select_slc_asset(feature.get('assets') or {})
                if selected is None:
                    continue
                if not self._append_feature(feature, *selected):
                    skipped += 1
                    continue
                if len(self.products) >= int(self.max_num_products):
                    break
            if len(self.products) >= int(self.max_num_products):
                break

        self._sort_search_results()
        if skipped:
            logging.info('Skipped %s products without a coverage frame match '
                         '(min overlap %.0f%%).', skipped, self.min_frame_overlap * 100)
        if not self.dates:
            logging.info('No RADARSAT-2 SLC images found!')
        else:
            combos = sorted({(t, f) for t, f in zip(self.tracks, self.frames)
                             if t is not None and f is not None})
            logging.info('Selected %s SLC products (%s track/frame combinations).',
                         len(self.products), len(combos))
            for track, frame in combos:
                n = sum(1 for t, f in zip(self.tracks, self.frames) if t == track and f == frame)
                logging.info('  track %s frame %s: %s product(s)', track, frame, n)
        return self.products

    def radarsat_download_SDP(self, database_folder='', username='', password=''):
        return self.radarsat_download_FTP(database_folder, username, password)

    def radarsat_download_FTP(self, database_folder='', username='', password=''):
        """Download selected SLC products via STAC HTTPS (MDA FTP package names)."""
        if not username or not password:
            username, password = self.get_account_credentials(
                'Satellietdataportaal', username, password)
        if not database_folder:
            database_folder = self.get_radar_database_folder('RADARSAT-2')
        if not self.products or not self.asset_keys:
            raise FileNotFoundError('No images found! Run radarsat_search_SDP() first.')

        downloader = SDPDownloader(username, password, self.verify_checksum)
        download_dat = []
        pos = 0
        for product_name, asset_key, date, track, polarisation, direction, checksum, assets in zip(
                self.product_names, self.asset_keys, self.dates, self.tracks,
                self.polarisations, self.orbit_directions, self.checksums, self.assets):
            href = (assets.get(asset_key) or {}).get('href') or ''
            local_name = product_name if str(product_name).endswith('.zip') else f'{product_name}.zip'
            file_path = self.find_database_folder(
                database_folder, local_name, date, track, polarisation, direction)

            already = os.path.exists(file_path)
            if not already and date is not None:
                stamp = date.strftime('%Y%m%d_%H%M%S')
                for root, _dirs, files in os.walk(database_folder):
                    if any(stamp in n and n.endswith('.zip') and not n.endswith('.part')
                           for n in files):
                        already = True
                        logging.info('Already downloaded (matched %s)', stamp)
                        break
            if already:
                continue

            download_dat.append([product_name, asset_key, href, checksum, file_path, pos])
            pos += 1

        run_parallel_downloads(downloader, download_dat, n_processes=self.n_processes)

    def _format_product_label(self, index: int) -> str:
        track = self.tracks[index] if index < len(self.tracks) else None
        frame = self.frames[index] if index < len(self.frames) else None
        tf = (f't{int(track):03d}-f{int(frame)}' if track is not None and frame is not None
              else 't?-f?')
        pol = self.polarisations[index] if index < len(self.polarisations) else '?'
        key = self.asset_keys[index] if index < len(self.asset_keys) else ''
        return f'{self.product_names[index]} [{tf}, {key}, pol={pol or "?"}]'

    @staticmethod
    def find_database_folder(database_folder, product_name, date, track, polarisation,
                             direction):
        # All frames of a track share one folder:
        #   radarsat2/rs2_<asc|dsc>_tXXX/slc_<pol>/<YYYYMMDD>/<product>.zip
        return DownloadSar.ensure_database_path(
            database_folder=database_folder, sensor_prefix='rs2', date=date, track=track,
            polarisation=polarisation, direction=direction, type_name='slc',
            product_name=product_name)
