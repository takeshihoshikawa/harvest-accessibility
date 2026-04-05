import os
import tempfile

from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterNumber,
    QgsProcessingException,
    QgsProcessingOutputHtml,
    QgsProcessingContext,
    QgsProcessingFeedback,
    QgsFeature,
    QgsFields,
    QgsField,
    QgsWkbTypes
)
from qgis.PyQt.QtCore import QVariant
from qgis import processing


class HarvestAccessibilityAlg(QgsProcessingAlgorithm):
    POLY = "POLY"
    ROADS = "ROADS"
    LANDING = "LANDING"
    GRID = "GRID"
    SNAP_TOL = "SNAP_TOL"
    P1_OUT = "P1_OUT"
    P2_OUT = "P2_OUT"
    ROUTE_OUT = "ROUTE_OUT"
    SUMMARY_OUT = "SUMMARY_OUT"
    HTML_OUT = "HTML_OUT"

    def name(self):
        return "harvest_accessibility"

    def displayName(self):
        return "Harvest Accessibility (d1 straight to road + d2 network to nearest landing)"

    def group(self):
        return "Harvest Accessibility"

    def groupId(self):
        return "harvest_accessibility"

    def shortHelpString(self):
        return (
            "Inputs: operation polygon, forest road lines (also used as network), landing points (multiple OK).\n"
            "1) Create grid points within polygon (p1)\n"
            "2) Shortest straight line to road -> d1, endpoint on road -> p2\n"
            "3) Shortest path on road network from p2 to the nearest landing -> d2 (NULL if unreachable)\n"
            "4) Output p2 and summary means.\n"
            "NOTE: Use a projected CRS in metres."
        )

    def createInstance(self):
        return HarvestAccessibilityAlg()

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.POLY, "Operation area polygon", [QgsProcessing.TypeVectorPolygon]
        ))
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.ROADS, "Forest road lines (also network)", [QgsProcessing.TypeVectorLine]
        ))
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.LANDING, "Landing points (multiple OK)", [QgsProcessing.TypeVectorPoint]
        ))
        self.addParameter(QgsProcessingParameterNumber(
            self.GRID, "Grid spacing (m)", QgsProcessingParameterNumber.Double, defaultValue=4.0, minValue=0.1
        ))
        self.addParameter(QgsProcessingParameterNumber(
            self.SNAP_TOL, "Network snapping tolerance (m)", QgsProcessingParameterNumber.Double, defaultValue=5.0, minValue=0.0
        ))

        # IMPORTANT: Explicit sink types to avoid geometry mismatch in QGIS 3.44+
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.P1_OUT, "Output p1 (grid points)", type=QgsProcessing.TypeVectorPoint
        ))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.P2_OUT, "Output p2 (road snap points with d1, d2)", type=QgsProcessing.TypeVectorPoint
        ))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.ROUTE_OUT, "Output routes (p2 to nearest landing)", type=QgsProcessing.TypeVectorLine
        ))
        # Summary is a table (no geometry). Use TypeVectorAnyGeometry but write NoGeometry.
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.SUMMARY_OUT, "Output summary (means)", type=QgsProcessing.TypeVector
        ))
        self.addOutput(QgsProcessingOutputHtml(self.HTML_OUT, "Result report"))

    def processAlgorithm(self, parameters, context: QgsProcessingContext, feedback: QgsProcessingFeedback):
        poly = self.parameterAsSource(parameters, self.POLY, context)
        roads = self.parameterAsSource(parameters, self.ROADS, context)
        landing = self.parameterAsSource(parameters, self.LANDING, context)
        grid = float(self.parameterAsDouble(parameters, self.GRID, context))
        snap_tol = float(self.parameterAsDouble(parameters, self.SNAP_TOL, context))

        if poly is None or roads is None or landing is None:
            raise QgsProcessingException("Invalid input layers.")

        if poly.featureCount() == 0:
            raise QgsProcessingException("Operation area polygon has no features.")
        if roads.featureCount() == 0:
            raise QgsProcessingException("Forest roads layer has no features.")

        crs = poly.sourceCrs()
        if crs.isGeographic():
            raise QgsProcessingException(
                "Polygon CRS is geographic (degrees). Reproject to a projected CRS in metres."
            )

        try:
            # 1) Create grid points and clip to polygon
            feedback.pushInfo("1) Creating grid points (p1)...")
            grid_layer = processing.run(
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
            )["OUTPUT"]

            p1 = processing.run(
                "native:extractbylocation",
                {
                    "INPUT": grid_layer,
                    "PREDICATE": [0],  # intersects
                    "INTERSECT": self.parameterAsLayer(parameters, self.POLY, context),
                    "OUTPUT": "memory:"
                },
                context=context, feedback=feedback
            )["OUTPUT"]

            p1 = processing.run(
                "native:fieldcalculator",
                {
                    "INPUT": p1,
                    "FIELD_NAME": "tree_id",
                    "FIELD_TYPE": 1,  # int
                    "FIELD_LENGTH": 10,
                    "FIELD_PRECISION": 0,
                    "FORMULA": "@row_number",
                    "OUTPUT": "memory:"
                },
                context=context, feedback=feedback
            )["OUTPUT"]

            # 2) Shortest line to roads -> d1, nearest point on road -> p2
            feedback.pushInfo("2) Computing shortest lines to roads (d1) and nearest points (p2)...")
            shortest_lines = processing.run(
                "native:shortestline",
                {
                    "SOURCE": p1,
                    "DESTINATION": self.parameterAsLayer(parameters, self.ROADS, context),
                    "METHOD": 0,
                    "NEIGHBORS": 1,
                    "OUTPUT": "memory:"
                },
                context=context, feedback=feedback
            )["OUTPUT"]

            shortest_lines = processing.run(
                "native:fieldcalculator",
                {
                    "INPUT": shortest_lines,
                    "FIELD_NAME": "d1",
                    "FIELD_TYPE": 0,  # float
                    "FIELD_LENGTH": 20,
                    "FIELD_PRECISION": 3,
                    "FORMULA": "$length",
                    "OUTPUT": "memory:"
                },
                context=context, feedback=feedback
            )["OUTPUT"]

            p2 = processing.run(
                "native:extractspecificvertices",
                {
                    "INPUT": shortest_lines,
                    "VERTICES": "-1",  # last vertex = nearest point on road
                    "OUTPUT": "memory:"
                },
                context=context, feedback=feedback
            )["OUTPUT"]

            # 3) Shortest path to nearest landing along road network
            # NOTE: QGIS 'native:shortestpathpointtolayer' expects a SINGLE START_POINT (coordinate),
            # so for multiple start points we use 'native:shortestpathlayertopoint' and run it
            # for each landing, then take the minimum cost per start point.
            feedback.pushInfo("3) Computing shortest path along road network to nearest landing (d2)...")

            landing_layer = self.parameterAsLayer(parameters, self.LANDING, context)
            if landing_layer.featureCount() == 0:
                raise QgsProcessingException("Landing layer has no features.")

            routes_list = []
            authid = crs.authid()

            for lf in landing_layer.getFeatures():
                geom = lf.geometry()
                if geom is None or geom.isEmpty():
                    continue
                pt = geom.asPoint()
                end_point = f"{pt.x()},{pt.y()} [{authid}]"

                out = processing.run(
                    "native:shortestpathlayertopoint",
                    {
                        "INPUT": self.parameterAsLayer(parameters, self.ROADS, context),
                        "START_POINTS": p2,
                        "END_POINT": end_point,
                        "STRATEGY": 0,  # shortest distance
                        "DEFAULT_DIRECTION": 2,
                        "TOLERANCE": snap_tol,
                        "OUTPUT": "memory:",
                        "OUTPUT_NON_ROUTABLE": "memory:"
                    },
                    context=context, feedback=feedback
                )

                r = out["OUTPUT"]
                # Record which landing was routed to (useful for inspection)
                r = processing.run(
                    "native:fieldcalculator",
                    {
                        "INPUT": r,
                        "FIELD_NAME": "landing_fid",
                        "FIELD_TYPE": 1,  # int
                        "FIELD_LENGTH": 20,
                        "FIELD_PRECISION": 0,
                        "FORMULA": str(lf.id()),
                        "OUTPUT": "memory:"
                    },
                    context=context, feedback=feedback
                )["OUTPUT"]

                routes_list.append(r)

            if not routes_list:
                raise QgsProcessingException("No valid landing points were found (all geometries empty?).")

            # Merge all route candidates (one set per landing)
            merged_routes = processing.run(
                "native:mergevectorlayers",
                {"LAYERS": routes_list, "CRS": crs, "OUTPUT": "memory:"},
                context=context, feedback=feedback
            )["OUTPUT"]

            # Use 'cost' field if present, otherwise fall back to geometry length
            cost_field = "cost" if merged_routes.fields().indexFromName("cost") != -1 else None
            if cost_field is None:
                feedback.pushInfo(
                    "Note: routing output has no 'cost' field; using geometry length ($length) for d2. "
                    "This is expected for distance-based routing."
                )
            routes = processing.run(
                "native:fieldcalculator",
                {
                    "INPUT": merged_routes,
                    "FIELD_NAME": "d2",
                    "FIELD_TYPE": 0,
                    "FIELD_LENGTH": 20,
                    "FIELD_PRECISION": 3,
                    "FORMULA": f"\"{cost_field}\"" if cost_field else "$length",
                    "OUTPUT": "memory:"
                },
                context=context, feedback=feedback
            )["OUTPUT"]

            if routes.fields().indexFromName("tree_id") == -1:
                raise QgsProcessingException(
                    "Routing output has no 'tree_id' field. Ensure p2 has 'tree_id' attribute."
                )

            # Drop features with NULL d2 (unroutable start points)
            routes = processing.run(
                "native:extractbyexpression",
                {"INPUT": routes, "EXPRESSION": "\"d2\" IS NOT NULL", "OUTPUT": "memory:"},
                context=context, feedback=feedback
            )["OUTPUT"]

            stats = processing.run(
                "qgis:statisticsbycategories",
                {
                    "INPUT": routes,
                    "CATEGORIES_FIELD_NAME": ["tree_id"],
                    "VALUES_FIELD_NAME": "d2",
                    "OUTPUT": "memory:"
                },
                context=context, feedback=feedback
            )["OUTPUT"]

            if stats.fields().indexFromName("min") == -1:
                raise QgsProcessingException("Unexpected statistics output (no 'min' field).")

            # Join minimum d2 back to p2
            p2_tmp = processing.run(
                "native:joinattributestable",
                {
                    "INPUT": p2,
                    "FIELD": "tree_id",
                    "INPUT_2": stats,
                    "FIELD_2": "tree_id",
                    "FIELDS_TO_COPY": ["min"],
                    "METHOD": 1,
                    "DISCARD_NONMATCHING": False,
                    "PREFIX": "",
                    "OUTPUT": "memory:"
                },
                context=context, feedback=feedback
            )["OUTPUT"]

            # Rename 'min' -> 'd2'
            p2_with = processing.run(
                "native:fieldcalculator",
                {
                    "INPUT": p2_tmp,
                    "FIELD_NAME": "d2",
                    "FIELD_TYPE": 0,
                    "FIELD_LENGTH": 20,
                    "FIELD_PRECISION": 3,
                    "FORMULA": "\"min\"",
                    "OUTPUT": "memory:"
                },
                context=context, feedback=feedback
            )["OUTPUT"]

            # 4) Summary statistics
            feedback.pushInfo("4) Computing summary statistics...")
            d1_vals, d2_vals = [], []
            null_d2 = 0
            total = 0
            field_names = p2_with.fields().names()

            for f in p2_with.getFeatures():
                total += 1
                if f["d1"] is not None:
                    d1_vals.append(float(f["d1"]))
                d2 = f["d2"] if "d2" in field_names else None
                if d2 is None:
                    null_d2 += 1
                else:
                    d2_vals.append(float(d2))

            d1_mean = (sum(d1_vals) / len(d1_vals)) if d1_vals else None
            d2_mean = (sum(d2_vals) / len(d2_vals)) if d2_vals else None

            if d2_mean is None:
                feedback.reportError(
                    "WARNING: d2_mean is None — no grid points could be routed to any landing. "
                    "Check that the road network is connected and the snapping tolerance is sufficient.",
                    fatalError=False
                )

            # HTML result report
            def fmt(val, unit="m"):
                return f"{val:.1f} {unit}" if val is not None else "N/A"

            html_path = os.path.join(tempfile.gettempdir(), "harvest_accessibility_result.html")
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
  {"<br><b style='color:#c00'>⚠ 全点が土場に到達できませんでした。林道の接続とスナップ許容誤差を確認してください。</b>" if d2_mean is None else ""}
</p>
</body>
</html>""")

            summary_fields = QgsFields()
            summary_fields.append(QgsField("n_points", QVariant.Int))
            summary_fields.append(QgsField("n_d2_null", QVariant.Int))
            summary_fields.append(QgsField("d1_mean", QVariant.Double))
            summary_fields.append(QgsField("d2_mean", QVariant.Double))

            (summary_sink, summary_id) = self.parameterAsSink(
                parameters, self.SUMMARY_OUT, context, summary_fields, QgsWkbTypes.NoGeometry, crs
            )
            sf = QgsFeature(summary_fields)
            sf["n_points"] = total
            sf["n_d2_null"] = null_d2
            sf["d1_mean"] = d1_mean
            sf["d2_mean"] = d2_mean
            summary_sink.addFeature(sf)

            (p1_sink, p1_id) = self.parameterAsSink(
                parameters, self.P1_OUT, context, p1.fields(), QgsWkbTypes.Point, crs
            )
            for f in p1.getFeatures():
                p1_sink.addFeature(f)

            (p2_sink, p2_id) = self.parameterAsSink(
                parameters, self.P2_OUT, context, p2_with.fields(), QgsWkbTypes.Point, crs
            )
            for f in p2_with.getFeatures():
                p2_sink.addFeature(f)

            (route_sink, route_id) = self.parameterAsSink(
                parameters, self.ROUTE_OUT, context, routes.fields(), QgsWkbTypes.LineString, crs
            )
            for f in routes.getFeatures():
                route_sink.addFeature(f)

            feedback.pushInfo(
                f"Done. d1_mean={d1_mean:.3f}m, d2_mean={d2_mean:.3f}m, "
                f"points={total}, d2_null={null_d2}"
                if d1_mean is not None and d2_mean is not None
                else f"Done. d1_mean={d1_mean}, d2_mean={d2_mean}, points={total}, d2_null={null_d2}"
            )

            return {
                self.P1_OUT: p1_id,
                self.P2_OUT: p2_id,
                self.ROUTE_OUT: route_id,
                self.SUMMARY_OUT: summary_id,
                self.HTML_OUT: html_path,
            }

        except QgsProcessingException:
            raise
        except Exception as e:
            raise QgsProcessingException(f"Unexpected error during processing: {e}") from e
