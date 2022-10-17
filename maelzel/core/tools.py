from __future__ import annotations

from maelzel import scorestruct

from .workspace import getWorkspace
from . import mobj as mobj
from . import score
from maelzel import packing
from maelzel.rational import Rat

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from ._typedefs import time_t
    from maelzel.core.mobj import *


__all__ = (
    'amplitudeToDynamics',
    'makeClickTrack',
    'packInVoices'
)


def amplitudeToDynamics(amp: float) -> str:
    """
    Convert an amplitude 0-1 to a dynamic according to the current dynamics curve

    Args:
        amp: an amplitude between 0-1

    Returns:
        a dynamic ('pp', 'f', etc)

    Example
    ~~~~~~~

        TODO

    """
    w = getWorkspace()
    dyncurve = w.dynamicCurve
    return dyncurve.amp2dyn(amp)


def makeClickTrack(struct: scorestruct.ScoreStruct,
                   minMeasures: int = 0,
                   clickdur: time_t = None,
                   strongBeatPitch="5C",
                   weakBeatPitch="5G",
                   playpreset: str = '_click',
                   playparams: dict[str, float] = None,
                   fade=0) -> score.Score:
    """
    Creates a score representing a clicktrack of the given ScoreStruct

    Args:
        struct: the ScoreStruct
        minMeasures: if given, the minimum number of measures. This might be needed
            in the case of an endless scorestruct
        clickdur: the length of each tick. Use None to use the duration of the beat.
            **NB**: the duration of the playback can be set individually from the duration
            of the displayed pitch
        strongBeatPitch: the pitch to use as a strong tick
        weakBeatPitch: the pitch to use as a weak tick
        playpreset: the preset instr to use for playback. The default plays the given
            pitches two octaves higher as very short clicks
        playparams: parameters passed to the play preset, if needed
        fade: a fadetime for the clicks

    Returns:
        a maelzel.core Score

    Example
    ~~~~~~~

    .. code-block:: python

        from maelzel.core import *
        scorestruct = ScoreStruct(r'''
        4/4, 72
        .
        5/8
        3/8
        2/4, 96
        .
        5/4
        3/4
        '')
        clicktrack = tools.makeClickTrack(scorestruct)
        clicktrack.show()
        clicktrack.rec('clicktrack.flac')

    .. image:: ../assets/clicktrack.png

    .. seealso:: :ref:`clicktrack_notebook`

    """
    now = 0
    events = []
    if minMeasures and minMeasures > struct.numDefinedMeasures():
        struct = struct.copy()
        struct.addMeasure(numMeasures=minMeasures - struct.numDefinedMeasures())

    for m in struct.measuredefs:
        num, den = m.timesig
        if den  == 4:
            for i, n in enumerate(range(m.timesig[0])):
                pitch = strongBeatPitch if i == 0 else weakBeatPitch
                ev = mobj.Note(pitch, start=now, dur=clickdur or 1).setPlay(fade=(0, 0.1))
                events.append(ev)
                now += 1
        elif den == 8:
            for i, n in enumerate(range(m.timesig[0])):
                pitch = strongBeatPitch if i == 0 else weakBeatPitch
                ev = mobj.Note(pitch, start=now, dur=clickdur or 0.5).setPlay(fade=(0, 0.1))
                events.append(ev)
                now += 0.5
        elif den == 16:
            if m.quarterTempo > 80:
                dur = clickdur or m.durationBeats()
                ev = mobj.Note(strongBeatPitch, dur=dur, start=now)
                events.append(ev)
                now += m.durationBeats()
            else:
                beats = m.subdivisions()
                for i, beat in enumerate(beats):
                    pitch = strongBeatPitch if i == 0 else weakBeatPitch
                    ev = mobj.Note(pitch, dur=clickdur or beat, start=now)
                    events.append(ev)
                    now += beat
        else:
            raise ValueError(f"Timesig {m.timesig} not supported")
    voice = mobj.Voice(events)
    voice.setPlay(fade=fade)
    if playpreset:
        voice.setPlay(instr=playpreset, params=playparams)
    return score.Score([voice], scorestruct=struct)


def packInVoices(objs: list[MObj]) -> list[mobj.Voice]:
    """
    Distribute the items across voices
    """
    items = []
    unpitched = []
    for obj in objs:
        assert obj.start is not None and obj.dur is not None, \
            "Only objects with an explict start / duration can be packed"
        r = obj.pitchRange()
        if r is None:
            unpitched.append(r)
        else:
            pitch = (r[0] + r[1]) / 2
            item = packing.Item(obj,
                                offset=Rat(obj.start.numerator, obj.start.denominator),
                                dur=Rat(obj.dur.numerator, obj.dur.denominator),
                                step=pitch)
            items.append(item)
    tracks = packing.packInTracks(items)
    return [mobj.Voice(track.unwrap()) for track in tracks]
