from __future__ import annotations
from emlib import iterlib
from .common import *
from .notation import *
from . import util
import itertools
import logging
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from typing import *

logger = logging.getLogger("maelzel.scoring")


__all__ = (
    'Notation',
    'Part',
    'Arrangement',
    'stackNotations',
    'stackNotationsInPlace',
    'fillSilences',
    'distributeNotationsByClef',
    'packInParts',
    'mergeNotationsIfPossible',
    'durationsCanMerge',
    'notationsCanMerge',
    'fixOverlap',
    'Annotation',
    'NotatedDuration',
)


def durationsCanMerge(n0: Notation, n1: Notation) -> bool:
    """
    True if these Notations can be merged based on duration and start/end

    Two durations can be merged if their sum is regular, meaning
    the sum has a numerator of 1, 2, 3, 4, or 7 (3 means a dotted
    note, 7 a double dotted note) and the denominator is <= 64
    (1/1 being a quarter note)

    Args:
        n0: one Notation
        n1: the other Notation

    Returns:
        True if they can be merged
    """
    dur0 = n0.symbolicDuration()
    dur1 = n1.symbolicDuration()
    sumdur = dur0 + dur1
    num, den = sumdur.numerator, sumdur.denominator
    if den > 64 or num not in {1, 2, 3, 4, 7}:
        return False

    # Allow: r8 8 + 4 = r8 4.
    # Don't allow: r16 8. + 8. r16 = r16 4. r16
    grid = F(1, den)
    if (num == 3 or num == 7) and ((n0.offset % grid) > 0 or (n1.end % grid) > 0):
        return False
    return True


def notationsCanMerge(n0: Notation, n1: Notation) -> bool:
    """
    Returns True if n0 and n1 can me merged

    Two Notations can merge if the resulting duration is regular. A regular
    duration is one which can be represented via **one** notation (a quarter,
    a half, a dotted 8th, a double dotted 16th are all regular durations,
    5/8 of a quarter is not)

    """
    if n0.isRest and n1.isRest:
        return (n0.durRatios == n1.durRatios and
                durationsCanMerge(n0, n1))
    if (
            n0.tiedNext and
            n1.tiedPrev and
            (n0.durRatios == n1.durRatios) and
            (n0.pitches == n1.pitches) and
            (not n1.dynamic or n0.dynamic == n1.dynamic) and
            not n1.annotations and
            not n1.articulation and
            (n1.getNoteheads() == n0.getNoteheads()) and
            not n1.gliss
        ):
        return durationsCanMerge(n0, n1)
    return False



def mergeNotationsIfPossible(notations: list[Notation]) -> list[Notation]:
    """
    Merge the given notations into one, if possible

    If two consecutive notations have same .durRatio and merging them
    would result in a regular note, merge them::

        8 + 8 = q
        q + 8 = q·
        q + q = h
        16 + 16 = 8

    In general::;

        1/x + 1/x     2/x
        2/x + 1/x     3/x  (and viceversa)
        3/x + 1/x     4/x  (and viceversa)
        6/x + 1/x     7/x  (and viceversa)
    """
    assert len(notations) > 1
    out = [notations[0]]
    for n1 in notations[1:]:
        if notationsCanMerge(out[-1], n1):
            out[-1] = out[-1].mergeWith(n1)
        else:
            out.append(n1)
    assert len(out) <= len(notations)
    assert sum(n.duration for n in out) == sum(n.duration for n in notations)
    return out


class Part(list):
    """
    A Part is a list of non-simultaneous events

    Args:
        events: the events (notes, chords) in this track
        label: a label to identify this track in particular (a name)
        groupid: an identification (given by makeGroupId), used to identify
            tracks which belong to a same group
    """
    def __init__(self, events: list[Notation] = None, label: str = '', groupid: str = ''):

        if events:
            super().__init__(events)
        else:
            super().__init__()
        self.groupid: str = groupid
        self.label: str = label
        _fixGlissInPart(self)

    def __getitem__(self, item) -> Notation:
        return super().__getitem__(item)

    def __iter__(self) -> Iterator[Notation]:
        return super().__iter__()

    def __repr__(self) -> str:
        s0 = super().__repr__()
        return "Part"+s0

    def dump(self) -> None:
        for n in self:
            print(n)

    def distributeByClef(self) -> list[Part]:
        """
        Distribute the notations in this Part into multiple parts, based on pitch
        """
        return distributeNotationsByClef(self, groupid=self.groupid)

    def needsMultipleClefs(self) -> bool:
        """
        True if the notations in this Part extend over the range of one clef
        """
        midinotes: list[float] = sum((n.pitches for n in self), [])
        return util.midinotesNeedMultipleClefs(midinotes)

    def stack(self) -> None:
        """
        Stack the notations of this part **in place**.

        Stacking means filling in any unresolved offset/duration of the notations
        in this part. After this operation, all Notations in this Part have an
        explicit duration and start. See :meth:`stacked` for a version which
        returns a new Part instead of operating in place
        """
        stackNotationsInPlace(self)

    def meanPitch(self) -> float:
        """
        The mean pitch of this part, weighted by the duration of each pitch

        Returns:
            a float representing the mean pitch as midinote
        """
        pitch, dur = 0., 0.
        for n in self:
            if n.isRest:
                continue
            dur = n.duration or 1.
            pitch += n.meanPitch() * dur
            dur += dur
        return pitch / dur

    def fillGaps(self, mingap=1/64) -> None:
        """
        Fill gaps between notations in this Part, in place
        """
        if not self.hasGaps():
            return
        newevents = fillSilences(self, mingap=mingap, offset=0)
        self.clear()
        self.extend(newevents)
        assert not self.hasGaps()

    def hasGaps(self) -> bool:
        """Does this Part have gaps?"""
        assert all(n.offset is not None and n.duration is not None for n in self)
        return any(n0.end < n1.offset for n0, n1 in iterlib.pairwise(self))

    def stacked(self) -> Part:
        """
        Stack the Notations to make them adjacent if they have unset offset/duration

        Similar to :meth:`stack`, **this method returns a new Part** instead of
        operating in place.
        """
        notations = stackNotations(self)
        return Part(notations, label=self.label, groupid=self.groupid)


class Arrangement(list):
    """
    An Arrangement is a list of Parts
    """
    def __init__(self, parts: list[Part] = None, title: str = ''):
        if parts:
            super().__init__(parts)
        else:
            super().__init__()
        self.title = title


def _fixGlissInPart(notations: list[Notation]):
    """
    Removes superfluous end glissandi notes **in place**

    To be called after notations are "stacked". Removes superfluous
    end glissandi notes when the endgliss is the same as the next note
    """
    toBeRemoved = []
    for n0, n1, n2 in iterlib.window(notations, 3):
        if n0.gliss and n1.isGraceNote and n1.pitches == n2.pitches:
            toBeRemoved.append(n1)
    for item in toBeRemoved:
        notations.remove(item)
    stackNotationsInPlace(notations)


def stackNotationsInPlace(events: list[Notation], start=F(0), overrideOffset=False
                          ) -> None:
    """
    Stacks notations to the left, in place

    Stacks events together by placing an event at the end of the
    previous event whenever an event does not define its own offset

    Args:
        events: a list of Notations (or a Part)
        start: the start time, will override the offset of the first event
        overrideOffset: if True, offsets are overriden even if they are defined
    """
    if all(ev.offset is not None and ev.duration is not None for ev in events):
        return
    now = offset if (offset:=events[0].offset) is not None else start if start is not None else F(0)
    assert now is not None and now >= 0
    lasti = len(events)-1
    for i, ev in enumerate(events):
        if ev.offset is None or overrideOffset:
            assert ev.duration is not None
            ev.offset = now
        elif ev.duration is None:
            if i == lasti:
                raise ValueError("The last event should have a duration")
            ev.duration = events[i+1].offset - ev.offset
        now += ev.duration
    for ev1, ev2 in iterlib.pairwise(events):
        assert ev1.offset <= ev2.offset
    fixOverlap(events)


def stackNotations(events: list[Notation], start=F(0), overrideOffset=False
                   ) -> list[Notation]:
    """
    Stacks Notations to the left, returns the new notations

    This function stacks events together by placing an event at the end of the
    previous event whenever an event does not define its own offset, or sets
    the duration of an event if events are specified via offset alone

    Args:
        events: a list of notations
        start: the start time, will override the offset of the first event
        overrideOffset: if True, offsets are overriden even if they are defined

    Returns:
        a list of stacked events
    """
    if all(ev.offset is not None and ev.duration is not None for ev in events):
        return events
    assert all(ev.offset is not None or ev.duration is not None for ev in events)
    now = events[0].offset if events[0].offset is not None else start
    assert now is not None and now >= 0
    out = []
    lasti = len(events) - 1
    for i, ev in enumerate(events):
        if ev.offset is None or overrideOffset:
            assert ev.duration is not None
            ev = ev.clone(offset=now, duration=ev.duration)
        elif ev.duration is None:
            if i == lasti:
                raise ValueError("The last event should have a duration")
            ev = ev.clone(duration=events[i+1].offset - ev.offset)
        now += ev.duration
        out.append(ev)
    for ev1, ev2 in iterlib.pairwise(out):
        assert ev1.offset <= ev2.offset
    fixOverlap(out)
    return out


def fixOverlap(notations: list[Notation], mingap=F(1, 10000)) -> None:
    """
    Fix overlap between notations, in place.

    If two notations overlap, the first notation is cut, preserving the
    offset of the second notation. If there is a gap smaller than a given
    threshold the end of the note left of the gap is extended to match
    the offset of the note right of the gap

    Args:
        notations: the notations to fix
        mingap: min. gap to allow between notations. Any gap smaller than
            this will be removed, growing the previous notation to fill
            the gap (the start times are left unmodified)

    Returns:
        the fixed notations
    """
    if len(notations) < 2:
        return
    for n0, n1 in iterlib.pairwise(notations):
        assert n0.duration is not None and n0.offset is not None
        assert n1.offset is not None
        assert n0.offset <= n1.offset, "Notes are not sorted!"
        if n0.end > n1.offset or n1.offset - n0.end < mingap:
            n0.duration = n1.offset - n0.offset
            assert n0.end == n1.offset


def fillSilences(notations: list[Notation], mingap=1/64, offset: time_t = None
                 ) -> list[Notation]:
    """
    Return a list of Notations filled with rests

    Args:
        notations: the notes to fill
        mingap: min. gap between two notes. If any notes differ by less
                   than this, the first note absorvs the gap
        offset: if given, marks the start time to fill. If notations start after
            this offset a rest will be crated from this offset to the start
            of the first notation
    Returns:
        a list of new Notations
    """
    assert notations
    assert all(isinstance(n, Notation) and n.offset is not None and n.duration is not None
               for n in notations)
    if offset is not None:
        assert all(n.offset >= offset for n in notations
                   if n.offset is not None)

    out: list[Notation] = []
    n0 = notations[0]
    if offset is not None and n0.offset is not None and n0.offset > offset:
        out.append(makeRest(duration=n0.offset, offset=offset))
    for ev0, ev1 in iterlib.pairwise(notations):
        gap = ev1.offset - (ev0.offset + ev0.duration)
        assert gap >= 0, f"negative gap! = {gap}"
        if gap > mingap:
            out.append(ev0)
            rest = makeRest(duration=gap, offset=ev0.offset+ev0.duration)
            assert rest.offset is not None and rest.duration is not None
            out.append(rest)
        else:
            # adjust the dur of n0 to match start of n1
            out.append(ev0.clone(duration=ev1.offset - ev0.offset))
    out.append(notations[-1])
    assert all(n0.end == n1.offset for n0, n1 in iterlib.pairwise(out)), out
    return out


def _groupById(notations: list[Notation]) -> list[Union[Notation, list[Notation]]]:
    """
    Given a seq. of events, elements which are grouped together are wrapped
    in a list, whereas elements which don't belong to any group are
    appended as is

    """
    out: list[Union[Notation, list[Notation]]] = []
    for groupid, elementsiter in itertools.groupby(notations, key=lambda n: n.groupid):
        if not groupid:
            out.extend(elementsiter)
        else:
            elements = list(elementsiter)
            elements.sort(key=lambda elem: elem.offset or 0)
            out.append(elements)
    return out


def _pitchToClef(pitch: float, splitPoint=60) -> str:
    if splitPoint <= pitch <= 93:
        return 'g'
    elif 93 < pitch:
        return '15a'
    else:
        return 'f'


def distributeNotationsByClef(notations: list[Notation], filterRests=False, groupid=None
                              ) -> list[Part]:
    """
    Split the notations into parts

    Assuming that events are not simultanous, split the events into
    different Parts if the range makes it necessary, where each
    Part can be represented without clef changes. We don't enforce that the
    notations are not simultaneous within a part

    Args:
        notations: the events to split
        groupid: if given, this id will be used to identify the
            generated tracks (see makeGroupId)

    Returns:
         list of Parts (between 1 and 3, one for each clef)
    """
    parts = {'g': [], 'f': [], '15a': []}
    lowPitch = 0.
    lowPitch = sum(60 - p for n in notations for p in n.pitches if p <= 60)
    splitPoint = 60 if lowPitch > 8 else 56
    lastj = len(notations) - 1
    for j, notation in enumerate(notations):
        assert notation.offset is not None
        if notation.isRest:
            if filterRests:
                continue
            parts['g'].append(notation)
        elif len(notation) == 1:
            pitch = notation.pitches[0]
            clef = _pitchToClef(pitch, splitPoint)
            parts[clef].append(notation)
        else:
            # a chord
            chords = {'g': [], 'f': [], '15a': []}
            noteheads = notation.getNoteheads()
            matchNext = j < lastj and notation.gliss
            for i, pitch in enumerate(notation.pitches):
                clef = notation.getClefHint(i)
                if not clef:
                    clef = _pitchToClef(pitch, splitPoint)
                chords[clef].append(i)
                if j < lastj and matchNext:
                    notations[j+1].setClefHint(clef, i)
            for clef, chord in chords.items():
                if chord:
                    partialChord = notation.clone(pitches=[notation.notename(i) for i in chord],
                                                  notehead=[noteheads[i] for i in chord])
                    parts[clef].append(partialChord)

    # groupid = groupid or makeGroupId()
    # parts = [Part(part, groupid=groupid, label=name)
    #           for part, name in ((G15a, "G15a"), (G, "G"), (F, "F")) if part]
    parts = [Part(part) for part in parts.values() if part]
    return parts


def packInParts(notations: list[Notation], maxrange=36,
                keepGroupsTogether=True) -> list[Part]:
    """
    Pack a list of possibly simultaneous notations into tracks

    The notations within one track are NOT simulatenous. Notations belonging
    to the same group are kept in the same track.

    Args:
        notations: the Notations to pack
        maxrange: the max. distance between the highest and lowest Notation
        keepGroupsTogether: if True, items belonging to a same group are
            kept in a same track

    Returns:
        a list of Parts

    """
    from maelzel import packing
    items = []
    groups = _groupById(notations)
    for group in groups:
        if isinstance(group, Notation):
            n = group
            if not n.isRest:
                assert n.offset is not None and n.duration is not None
                items.append(packing.Item(obj=n, offset=n.offset,
                                          dur=n.duration, step=n.meanPitch()))
        else:
            assert isinstance(group, list)
            if keepGroupsTogether:
                dur = (max(n.end for n in group if n.end is not None) -
                       min(n.offset for n in group if n.offset is not None))
                step = sum(n.meanPitch() for n in group)/len(group)
                item = packing.Item(obj=group, offset=group[0].offset or 0, dur=dur, step=step)
                items.append(item)
            else:
                items.extend(packing.Item(obj=n, offset=n.offset or 0, dur=n.duration or 1,
                                          step=n.meanPitch())
                             for n in group)

    packedTracks = packing.packInTracks(items, maxAmbitus=maxrange)
    return [Part(track.unwrap()) for track in packedTracks]


def removeRedundantDynamics(notations: list[Notation],
                            resetAfterRest=True,
                            minRestDuration: time_t = F(1, 16)) -> None:
    """
    Removes redundant dynamics, in place

    A dynamic is redundant if it is the same as the last dynamic. It is
    possible to force a dynamic by adding a ``!`` sign to the dynamic
    (pp!)

    Args:
        notations: the notations to remove redundant dynamics from
        resetAfterRest: if True, any dynamic after a rest is not considered
            redundant
        minRestDuration: the min. duration of a rest to reset dynamic, in quarternotes
    """
    lastDynamic = ''
    for n in notations:
        if n.tiedPrev:
            continue
        if n.isRest:
            if resetAfterRest and n.duration > minRestDuration:
                lastDynamic = ''
        elif n.dynamic:
            if n.dynamic[-1] == '!':
                lastDynamic = n.dynamic[:-1]
            elif n.dynamic == lastDynamic:
                n.dynamic = ''
            else:
                lastDynamic = n.dynamic

