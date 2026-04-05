from qgis.core import QgsApplication

from .processing.provider import HarvestAccessibilityProvider


class HarvestAccessibilityPlugin:
    """QGIS plugin entry point.

    This plugin is implemented primarily as a Processing provider.
    It intentionally does NOT add a toolbar icon or menu entry.
    """

    def __init__(self, iface):
        self.iface = iface
        self.provider = None

    def initGui(self):
        # Register Processing provider (algorithms will appear in Processing Toolbox)
        self.provider = HarvestAccessibilityProvider()
        QgsApplication.processingRegistry().addProvider(self.provider)

    def unload(self):
        # Unregister provider
        if self.provider is not None:
            QgsApplication.processingRegistry().removeProvider(self.provider)
            self.provider = None
