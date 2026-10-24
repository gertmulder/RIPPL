"""
Backward-compatible SRTM download wrapper around OpenTopographyDownload.

Prefer rippl.external_dems.opentopography.opentopography_download.OpenTopographyDownload
for new code; that class also supports COP30, AW3D30, NASADEM, and other OT products.
"""

from rippl.external_dems.opentopography.opentopography_download import (
    OpenTopographyDownload,
    OPENTOPO_DEM_TYPES,
    is_opentopo_dem,
    get_dem_info,
    latlon_strings,
)


class SrtmDownload(OpenTopographyDownload):
    """SRTM download via OpenTopography (SRTM1 / SRTM3 aliases)."""

    def __init__(self, srtm_folder='', api_key='', username='', token='', password='',
                 srtm_type='SRTM3', quality=False, n_processes=4, filelist_folder='',
                 timeout=600):
        super().__init__(
            dem_folder=srtm_folder,
            api_key=api_key,
            dem_type=srtm_type,
            quality=quality,
            n_processes=n_processes,
            timeout=timeout,
            username=username,
            token=token,
            password=password,
        )
        # Legacy attribute names used by older callers
        self.srtm_folder = self.dem_folder
        self.srtm_type = self.dem_type

    @staticmethod
    def select_tiles(coordinates, srtm_folder, srtm_type='SRTM3', quality=False, download=True,
                     filelist=None):
        return OpenTopographyDownload.select_tiles(
            coordinates, srtm_folder, dem_type=srtm_type, quality=quality, download=download,
            filelist=filelist,
        )

    @staticmethod
    def srtm_listing(filelist_folder='', username='', password='', token='', api_key=''):
        logging_msg = (
            'srtm_listing() is deprecated: SRTM data is downloaded via the OpenTopography API for each AOI.'
        )
        import logging
        logging.info(logging_msg)
        return {'SRTM1': {}, 'SRTM3': {}, 'SRTM30': {}}


# Re-export helpers used elsewhere
__all__ = [
    'SrtmDownload',
    'OpenTopographyDownload',
    'OPENTOPO_DEM_TYPES',
    'is_opentopo_dem',
    'get_dem_info',
    'latlon_strings',
]
