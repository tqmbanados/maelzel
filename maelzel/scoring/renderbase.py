"""
This module declares the basic classes for all renderers.
"""

from __future__ import annotations
import tempfile
from dataclasses import dataclass
import music21 as m21

from maelzel.music import m21tools

from .common import *
from . import quant
from .config import config
from emlib.misc import open_with_standard_app


@dataclass
class RenderOptions:
    """
    orientation: one of "portrait" or "landscape"
    staffSize: the size of each staff in point
    pageSize: one of "a4", "a3"
    pageMarginMillimeters: page margin in mm. Only used by some backends

    divsPerSemitone: the number of divisions of the semitone
    showCents: should each note/chord have a text label attached
    indicating the cents deviation from the nearest semitone?
    centsPlacement: where to put the cents annotation
    centsFontSize: the font size of the cents annotation

    measureAnnotationFontSize: font size for measure annotations

    glissAllowNonContiguous: if True, allow glissandi between notes which
        have rests between them
    glissHideTiedNotes: if True, hide tied notes which are part of a gliss.

    lilypondPngBookPreamble: include the lilypond book preamble when rendering
        to png via lilypond

    title: the title of the score
    composer: the composer of the score
    """
    orientation: str = config['pageOrientation']
    staffSize: int = config['staffSize']
    pageSize: str = config['pageSize']
    pageMarginMillimeters: Opt[int] = 4

    divsPerSemitone: int = config['divisionsPerSemitone']
    showCents: bool = config['showCents']
    centsPlacement: str = "above"
    centsFontSize: int = config['centsFontSize']

    measureAnnotationFontSize: int = config['measureAnnotationFontSize']
    noteAnnotationsFontSize: int = config['noteAnnotationFontSize']

    glissAllowNonContiguous: bool = False
    glissHideTiedNotes: bool = True

    lilypondPngBookPreamble: bool = True

    title: str = ''
    composer: str = ''


class Renderer:
    def __init__(self, parts: List[quant.QuantizedPart], options:RenderOptions=None):
        assert parts
        assert parts[0].struct is not None
        self.parts = parts
        self.struct = parts[0].struct
        if options is None:
            options = RenderOptions()
        self.options = options
        self._rendered = False

    def render(self) -> None:
        """
        This method should be implemented by the backend
        """
        raise NotImplementedError("Please Implement this method")

    def writeFormats(self) -> List[str]:
        """
        Returns: a list of possible write formats (pdf, xml, musicxml, etc)
        """
        raise NotImplementedError("Please Implement this method")

    def write(self, outfile:str) -> None:
        raise NotImplementedError("Please Implement this method")

    def show(self, fmt='png') -> None:
        self.render()
        possibleFormats = self.writeFormats()
        if fmt not in possibleFormats:
            raise ValueError(f"{fmt} not supported. Possible write "
                             f"formats: {possibleFormats}")
        outfile = tempfile.mktemp(suffix="."+fmt)
        self.write(outfile)
        open_with_standard_app(outfile)

    def musicxml(self) -> Opt[str]:
        m21stream = self.asMusic21()
        if m21stream is None:
            return None
        return m21tools.getXml(m21stream)

    def asMusic21(self) -> Opt[m21.stream.Stream]:
        """
        If the renderer can return a music21 stream version of the render,
        return it here, otherwise return None
        """
        return None