"""Build a DEM for an operation area from published elevation tiles.

Kept separate from the main algorithm on purpose.  Harvest Accessibility is run
in the forest, where there is often no network; fetching elevation at calculation
time would fail exactly where the plugin is used.  Run this once in the office,
save the GeoTIFF, and feed that file to the main algorithm offline.
"""

import math
import urllib.error
import urllib.request

from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterEnum,
    QgsProcessingParameterNumber,
    QgsProcessingParameterDefinition,
    QgsProcessingParameterRasterDestination,
    QgsProcessingException,
    QgsProcessingContext,
    QgsProcessingFeedback,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsProject,
    QgsRectangle,
)
from qgis.PyQt.QtCore import QCoreApplication
from qgis import processing

TILE_SIZE = 256
MERCATOR_ORIGIN = 20037508.342789244

# Sources are (label, url template, max zoom, attribution).  The tile path order
# differs between services -- the GSJ set puts y before x -- so the template
# carries it rather than the fetching code.
# Sources are (label, url template, max zoom, attribution).  The tile path order
# differs between services -- the GSJ set puts y before x -- so the template
# carries it rather than the fetching code.  Zoom levels are the ones that
# actually return tiles: the published table is vaguer than the service.
SOURCES = [
    (
        "VIRTUAL SHIZUOKA (~0.5 m, Shizuoka only)",
        "https://tiles.gsj.jp/tiles/elev/shizuoka/{z}/{y}/{x}.png",
        18,
        "静岡県 VIRTUAL SHIZUOKA / 産業技術総合研究所 シームレス標高タイル (CC BY 4.0)",
    ),
    (
        "GSI DEM5A (~4 m, where surveyed)",
        "https://tiles.gsj.jp/tiles/elev/gsidem5a/{z}/{y}/{x}.png",
        15,
        "国土地理院 基盤地図情報数値標高モデル DEM5A / 産業技術総合研究所 シームレス標高タイル",
    ),
    (
        "GSI DEM10B (~8 m, nationwide)",
        "https://tiles.gsj.jp/tiles/elev/gsidem/{z}/{y}/{x}.png",
        14,
        "国土地理院 基盤地図情報数値標高モデル DEM10B / 産業技術総合研究所 シームレス標高タイル",
    ),
]


def _decode_gsj(arr):
    """Decode the GSJ elevation PNG encoding into metres.

    r' = r < 128 ? r : r - 256;  h = (65536 r' + 256 g + b) * 0.01
    Fully transparent pixels carry no elevation.
    """
    import numpy as np
    a = arr.astype("int32")
    r, g, b = a[0], a[1], a[2]
    rp = np.where(r < 128, r, r - 256)
    h = (65536.0 * rp + 256.0 * g + b) * 0.01
    if a.shape[0] > 3:
        h = np.where(a[3] == 0, float("nan"), h)
    return h


class FetchDemAlg(QgsProcessingAlgorithm):
    EXTENT_SOURCE = "EXTENT_SOURCE"
    SOURCE = "SOURCE"
    ZOOM = "ZOOM"
    MARGIN = "MARGIN"
    RESOLUTION = "RESOLUTION"
    OUTPUT = "OUTPUT"

    def tr(self, string):
        return QCoreApplication.translate("FetchDemAlg", string)

    def name(self):
        return "fetch_dem"

    def displayName(self):
        return self.tr("Fetch DEM from elevation tiles")

    def group(self):
        return self.tr("Harvest Accessibility")

    def groupId(self):
        return "harvestaccessibility"

    def createInstance(self):
        return FetchDemAlg()

    def shortHelpString(self):
        return self.tr(
            "Downloads published elevation tiles covering an operation area and "
            "writes a DEM in the layer's own CRS, ready to feed to Harvest "
            "Accessibility.\n\n"
            "Run this in the office: the main algorithm is used in the field, "
            "where there may be no network.\n\n"
            "The DEM is reprojected out of web mercator before being written. "
            "That is not cosmetic -- slope computed on mercator tiles comes out "
            "roughly 20% too gentle at these latitudes.\n\n"
            "Sources: 静岡県 VIRTUAL SHIZUOKA / 産業技術総合研究所 シームレス標高タイル "
            "(CC BY 4.0). Credit the source when you publish results."
        )

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.EXTENT_SOURCE,
            self.tr("Operation area (extent to cover)"),
            [QgsProcessing.TypeVectorAnyGeometry]
        ))
        self.addParameter(QgsProcessingParameterEnum(
            self.SOURCE,
            self.tr("Tile source"),
            options=[s[0] for s in SOURCES],
            defaultValue=0
        ))
        self.addParameter(QgsProcessingParameterNumber(
            self.RESOLUTION,
            self.tr("Output resolution (m; 0 = the tile's own resolution)"),
            QgsProcessingParameterNumber.Double,
            defaultValue=0.0, minValue=0.0
        ))
        margin = QgsProcessingParameterNumber(
            self.MARGIN,
            self.tr("Margin around the area (m)"),
            QgsProcessingParameterNumber.Double,
            defaultValue=60.0, minValue=0.0
        )
        zoom = QgsProcessingParameterNumber(
            self.ZOOM,
            self.tr("Zoom level (0 = use the source maximum)"),
            QgsProcessingParameterNumber.Integer,
            defaultValue=0, minValue=0, maxValue=20
        )
        for param in (margin, zoom):
            param.setFlags(param.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
            self.addParameter(param)

        self.addParameter(QgsProcessingParameterRasterDestination(
            self.OUTPUT, self.tr("DEM")
        ))

    def processAlgorithm(self, parameters, context: QgsProcessingContext,
                         feedback: QgsProcessingFeedback):
        try:
            import numpy as np
            from osgeo import gdal
        except ImportError as exc:
            raise QgsProcessingException(self.tr(
                "numpy and GDAL are required: {}"
            ).format(exc))

        source = self.parameterAsSource(parameters, self.EXTENT_SOURCE, context)
        if source is None:
            raise QgsProcessingException(self.tr("Invalid extent layer."))
        idx = self.parameterAsEnum(parameters, self.SOURCE, context)
        label, url_tpl, max_zoom, attribution = SOURCES[idx]
        zoom = self.parameterAsInt(parameters, self.ZOOM, context) or max_zoom
        margin = self.parameterAsDouble(parameters, self.MARGIN, context)
        res = self.parameterAsDouble(parameters, self.RESOLUTION, context)
        out_path = self.parameterAsOutputLayer(parameters, self.OUTPUT, context)

        src_crs = source.sourceCrs()
        if src_crs.isGeographic():
            raise QgsProcessingException(self.tr(
                "The extent layer is in a geographic CRS. Use a projected CRS in "
                "metres so that the margin and output resolution mean metres."
            ))

        ext = source.sourceExtent()
        ext = QgsRectangle(ext.xMinimum() - margin, ext.yMinimum() - margin,
                           ext.xMaximum() + margin, ext.yMaximum() + margin)

        wgs = QgsCoordinateReferenceSystem("EPSG:4326")
        to_wgs = QgsCoordinateTransform(src_crs, wgs, QgsProject.instance())
        ll = to_wgs.transformBoundingBox(ext)

        x0, y0 = self._tile_of(ll.xMinimum(), ll.yMaximum(), zoom)
        x1, y1 = self._tile_of(ll.xMaximum(), ll.yMinimum(), zoom)
        n_tiles = (x1 - x0 + 1) * (y1 - y0 + 1)
        feedback.pushInfo(self.tr("{} at zoom {}: {} tiles").format(label, zoom, n_tiles))
        if n_tiles > 400:
            raise QgsProcessingException(self.tr(
                "That area needs {} tiles. Reduce the zoom level (advanced) or "
                "split the area."
            ).format(n_tiles))

        width = (x1 - x0 + 1) * TILE_SIZE
        height = (y1 - y0 + 1) * TILE_SIZE
        mosaic = np.full((height, width), np.nan, dtype="float32")

        fetched = 0
        for ty in range(y0, y1 + 1):
            for tx in range(x0, x1 + 1):
                if feedback.isCanceled():
                    raise QgsProcessingException(self.tr("Processing cancelled by user."))
                url = url_tpl.format(z=zoom, x=tx, y=ty)
                try:
                    req = urllib.request.Request(
                        url, headers={"User-Agent": "QGIS harvest_accessibility"})
                    data = urllib.request.urlopen(req, timeout=30).read()
                except urllib.error.HTTPError as exc:
                    if exc.code == 404:
                        continue  # no tile here: sea, or outside the coverage
                    raise QgsProcessingException(self.tr(
                        "Tile request failed ({}): {}").format(exc.code, url))
                except Exception as exc:
                    raise QgsProcessingException(self.tr(
                        "Could not reach the tile service. This algorithm needs a "
                        "network connection; run it before going to the site. ({})"
                    ).format(exc))
                vsi = "/vsimem/harvest_dem_tile.png"
                gdal.FileFromMemBuffer(vsi, data)
                tile = gdal.Open(vsi)
                arr = tile.ReadAsArray()
                tile = None
                gdal.Unlink(vsi)
                if arr is None or arr.ndim != 3:
                    continue
                oy = (ty - y0) * TILE_SIZE
                ox = (tx - x0) * TILE_SIZE
                mosaic[oy:oy + TILE_SIZE, ox:ox + TILE_SIZE] = _decode_gsj(arr)
                fetched += 1
                feedback.setProgress(100.0 * fetched / n_tiles)

        if fetched == 0:
            raise QgsProcessingException(self.tr(
                "No tiles were returned for this area. The chosen source may not "
                "cover it -- try the nationwide source."
            ))
        feedback.pushInfo(self.tr("Fetched {} of {} tiles.").format(fetched, n_tiles))

        nodata = -9999.0
        mosaic = np.where(np.isnan(mosaic), nodata, mosaic)

        mem = gdal.GetDriverByName("MEM").Create("", width, height, 1, gdal.GDT_Float32)
        band = mem.GetRasterBand(1)
        band.SetNoDataValue(nodata)
        band.WriteArray(mosaic)
        pixel = 2 * MERCATOR_ORIGIN / (2 ** zoom) / TILE_SIZE
        mem.SetGeoTransform((
            -MERCATOR_ORIGIN + x0 * TILE_SIZE * pixel, pixel, 0,
            MERCATOR_ORIGIN - y0 * TILE_SIZE * pixel, 0, -pixel
        ))
        mem.SetProjection(QgsCoordinateReferenceSystem("EPSG:3857").toWkt())

        # Leave web mercator here, not later: horizontal distances there are
        # stretched by 1/cos(latitude), so slope taken from mercator pixels is
        # about 20% too gentle at 35N -- and the felling model branches on a
        # slope threshold.
        if res <= 0:
            # Resampling an 8 m source onto a 0.5 m grid invents detail that is
            # not there, so default to what the tiles actually carry.
            lat_mid = math.radians((ll.yMinimum() + ll.yMaximum()) / 2.0)
            res = pixel * math.cos(lat_mid)
            feedback.pushInfo(self.tr(
                "Output resolution not given; using the tile resolution "
                "({:.2f} m)."
            ).format(res))
        feedback.pushInfo(self.tr("Reprojecting to {} at {:.2f} m...").format(
            src_crs.authid(), res))
        gdal.Warp(out_path, mem, dstSRS=src_crs.toWkt(), xRes=res, yRes=res,
                  resampleAlg="bilinear", dstNodata=nodata)
        mem = None

        feedback.pushInfo(self.tr("Source: {}").format(attribution))
        return {self.OUTPUT: out_path}

    @staticmethod
    def _tile_of(lon, lat, zoom):
        n = 2 ** zoom
        x = int(math.floor((lon + 180.0) / 360.0 * n))
        lat_r = math.radians(max(min(lat, 85.05112878), -85.05112878))
        y = int(math.floor((1.0 - math.asinh(math.tan(lat_r)) / math.pi) / 2.0 * n))
        return max(0, min(n - 1, x)), max(0, min(n - 1, y))
