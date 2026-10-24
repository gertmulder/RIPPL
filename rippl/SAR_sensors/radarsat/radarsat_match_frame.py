# Match RADARSAT-2 scene footprints to Netherlands coverage frames
# (track / relative orbit + frame) from radarsat2_coverage_NL.gpkg.
#
# Frame polygons come from NSO acquisition planning KMLs. Overlap is measured
# in RD New (EPSG:28992) as intersection / min(footprint, frame) area.
from typing import Optional, Dict, Any, Union
import logging
import os
import re

import geopandas as gpd
from shapely.geometry.base import BaseGeometry

DEFAULT_COVERAGE_GPKG = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'radarsat2_coverage_NL.gpkg')
COVERAGE_LAYER = 'radarsat_2'
MATCH_CRS = 'EPSG:28992'
LAYER_TRACK_RE = re.compile(
    r'radarsat_(?P<direction>asc|dsc)_track_(?P<track>\d+)', re.IGNORECASE)
FRAME_NAME_RE = re.compile(r'frame-(?P<frame>\d+)', re.IGNORECASE)


class RadarsatMatchFrame(object):
    """
    Assign track (relative orbit) and frame IDs by footprint overlap.

    Parameters
    ----------
    coverage_gpkg :
        Path to radarsat2_coverage_NL.gpkg (defaults to the copy next to this module).
    min_overlap :
        Minimum intersection / min(areas) required for a match (default 0.9).
    track :
        Optional relative-orbit filter; only frames of this track are considered.
    """

    def __init__(self, coverage_gpkg: str = '', min_overlap: float = 0.9, track: Union[str, int] = ''):
        self.coverage_gpkg = coverage_gpkg or DEFAULT_COVERAGE_GPKG
        self.min_overlap = float(min_overlap)
        self.track = int(track) if track not in ('', None) else None
        self.coverage_frames = None
        self.load_coverage_frames()

    @property
    def available(self) -> bool:
        return self.coverage_frames is not None and len(self.coverage_frames) > 0

    def load_coverage_frames(self) -> Optional[gpd.GeoDataFrame]:
        """
        Load NL track/frame polygons from the coverage geopackage.

        Only frame features are kept (not full-swath outlines). Geometries are
        stored in RD New (EPSG:28992) for stable overlap fractions.
        """
        self.coverage_frames = None
        path = self.coverage_gpkg
        if not path or not os.path.exists(path):
            logging.warning('RADARSAT-2 coverage geopackage not found at %s. '
                            'Track/frame matching disabled.', path)
            return None

        gdf = gpd.read_file(path, layer=COVERAGE_LAYER)
        if gdf.crs is None:
            gdf = gdf.set_crs(MATCH_CRS)
        elif str(gdf.crs).upper() not in (MATCH_CRS, 'EPSG:28992'):
            gdf = gdf.to_crs(MATCH_CRS)

        rows = []
        for _, row in gdf.iterrows():
            name = str(row.get('Name') or '')
            frame_match = FRAME_NAME_RE.search(name)
            if not frame_match:
                continue
            layer_match = LAYER_TRACK_RE.search(str(row.get('layer') or ''))
            if not layer_match:
                continue
            direction = ('ascending' if layer_match.group('direction').lower() == 'asc'
                         else 'descending')
            track = int(layer_match.group('track'))
            frame = int(frame_match.group('frame'))
            swath = name.split('-frame-')[0] if '-frame-' in name else name
            geom = row.geometry
            if geom is None or geom.is_empty:
                continue
            rows.append({
                'track': track,
                'frame': frame,
                'orbit_direction': direction,
                'swath_id': swath,
                'name': name,
                'geometry': geom,
            })

        if not rows:
            logging.warning('No frame polygons found in %s', path)
            return None

        self.coverage_frames = gpd.GeoDataFrame(rows, crs=MATCH_CRS)
        logging.info('Loaded %s RADARSAT-2 coverage frames from %s '
                     '(%s track/frame combinations)',
                     len(self.coverage_frames), os.path.basename(path),
                     self.coverage_frames.groupby(['track', 'frame']).ngroups)
        return self.coverage_frames

    def match_footprint(self, footprint: Optional[BaseGeometry],
                        orbit_direction: str = '',
                        track: Union[str, int, None] = None,
                        min_overlap: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """
        Match a WGS84 footprint to the best coverage frame.

        Overlap is intersection_area / min(footprint_area, frame_area) in
        EPSG:28992. STAC footprints are often slightly larger than the planned
        frames, so normalising by the smaller polygon keeps a 90% threshold
        meaningful.

        Returns
        -------
        dict or None
            Keys: track, frame, orbit_direction, swath_id, name, overlap.
        """
        if self.coverage_frames is None or footprint is None or footprint.is_empty:
            return None

        threshold = self.min_overlap if min_overlap is None else float(min_overlap)
        track_filter = self.track if track in ('', None) else int(track)

        candidates = self.coverage_frames
        direction = (orbit_direction or '').lower()
        if direction.startswith('asc'):
            candidates = candidates[candidates['orbit_direction'] == 'ascending']
        elif direction.startswith('desc'):
            candidates = candidates[candidates['orbit_direction'] == 'descending']
        if track_filter is not None:
            candidates = candidates[candidates['track'] == track_filter]
        if candidates.empty:
            return None

        footprint_proj = gpd.GeoSeries([footprint], crs='EPSG:4326').to_crs(MATCH_CRS).iloc[0]
        if footprint_proj.is_empty or footprint_proj.area <= 0:
            return None

        best = None
        best_overlap = -1.0
        for _, row in candidates.iterrows():
            frame_geom = row.geometry
            if frame_geom is None or frame_geom.is_empty or frame_geom.area <= 0:
                continue
            inter = footprint_proj.intersection(frame_geom)
            if inter.is_empty:
                continue
            overlap = float(inter.area / min(footprint_proj.area, frame_geom.area))
            if overlap > best_overlap:
                best_overlap = overlap
                best = {
                    'track': int(row['track']),
                    'frame': int(row['frame']),
                    'orbit_direction': row['orbit_direction'],
                    'swath_id': row['swath_id'],
                    'name': row['name'],
                    'overlap': overlap,
                }

        if best is None or best['overlap'] < threshold:
            return None
        return best
