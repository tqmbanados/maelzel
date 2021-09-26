"""
This module handles playing of events

Each Note, Chord, Line, etc, can express its playback in terms of CsoundEvents

A CsoundEvent is a score line with a number of fixed fields,
user-defined fields and a sequence of breakpoints

A breakpoint is a tuple of values of the form (offset, pitch [, amp, ...])
The size if each breakpoint and the number of breakpoints are given
by inumbps, ibplen

An instrument to handle playback should be defined with `defPreset` which handles
breakpoints and only needs the audio generating part of the csound code.

Whenever a note actually is played with a given preset, this preset is
 sent to the csound engine and instantiated/evaluated.

Examples
~~~~~~~~

.. code::

    from maelzel.core import *
    f0 = n2f("1E")
    notes = [Note(f2m(i*f0), dur=0.5) for i in range(20)]
    play.defPreset("detuned", r'''

    ''')

"""
from __future__ import annotations
import os

from datetime import datetime

import csoundengine

from .config import logger
from .workspace import getConfig, currentWorkspace, recordPath
from . import tools
from .presetbase import *
from .presetman import presetManager, csoundPrelude as _prelude
from .state import appstate as _appstate

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from typing import *
    from .csoundevent import CsoundEvent


__all__ = ('OfflineRenderer',
           'playEvents',
           'recEvents')

class PlayEngineNotStarted(Exception): pass


_invalidVariables = {"kfreq", "kamp", "kpitch"}


class OfflineRenderer:
    def __init__(self, sr=None, ksmps=64, outfile:str=None):
        w = currentWorkspace()
        self.a4 = w.a4  # m2f(69)
        self.sr = sr or getConfig()['rec.samplerate']
        self.ksmps = ksmps
        self.outfile = outfile
        self.events: List[CsoundEvent] = []

    def sched(self, event:CsoundEvent) -> None:
        self.events.append(event)

    def schedMany(self, events: List[CsoundEvent]) -> None:
        self.events.extend(events)

    def render(self, outfile:str=None, wait=None, quiet=None) -> None:
        """
        Render the events scheduled until now.

        Args:
            outfile: the soundfile to generate. Use "?" to save via a GUI dialog
            wait: if True, wait until rendering is done
            quiet: if True, supress all output generated by csound itself
                (print statements and similar opcodes still produce output)

        """
        quiet = quiet or getConfig()['rec.quiet']
        outfile = outfile or self.outfile
        recEvents(events=self.events, outfile=outfile, sr=self.sr,
                  wait=wait, quiet=quiet)

    def getCsd(self, outfile:str=None) -> str:
        """
        Generate the .csd which would render all events scheduled until now

        Args:
            outfile: if given, the .csd is saved to this file

        Returns:
            a string representing the .csd file
        """
        renderer = presetManager.makeRenderer(events=self.events, sr=self.sr,
                                              ksmps=self.ksmps)
        csdstr = renderer.generateCsd()
        if outfile == "?":
            lastdir = _appstate['saveCsdLastDir']
            outfile = tools.dialogs.saveDialog("Csd (*.csd)", directory=lastdir)
            if outfile:
                _appstate['saveCsdLastDir'] = os.path.split(outfile)[0]
        if outfile:
            with open(outfile, "w") as f:
                f.write(csdstr)
        return csdstr


def recEvents(events: List[CsoundEvent], outfile:str=None,
              sr:int=None, wait:bool=None, ksmps:int=None,
              quiet=None
              ) -> str:
    """
    Record the events to a soundfile

    Args:
        events: a list of events as returned by .events(...)
        outfile: the generated file. If left unset, a file inside the recording
            path is created (see `recordPath`). Use "?" to save via a GUI dialog
        sr: sample rate of the soundfile
        ksmps: number of samples per cycle (config 'rec.ksmps')
        wait: if True, wait until recording is finished. If None,
            use the config 'rec.block'
        quiet: if True, supress debug information when calling
            the csound subprocess

    Returns:
        the path of the generated soundfile

    Example::

        a = Chord("A4 C5", start=1, dur=2)
        b = Note("G#4", dur=4)
        events = sum([
            a.events(chan=1),
            b.events(chan=2, gain=0.2)
        ], [])
        recEvents(events, outfile="out.wav")
    """
    if outfile == "?":
        outfile = tools.saveRecordingDialog()
    if outfile is None:
        outfile = _makeRecordingFilename(ext=".wav")
        logger.info(f"Saving recording to {outfile}")
    renderer = presetManager.makeRenderer(events=events, sr=sr, ksmps=ksmps)
    if quiet is None:
        quiet = getConfig()['rec.quiet']
    renderer.render(outfile, wait=wait, quiet=quiet)
    return outfile


def _path2name(path):
    return os.path.splitext(os.path.split(path)[1])[0].replace("-", "_")


def _makeRecordingFilename(ext=".wav", prefix="rec-"):
    """
    Generate a new filename for a recording.

    This is used when rendering and no outfile is given

    Args:
        ext: the extension of the soundfile (should start with ".")
        prefix: a prefix used to identify this recording

    Returns:
        an absolute path. It is guaranteed that the filename does not exist.
        The file will be created inside the recording path (see ``state.recordPath``)
    """
    path = recordPath()
    assert ext.startswith(".")
    base = datetime.now().isoformat(timespec='milliseconds')
    if prefix:
        base = prefix + base
    out = os.path.join(path, base + ext)
    assert not os.path.exists(out)
    return out


def _registerPresetInSession(preset: PresetDef,
                             session:csoundengine.session.Session
                             ) -> csoundengine.Instr:
    """
    Create and register a :class:`csoundengine.instr.Instr` from a preset

    Args:
        preset: the PresetDef.
        session: the session to manage the instr

    Returns:
        the registered Instr
    """
    # each preset caches the generated instr
    instr = preset.makeInstr()
    # registerInstr checks itself if the instr is already defined
    session.registerInstr(instr)
    return instr


def _soundfontToTabname(sfpath: str) -> str:
    path = os.path.abspath(sfpath)
    return f"gi_sf2func_{hash(path)%100000}"


def _soundfontToChannel(sfpath:str) -> str:
    basename = os.path.split(sfpath)[1]
    return f"_sf:{basename}"


def startPlayEngine(numChannels=None, backend=None) -> csoundengine.Engine:
    """
    Start the play engine

    If an engine is already active, nothing happens, even if the
    configuration is different. To start the play engine with a different
    configuration, stop the engine first.

    Args:
        numChannels: the number of output channels, overrides config 'play.numChannels'
        backend: the audio backend used, overrides config 'play.backend'
    """
    config = getConfig()
    engineName = config['play.engineName']
    if engineName in csoundengine.activeEngines():
        return csoundengine.getEngine(engineName)
    numChannels = numChannels or config['play.numChannels']
    if backend == "?":
        backends = [b.name for b in csoundengine.csoundlib.audioBackends(available=True)]
        backend = tools.selectFromList(backends, title="Select Backend")
    backend = backend or config['play.backend']
    logger.debug(f"Starting engine {engineName} (nchnls={numChannels})")
    return csoundengine.Engine(name=engineName, nchnls=numChannels,
                               backend=backend,
                               globalcode=_prelude,
                               quiet=not config['play.verbose'])


def stopSynths(stopengine=False, cancelfuture=True):
    """
    Stops all synths (notes, chords, etc) being played

    If stopengine is True, the play engine itself is stopped
    """
    session = getPlaySession()
    session.unschedAll(future=cancelfuture)
    if stopengine:
        getPlayEngine().stop()


def getPlaySession() -> csoundengine.Session:
    config = getConfig()
    group = config['play.engineName']
    if not isEngineActive():
        if config['play.autostartEngine']:
            startPlayEngine()
        else:
            raise PlayEngineNotStarted("Engine is not running. Call startPlayEngine")
    return csoundengine.getSession(group)


def isEngineActive() -> bool:
    """
    Returns True if the sound engine is active
    """
    name = getConfig()['play.engineName']
    return csoundengine.getEngine(name) is not None


def getPlayEngine(start=None) -> Opt[csoundengine.Engine]:
    """
    Return the sound engine, or None if it has not been started
    """
    cfg = getConfig()
    engine = csoundengine.getEngine(name=cfg['play.engineName'])
    if not engine:
        logger.debug("engine not started")
        start = start if start is not None else cfg['play.autostartEngine']
        if start:
            engine = startPlayEngine()
            return engine
        return None
    return engine


class rendering:
    def __init__(self, outfile:str=None, wait=True, quiet=None,
                 sr:int=None, nchnls:int=None):
        """
        Context manager to transform all calls to .play to be renderer offline

        Args:
            outfile: events played within this context will be rendered
                to this file. If set to None, rendering is performed to an auto-generated
                file in the recordings folder
            wait: if True, wait until rendering is done
            quiet: if True, supress any output from the csound
                subprocess (config 'rec.quiet')

        Example::

            # this will generate a file foo.wav after leaving the `with` block
            with rendering("foo.wav"):
                chord.play(dur=2)
                note.play(dur=1, fade=0.1, delay=1)

            # You can render manually, if needed
            with rendering() as r:
                chord.play(dur=2)
                ...
                print(r.getCsd())
                r.render("outfile.wav")

        """
        self.sr = sr
        self.nchnls = nchnls
        self.outfile = outfile
        self._oldRenderer: Opt[OfflineRenderer] = None
        self.renderer: Opt[OfflineRenderer] = None
        self.quiet = quiet or getConfig()['rec.quiet']
        self.wait = wait

    def __enter__(self):
        workspace = currentWorkspace()
        self._oldRenderer = workspace.renderer
        self.renderer = OfflineRenderer(sr=self.sr, outfile=self.outfile)
        workspace.renderer = self.renderer
        return self.renderer

    def __exit__(self, *args, **kws):
        w = currentWorkspace()
        w.renderer = self._oldRenderer
        if self.outfile is None:
            self.outfile = _makeRecordingFilename()
            logger.info(f"Rendering to {self.outfile}")
        self.renderer.render(outfile=self.outfile, wait=self.wait,
                             quiet=self.quiet)


def _schedOffline(renderer: csoundengine.Renderer,
                  events: List[CsoundEvent],
                  _checkNchnls=True
                  ) -> None:
    """
    Schedule the given events for offline rendering.

    You need to call renderer.render(...) to actually render/play the
    scheduled events

    Args:
        renderer: a Renderer as returned by makeRenderer
        events: events as returned by, for example, chord.events(**kws)
        _checkNchnls: (internal parameter)
            if True, will check (and adjust) nchnls in
            the renderer so that it is high enough for all
            events to render properly
    """
    if _checkNchnls:
        maxchan = max(presetManager.eventMaxNumChannels(event)
                      for event in events)
        if renderer.nchnls < maxchan:
            logger.info(f"_schedOffline: the renderer was defined with "
                        f"nchnls={renderer.csd.nchnls}, but {maxchan} "
                        f"are needed to render the given events. "
                        f"Setting nchnls to {maxchan}")
            renderer.csd.nchnls = maxchan
    for event in events:
        pargs = event.getPfields()
        if pargs[2] != 0:
            logger.warn(f"got an event with a tabnum already set...: {pargs}")
            logger.warn(f"event: {event}")
        instrName = event.instr
        assert instrName is not None
        presetdef = presetManager.getPreset(instrName)
        instr = presetdef.makeInstr()
        if not renderer.isInstrDefined(instr.name):
            renderer.registerInstr(presetdef.makeInstr())
        # renderer.defInstr(instrName, body=presetdef.body, tabledef=presetdef.params)
        renderer.sched(instrName, delay=pargs[0], dur=pargs[1],
                       pargs=pargs[3:],
                       tabargs=event.namedArgs,
                       priority=event.priority)


def playEvents(events: List[CsoundEvent],
               ) -> csoundengine.synth.SynthGroup:
    """
    Play a list of events

    Args:
        events: a list of CsoundEvents

    Returns:
        A SynthGroup

    Example::

        from maelzel.core import *
        group = Group([
            Note("4G", dur=8),
            Chord("4C 4E", dur=7, start=1)
            Note("4C#", start=1.5, dur=6)])
        play.playEvents(group.events(instr='.piano')
    """
    synths = []
    session = getPlaySession()
    presetNames = {ev.instr for ev in events}
    presetDefs = [presetManager.getPreset(name) for name in presetNames]
    presetToInstr: Dict[str, csoundengine.Instr] = {preset.name:_registerPresetInSession(preset, session)
                                                    for preset in presetDefs}

    for ev in events:
        instr = presetToInstr[ev.instr]
        args = ev.getPfields(numchans=instr.numchans)
        synth = session.sched(instr.name,
                              delay=args[0],
                              dur=args[1],
                              pargs=args[3:],
                              tabargs=ev.namedArgs,
                              priority=ev.priority)
        synths.append(synth)
    return csoundengine.synth.SynthGroup(synths)
