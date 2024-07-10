from __future__ import annotations

import re
import sys

from maelzel.scorestruct import ScoreStruct
from maelzel.common import F, asF, F0
from maelzel import _util
from maelzel.core.config import CoreConfig
from .mobj import MObj, MContainer
from .event import MEvent, asEvent, Note, Chord
from .workspace import Workspace
from .synthevent import PlayArgs, SynthEvent
from . import symbols
from . import environment
from . import presetmanager
from . import _mobjtools
from . import _tools
from ._common import UNSET, _Unset, logger

from maelzel import scoring
from maelzel.colortheory import safeColors

from emlib import iterlib

from typing import TYPE_CHECKING, overload
if TYPE_CHECKING:
    from typing_extensions import Self
    from typing import Any, Iterable, Iterator, Callable, Sequence
    from ._typedefs import time_t, location_t, num_t, beat_t


__all__ = (
    'Chain',
    'Voice',
)


def _stackEvents(events: list[MEvent | Chain],
                 explicitOffsets=True,
                 ) -> F:
    """
    Stack events to the left **inplace**, making any unset offset explicit

    Args:
        events: the events to modify
        explicitOffsets: if True, all offsets are made explicit, recursively

    Returns:
        the accumulated duration of all events

    """
    # All offset times given in the events are relative to the start of the chain
    now = F(0)
    for ev in events:
        if ev.offset is not None:
            now = ev.offset
        elif explicitOffsets:
            ev.offset = now

        ev._resolvedOffset = now
        if isinstance(ev, MEvent):
            if ev.dur is None:
                raise ValueError(f"event has None duration: {ev=}")
            now += ev.dur
        else:
            # A Chain
            stackeddur = _stackEvents(ev.items, explicitOffsets=explicitOffsets)
            ev._dur = stackeddur
            now = ev._resolvedOffset + stackeddur
    return now


def _removeRedundantOffsets(items: list[MEvent | Chain],
                            frame=F(0)
                            ) -> tuple[F, bool]:
    """
    Remove over-secified start times in this Chain

    Args:
        items: the items to process
        frame: the frame of reference

    Returns:
        a tuple (total duration of *items*, True if items was modified)

    """
    # This is the relative position (independent of the chain's start)
    now = frame
    modified = False
    for i, item in enumerate(items):
        if (itemoffset := item._detachedOffset()) is not None:
            absoffset = itemoffset + frame
            if absoffset == now and (i == 0 or items[i-1].dur is not None):
                if item.offset is not None:
                    modified = True
                    item.offset = None
            elif absoffset < now:
                raise ValueError(f"Items overlap: {item} (offset={_util.showT(absoffset)}) "
                                 f"starts before current time ({_util.showT(now)})")
            else:
                now = absoffset

        if isinstance(item, MEvent):
            now += item.dur
        elif isinstance(item, Chain):
            dur, submodified = _removeRedundantOffsets(item.items, frame=now)
            if submodified:
                item._changed()
                modified = True
            now += dur
        else:
            raise TypeError(f"Expected a Note, Chord or Chain, got {item}")

    return now - frame, modified


class Chain(MContainer):
    """
    A Chain is a sequence of Notes, Chords or other Chains

    Args:
        items: the items of this Chain. The start time of any object, if given, is
            interpreted as relative to the start of the chain.
        offset: offset of the chain itself relative to its parent
        label: a label for this chain
        properties: any properties for this chain. Properties can be anything,
            they are a way for the user to attach data to an object
    """

    __slots__ = ('items', '_modified', '_cachedEventsWithOffset', '_postSymbols',
                 '_absOffset')

    def __init__(self,
                 items: Sequence[MEvent | Chain | str] | str | None = None,
                 offset: time_t = None,
                 label: str = '',
                 properties: dict[str, Any] = None,
                 parent: MContainer = None,
                 _init=True):
        if isinstance(items,  str):
            _init = True

        if _init:
            if offset is not None:
                offset = offset if isinstance(offset, F) else asF(offset)
            if items is not None:
                if isinstance(items, str):
                    # split using new lines and semicolons as separators
                    items = _tools.regexSplit('[\n;]', items, strip=True, removeEmpty=True)
                    items = [_tools.stripNoteComments(item) for item in items]
                    items = [asEvent(item) for item in items if item if item]
                else:
                    items = [item if isinstance(item, (MEvent, Chain)) else asEvent(item)
                             for item in items]

        if items is None:
            items = []
        elif items:
            if not isinstance(items, list):
                items = list(items)
            for item in items:
                if item.parent is not None:
                    # We need to make a copy in this case
                    item = item.copy()
                item.parent = self
        assert isinstance(items, list)
        super().__init__(offset=offset, dur=F0, label=label,
                         properties=properties, parent=parent)

        self.items: list[MEvent | Chain] = items
        """The items in this chain, a list of events of other chains"""

        self._modified = bool(items)
        self._cachedEventsWithOffset: list[tuple[MEvent, F]] | None = None

        self._postSymbols: list[tuple[time_t, symbols.Symbol]] = []
        """Symbols to apply a posteriory. """

        self._absOffset: F | None = None
        """Cached absolute offset"""

        self._hasRedundantOffsets = True
        """Assume redundant offsets at creation"""

    def _check(self):
        for item in self.items:
            assert item.parent is self
            if isinstance(item, Chain):
                item._check()

    def __hash__(self):
        items = [type(self).__name__, self.label, self.offset, len(self.items)]
        if self.symbols:
            items.extend(self.symbols)
        if self._postSymbols:
            items.extend(self._postSymbols)
        items.extend(self.items)
        out = hash(tuple(items))
        return out

    def clone(self,
              items: Sequence[MEvent | Chain] | None = None,
              offset: time_t | None | _Unset = UNSET,
              label: str | None = None,
              properties: dict | None = None
              ) -> Self:
        # parent is not cloned
        out = self.__class__(items=self.items if items is None else items,
                             offset=self.offset if offset is UNSET else asF(offset),
                             label=self.label if label is None else label,
                             _init=False)
        self._copyAttributesTo(out)
        return out

    def __copy__(self) -> Self:
        out = self.__class__(self.items.copy(), offset=self.offset, label=self.label,
                             properties=self.properties, _init=False)
        self._copyAttributesTo(out)
        return out

    def __deepcopy__(self, memodict={}) -> Self:
        items = [item.copy() for item in self.items]
        out = self.__class__(items=items, offset=self.offset, label=self.label, _init=False)
        self._copyAttributesTo(out)
        out._check()
        return out

    def copy(self) -> Self:
        return self.__deepcopy__()

    def stack(self) -> F:
        """
        Stack events to the left **INPLACE**, making offsets explicit

        Returns:
            the total duration of self
        """
        dur = _stackEvents(self.items, explicitOffsets=True)
        self._dur = dur
        self._changed()
        return dur

    def fillGaps(self, recurse=True) -> None:
        """
        Fill any gaps with rests, inplace

        A gap is produced when an event within a chain has an explicit offset
        later than the offset calculated by stacking the previous objects in terms
        of their duration

        Args:
            recurse: if True, fill gaps within subchains
        """
        self._update()
        now = F(0)
        items = []
        changed = False
        for item in self.items:
            if item.offset is not None and item.offset > now:
                gapdur = item.offset - now
                r = Note.makeRest(gapdur)
                items.append(r)
                now += gapdur
                changed = True
            items.append(item)
            if isinstance(item, Chain) and recurse:
                item.fillGaps(recurse=True)
            now += item.dur
        self.items = items
        if changed:
            self._changed()

    def nextItem(self, item: MEvent | Chain) -> MEvent | Chain | None:
        """
        Returns the next item after *item*

        An item can be an event (note, chord) or another chain

        Args:
            item: the item to find its next item

        Returns:
            the item following *item* or None if the given item is not
            in this container, or it has no item after it

        Example
        ~~~~~~~

            >>> from maelzel.core import *
            >>> chain = Chain(['4C', '4D', Chain(['4E', '4F'])])
            >>> chain.eventAfter(chain[1])
            4E
            >>> chain.itemAfter(chain[1])
            Chain([4E, 4F])

        """
        idx = self.items.index(item)
        return self.items[idx + 1] if idx < len(self.items) - 2 else None

    def nextEvent(self, event: MEvent) -> MEvent | None:
        """
        Returns the next event after *event*

        Example
        ~~~~~~~

            >>> from maelzel.core import *
            >>> chain = Chain(['4C', '4D', Chain(['4E', '4F'])])
            >>> chain.eventAfter(chain[1])
            4E
            >>> chain.itemAfter(chain[1])
            Chain([4E, 4F])
        """
        idx = self.items.index(event)
        if idx >= len(self.items) - 1:
            return None
        nextitem = self.items[idx+1]
        return nextitem if isinstance(nextitem, MEvent) else nextitem.firstEvent()

    def previousItem(self, item: MEvent | Chain) -> MEvent | Chain | None:
        """
        Returns the item (an event or a chain) previous to the given one

        Args:
            item: the item to query.

        Returns:
            the item previous to *item*

        .. seealso:: :meth:`Chain.previousEvent`
        """
        try:
            idx = self.items.index(item)
            return None if idx == 0 else self.items[idx - 1]
        except ValueError as e:
            raise ValueError(f"The item {item} is not a part of {self}")

    def previousEvent(self, event: MEvent) -> MEvent | None:
        """
        Returns the event before the given event

        Args:
            event: the event to query

        Returns:
            the event before the given event, or None if no event is found. Raises
            ValueError if event is not part of this container

        """
        try:
            idx = self.items.index(event)
            if idx == 0:
                # This is the first event, so no previous event
                return None
        except ValueError as e:
            raise ValueError(f"event {event} not part of {self}")
        previtem = self.items[idx - 1]
        return previtem if isinstance(previtem, MEvent) else previtem.lastEvent()

    def isFlat(self) -> bool:
        """
        Is self flat?

        A flat chain/voice contains only events, not other containers
        """
        return all(isinstance(item, MEvent) for item in self.items)

    def flatEvents(self) -> list[MEvent]:
        """
        A list of flat events, with explicit absolute offsets set

        The returned events are a clone of the events in this chain,
        not the actual events themselves

        Returns:
            a list of events (Notes, Chords, Clips, ...) with explicit
            offset
        """
        if not self.items:
            return []
        self._update()
        flatitems = [ev.clone(offset=evoffset) if ev.offset != evoffset else ev
                     for ev, evoffset in self.eventsWithOffset()]
        _resolveGlissandi(flatitems)
        return flatitems

    def flat(self, forcecopy=False) -> Self:
        """
        A flat version of this Chain

        A Chain can contain other Chains. This method flattens all objects inside
        this Chain and any sub-chains to a flat chain of events (notes/chords/clips).

        If this Chain is already flat (it does not contain any
        Chains), self is returned unmodified (unless forcecopy=True).

        .. note::

            All items in the returned Chain will have an explicit ``.offset`` attribute.
            To remove any redundant .offset call :meth:`Chain.removeRedundantOffsets`

        Args:
            forcecopy: all items in the returned Chain are a copy of self, even if
                self is already flat

        Returns:
            a flat chain

        .. seealso:: :meth:`Chain.isFlat`
        """
        self._update()

        if all(isinstance(item, MEvent) for item in self.items) and not forcecopy and self.hasOffsets():
            return self

        flatevents = self.eventsWithOffset()
        ownoffset = self.absOffset()
        events = []
        if ownoffset == F0:
            for ev, offset in flatevents:
                if ev.offset == offset and not forcecopy:
                    events.append(ev)
                else:
                    events.append(ev.clone(offset=offset))
        else:
            for ev, offset in flatevents:
                events.append(ev.clone(offset=offset - ownoffset))
        return self.clone(items=events)

    def pitchRange(self) -> tuple[float, float] | None:
        pitchRanges = [pitchrange for item in self.items
                       if (pitchrange := item.pitchRange()) is not None]
        if not pitchRanges:
            return None
        return min(p[0] for p in pitchRanges), max(p[1] for p in pitchRanges)

    def meanPitch(self):
        items = [item for item in self.items if not item.isRest()]
        gracenoteDur = F(1, 16)
        pitches = [item.meanPitch() for item in items]
        durs = [max(item.dur, gracenoteDur) for item in items]
        return float(sum(pitch * dur for pitch, dur in zip(pitches, durs)) / sum(durs))

    def withExplicitOffset(self, forcecopy=False) -> Self:
        """
        Copy of self with explicit offset

        If self already has explicit offset, self itself
        is returned.

        Args:
            forcecopy: if forcecopy, a copy of self will be returned even
                if self already has explicit times

        Returns:
            a clone of self with explicit times

        Example
        ~~~~~~~

        The offset and dur shown as the first two columns are the resolved
        times. When an event has an explicit offset, these are
        shown as part of the event repr. See for example the second note, 4C,
        which in the first version does not have any explicit times and is shown
        as "4C" and in the second version it appears as "4C:2.5♩:offset=0.5"

            >>> from maelzel.core import *
            >>> chain = Chain([Rest(0.5), Note("4C"), Chord("4D 4E", offset=3)])
            >>> chain.dump()
            Chain
              offset: 0      dur: 0.5    | Rest:0.5♩
              offset: 0.5    dur: 2.5    | 4C
              offset: 3      dur: 1      | ‹4D 4E offset=3›
            >>> chain.withExplicitTimes().dump()
            Chain
              offset: 0      dur: 0.5    | Rest:0.5♩:offset=0
              offset: 0.5    dur: 2.5    | 4C:2.5♩:offset=0.5
              offset: 3      dur: 1      | ‹4D 4E 1♩ offset=3›


        """
        if self.hasOffsets() and not forcecopy:
            return self
        out = self.copy()
        out.stack()
        return out

    def hasOffsets(self) -> bool:
        """
        True if self has an explicit offset and all items as well (recursively)

        Returns:
            True if all items in self have explicit offsets
        """
        if self.offset is None:
            return False

        return all(item.offset is not None if isinstance(item, MEvent) else item.hasOffsets()
                   for item in self.items)

    def _resolveGlissandi(self, force=False) -> None:
        """
        Set the _glissTarget attribute with the pitch of the gliss target
        if a note or chord has an unset gliss target

        Args:
            force: if True, calculate/update all glissando targets

        """
        _resolveGlissandi(self.recurse(), force=force)
        return

    def _synthEvents(self,
                     playargs: PlayArgs,
                     parentOffset: F,
                     workspace: Workspace
                     ) -> list[SynthEvent]:
        # TODO: add playback for crescendi (hairpins)
        conf = workspace.config
        if self.playargs:
            # We don't include the chain's automations since these are added
            # later, after events have been merged.
            playargs = playargs.updated(self.playargs, automations=False)

        flatitems = self.flatEvents()
        assert all(item.offset is not None and item.dur >= 0 for item in flatitems)
        if self.offset:
            for item in flatitems:
                item.offset += self.offset

        if any(n.isGracenote() for n in flatitems):
            gracenoteDur = F(conf['play.gracenoteDuration'])
            _mobjtools.addDurationToGracenotes(flatitems, gracenoteDur)

        if conf['play.useDynamics']:
            _mobjtools.fillTempDynamics(flatitems, initialDynamic=conf['play.defaultDynamic'])

        synthevents = []
        offset = parentOffset + self.relOffset()
        groups = _mobjtools.groupLinkedEvents(flatitems)
        for item in groups:
            if isinstance(item, MEvent):
                events = item._synthEvents(playargs,
                                           parentOffset=offset,
                                           workspace=workspace)
                synthevents.extend(events)
            elif isinstance(item, list):
                synthgroups = [event._synthEvents(playargs, parentOffset=offset, workspace=workspace)
                               for event in item]
                synthlines = _splitSynthGroupsIntoLines(synthgroups)
                for synthline in synthlines:
                    if isinstance(synthline, SynthEvent):
                        synthevent = synthline
                    elif isinstance(synthline, list):
                        if len(synthline) == 1:
                            synthevent = synthline[0]
                        else:
                            synthevent = SynthEvent.mergeEvents(synthline)
                    else:
                        raise TypeError(f"Expected a SynthEvent or a list thereof, got {synthline}")
                    synthevents.append(synthevent)
                    # TODO: fix / add playargs
            else:
                raise TypeError(f"Did not expect {item}")

        if self.playargs and self.playargs.automations:
            scorestruct = self.scorestruct() or workspace.scorestruct
            for automation in self.playargs.automations:
                startsecs, endsecs = automation.absTimeRange(parentOffset=offset, scorestruct=scorestruct)
                for ev in synthevents:
                    overlap0, overlap1 = _util.overlap(float(startsecs), float(endsecs), ev.delay, ev.end)
                    if overlap0 > overlap1:
                        continue
                    preset = presetmanager.presetManager.getPreset(ev.instr)
                    if automation.param in preset.dynamicParams(aliases=True, aliased=True):
                        synthautom = automation.makeSynthAutomation(scorestruct=scorestruct, parentOffset=offset)
                        ev.addAutomation(synthautom.cropped(overlap0, overlap1))

        return synthevents

    def mergeTiedEvents(self) -> None:
        """
        Merge tied events **inplace**

        Two events can be merged if they are tied and the second
        event does not provide any extra information (does not have
        an individual amplitude, dynamic, does not start a gliss, etc.)

        Returns:
            True if self was modified
        """
        out = []
        last = None
        lastidx = len(self.items) - 1
        modified = False
        for i, item in enumerate(self.items):
            if isinstance(item, Chain):
                item.mergeTiedEvents()
                out.append(item)
                last = None
            elif last is not None and type(last) == type(item):
                merged = last.mergeWith(item)
                if merged is None:
                    if last is not None:
                        out.append(last)
                    last = item
                else:
                    if i < lastidx:
                        last = merged
                    else:
                        out.append(merged)
                    modified = True

            else:
                if last is not None:
                    out.append(last)
                last = item
                if i == lastidx:
                    out.append(item)
        self.items = out
        if modified:
            self._changed()

    def childOffset(self, child: MObj) -> F:
        """
        Returns the offset of child within this chain

        raises ValueError if self is not a parent of child

        Args:
            child: the object whose offset is to be determined

        Returns:
            The offset of this child within this chain
        """
        if not any(item is child for item in self.items):
            raise ValueError(f"The item {child} is not a child of {self}")

        if child.offset is not None:
            return child.offset

        self._update()
        return child._resolvedOffset

    @property
    def dur(self) -> F:
        """The duration of this sequence"""
        if not self._modified:
            return self._dur

        if not self.items:
            self._dur = F(0)
            return self._dur

        self._update()
        # assert self._dur is not None
        return self._dur

    @dur.setter
    def dur(self, value):
        raise AttributeError(f"Objects of class {type(self).__name__} cannot set their duration")

    def append(self, item: MEvent) -> None:
        """
        Append an item to this chain

        Args:
            item: the item to add
        """
        item.parent = self
        self.items.append(item)
        self._changed()

    def extend(self, items: list[MEvent]) -> None:
        """
        Extend this chain with items

        Args:
            items: a list of items to append to this chain

        .. note::

            Items passed are marked as children of this chain (their *.parent* attribute
            is modified)
        """
        for item in items:
            item.parent = self
        self.items.extend(items)
        self._changed()

    def _update(self):
        if not self._modified and self._dur > 0:
            return
        self._dur = _stackEvents(self.items, explicitOffsets=False)
        self._resolveGlissandi()
        self._modified = False
        self._absOffset = None
        self._hasRedundantOffsets = True

    # def absOffset(self) -> F:
    #     self._update()
    #     if self._absOffset:
    #         return self._absOffset
    #     self._absOffset = offset = super().absOffset()
    #     return offset

    def _changed(self) -> None:
        if self._modified:
            return
        self._modified = True
        self._dur = None
        self._cachedEventsWithOffset = None
        self._absOffset = None
        if self.parent:
            self.parent._childChanged(self)

    def _childChanged(self, child: MObj) -> None:
        if not self._modified:
            self._changed()

    def __len__(self) -> int:
        return len(self.items)

    def __iter__(self) -> Iterator[MEvent | Chain]:
        return iter(self.items)

    @overload
    def __getitem__(self, idx: int) -> MEvent: ...

    @overload
    def __getitem__(self, idx: slice) -> list[MEvent | Chain]: ...

    def __getitem__(self, idx):
        if isinstance(idx, int):
            return self.items[idx]
        else:
            return self.items.__getitem__(idx)

    def _dumpRows(self, indents=0, now=F(0), forcetext=False) -> list[str]:
        fontsize = '85%'
        IND = '  '
        selfstart = f"{float(self.offset):.3g}" if self.offset is not None else 'None'
        namew = max((sum(len(n.name) for n in event.notes) + len(event.notes)
                     for event in self.recurse()
                     if isinstance(event, Chord)),
                    default=10)

        widths = {
            'location': 10,
            'beat': 7,
            'offset': 12,
            'dur': 12,
            'name': namew,
            'gliss': 6,
            'dyn': 5,
            'playargs': 20,
            'info': 20
        }

        struct = self.scorestruct() or Workspace.active.scorestruct

        if environment.insideJupyter and not forcetext:
            r = type(self).__name__

            header = (f'<code><span style="font-size: {fontsize}">{IND*indents}<b>{r}</b> - '
                      f'beat: {_util.showT(self.absOffset())}, offset: {selfstart}, '
                      f'dur: {_util.showT(self.dur)}'
                      )
            if self.label:
                header += f', label: {self.label}'
            header += '</span></code>'
            rows = [header]
            columnparts = [IND*(indents+1)]
            for k, width in widths.items():
                columnparts.append(k.ljust(width))
            columnnames = ''.join(columnparts)
            row = f"<code>{_tools.htmlSpan(columnnames, ':grey1', fontsize=fontsize)}</code>"
            rows.append(row)

            items, itemsdur = self._iterateWithTimes(recurse=False, frame=F(0))
            for item, itemoffset, itemdur in items:
                infoparts = []
                assert isinstance(item, (MEvent, Chain))
                if item.label:
                    infoparts.append(f'label: {item.label}')
                if item.properties:
                    infoparts.append(f'properties: {item.properties}')

                if isinstance(item, MEvent):
                    name = item.name
                    if isinstance(item, (Note, Chord)) and item.tied:
                        name += "~"
                    if item.offset is not None:
                        offsetstr = _util.showT(item.offset)
                    else:
                        offsetstr = f'({_util.showT(itemoffset)})'
                    offsetstr = offsetstr.ljust(widths['dur'])
                    durstr = _util.showT(item.dur).ljust(widths['dur'])
                    measureidx, measurebeat = struct.beatToLocation(now + itemoffset)
                    locationstr = f'{measureidx}:{_util.showT(measurebeat)}'.ljust(widths['location'])
                    playargs = 'None' if not item.playargs else ', '.join(f'{k}={v}' for k, v in item.playargs.db.items())
                    if isinstance(item, (Note, Chord)):
                        glissstr = 'F' if not item.gliss else f'T ({item.resolveGliss()})' if isinstance(item.gliss, bool) else str(item.gliss)
                    else:
                        glissstr = '-'
                    rowparts = [IND*(indents+1),
                                locationstr,
                                _util.showT(now + itemoffset).ljust(widths['beat']),
                                offsetstr,
                                durstr,
                                name.ljust(widths['name']),
                                glissstr.ljust(widths['gliss']),
                                str(item.dynamic).ljust(widths['dyn']),
                                playargs.ljust(widths['playargs']),
                                ' '.join(infoparts) if infoparts else '-'
                                ]
                    row = f"<code>{_tools.htmlSpan(''.join(rowparts), ':blue1', fontsize=fontsize)}</code>"
                    rows.append(row)
                    if item.symbols:
                        row = f"<code>      {_tools.htmlSpan(str(item.symbols), ':green2', fontsize=fontsize)}</code>"
                        rows.append(row)

                elif isinstance(item, Chain):
                    rows.extend(item._dumpRows(indents=indents+1, now=now+itemoffset, forcetext=forcetext))
            return rows
        else:
            rows = [f"{IND * indents}Chain -- beat: {self.absOffset()}, offset: {selfstart}, dur: {self.dur}",
                    f'{IND * (indents + 1)}beat   offset  dur    item']
            items, itemsdur = self._iterateWithTimes(recurse=False, frame=F(0))
            for item, itemoffset, itemdur in items:
                if isinstance(item, MEvent):
                    rows.append(f'{IND * (indents+1)}'
                                f'{repr(now + itemoffset).ljust(7)}'
                                f'{repr(itemoffset).ljust(7)} '
                                f'{repr(itemdur).ljust(7)}'
                                f'{item}')
                elif isinstance(item, Chain):
                    rows.extend(item._dumpRows(indents=indents+1, forcetext=forcetext, now=now+itemoffset))
                else:
                    raise TypeError(f"Expected an MEvent or a Chain, got {item}")
            return rows

    def dump(self, indents=0, forcetext=False) -> None:
        """
        Dump this chain, recursively

        Values inside parenthesis are implicit. For example if an object inside
        this chain does not have an explicit .offset, its withExplicitTimes offset will
        be shown within parenthesis

        Args:
            indents: the number of indents to use
            forcetext: if True, force print output instea of html, even when running
                inside jupyter
        """
        self._update()
        rows = self._dumpRows(indents=indents, now=self.offset or F(0), forcetext=forcetext)
        if environment.insideJupyter and not forcetext:
            html = '<br>'.join(rows)
            from IPython.display import HTML, display
            display(HTML(html))
        else:
            for row in rows:
                print(row)

    def __repr__(self):
        if len(self.items) < 10:
            itemstr = ", ".join(repr(_) for _ in self.items)
        else:
            itemstr = ", ".join(repr(_) for _ in self.items[:10]) + ", …"
        cls = self.__class__.__name__
        namedargs = []
        if self.offset is not None:
            namedargs.append(f'offset={self.offset}')
        if namedargs:
            info = ', ' + ', '.join(namedargs)
        else:
            info = ''
        return f'{cls}([{itemstr}]{info})'

    def _repr_html_header(self) -> str:
        self._update()
        itemcolor = safeColors['blue2']
        items = self.items if len(self.items) < 10 else self.items[:10]
        itemstr = ", ".join(f'<span style="color:{itemcolor}">{repr(_)}</span>'
                            for _ in items)
        if len(self.items) >= 10:
            itemstr += ", …"
        cls = self.__class__.__name__
        namedargs = [f'dur={_util.showT(self.dur)}']
        if self.offset:
            namedargs.append(f'offset={_util.showT(self.offset)}')
        info = ', ' + ', '.join(namedargs)
        return f'{cls}([{itemstr}]{info})'

    def removeRedundantOffsets(self) -> None:
        """
        Remove over-specified start times in this Chain (in place)
        """
        # This is the relative position (independent of the chain's start)
        if not self._hasRedundantOffsets and not self._modified:
            return

        self._update()

        _, modified = _removeRedundantOffsets(self.items, frame=F(0))
        if self.offset == F0:
            self.offset = None
            modified = True
        if modified:
            self._changed()
            self._hasRedundantOffsets = False

    def asVoice(self, removeOffsets=True) -> Voice:
        """
        Create a Voice as a copy of this Chain

        Args:
            removeOffsets: if True, remove any redundant offsets in the returned voice
        """
        self._update()
        items = self.copy().items
        _ = _stackEvents(items, explicitOffsets=True)
        if self.offset:
            for item in items:
                item.offset += self.offset
        voice = Voice(items, name=self.label)
        if removeOffsets:
            voice.removeRedundantOffsets()
        if self.symbols:
            for symbol in self.symbols:
                voice.addSymbol(symbol)
        if self.playargs:
            voice.playargs = self.playargs.copy()
        return voice

    def _asVoices(self) -> list[Voice]:
        return [self.asVoice()]

    def timeTransform(self, timemap: Callable[[F], F], inplace=False
                      ) -> Self:
        items = []
        for item in self.items:
            items.append(item.timeTransform(timemap, inplace=inplace))
        return self if inplace else self.clone(items=items)

    def scoringEvents(self,
                      groupid='',
                      config: CoreConfig = None,
                      parentOffset: F | None = None
                      ) -> list[scoring.Notation]:
        """
        Returns the scoring events corresponding to this object.

        The scoring events returned always have an absolute offset

        Args:
            groupid: if given, all events are given this groupid
            config: the configuration used (None to use the active config)
            parentOffset: if given will override the parent's offset

        Returns:
            the scoring notations representing this object
        """
        if not self.items:
            return []

        if config is None:
            config = Workspace.active.config

        if parentOffset is None:
            parentOffset = self.parentAbsOffset()

        if self._postSymbols:
            postsymbols = self._postSymbols
            self = self.copy()
            for offset, symbol in postsymbols:
                event = self.splitAt(offset, beambreak=False)
                if event:
                    event.addSymbol(symbol)
                else:
                    logger.error(f"No event found at {offset} for symbol {symbol}")

        chainitems = self.flatEvents()
        notations: list[scoring.Notation] = []
        if self.label and chainitems[0].relOffset() > 0:
            firstrest = scoring.makeRest(duration=chainitems[0].dur, annotation=self.label)
            notations.append(firstrest)

        for item in chainitems:
            itemNotations = item.scoringEvents(groupid=groupid, config=config, parentOffset=parentOffset)
            if self.symbols:
                for s in self.symbols:
                    if isinstance(s, symbols.EventSymbol) and not isinstance(s, symbols.VoiceSymbol):
                        for n in itemNotations:
                            s.applyToNotation(n, parent=item)
            notations.extend(itemNotations)

        if len(notations) > 1:
            n0 = notations[0]
            for n1 in notations[1:]:
                if n0.tiedNext and not n1.isRest:
                    n1.tiedPrev = True
                n0 = n1

        return notations

    def _solveOrfanHairpins(self, currentDynamic='mf'):
        lastHairpin: symbols.Hairpin | None = None
        for n in self.recurse():
            if not isinstance(n, (Chord, Note)):
                continue
            if n.dynamic and n.dynamic != currentDynamic:
                if lastHairpin:
                    n.addSpanner(lastHairpin.makeEndSpanner())
                    lastHairpin = None
                currentDynamic = n.dynamic

            if n.symbols:
                for s in n.symbols:
                    if isinstance(s, symbols.Hairpin) and s.kind == 'start' and not s.partnerSpanner:
                        lastHairpin = s

    def _scoringParts(self,
                      config: CoreConfig,
                      maxstaves: int = None,
                      name='',
                      shortname='',
                      groupParts=False,
                      addQuantizationProfile=False):
        self._update()
        notations = self.scoringEvents(config=config)
        if not notations:
            return []
        scoring.resolveOffsets(notations)
        maxstaves = maxstaves or config['show.voiceMaxStaves']

        if maxstaves == 1:
            parts = [scoring.UnquantizedPart(notations, name=name, shortname=shortname)]
        else:
            parts = scoring.distributeNotationsByClef(notations, name=name, shortname=shortname,
                                                      maxstaves=maxstaves)
            if len(parts) > 1 and groupParts:
                scoring.UnquantizedPart.groupParts(parts, name=name, shortname=shortname)

        if addQuantizationProfile:
            quantProfile = config.makeQuantizationProfile()
            for part in parts:
                part.quantProfile = quantProfile
        return parts

    def scoringParts(self,
                     config: CoreConfig = None
                     ) -> list[scoring.UnquantizedPart]:
        return self._scoringParts(config or Workspace.active.config, name=self.label)

    def quantizePitch(self, step=0.25):
        if step <= 0:
            raise ValueError(f"Step should be possitive, got {step}")
        items = [i.quantizePitch(step) for i in self.items]
        return self.clone(items=items)

    def _setItems(self, items: list[MEvent|Chain]) -> None:
        for item in items:
            item.parent = self
        self.items = items
        self._changed()

    def timeShift(self, timeoffset: time_t) -> Self:
        if timeoffset == 0:
            return self
        reloffset = self.relOffset()
        if timeoffset > 0:
            return self.clone(offset=reloffset + timeoffset)

        if reloffset + timeoffset >= 0:
            return self.clone(offset=reloffset + timeoffset)

        out = self.copy()
        out.timeShiftInPlace(timeoffset)
        return out

    def timeShiftInPlace(self, timeoffset: time_t) -> None:
        """
        Shift the time of this by the given offset (inplace)

        Args:
            timeoffset: the time delta (in quarterNotes)
        """

        timeoffset = asF(timeoffset)
        if timeoffset == 0:
            return

        self._update()
        reloffset = self.relOffset()
        if timeoffset > 0:
            self.offset = reloffset + timeoffset
            self._changed()
            return

        # Negative offset. First decrease the offset to the first event.
        firstoffset = self.firstOffset()
        assert firstoffset is not None
        newfirstoffset = max(F0, firstoffset + timeoffset)
        itemshift = newfirstoffset - firstoffset
        for item in self.items:
            item.timeShiftInPlace(itemshift)

        # Remaining shift
        restshift = timeoffset + firstoffset - newfirstoffset
        if restshift:
            newreloffset = reloffset + restshift
            if newreloffset < 0:
                raise ValueError(f"The shift would result in negative time. "
                                 f"Resulting offset: {newreloffset}, current "
                                 f"offset: {reloffset}, self: {self}")
            if not self.parent:
                self.offset = newreloffset
            else:
                previtem = self.parent.previousItem(self)
                if previtem is None:
                    # No previous item, so can just adjust own offset
                    self.offset = newreloffset
                else:
                    assert isinstance(previtem, (MEvent, Chain))
                    if newreloffset < previtem.resolveEnd():
                        raise ValueError("The shift would result in negative time")
                    self.offset = newreloffset
        self._changed()

    def firstOffset(self) -> F | None:
        """
        Returns the offset (relative to the start of this chain) of the first event in this chain
        """
        event = self.firstEvent()
        return None if not event else event.absOffset() - self.absOffset()

    def pitchTransform(self, pitchmap: Callable[[float], float]) -> Self:
        newitems = [item.pitchTransform(pitchmap) for item in self.items]
        return self.clone(items=newitems)

    def recurse(self, reverse=False) -> Iterator[MEvent]:
        """
        Yields all events (Notes/Chords) in this chain, recursively

        This method guarantees that the yielded events are the actual objects included
        in this chain or its sub-chains. This is usefull when used in combination with
        methods like addSpanner, which modify the objects themselves.

        Args:
            reverse: if True, recurse the chain in reverse

        Returns:
            an iterator over all notes/chords within this chain and its sub-chains, where
            for each event a tuple (event: MEvent, offset: F) is returned. The offset is
            relative to the offset of this chain, so in order to determine the absolute
            offset for each returned event one needs to add the absolute offset of this
            chain


        .. seealso:: :meth:`Chain.eventsWithOffset`, :meth:`Chain.itemsWithOffset`
        """
        if not reverse:
            for item in self.items:
                if isinstance(item, MEvent):
                    yield item
                elif isinstance(item, Chain):
                    yield from item.recurse(reverse=False)
        else:
            for item in reversed(self.items):
                if isinstance(item, MEvent):
                    yield item
                else:
                    yield from item.recurse(reverse=True)

    def eventsWithOffset(self,
                         start: beat_t = None,
                         end: beat_t = None,
                         partial=True) -> list[tuple[MEvent, F]]:
        """
        Recurse the events in self and resolves each event's offset

        Args:
            start: absolute start beat/location. Filters the returned
                event pairs to events within this time range
            end: absolute end beat/location. Filters the returned event
                pairs to events within the given range
            partial: only used if either start or end are given, this controls
                how events are matched. If True, events only need to be
                defined within the time range. Otherwise, events need
                to be fully included within the time range

        Returns:
            a list of pairs, where each pair has the form (event, offset), the offset being
             the **absolute** offset of the event. Event themselves are not modified

        Example
        ~~~~~~~

            >>> from maelzel.core import *
            >>> chain = Chain([
            ... "4C:0.5",
            ... "4D",
            ... Chain(["4E:0.5"], offset=2)
            ... ], offset=1)

        """
        self._update()
        if self._cachedEventsWithOffset:
            eventpairs = self._cachedEventsWithOffset
        else:
            eventpairs, totaldur = self._eventsWithOffset(frame=self.absOffset())
            self._dur = totaldur
            self._cachedEventsWithOffset = eventpairs
        if start is not None or end is not None:
            struct = self.activeScorestruct()
            start = struct.asBeat(start) if start else F0
            end = struct.asBeat(end) if end else F(sys.maxsize)
            eventpairs = _eventPairsBetween(eventpairs,
                                            start=start,
                                            end=end,
                                            partial=partial)
        return eventpairs

    def itemsWithOffset(self) -> Iterator[tuple[MEvent|Chain, F]]:
        """
        Iterate over the items of this chain with their absolute offset

        Returns:
            an iterator over tuple[item, offset], where an item can be
            an event or a Chain, and offset is the absolute offset

        .. seealso:: :meth:`Chain.eventsWithOffset`
        """
        self._update()
        frame = self.absOffset()
        for item, offset in self.itemsWithRelativeOffset():
            yield item, offset + frame

    def itemsWithRelativeOffset(self) -> Iterator[tuple[MEvent|Chain, F]]:
        """
        Iterate over the items of this chain with their relative offset

        Returns:
            an iterator over tuple[item, offset], where an item can be
            an event or a Chain, and offset is the offset relative to this chain

        .. seealso:: :meth:`Chain.eventsWithOffset`
        """

        self._update()
        now = F0
        for item in self.items:
            itemoffset = item.relOffset()
            if itemoffset > now:
                now = itemoffset
            yield item, now
            now += item.dur

    def _eventsWithOffset(self,
                          frame: F
                          ) -> tuple[list[tuple[MEvent, F]], F]:
        events = []
        now = frame
        for item in self.items:
            if item.offset:
                now = frame + item.offset
            if isinstance(item, MEvent):
                events.append((item, now))
                now += item.dur
            else:
                subitems, subdur = item._eventsWithOffset(frame=now)
                events.extend(subitems)
                now += subdur
        return events, now - frame

    def _iterateWithTimes(self,
                          recurse: bool,
                          frame: F,
                          ) -> tuple[list[tuple[MEvent | list, F, F]], F]:
        """
        For each item returns a tuple (item, offset, dur)

        Each event is represented as a tuple (event, offset, dur), a chain
        is represented as a list of such tuples

        Args:
            recurse: if True, traverse any subchain
            frame: the frame of reference

        Returns:
            a tuple (eventtuples, duration) where eventtuples is a list of
            tuples (event, offset, dur). If recurse is True,
            any subchain is returned as a list of eventtuples. Otherwise,
            a flat list is returned. In each eventtuple, the offset is relative
            to the first frame passed, so if the first offset was 0
            the offsets will hold the absolute offset of each event. Duration
            is the total duration of the items in
            the chain (not including its own offset)

        """
        assert isinstance(frame, F)
        now = frame
        out = []
        for i, item in enumerate(self.items):
            if item.offset is not None:
                t = frame + item.offset
                assert t >= now, f"Invalid time: {now=}, {t=}, {frame=}, {item.offset=}"
                now = t
            if isinstance(item, MEvent):
                dur = item.dur
                out.append((item, now, dur))
                item._resolvedOffset = now - frame
                if i == 0 and self.label:
                    item.setProperty('.chainlabel', self.label)
                now += dur
            else:
                # a Chain
                if recurse:
                    subitems, subdur = item._iterateWithTimes(frame=now, recurse=True)
                    item._dur = subdur
                    item._resolvedOffset = now - frame
                    out.append((subitems, now, subdur))
                else:
                    subdur = item.dur
                    out.append((item, now, subdur))
                now += subdur
        return out, now - frame

    def addSpanner(self,
                   spanner: str | symbols.Spanner,
                   endobj: MEvent = None
                   ) -> Self:
        """
        Adds a spanner symbol across this object

        A spanner is a slur, line or any other symbol attached to two or more
        objects. A spanner always has a start and an end.

        Args:
            spanner: a Spanner object or a spanner description (one of 'slur', '<', '>',
                'trill', 'bracket', etc. - see :func:`maelzel.core.symbols.makeSpanner`
                When passing a string description, prepend it with '~' to create an end spanner
            endobj: the object where this spanner ends. If not given, the last event
                of this chain

        Returns:
            self (allows to chain calls)

        Example
        ~~~~~~~

            >>> chain = Chain([
            ... Note("4C", 1),
            ... Note("4D", 0.5),
            ... Note("4E")   # This ends the hairpin spanner
            ... ])
            >>> chain.addSpanner('slur')

        This is the same as:

            >>> chain[0].addSpanner('slur', chain[-1])

        """
        startobj = self.firstEvent()
        if endobj is None:
            endobj = self.lastEvent()
        if isinstance(spanner, str):
            spanner = symbols.makeSpanner(spanner)
        assert isinstance(startobj, (Note, Chord)) and isinstance(endobj, (Note, Chord))
        spanner.bind(startobj, endobj)
        return self

    def addSymbolAt(self, location: time_t | tuple[int, time_t], symbol: symbols.Symbol
                    ) -> Self:
        """
        Adds a symbol at the given location

        If there is no event starting at the given location, the quantized part is split at
        the location when rendering and the symbol is added to the event. This allows to add
        'soft' symbols at any location without the need to modify the events themselves.
        If there actually is an event **starting** at the given offset, the symbol
        is added to the event directly.

        Args:
            location: the location to add the symbol at
            symbol: the symbol to add

        Returns:
            self

        Example
        -------

            >>> chain = Chain([
            ... Note("4C", 2),
            ... Note("4E", 1)
            ])
            >>> chain.addSymbolAt(1, symbols.Fermata())


        .. seealso:: :meth:`Chain.beamBreak`
        """
        offset = self.activeScorestruct().asBeat(location)
        event = self.eventAt(offset)
        if event and event.absOffset() == offset:
            event.addSymbol(symbol)
        else:
            self._postSymbols.append((offset, symbol))
        return self

    def beamBreak(self, location: time_t | tuple[int, time_t]) -> Self:
        """
        Add a 'soft' beam break at the given location

        A soft beam break does not modify the actual events in this Chain/Voice. It
        adds a beam break to the quantized score when rendering. Any syncopation
        at the given location will be broken for rendering but stays unmodified
        as an event. This is similar to performing:

            >>> chain.splitAt(location).addSymbol(symbols.BeamBreak())

        But the Chain/Voice itself remains unmodified: the operation is only performed
        at the quantized score

        Args:
            location: the location to add a beam break to

        Returns:
            self

        .. seealso:: :meth:`Chain.addSymbolAt`

        """
        return self.addSymbolAt(location=location, symbol=symbols.BeamBreak())

    def firstEvent(self) -> MEvent | None:
        """The first event in this chain, recursively"""
        return next(self.recurse(), None)

    def lastEvent(self) -> MEvent | None:
        """The last event in this chain, recursively"""
        return next(self.recurse(reverse=True))

    def _asAbsOffset(self, location: time_t | tuple[int, time_t]) -> F:
        if isinstance(location, tuple):
            struct = self.scorestruct() or Workspace.active.scorestruct
            return struct.locationToBeat(*location)
        else:
            return asF(location)

    def eventAt(self, location: time_t | tuple[int, time_t], margin=F(1, 8), split=False
                ) -> MEvent | None:
        """
        The event present at the given location

        Args:
            location: the beat or a tuple (measureindex, beatoffset). If a beat is given,
                it is interpreted as an absoute offset
            margin: a time margin (in quarternotes) from the given location. This can
                help in corner cases
            split: if True split the event at the given offset. This will modify the
                event itself, which will remain split after this call.

        Returns:
            the event present at the given location, or None if no event was found. An
            explicit rest will be returned if found but empty space will return None
        """
        start = self._asAbsOffset(location)
        end = start + margin
        events = self.eventsBetween(start, end)
        if not events:
            return None
        event = events[0]
        if split:
            eventoffset = event.absOffset()
            if eventoffset < start < eventoffset + event.dur:
                event = self.splitAt(start, beambreak=False, nomerge=False)
        return event

    def eventsBetween(self,
                      start: beat_t,
                      end: beat_t,
                      partial=True,
                      ) -> list[MEvent]:
        """
        Events between the given time range

        If ``partial`` is false, only events which lie completey within
        the given range are included. Gracenotes at the edges are always
        included. The returned events are the actual events in this
        Chain or subchains: they are NOT copies.

        Args:
            start: absolute start location (a beat or a score location)
            end: absolute end location (a beat or score location)
            partial: include also events wich are partially included within
                the given time range

        Returns:
            a list of the events within the given time range

        .. seealso:: :meth:`Chain.eventsWithOffset`, :meth:`Chain.itemsBetween`
        """
        eventpairs = self.eventsWithOffset(start=start, end=end, partial=partial)
        return [event for event, offset in eventpairs]

    def itemsBetween(self,
                     start: beat_t,
                     end: beat_t,
                     partial=True
                     ) -> list[MEvent | Chain]:
        """
        Items between the given time range

        An item is either an event (Note, Chord, Clip, etc.) or another Chain.

        If ``partial`` is false, only items which lie completey within
        the given range are included. Gracenotes at the edges are always
        included

        Args:
            start: absolute start location (a beat or a score location)
            end: absolute end location (a beat or score location)
            partial: include also events wich are partially included within
                the given time range

        Returns:
            a list of the items within the given time range

        .. seealso:: :meth:`Chain.itemsWithOffset`, :meth:`Chain.eventsBetween`

        """
        sco = self.activeScorestruct()
        startbeat = sco.asBeat(start)
        endbeat = sco.asBeat(end)
        out = []
        if partial:
            for item, offset in self.itemsWithOffset():
                if offset > endbeat or (offset == endbeat and item.dur > 0):
                    break
                if offset + item.dur >= startbeat:
                    out.append(item)
        else:
            for item, offset in self.eventsWithOffset():
                if offset > endbeat:
                    break
                if startbeat <= offset and offset + item.dur <= endbeat:
                    out.append(item)
        return out

    def splitEventsAtMeasures(self,
                              scorestruct: ScoreStruct = None,
                              startindex=0,
                              stopindex=0
                              ) -> None:
        """
        Splits items in self at measure offsets, **inplace** (recursively)

        After this method is called, no event extends for longer than a measure,
        as defined in the given scorestruct or this object's scorestruct.

        .. note::

            To avoid modifying self, create a copy first:
            ``newchain = self.copy().splitEventsAtMeasure(...)``

        Args:
            scorestruct: if given, overrides any active scorestruct for this object
            startindex: the first measure index to use
            stopindex: the last measure index to use. 0=len(measures). The stopindex is not
                included (similar to how python's builtin `range` behaves`
        """
        if scorestruct is None:
            scorestruct = self.activeScorestruct()
        else:
            if self.scorestruct():
                clsname = type(self).__name__
                logger.warning(f"This {clsname} has already an active ScoreStruct "
                               f"via its parent. "
                               f"Passing an ad-hoc scorestruct might cause problems...")
        offsets = scorestruct.measureOffsets(startIndex=startindex, stopIndex=stopindex)
        self.splitEventsAtOffsets(offsets, tie=True)

    def splitAt(self,
                location: beat_t,
                tie=True,
                beambreak=False,
                nomerge=False
                ) -> MEvent | None:
        """
        Split any event present at the given absolute offset (in place)

        The parts resulting from the split operation will be part of this chain/voice.

        To split at a relative offset, substract the absolute offset of this Chain
        from the given offset

        Args:
            location: the absolute offset to split at, or a score location (measureindex, measureoffset)
            tie: tie the parts of an event together if the split intersects an event
            beambreak: if True, add a BeamBreak symbol to the given event
            nomerge: if True, enforce that the items splitted cannot be
                merged at a later stage (they are marked with a NoMerge symbol)

        Returns:
            if an event was split, returns the part of the event starting at
            the given offset. Otherwise returns None.
        """
        absoffset = self._asAbsOffset(location)
        self.splitEventsAtOffsets([absoffset], tie=tie)
        ev = self.eventAt(absoffset)

        if not ev:
            return None

        assert ev.absOffset() == absoffset, f"Failed to split correctly? {ev=}, event offset: {ev.absOffset()}, offset should be {absoffset}"
        if beambreak:
            ev.addSymbol(symbols.BeamBreak())

        if nomerge:
            ev.addSymbol(symbols.NoMerge())

        return ev

    def splitEventsAtOffsets(self,
                             offsets: list[time_t | tuple[int, time_t]],
                             tie=True,
                             nomerge=False
                             ) -> None:
        """
        Splits items in self at the given offsets, **inplace** (recursively)

        The offsets are absolute. Split items are by default tied together.
        This method is useful for the case where a part of an event needs
        to be adressed in some way. For example, a symbol needs to be
        added to a part of a note (a crescendo hairpin which starts in the
        middle of an event).

        Args:
            offsets: the offsets to split items at (either absolute offsets or
                score locations as tuple (measureindex, measureoffset)
            tie: if True, parts of an item are tied together
            nomerge: if True, add event breaks to prevent events from being
                merged
        """
        if not offsets:
            raise ValueError("No locations given")
        items = []
        sco = self.activeScorestruct()
        absoffsets = [sco.asBeat(offset) for offset in offsets]
        for item, offset in self.itemsWithOffset():
            if isinstance(item, Chain):
                item.splitEventsAtOffsets(absoffsets, tie=tie, nomerge=nomerge)
                items.append(item)
            else:
                parts = item.splitAtOffsets(absoffsets, tie=tie, nomerge=nomerge)
                for part in parts:
                    part.parent = self
                items.extend(parts)
        self.items = items
        self._changed()

    def cycle(self, totaldur: F, crop=False) -> Self:
        """
        Cycle over the items of self for the given total duration

        Args:
            totaldur: the total duration of the resulting sequence
            crop: if True, crop last item if it exceeds the given
                total duration

        Returns:
            a copy of self representing cycles of its items

        """
        filled = self.copy()
        filled.fillGaps()
        filled.removeRedundantOffsets()
        flatitems = list(filled.recurse())
        items: list[MEvent] = []
        accum = F(0)
        for item in iterlib.cycle(flatitems):
            items.append(item.copy())
            accum += item.dur
            if accum >= totaldur:
                break
        if crop and accum > totaldur:
            diff = accum - totaldur
            lastitem = items[-1]
            assert diff < lastitem.dur
            lastitem = lastitem.clone(dur=lastitem.dur - diff)
            items[-1] = lastitem
        return self.clone(items=items)

    def matchOrfanSpanners(self, removeUnmatched=False) -> None:
        """
        Match unmatched spanners

        When adding spanners to objects, it is possible to create a spanner
        without a partner spanner. As long as there are as many start spanners
        as end spanners for a specific spanner class, these "orfan" spanners
        are matched. This method makes the matches explicit, as if they had
        been created with a partner spanner.

        Args:
            removeUnmatched: if True, any spanners which cannot be matched will
                be removed
        """
        unmatched: list[symbols.Spanner] = []
        for event in self.recurse():
            if event.symbols:
                for symbol in event.symbols:
                    if isinstance(symbol, symbols.Spanner) and symbol.partnerSpanner is None:
                        unmatched.append(symbol)
        if not unmatched:
            return
        # sort by class
        byclass: dict[type, list[symbols.Spanner]] = {}
        for spanner in unmatched:
            byclass.setdefault(type(spanner), []).append(spanner)
        for cls, spanners in byclass.items():
            stack = []
            for spanner in spanners:
                if spanner.kind == 'start':
                    stack.append(spanner)
                else:
                    assert spanner.kind == 'end'
                    if stack:
                        startspanner = stack.pop()
                        startspanner.setPartnerSpanner(spanner)
                    elif removeUnmatched:
                        assert spanner.anchor is not None
                        obj = spanner.anchor
                        if obj is None:
                            logger.error(f"The spanner has no anchor ({spanner=})")
                        elif obj.symbols is None:
                            logger.error(f"The spanner's anchor seems invalid. {spanner=}, anchor={obj}")
                        else:
                            logger.debug(f"Removing spanner {spanner} from {obj}")
                            obj.symbols.remove(spanner)
                            spanner.anchor = None

    def remap(self, deststruct: ScoreStruct, sourcestruct: ScoreStruct = None
              ) -> Self:
        remappedEvents = [ev.remap(deststruct, sourcestruct=sourcestruct)
                          for ev in self]
        return self.clone(items=remappedEvents)

    def automate(self,
                 param: str,
                 breakpoints: list[tuple[time_t|location_t, float]] | list[num_t],
                 relative=True,
                 interpolation='linear'
                 ) -> None:
        if self.playargs is None:
            self.playargs = PlayArgs()
        self.playargs.addAutomation(param=param, breakpoints=breakpoints,
                                    interpolation=interpolation, relative=relative)

    def absorbInitialOffset(self, removeRedundantOffsets=True):
        """
        Moves the offset of the first event to the offset of the chain itself

        Args:
            removeRedundantOffsets: remove redundant offsets.

        Example
        ~~~~~~~

        Notice how the offset of the first note is now None and the chain
        itself has an offset of 0.5

            >>> ch = Chain([
            ...     "4C:1:offset=0.5",
            ...     "4E:1",
            ...     "4G:1"
            ... ])
            >>> ch.dump()
            Chain - beat: 0, offset: None, dur: 3.5
            location  beat   offset      dur         name
            0:0.5     0.5    0.5         1           4C
            0:1.5     1.5    (1.5)       1           4E
            0:2.5     2.5    (2.5)       1           4G
            >>> ch._absorbInternalOffset()
            >>> ch.dump()
            Chain - beat: 1/2, offset: 0.5, dur: 3
            location  beat   offset      dur         name
            0:0.5     0.5    (0)         1           4C
            0:1.5     1.5    (1)         1           4E
            0:2.5     2.5    (2)         1           4G
            
        """
        firstoffset = self.firstOffset()
        if firstoffset is not None and firstoffset > 0:
            # self.stack()
            self._update()
            for item in self.items:
                item.timeShiftInPlace(-firstoffset)
            self.offset = self.relOffset() + firstoffset
            if removeRedundantOffsets:
                self.removeRedundantOffsets()
            self._changed()

    def cropped(self, start: beat_t, end: beat_t) -> Self | None:
        """
        Returns a copy of this chain, cropped to the given beat range

        Returns None if there are no events in this chain within
        the given time range

        Args:
            start: start of the beat range
            end: end of the beat range

        Returns:
            a Chain cropped at the given beat range
        """
        sco = self.activeScorestruct()
        startbeat = sco.asBeat(start)
        endbeat = sco.asBeat(end)
        cropped = _cropped(self, startbeat=startbeat, endbeat=endbeat)
        if not cropped:
            return None
        cropped.removeRedundantOffsets()
        return cropped


def _cropped(chain: Chain, startbeat: F, endbeat: F, absorbOffset=False
             ) -> Chain:
    items = []
    # frame = chain.absOffset()
    for item, offset in chain.itemsWithOffset():
        if offset > endbeat or (offset == endbeat and item.dur > 0):
            break

        if item.dur == 0 and startbeat <= offset:
            items.append(item.clone(offset=offset - startbeat))
        elif offset + item.dur > startbeat:
            # Add a cropped part or the entire item?
            if startbeat <= offset and offset + item.dur <= endbeat:
                items.append(item.clone(offset=offset - startbeat))
            else:
                if isinstance(item, MEvent):
                    item2 = item.cropped(startbeat, endbeat)
                    items.append(item2.clone(offset=item2.offset - startbeat))
                else:
                    # TODO: combine these two operations, if needed
                    chain = _cropped(item, startbeat, endbeat, absorbOffset=True)
                    items.append(chain.clone(offset=chain.offset - startbeat))
    out = chain.clone(items=items, offset=startbeat)
    if absorbOffset:
        out.absorbInitialOffset()
    return out


class PartGroup:
    """
    This class represents a group of parts

    It is used to indicate that a group of parts are to be notated
    within a staff group, sharing a name/shortname if given. This is
    usefull for things like piano scores, for example

    Args:
        parts: the parts inside this group
        name: the name of the group
        shortname: a shortname to use in systems other than the first
        showPartNames: if True, the name of each part will still be shown in notation.
            Otherwise, it is hidden and only the group name appears
    """
    def __init__(self, parts: list[Voice], name='', shortname='', showPartNames=False):
        for part in parts:
            part._group = self

        self.parts = parts
        """The parts in this group"""

        self.name = name
        """The name of the group"""

        self.shortname = shortname
        """A short name for the group"""

        self.groupid = scoring.makeGroupId()
        """A group ID"""

        self.showPartNames = showPartNames
        """Show the names of the individual parts?"""

    def append(self, part: Voice) -> None:
        """Append a part to this group"""
        if part not in self.parts:
            self.parts.append(part)


class Voice(Chain):
    """
    A Voice is a sequence of non-overlapping objects.

    It is **very** similar to a Chain, the only difference being that its offset
    is always 0.


    Voice vs Chain
    ~~~~~~~~~~~~~~

    * A Voice can contain a Chain, but not vice versa.
    * A Voice does not have a time offset, its offset is always 0.

    Args:
        items: the items in this voice. Items can also be added later via .append
        name: the name of this voice. This will be interpreted as the staff name
            when shown as notation
        shortname: optionally a shortname can be given, it will be used for subsequent
            systems when shown as notation
        maxstaves: if given, a max. number of staves to explode this voice when shown
            as notation. If not given the config key 'show.voiceMaxStaves' is used
    """

    _configKeys: set[str] | None = None

    def __init__(self,
                 items: list[MEvent | str] | Chain = None,
                 name='',
                 shortname='',
                 maxstaves: int = None,
                 ):
        if isinstance(items, Chain):
            chain = items
            if chain.offset and chain.offset > 0:
                events = chain.timeShift(chain.offset).items
            else:
                events = chain.items
        else:
            events = items

        super().__init__(items=events, offset=F(0))
        self.name = name
        """The name of this voice/staff"""

        self.shortname = shortname
        """A shortname to display as abbreviation after the first system"""

        self.maxstaves = maxstaves if maxstaves is not None else Workspace.active.config['show.voiceMaxStaves']
        """The max. number of staves this voice can be expanded to"""

        self._config: dict[str, Any] = {}
        """Any key set here will override keys from the coreconfig for rendering
        Any key in CoreConfig is supported"""

        self._group: PartGroup | None = None
        """A part group is created via Score.makeGroup"""

    def __repr__(self):
        if len(self.items) < 10:
            itemstr = ", ".join(repr(_) for _ in self.items)
        else:
            itemstr = ", ".join(repr(_) for _ in self.items[:10]) + ", …"
        cls = self.__class__.__name__
        namedargs = []
        if namedargs:
            info = ', ' + ', '.join(namedargs)
        else:
            info = ''
        return f'{cls}([{itemstr}]{info})'

    def __hash__(self):
        superhash = super().__hash__()
        return hash((superhash, self.name, self.shortname, self.maxstaves, id(self._group)))

    def _copyAttributesTo(self, other: Self) -> None:
        super()._copyAttributesTo(other)
        if self._config:
            other._config = self._config.copy()
        if self._scorestruct:
            other._scorestruct = self._scorestruct

    def __copy__(self: Voice) -> Voice:
        # always a deep copy
        voice = Voice(items=[item.copy() for item in self.items],
                      name=self.name,
                      shortname=self.shortname,
                      maxstaves=self.maxstaves)
        self._copyAttributesTo(voice)
        return voice

    def __deepcopy__(self, memodict={}) -> Voice:
        return self.__copy__()

    @property
    def group(self) -> PartGroup | None:
        return self._group

    def parentAbsOffset(self) -> F:
        return F0

    def setConfig(self, key: str, value) -> Voice:
        """
        Set a configuration key for this object.

        Possible keys are any CoreConfig keys with the prefixes 'quant.' and 'show.'

        Args:
            key: the key to set
            value: the value. It will be validated via CoreConfig

        Returns:
            self. This allows multiple calls to be chained

        Example
        ~~~~~~~

        Configure the voice to break syncopations at every beat when
        rendered or quantized as a QuantizedScore

            >>> voice = Voice(...)
            >>> voice.setConfig('quant.brakeSyncopationsLevel', 'all')

        Now, whenever the voice is shown in itself or as part of a score
        all syncopations across beat boundaries will be split into tied notes.

        This is the same as:

            >>> voice = Voice(...)
            >>> score = Score([voice])
            >>> quantizedscore = score.quantizedScore()
            >>> quantizedscore.parts[0].brakeSyncopations(level='all')
            >>> quantizedscore.render()
        """
        configkeys = Voice._configKeys
        if not configkeys:
            pattern = r'(quant|show)\..+'
            corekeys = CoreConfig.root.keys()
            configkeys = set(k for k in corekeys if re.match(pattern, k))
            Voice._configKeys = configkeys
        if key not in configkeys:
            raise KeyError(f"Key {key} not known. Possible keys: {configkeys}")
        if errormsg := CoreConfig.root.checkValue(key, value):
            raise ValueError(f"Cannot set {key} to {value}: {errormsg}")
        self._config[key] = value
        return self

    def getConfig(self, prototype: CoreConfig = None) -> CoreConfig | None:
        """
        Returns a CoreConfig overloaded with any option set via :meth:`~Voice.setConfig`

        Args:
            prototype: the config to use as prototype. If not given, the active config
                will be used

        Returns:
            the resulting CoreConfig set via :meth:`Voice.setConfig`. If not prototype
            and no changes have been made, None is returned

        """
        if not self._config:
            return prototype
        return (prototype or Workspace.active.config).clone(self._config)

    def configQuantization(self,
                           breakSyncopationsLevel: str = None,
                           ) -> None:
        """
        Customize the quantization process for this Voice

        Args:
            breakSyncopationsLevel: one of 'all', 'weak', 'strong' (see
                config key `quant.breakSyncopationsLevel <config_quant_breaksyncopationslevel>`)

        """
        if breakSyncopationsLevel is not None:
            self.setConfig('quant.breakSyncopationsLevel', breakSyncopationsLevel)

    def clone(self, **kws) -> Voice:
        if 'items' not in kws:
            kws['items'] = self.items.copy()
        if 'shortname' not in kws:
            kws['shortname'] = self.shortname
        if 'name' not in kws:
            kws['name'] = self.name
        out = Voice(**kws)
        if self.label:
            out.label = self.label
        return out

    def scoringParts(self, config: CoreConfig = None
                     ) -> list[scoring.UnquantizedPart]:
        ownconfig = self.getConfig(prototype=config)
        parts = self._scoringParts(config=ownconfig or Workspace.active.config,
                                   maxstaves=self.maxstaves,
                                   name=self.name or self.label,
                                   shortname=self.shortname,
                                   groupParts=self._group is None,
                                   addQuantizationProfile=ownconfig is not None)
        if self.symbols:
            for symbol in self.symbols:
                if isinstance(symbol, symbols.VoiceSymbol):
                    if symbol.applyToAllParts:
                        for part in parts:
                            symbol.applyToPart(part)
                    else:
                        symbol.applyToPart(parts[0])

        if self._group:
            scoring.UnquantizedPart.groupParts(parts,
                                               groupid=self._group.groupid,
                                               name=self._group.name,
                                               shortname=self._group.shortname,
                                               showPartNames=self._group.showPartNames)
        return parts

    def addSymbol(self, *args, **kws) -> Voice:
        symbol = symbols.parseAddSymbol(args, kws)
        if not isinstance(symbol, symbols.VoiceSymbol):
            raise ValueError(f"Cannot add {symbol} to a {type(self).__name__}")
        self._addSymbol(symbol)
        return self

    def breakBeam(self, location: F | tuple[int, F]) -> None:
        """
        Shortcut to ``Voice.addSymbol(symbols.BeamBreak(...))``

        Args:
            location: the location to break the beam (a beat or a tuple (measureidx, beat))

        Returns:

        """
        self.addSymbol(symbols.BeamBreak(location=location))

    def _asVoices(self) -> list[Voice]:
        return [self]


def _splitSynthGroupsIntoLines(groups: list[list[SynthEvent]]
                               ) -> list[SynthEvent | list[SynthEvent]]:
    """
    Split synthevent groups into individual lines

    When resolving the synthevents of a chain, each item in the chain is asked to
    deliver its synthevents. For an individual item which is neither tied to a
    following item nor makes a glissando the result are one or multiple synthevents
    which are independent from any other. Such synthevents are placed in the output
    list as is, flattened. Any tied events are packed inside a list

    .. code::

        C4 --gliss-- D4 --tied-- D4
                                 G3

     This results in the list [[C4, D4, D4], G3]

    Args:
        groups: A list of synthevents. Each synthevent group corresponds
            to the synthevents returned by a note/chord

    Returns:
        a list of either a single SynthEvent or a list thereof, in which case
        these enclosed synthevents build together a line


    **Algorithm**

    TODO
    """
    def matchNext(event: SynthEvent, group: list[SynthEvent], availableNodes: set[int]) -> int | None:
        pitch = event.bps[-1][1]
        for idx in availableNodes:
            candidate = group[idx]
            if abs(pitch - candidate.bps[0][1]) < 1e-6:
                return idx
        return None

    def makeLine(nodeindex: int, groupindex: int, availableNodesPerGroup: list[set[int]]
                 ) -> list[SynthEvent]:
        event = groups[groupindex][nodeindex]
        out = [event]
        if not event.linkednext or groupindex == len(groups) - 1:
            return out
        availableNodes = availableNodesPerGroup[groupindex + 1]
        if not availableNodes:
            return out
        nextEventIndex = matchNext(event, group=groups[groupindex+1], availableNodes=availableNodes)
        if nextEventIndex is None:
            return out
        availableNodes.discard(nextEventIndex)
        continuationLine = makeLine(nextEventIndex, groupindex + 1,
                                    availableNodesPerGroup=availableNodesPerGroup)
        out.extend(continuationLine)
        return out

    out: list[SynthEvent | list[SynthEvent]] = []
    availableNodesPerGroup: list[set[int]] = [set(range(len(group))) for group in groups]
    # Iterate over each group. A group is just the list of events generated by a given chord
    # Within a group, iterate over the _beatNodes of each group
    for groupindex in range(len(groups)):
        for nodeindex in availableNodesPerGroup[groupindex]:
            line = makeLine(nodeindex, groupindex=groupindex,
                            availableNodesPerGroup=availableNodesPerGroup)
            line[-1].linkednext = False
            assert isinstance(line, list) and len(line) >= 1, f"{nodeindex=}, event={groups[groupindex][nodeindex]}"
            if len(line) == 1:
                out.append(line[0])
            else:
                out.append(line)

    # last group
    if len(groups) > 1 and (lastGroupIndexes := availableNodesPerGroup[-1]):
        lastGroup = groups[-1]
        out.extend(lastGroup[idx] for idx in lastGroupIndexes)

    return out


def _resolveGlissandi(flatevents: Iterable[MEvent], force=False) -> None:
    """
    Set the _glissTarget attribute with the pitch of the gliss target
    if a note or chord has an unset gliss target (in place)

    Args:
        flatevents: subsequent events
        force: if True, calculate/update all glissando targets

    """
    ev2 = None
    for ev1, ev2 in iterlib.pairwise(flatevents):
        if ev1.isRest() or ev2.isRest():
            continue
        if ev1.gliss or (ev1.playargs and ev1.playargs.get('glisstime') is not None):
            # Only calculate glissTarget if gliss is True
            if not force and ev1._glissTarget:
                continue
            if isinstance(ev1, Note):
                if isinstance(ev2, Note):
                    ev1._glissTarget = ev2.pitch
                elif isinstance(ev2, Chord):
                    ev1._glissTarget = max(n.pitch for n in ev2.notes)
                else:
                    ev1._glissTarget = ev1.pitch
            elif isinstance(ev1, Chord):
                if isinstance(ev2, Chord):
                    ev2pitches = ev2.pitches
                    if len(ev2pitches) > len(ev1.notes):
                        ev2pitches = ev2pitches[-len(ev1.notes):]
                    ev1._glissTarget = ev2pitches
                elif isinstance(ev2, Note):
                    ev1._glissTarget = [ev2.pitch] * len(ev1.notes)
                else:
                    ev1._glissTarget = ev1.pitches

    # last event
    if ev2 and ev2.gliss:
        if isinstance(ev2, Chord):
            ev2._glissTarget = ev2.pitches
        elif isinstance(ev2, Note):
            ev2._glissTarget = ev2.pitch


def _eventPairsBetween(eventpairs: list[tuple[MEvent, F]],
                       start: F,
                       end: F,
                       partial=True,
                       ) -> list[tuple[MEvent, F]]:
    """
    Events between the given time range

    If ``partial`` is false, only events which lie completey within
    the given range are included. Gracenotes at the edges are always
    included

    Args:
        eventpairs: list of pairs (event, absoluteOffset)
        start: absolute start location in beats
        end: absolute end location in beats
        partial: include also events wich are partially included within
            the given time range

    Returns:
        a list pairs (event: MEvent, absoluteoffset: F)
    """
    out = []
    if partial:
        for event, offset in eventpairs:
            if offset > end:
                break
            if event.dur > 0:
                if offset < end and offset + event.dur > start:
                    out.append((event, offset))
            else:
                # A gracenote
                if start <= offset <= end:
                    out.append((event, offset))
    else:
        for event, offset in eventpairs:
            if offset > end:
                break
            if start <= offset and offset + event.dur <= end:
                out.append((event, offset))
    return out
