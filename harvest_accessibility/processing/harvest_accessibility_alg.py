import math
import os
import tempfile

from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterRasterLayer,
    QgsProcessingParameterEnum,
    QgsProcessingParameterField,
    QgsProcessingParameterNumber,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterDefinition,
    QgsProcessingException,
    QgsProcessingOutputHtml,
    QgsProcessingContext,
    QgsProcessingFeedback,
    QgsFeature,
    QgsFields,
    QgsField,
    QgsUnitTypes,
    QgsWkbTypes,
    QgsGeometry,
    QgsPointXY,
    QgsVectorLayer
)
from qgis.PyQt.QtCore import QCoreApplication, QVariant
from qgis.core import QgsRasterLayer, QgsCoordinateReferenceSystem, \
    QgsCoordinateTransform, QgsProject, QgsRectangle
from qgis import processing

from . import tiles


def _azimuth(dx, dy):
    """Azimuth in degrees, clockwise from north."""
    return math.degrees(math.atan2(dx, dy)) % 360.0


def _angle_diff(a, b):
    """Smallest signed difference a - b, in (-180, 180]."""
    return (a - b + 180.0) % 360.0 - 180.0


def _slope_along(slope_deg, phi_deg):
    """Ground slope in a direction phi degrees away from steepest descent.

    tan(theta_dir) = tan(theta_max) * cos(phi):  straight downslope keeps the
    full slope, across-slope (phi = 90) is level.
    """
    return math.degrees(math.atan(
        math.tan(math.radians(slope_deg)) * math.cos(math.radians(phi_deg))
    ))


def _candidate_directions(target_az, aspect_deg, slope_deg, sector, flat_slope):
    """Felling directions to try, best first.

    On gentle ground any direction is allowed, so aiming at the road is optimal.
    On steeper ground the direction is clamped into the sector centred on
    downslope.  The clamp is exact for a straight road; for a curved or branching
    network it is an approximation -- the direction that truly minimises the
    distance from the stem's near end to the road need not be the clamped
    bearing.  Both sector edges are offered as fallbacks so that a barrier can
    reject the first choice without losing the point.
    """
    if slope_deg <= flat_slope or abs(_angle_diff(target_az, aspect_deg)) <= sector:
        cands = [target_az]
    else:
        cands = []
    edges = [(aspect_deg + sector) % 360.0, (aspect_deg - sector) % 360.0]
    edges.sort(key=lambda az: abs(_angle_diff(target_az, az)))
    cands.extend(edges)
    cands.append(aspect_deg)  # straight downslope: always allowed by the sector
    seen, out = set(), []
    for az in cands:
        key = round(az, 3)
        if key not in seen:
            seen.add(key)
            out.append(az)
    return out


class HarvestAccessibilityAlg(QgsProcessingAlgorithm):
    POLY = "POLY"
    ROADS = "ROADS"
    LANDING = "LANDING"
    GRID = "GRID"
    SNAP_TOL = "SNAP_TOL"
    HTML_OUT = "HTML_OUT"
    DEBUG = "DEBUG"
    SPLIT_ROADS = "SPLIT_ROADS"
    # Felling model (all optional; absent inputs keep the plain geometric behaviour)
    DEM = "DEM"
    AUTO_DEM = "AUTO_DEM"
    TREES = "TREES"
    HEIGHT_FIELD = "HEIGHT_FIELD"
    TREE_HEIGHT = "TREE_HEIGHT"
    BARRIERS = "BARRIERS"
    FELL_SECTOR = "FELL_SECTOR"
    FLAT_SLOPE = "FLAT_SLOPE"
    GRAPPLE_REACH = "GRAPPLE_REACH"
    ASPECT_SMOOTH = "ASPECT_SMOOTH"
    ROAD_CANDIDATES = "ROAD_CANDIDATES"

    def tr(self, string):
        return QCoreApplication.translate("HarvestAccessibilityAlg", string)

    def name(self):
        return "harvest_accessibility"

    def displayName(self):
        return self.tr("Harvest Accessibility")

    def group(self):
        return self.tr("Harvest Accessibility")

    def groupId(self):
        return "harvest_accessibility"

    def shortHelpString(self):
        return self.tr(
            "Inputs: operation polygon, forest road lines (also used as network), "
            "landing points (multiple OK).\n"
            "1) Create grid points within polygon (p1)\n"
            "2) Shortest straight line to road -> d1, endpoint on road -> p2\n"
            "3) Shortest path on road network from p2 to the nearest landing -> d2 (NULL if unreachable)\n"
            "4) HTML result report.\n"
            "NOTE: Use a projected CRS in metres.\n\n"
            "Advanced: enable debug mode to load intermediate layers into the project."
        )

    def createInstance(self):
        return HarvestAccessibilityAlg()

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.POLY,
            self.tr("Operation area polygon"),
            [QgsProcessing.TypeVectorPolygon]
        ))
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.ROADS,
            self.tr("Forest road lines (also network)"),
            [QgsProcessing.TypeVectorLine]
        ))
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.LANDING,
            self.tr("Landing points (multiple OK)"),
            [QgsProcessing.TypeVectorPoint]
        ))
        # --- Felling model inputs (optional) -----------------------------
        # The model switches on by the presence of its inputs: no DEM means the
        # plain geometric d1, no barrier layer means no barrier filtering.
        dem = QgsProcessingParameterRasterLayer(
            self.DEM,
            self.tr("DEM (enables the felling model)"),
            optional=True
        )
        self.addParameter(dem)

        # Fetching is offered right here rather than only as a separate
        # algorithm: the normal case is office work, and making people run one
        # algorithm, save a file and point a second one at it buys nothing.
        self.addParameter(QgsProcessingParameterEnum(
            self.AUTO_DEM,
            self.tr("...or download a DEM for this area"),
            options=[self.tr("Do not download")] + [s[0] for s in tiles.SOURCES],
            defaultValue=0
        ))

        trees = QgsProcessingParameterFeatureSource(
            self.TREES,
            self.tr("Individual tree points (used as sample points instead of the grid)"),
            [QgsProcessing.TypeVectorPoint],
            optional=True
        )
        self.addParameter(trees)

        height_field = QgsProcessingParameterField(
            self.HEIGHT_FIELD,
            self.tr("Tree height field"),
            parentLayerParameterName=self.TREES,
            type=QgsProcessingParameterField.Numeric,
            optional=True
        )
        self.addParameter(height_field)

        barriers = QgsProcessingParameterFeatureSource(
            self.BARRIERS,
            self.tr("Barriers (rivers etc.; lines or polygons)"),
            [QgsProcessing.TypeVectorLine, QgsProcessing.TypeVectorPolygon],
            optional=True
        )
        self.addParameter(barriers)

        self.addParameter(QgsProcessingParameterNumber(
            self.TREE_HEIGHT,
            self.tr("Tree height (m), used when no height field is given"),
            QgsProcessingParameterNumber.Double,
            defaultValue=20.0,
            minValue=0.0
        ))

        self.addParameter(QgsProcessingParameterNumber(
            self.GRID,
            self.tr("Grid spacing (m)"),
            QgsProcessingParameterNumber.Double,
            defaultValue=4.0,
            minValue=0.1
        ))
        self.addParameter(QgsProcessingParameterNumber(
            self.SNAP_TOL,
            self.tr("Network snapping tolerance (m)"),
            QgsProcessingParameterNumber.Double,
            defaultValue=5.0,
            minValue=0.0
        ))
        split_param = QgsProcessingParameterBoolean(
            self.SPLIT_ROADS,
            self.tr("Split roads at intersections before routing"),
            defaultValue=True
        )
        # Default on, and turning it off only breaks turning at junctions.
        split_param.setFlags(
            split_param.flags() | QgsProcessingParameterDefinition.FlagAdvanced
        )
        self.addParameter(split_param)

        self.addOutput(QgsProcessingOutputHtml(self.HTML_OUT, self.tr("Result report")))

        debug_param = QgsProcessingParameterBoolean(
            self.DEBUG,
            self.tr("Debug mode (add intermediate layers to project)"),
            defaultValue=False
        )
        debug_param.setFlags(
            debug_param.flags() | QgsProcessingParameterDefinition.FlagAdvanced
        )
        self.addParameter(debug_param)

        # Calibration values for the felling model.  These are method constants
        # rather than per-site inputs, so they live behind the advanced flag --
        # but they must stay adjustable: the result is sensitive to the sector
        # half-angle in particular.
        for param in (
            QgsProcessingParameterNumber(
                self.FELL_SECTOR,
                self.tr("Felling sector half-angle from downslope (deg)"),
                QgsProcessingParameterNumber.Double,
                defaultValue=105.0, minValue=0.0, maxValue=180.0
            ),
            QgsProcessingParameterNumber(
                self.FLAT_SLOPE,
                self.tr("Slope at or below which any direction is allowed (deg)"),
                QgsProcessingParameterNumber.Double,
                defaultValue=10.0, minValue=0.0, maxValue=90.0
            ),
            QgsProcessingParameterNumber(
                self.GRAPPLE_REACH,
                self.tr("Direct grapple reach from the road (m)"),
                QgsProcessingParameterNumber.Double,
                defaultValue=3.0, minValue=0.0
            ),
            QgsProcessingParameterNumber(
                self.ASPECT_SMOOTH,
                self.tr("Slope/aspect smoothing window (m)"),
                QgsProcessingParameterNumber.Double,
                defaultValue=5.0, minValue=0.0
            ),
            QgsProcessingParameterNumber(
                self.ROAD_CANDIDATES,
                self.tr("Number of candidate roads per sample point"),
                QgsProcessingParameterNumber.Integer,
                defaultValue=10, minValue=1
            ),
        ):
            param.setFlags(param.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
            self.addParameter(param)

    def _download_dem(self, poly, crs, source_idx, feedback, margin=60.0):
        """Fetch a DEM covering the operation area and hand back a raster layer.

        Repeated runs over the same block reuse the file already fetched, so
        tuning the model does not re-download the same tiles each time.
        """
        ext = poly.sourceExtent()
        ext = QgsRectangle(ext.xMinimum() - margin, ext.yMinimum() - margin,
                           ext.xMaximum() + margin, ext.yMaximum() + margin)
        wgs = QgsCoordinateReferenceSystem("EPSG:4326")
        ll = QgsCoordinateTransform(crs, wgs, QgsProject.instance()) \
            .transformBoundingBox(ext)
        try:
            path = tiles.build_dem(
                (ll.xMinimum(), ll.yMinimum(), ll.xMaximum(), ll.yMaximum()),
                source_idx, 0, crs.toWkt(), crs.authid(),
                log=feedback.pushInfo,
                progress=feedback.setProgress,
                cancelled=feedback.isCanceled,
            )
        except tiles.TileError as exc:
            raise QgsProcessingException(str(exc))
        layer = QgsRasterLayer(path, "downloaded_dem")
        if not layer.isValid():
            raise QgsProcessingException(self.tr(
                "The downloaded DEM could not be opened: {}").format(path))
        return layer

    def _apply_felling_model(self, p1, p2_geom_only, parameters, context, feedback,
                             _reg, dem_layer, barriers_source, height_field,
                             tree_height, fell_sector, flat_slope, grapple_reach,
                             aspect_smooth, crs):
        """Recompute d1 and p2 from the felled stem rather than the standing tree.

        Returns (p2_layer, stats).  The returned layer keeps the contract the
        routing step depends on: one feature per sample point, carrying tree_id
        and d1.  Points are never dropped -- a point with d1 = 0 still needs a
        route to a landing.
        """
        # Slope and aspect are evaluated on a DEM resampled to the smoothing
        # window.  Reprojecting here is not optional: elevation tiles arrive in
        # web mercator, where horizontal distances are stretched by 1/cos(lat)
        # (~22% at 35N) and every slope would come out that much too gentle --
        # and the model branches on a 15 degree threshold.
        feedback.pushInfo(self.tr("2b) Preparing slope and aspect for the felling model..."))
        dem_r = processing.run(
            "gdal:warpreproject",
            {
                "INPUT": dem_layer,
                "TARGET_CRS": crs,
                "RESAMPLING": 1,  # bilinear
                "TARGET_RESOLUTION": aspect_smooth if aspect_smooth > 0 else None,
                "OUTPUT": "TEMPORARY_OUTPUT"
            },
            context=context, feedback=feedback
        )["OUTPUT"]

        slope_r = processing.run(
            "native:slope", {"INPUT": dem_r, "Z_FACTOR": 1, "OUTPUT": "TEMPORARY_OUTPUT"},
            context=context, feedback=feedback
        )["OUTPUT"]
        aspect_r = processing.run(
            "native:aspect", {"INPUT": dem_r, "Z_FACTOR": 1, "OUTPUT": "TEMPORARY_OUTPUT"},
            context=context, feedback=feedback
        )["OUTPUT"]

        sampled = _reg(processing.run(
            "native:rastersampling",
            {"INPUT": p1.id(), "RASTERCOPY": slope_r, "COLUMN_PREFIX": "slp_",
             "OUTPUT": "memory:"},
            context=context, feedback=feedback
        )["OUTPUT"])
        sampled = _reg(processing.run(
            "native:rastersampling",
            {"INPUT": sampled.id(), "RASTERCOPY": aspect_r, "COLUMN_PREFIX": "asp_",
             "OUTPUT": "memory:"},
            context=context, feedback=feedback
        )["OUTPUT"])

        roads_layer = self.parameterAsLayer(parameters, self.ROADS, context)
        roads_geom = QgsGeometry.unaryUnion(
            [f.geometry() for f in roads_layer.getFeatures() if not f.geometry().isEmpty()]
        )
        barriers_geom = None
        if barriers_source is not None:
            parts = [f.geometry() for f in barriers_source.getFeatures()
                     if not f.geometry().isEmpty()]
            if parts:
                barriers_geom = QgsGeometry.unaryUnion(parts)

        out = QgsVectorLayer("Point?crs=" + crs.authid(), "p2_model", "memory")
        fields = QgsFields()
        fields.append(QgsField("tree_id", QVariant.Int))
        fields.append(QgsField("d1", QVariant.Double))
        fields.append(QgsField("d1_geom", QVariant.Double))   # geometric d1, for comparison
        fields.append(QgsField("fell_az", QVariant.Double))
        fields.append(QgsField("stem_len", QVariant.Double))  # horizontal reach
        fields.append(QgsField("on_road", QVariant.Int))      # stem reaches the road
        fields.append(QgsField("blocked", QVariant.Int))      # no direction survived
        out.dataProvider().addAttributes(fields.toList())
        out.updateFields()

        n_on_road, n_blocked, n_grapple = 0, 0, 0
        feats = []
        for f in sampled.getFeatures():
            if feedback.isCanceled():
                raise QgsProcessingException(self.tr("Processing cancelled by user."))
            base = f.geometry().asPoint()
            base_geom = QgsGeometry.fromPointXY(base)
            d_base = base_geom.distance(roads_geom)

            # Within grapple reach nothing is winched at all.
            if d_base <= grapple_reach:
                n_grapple += 1
                foot = roads_geom.nearestPoint(base_geom)
                feats.append(self._p2_feature(out, f, 0.0, d_base, None, 0.0,
                                              foot, on_road=0, blocked=0))
                continue

            slope_deg = f["slp_1"]
            aspect_deg = f["asp_1"]
            height = None
            if height_field:
                height = f[height_field]
            if height is None or height <= 0:
                height = tree_height
            if slope_deg is None or aspect_deg is None:
                # Outside the DEM: fall back to the geometric answer rather than
                # inventing a felling direction.
                foot = roads_geom.nearestPoint(base_geom)
                feats.append(self._p2_feature(out, f, d_base, d_base, None, 0.0,
                                              foot, on_road=0, blocked=1))
                n_blocked += 1
                continue

            foot_geom = roads_geom.nearestPoint(base_geom)
            foot_pt = foot_geom.asPoint()
            target_az = _azimuth(foot_pt.x() - base.x(), foot_pt.y() - base.y())

            best = None
            for az in _candidate_directions(target_az, aspect_deg, slope_deg,
                                            fell_sector, flat_slope):
                phi = _angle_diff(az, aspect_deg)
                reach = height * math.cos(math.radians(_slope_along(slope_deg, phi)))
                tip = QgsPointXY(base.x() + reach * math.sin(math.radians(az)),
                                 base.y() + reach * math.cos(math.radians(az)))
                stem = QgsGeometry.fromPolylineXY([base, tip])
                # A direction whose stem crosses a barrier is not available:
                # a stem thrown over the river cannot be pulled back across it.
                if barriers_geom is not None and stem.intersects(barriers_geom):
                    continue
                if stem.intersects(roads_geom):
                    # Blocking the road is accepted, so reaching it ends the haul.
                    hit = stem.intersection(roads_geom)
                    p2_geom = hit if hit.wkbType() == QgsWkbTypes.Point \
                        else QgsGeometry.fromPointXY(
                            hit.nearestPoint(base_geom).asPoint())
                    best = (0.0, az, reach, p2_geom, 1)
                    break
                tip_geom = QgsGeometry.fromPointXY(tip)
                d_tip = tip_geom.distance(roads_geom)
                # Either end can be grabbed, so the near one decides.
                if d_tip < d_base:
                    cand = (d_tip, az, reach, roads_geom.nearestPoint(tip_geom), 0)
                else:
                    cand = (d_base, az, reach, foot_geom, 0)
                if best is None or cand[0] < best[0]:
                    best = cand

            if best is None:
                # Every direction was blocked: leave the stem standing and use
                # the plain geometric distance, and count it so the report can
                # say how often this happened.
                n_blocked += 1
                feats.append(self._p2_feature(out, f, d_base, d_base, None, 0.0,
                                              foot_geom, on_road=0, blocked=1))
                continue

            d1_new, az, reach, p2_geom, on_road = best
            if d1_new <= grapple_reach:
                d1_new = 0.0
            n_on_road += on_road
            feats.append(self._p2_feature(out, f, d1_new, d_base, az, reach,
                                          p2_geom, on_road=on_road, blocked=0))

        out.dataProvider().addFeatures(feats)
        out.updateExtents()
        _reg(out)
        stats = {"on_road": n_on_road, "blocked": n_blocked, "grapple": n_grapple}
        feedback.pushInfo(self.tr(
            "    -> stem reaches the road: {} / within grapple reach: {} / "
            "no felling direction available: {}"
        ).format(n_on_road, n_grapple, n_blocked))
        return out, stats

    @staticmethod
    def _p2_feature(layer, src, d1, d1_geom, az, reach, geom, on_road, blocked):
        feat = QgsFeature(layer.fields())
        feat["tree_id"] = src["tree_id"]
        feat["d1"] = float(d1)
        feat["d1_geom"] = float(d1_geom)
        feat["fell_az"] = None if az is None else float(az)
        feat["stem_len"] = float(reach)
        feat["on_road"] = int(on_road)
        feat["blocked"] = int(blocked)
        feat.setGeometry(geom if isinstance(geom, QgsGeometry)
                         else QgsGeometry.fromPointXY(geom))
        return feat

    def processAlgorithm(self, parameters, context: QgsProcessingContext, feedback: QgsProcessingFeedback):
        poly = self.parameterAsSource(parameters, self.POLY, context)
        roads = self.parameterAsSource(parameters, self.ROADS, context)
        landing = self.parameterAsSource(parameters, self.LANDING, context)
        grid = float(self.parameterAsDouble(parameters, self.GRID, context))
        snap_tol = float(self.parameterAsDouble(parameters, self.SNAP_TOL, context))
        split_roads = self.parameterAsBool(parameters, self.SPLIT_ROADS, context)
        debug = self.parameterAsBool(parameters, self.DEBUG, context)

        # Felling model inputs.  Each feature turns itself on by being supplied.
        dem_layer = self.parameterAsRasterLayer(parameters, self.DEM, context)
        auto_dem = self.parameterAsEnum(parameters, self.AUTO_DEM, context)
        trees_source = self.parameterAsSource(parameters, self.TREES, context)
        height_field = self.parameterAsString(parameters, self.HEIGHT_FIELD, context)
        barriers_source = self.parameterAsSource(parameters, self.BARRIERS, context)
        tree_height = float(self.parameterAsDouble(parameters, self.TREE_HEIGHT, context))
        fell_sector = float(self.parameterAsDouble(parameters, self.FELL_SECTOR, context))
        flat_slope = float(self.parameterAsDouble(parameters, self.FLAT_SLOPE, context))
        grapple_reach = float(self.parameterAsDouble(parameters, self.GRAPPLE_REACH, context))
        aspect_smooth = float(self.parameterAsDouble(parameters, self.ASPECT_SMOOTH, context))
        road_candidates = int(self.parameterAsInt(parameters, self.ROAD_CANDIDATES, context))

        if poly is None or roads is None or landing is None:
            raise QgsProcessingException(self.tr("Invalid input layers."))

        if poly.featureCount() == 0:
            raise QgsProcessingException(self.tr("Operation area polygon has no features."))
        if roads.featureCount() == 0:
            raise QgsProcessingException(self.tr("Forest roads layer has no features."))

        crs = poly.sourceCrs()
        if crs.isGeographic():
            raise QgsProcessingException(self.tr(
                "Polygon CRS is geographic (degrees). Reproject to a projected CRS in metres."
            ))

        if crs.mapUnits() != QgsUnitTypes.DistanceMeters:
            unit_name = QgsUnitTypes.encodeUnit(crs.mapUnits())
            raise QgsProcessingException(self.tr(
                "Polygon CRS unit is '{}', not metres. "
                "Grid spacing and distances will be incorrect. Reproject to a metric CRS."
            ).format(unit_name))

        roads_crs = roads.sourceCrs()
        landing_crs = landing.sourceCrs()
        if roads_crs != crs:
            raise QgsProcessingException(self.tr(
                "Road layer CRS ({}) differs from polygon CRS ({}). "
                "Reproject all layers to the same CRS."
            ).format(roads_crs.authid(), crs.authid()))
        if landing_crs != crs:
            raise QgsProcessingException(self.tr(
                "Landing layer CRS ({}) differs from polygon CRS ({}). "
                "Reproject all layers to the same CRS."
            ).format(landing_crs.authid(), crs.authid()))

        if dem_layer is None and auto_dem > 0:
            dem_layer = self._download_dem(
                poly, crs, auto_dem - 1, feedback)
        elif dem_layer is not None and auto_dem > 0:
            feedback.pushInfo(self.tr(
                "A DEM layer was given, so the download option is ignored."
            ))

        try:
            # Helper: register a memory layer in the context's temporary store so it can be
            # referenced by ID in subsequent processing.run() calls.  Without this,
            # passing QgsVectorLayer objects in parameter dicts requires PyQt6/SIP to convert
            # them to QVariant, which fails intermittently in QGIS 4.0 (PyQt6).
            def _reg(lyr):
                context.temporaryLayerStore().addMapLayer(lyr)
                return lyr

            # 1) Create grid points and clip to polygon
            if trees_source is not None:
                # Individual tree points stand in for the grid: extraction distance
                # is a per-tree quantity, so real stem positions beat a regular
                # lattice.  Grid spacing has no meaning here -- say so rather than
                # letting it look like it was applied.
                feedback.pushInfo(self.tr(
                    "1) Using supplied tree points as sample points (p1); "
                    "grid spacing is ignored."
                ))
                sample_input = parameters[self.TREES]
            else:
                feedback.pushInfo(self.tr("1) Creating grid points (p1)..."))
                sample_input = _reg(processing.run(
                "native:creategrid",
                    {
                        "TYPE": 0,  # point
                        "EXTENT": poly.sourceExtent(),
                        "HSPACING": grid,
                        "VSPACING": grid,
                        "HOVERLAY": 0,
                        "VOVERLAY": 0,
                        "CRS": crs,
                        "OUTPUT": "memory:"
                    },
                    context=context, feedback=feedback
                )["OUTPUT"]).id()

            # Both sources get clipped to the operation area the same way.
            p1 = _reg(processing.run(
                "native:extractbylocation",
                {
                    "INPUT": sample_input,
                    "PREDICATE": [0],  # intersects
                    "INTERSECT": parameters[self.POLY],
                    "OUTPUT": "memory:"
                },
                context=context, feedback=feedback
            )["OUTPUT"])

            if p1.featureCount() == 0:
                raise QgsProcessingException(self.tr(
                    "No sample points fall within the operation polygon. "
                    "With a grid, try a smaller spacing; with tree points, check "
                    "that they overlap the operation area."
                ))

            p1 = _reg(processing.run(
                "native:fieldcalculator",
                {
                    "INPUT": p1.id(),
                    "FIELD_NAME": "tree_id",
                    "FIELD_TYPE": 1,  # int
                    "FIELD_LENGTH": 10,
                    "FIELD_PRECISION": 0,
                    "FORMULA": "@row_number",
                    "OUTPUT": "memory:"
                },
                context=context, feedback=feedback
            )["OUTPUT"])

            # 2) Shortest line to roads -> d1, nearest point on road -> p2
            feedback.pushInfo(self.tr("2) Computing shortest lines to roads (d1) and nearest points (p2)..."))
            shortest_lines = _reg(processing.run(
                "native:shortestline",
                {
                    "SOURCE": p1.id(),
                    "DESTINATION": parameters[self.ROADS],
                    "METHOD": 0,
                    "NEIGHBORS": 1,
                    "OUTPUT": "memory:"
                },
                context=context, feedback=feedback
            )["OUTPUT"])

            shortest_lines = _reg(processing.run(
                "native:fieldcalculator",
                {
                    "INPUT": shortest_lines.id(),
                    "FIELD_NAME": "d1",
                    "FIELD_TYPE": 0,  # float
                    "FIELD_LENGTH": 20,
                    "FIELD_PRECISION": 3,
                    "FORMULA": "$length",
                    "OUTPUT": "memory:"
                },
                context=context, feedback=feedback
            )["OUTPUT"])

            p2 = _reg(processing.run(
                "native:extractspecificvertices",
                {
                    "INPUT": shortest_lines.id(),
                    "VERTICES": "-1",  # last vertex = nearest point on road
                    "OUTPUT": "memory:"
                },
                context=context, feedback=feedback
            )["OUTPUT"])

            # 2b) Felling model.  d1 stops being "distance to the nearest road"
            # and becomes "distance from the road to the near end of the felled
            # stem", which also moves p2 -- and p2 is where routing starts.
            geometric_d1 = None
            model_stats = None
            if dem_layer is not None:
                geometric_d1 = {}
                for f in p2.getFeatures():
                    geometric_d1[f["tree_id"]] = f["d1"]
                p2, model_stats = self._apply_felling_model(
                    p1, p2, parameters, context, feedback, _reg,
                    dem_layer=dem_layer,
                    barriers_source=barriers_source,
                    height_field=height_field,
                    tree_height=tree_height,
                    fell_sector=fell_sector,
                    flat_slope=flat_slope,
                    grapple_reach=grapple_reach,
                    aspect_smooth=aspect_smooth,
                    crs=crs,
                )

            # 3) Shortest path to nearest landing along road network
            # NOTE: QGIS 'native:shortestpathpointtolayer' expects a SINGLE START_POINT (coordinate),
            # so for multiple start points we use 'native:shortestpathlayertopoint' and run it
            # for each landing, then take the minimum cost per start point.
            feedback.pushInfo(self.tr("3) Computing shortest path along road network to nearest landing (d2)..."))

            landing_layer = self.parameterAsLayer(parameters, self.LANDING, context)
            if landing_layer.featureCount() == 0:
                raise QgsProcessingException(self.tr("Landing layer has no features."))

            if split_roads:
                feedback.pushInfo(self.tr("3a) Splitting roads at intersections..."))
                roads_layer = _reg(processing.run(
                    "native:splitwithlines",
                    {
                        "INPUT": parameters[self.ROADS],
                        "LINES": parameters[self.ROADS],
                        "OUTPUT": "memory:"
                    },
                    context=context, feedback=feedback
                )["OUTPUT"])
                feedback.pushInfo(self.tr("    -> {} segments after split.").format(roads_layer.featureCount()))
                roads_source = roads_layer.id()
            else:
                roads_source = parameters[self.ROADS]

            routes_id_list = []
            authid = crs.authid()

            for lf in landing_layer.getFeatures():
                if feedback.isCanceled():
                    raise QgsProcessingException(self.tr("Processing cancelled by user."))
                geom = lf.geometry()
                if geom is None or geom.isEmpty():
                    continue
                pt = geom.asPoint()
                end_point = f"{pt.x()},{pt.y()} [{authid}]"

                out = processing.run(
                    "native:shortestpathlayertopoint",
                    {
                        "INPUT": roads_source,
                        "START_POINTS": p2.id(),
                        "END_POINT": end_point,
                        "STRATEGY": 0,  # shortest distance
                        "DEFAULT_DIRECTION": 2,
                        "TOLERANCE": snap_tol,
                        "OUTPUT": "memory:",
                        "OUTPUT_NON_ROUTABLE": "memory:"
                    },
                    context=context, feedback=feedback
                )

                r = _reg(out["OUTPUT"])
                r = _reg(processing.run(
                    "native:fieldcalculator",
                    {
                        "INPUT": r.id(),
                        "FIELD_NAME": "landing_fid",
                        "FIELD_TYPE": 1,  # int
                        "FIELD_LENGTH": 20,
                        "FIELD_PRECISION": 0,
                        "FORMULA": str(lf.id()),
                        "OUTPUT": "memory:"
                    },
                    context=context, feedback=feedback
                )["OUTPUT"])

                routes_id_list.append(r.id())

            if not routes_id_list:
                raise QgsProcessingException(self.tr(
                    "No valid landing points were found (all geometries empty?)."
                ))

            merged_routes = _reg(processing.run(
                "native:mergevectorlayers",
                {"LAYERS": routes_id_list, "CRS": crs, "OUTPUT": "memory:"},
                context=context, feedback=feedback
            )["OUTPUT"])

            cost_field = "cost" if merged_routes.fields().indexFromName("cost") != -1 else None
            if cost_field is None:
                feedback.pushInfo(self.tr(
                    "Note: routing output has no 'cost' field; using geometry length for d2."
                ))
            routes = _reg(processing.run(
                "native:fieldcalculator",
                {
                    "INPUT": merged_routes.id(),
                    "FIELD_NAME": "d2",
                    "FIELD_TYPE": 0,
                    "FIELD_LENGTH": 20,
                    "FIELD_PRECISION": 3,
                    "FORMULA": f"\"{cost_field}\"" if cost_field else "$length",
                    "OUTPUT": "memory:"
                },
                context=context, feedback=feedback
            )["OUTPUT"])

            if routes.fields().indexFromName("tree_id") == -1:
                raise QgsProcessingException(self.tr(
                    "Routing output has no 'tree_id' field. Ensure p2 has 'tree_id' attribute."
                ))

            routes = _reg(processing.run(
                "native:extractbyexpression",
                {"INPUT": routes.id(), "EXPRESSION": "\"d2\" IS NOT NULL", "OUTPUT": "memory:"},
                context=context, feedback=feedback
            )["OUTPUT"])

            if routes.featureCount() == 0:
                raise QgsProcessingException(self.tr(
                    "All grid points are unreachable from all landings. "
                    "Check that the road network is connected, "
                    "landing points are on or near the road, "
                    "and the snapping tolerance is sufficient."
                ))

            stats = _reg(processing.run(
                "qgis:statisticsbycategories",
                {
                    "INPUT": routes.id(),
                    "CATEGORIES_FIELD_NAME": ["tree_id"],
                    "VALUES_FIELD_NAME": "d2",
                    "OUTPUT": "memory:"
                },
                context=context, feedback=feedback
            )["OUTPUT"])

            if stats.fields().indexFromName("min") == -1:
                raise QgsProcessingException(self.tr("Unexpected statistics output (no 'min' field)."))

            p2_tmp = _reg(processing.run(
                "native:joinattributestable",
                {
                    "INPUT": p2.id(),
                    "FIELD": "tree_id",
                    "INPUT_2": stats.id(),
                    "FIELD_2": "tree_id",
                    "FIELDS_TO_COPY": ["min"],
                    "METHOD": 1,
                    "DISCARD_NONMATCHING": False,
                    "PREFIX": "",
                    "OUTPUT": "memory:"
                },
                context=context, feedback=feedback
            )["OUTPUT"])

            p2_with = _reg(processing.run(
                "native:fieldcalculator",
                {
                    "INPUT": p2_tmp.id(),
                    "FIELD_NAME": "d2",
                    "FIELD_TYPE": 0,
                    "FIELD_LENGTH": 20,
                    "FIELD_PRECISION": 3,
                    "FORMULA": "\"min\"",
                    "OUTPUT": "memory:"
                },
                context=context, feedback=feedback
            )["OUTPUT"])

            # 4) Summary statistics
            feedback.pushInfo(self.tr("4) Computing summary statistics..."))
            d1_vals, d2_vals = [], []
            null_d2 = 0
            total = 0
            field_names = p2_with.fields().names()

            d1_geom_vals = []
            for f in p2_with.getFeatures():
                total += 1
                if f["d1"] is not None:
                    d1_vals.append(float(f["d1"]))
                if "d1_geom" in field_names and f["d1_geom"] is not None:
                    d1_geom_vals.append(float(f["d1_geom"]))
                d2 = f["d2"] if "d2" in field_names else None
                if d2 is None:
                    null_d2 += 1
                else:
                    d2_vals.append(float(d2))

            d1_mean = (sum(d1_vals) / len(d1_vals)) if d1_vals else None
            d2_mean = (sum(d2_vals) / len(d2_vals)) if d2_vals else None
            # With the model on, the plain geometric d1 is reported next to it:
            # the drop is the whole point of the model and should be visible in
            # one run rather than asserted.
            d1_geom_mean = (sum(d1_geom_vals) / len(d1_geom_vals)) if d1_geom_vals else None

            if d2_mean is None:
                feedback.reportError(self.tr(
                    "WARNING: d2_mean is None — no grid points could be routed to any landing. "
                    "Check that the road network is connected and the snapping tolerance is sufficient."
                ), fatalError=False)

            def fmt(val, unit="m"):
                return f"{val:.1f} {unit}" if val is not None else "N/A"

            # State the assumptions in the report itself.  The model's numbers
            # only mean something next to the assumptions that produced them.
            if model_stats is not None:
                height_note = (f"樹高フィールド {height_field}" if height_field
                               else f"樹高 {tree_height:.0f} m")
                model_note = (
                    "<br><b>伐倒モデル適用</b>"
                    f"（{height_note}／許容扇形 下方向±{fell_sector:.0f}度／"
                    f"傾斜{flat_slope:.0f}度以下は全方向／直接把持 {grapple_reach:.0f} m／"
                    f"斜面方位の評価 {aspect_smooth:.0f} m）"
                    f"<br>伐倒前の幾何的な平均 d1: <b>{fmt(d1_geom_mean)}</b>"
                    f"　→　モデル適用後: <b>{fmt(d1_mean)}</b>"
                    f"<br>幹が林道に達した点: {model_stats['on_road']} 点 ／ "
                    f"直接つかめる範囲: {model_stats['grapple']} 点 ／ "
                    f"伐倒方向が取れなかった点: {model_stats['blocked']} 点"
                )
            else:
                model_note = ""

            html_path = os.path.join(
                tempfile.gettempdir(),
                f"harvest_accessibility_{os.getpid()}.html"
            )
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8">
<style>
  body {{ font-family: sans-serif; margin: 2em; color: #333; }}
  h2 {{ color: #2e6b2e; }}
  table {{ border-collapse: collapse; margin-top: 1em; }}
  td {{ padding: 0.5em 1.2em 0.5em 0; }}
  .val {{ font-size: 1.6em; font-weight: bold; color: #2e6b2e; }}
  .label {{ color: #555; font-size: 0.9em; }}
  .note {{ color: #888; font-size: 0.85em; margin-top: 1.5em; }}
</style>
</head>
<body>
<h2>Harvest Accessibility — Result</h2>
<table>
  <tr>
    <td><span class="label">平均木寄せ距離 (d1)</span><br>
        <span class="val">{fmt(d1_mean)}</span></td>
    <td><span class="label">平均運材距離 (d2)</span><br>
        <span class="val">{fmt(d2_mean)}</span></td>
  </tr>
</table>
<p class="note">
  サンプル点数: {total} 点 ／ d2 未到達: {null_d2} 点
  {model_note}
  {"<br><b style='color:#c00'>⚠ 全点が土場に到達できませんでした。林道の接続とスナップ許容誤差を確認してください。</b>" if d2_mean is None else ""}
</p>
</body>
</html>""")

            if d1_geom_mean is not None and d1_mean is not None:
                feedback.pushInfo(
                    f"d1 geometric={d1_geom_mean:.3f}m -> model={d1_mean:.3f}m"
                )
            feedback.pushInfo(
                f"Done. d1_mean={d1_mean:.3f}m, d2_mean={d2_mean:.3f}m, "
                f"points={total}, d2_null={null_d2}"
                if d1_mean is not None and d2_mean is not None
                else f"Done. d1_mean={d1_mean}, d2_mean={d2_mean}, points={total}, d2_null={null_d2}"
            )

            if debug:
                project = context.project()
                if project is not None:
                    # p1 itself has no d1.  The shortest-line layer carries p1's attributes
                    # plus d1, so its first vertex is the grid point with d1 attached.
                    # tree_id keeps it aligned with debug_p2_road_snap.
                    p1_dbg = _reg(processing.run(
                        "native:extractspecificvertices",
                        {
                            "INPUT": shortest_lines.id(),
                            "VERTICES": "0",  # first vertex = original grid point
                            "OUTPUT": "memory:"
                        },
                        context=context, feedback=feedback
                    )["OUTPUT"])

                    for layer, name in [
                        (p1_dbg,  "debug_p1_grid"),
                        (p2_with, "debug_p2_road_snap"),
                        (routes,  "debug_routes"),
                    ]:
                        context.addLayerToLoadOnCompletion(
                            layer.id(),
                            QgsProcessingContext.LayerDetails(name, project)
                        )

                    summary_fields = QgsFields()
                    summary_fields.append(QgsField("n_points", QVariant.Int))
                    summary_fields.append(QgsField("n_d2_null", QVariant.Int))
                    summary_fields.append(QgsField("d1_mean", QVariant.Double))
                    summary_fields.append(QgsField("d2_mean", QVariant.Double))
                    from qgis.core import QgsVectorLayer, QgsProject
                    summary_layer = QgsVectorLayer("NoGeometry", "debug_summary", "memory")
                    summary_layer.dataProvider().addAttributes(summary_fields.toList())
                    summary_layer.updateFields()
                    sf = QgsFeature(summary_layer.fields())
                    sf["n_points"] = total
                    sf["n_d2_null"] = null_d2
                    sf["d1_mean"] = d1_mean
                    sf["d2_mean"] = d2_mean
                    summary_layer.dataProvider().addFeatures([sf])
                    QgsProject.instance().addMapLayer(summary_layer)
                else:
                    feedback.pushInfo(self.tr("Debug: no project context, skipping layer output."))

            return {self.HTML_OUT: html_path}

        except QgsProcessingException:
            raise
        except Exception as e:
            raise QgsProcessingException(
                self.tr("Unexpected error during processing: {}").format(e)
            ) from e
