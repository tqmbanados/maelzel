from __future__ import annotations
import dataclasses
import math
import re
import textwrap as _textwrap

import emlib.textlib
import emlib.textlib as _textlib
from . import presetutils
from ._common import logger
from . import _util
import csoundengine

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from typing import *




_INSTR_INDENT = "  "


@dataclasses.dataclass
class AudiogenAnalysis:
    signals: Set[str]
    numSignals: int
    minSignal: int
    maxSignal: int
    numOutchs: int
    needsRouting: bool
    numOutputs: int


def analyzeAudiogen(audiogen: str, check=False) -> AudiogenAnalysis:
    """
    Analyzes the audio generating part of an instrument definition,
    returns the analysis results as a dictionary

    Args:
        audiogen: as passed to play.defPreset
        check: if True, will check that audiogen is well formed

    Returns:
        an instance of AudiogenAnalysis (normally
        minsignal+numsignals = maxsignal)
    """
    audiovarRx = re.compile(r"\baout[1-9]\b")
    outOpcodeRx = re.compile(r"^.*\b(outch)\b")
    audiovarsList = []
    numOutchs = 0
    for line in audiogen.splitlines():
        foundAudiovars = audiovarRx.findall(line)
        audiovarsList.extend(foundAudiovars)
        outOpcode = outOpcodeRx.fullmatch(line)
        if outOpcode is not None:
            opcode = outOpcode.group(0)
            args = line.split(opcode)[1].split(",")
            assert len(args) % 2 == 0
            numOutchs = len(args) // 2

    if not audiovarsList:
        logger.debug(f"Invalid audiogen: no output audio signals (aoutx): {audiogen}")
        needsRouting = False
        audiovars = set()
    else:
        audiovars = set(audiovarsList)
        needsRouting = numOutchs == 0

    chans = [int(v[4:]) for v in audiovars]
    maxchan = max(chans) if chans else 0
    # check that there is an audiovar for each channel
    if check:
        if len(audiovars) == 0:
            raise ValueError("audiogen defines no output signals (aout_ variables)")

        for i in range(1, maxchan+1):
            if f"aout{i}" not in audiovars:
                raise ValueError("Not all channels are defined", i, audiovars)

    numSignals = len(audiovars)
    if needsRouting:
        numOuts = int(math.ceil(numSignals/2))*2
    else:
        numOuts = numOutchs

    return AudiogenAnalysis(signals=audiovars,
                            numSignals=numSignals,
                            minSignal=min(chans) if chans else 0,
                            maxSignal=max(chans) if chans else 0,
                            numOutchs=numOutchs,
                            needsRouting=needsRouting,
                            numOutputs=numOuts)


def _instrNameFromPresetName(presetName: str) -> str:
    # an Instr derived from a PresetDef gets a prefix to prevent collisions
    # with Instrs a user might want to define in the same Session
    return f'preset.{presetName}'


def _makePresetBody(audiogen: str,
                    numsignals: int,
                    withEnvelope=True,
                    withPanning=True,
                    withRouting=True,
                    epilogue='') -> str:
    """
    Generate the presets body

    Args:
        audiogen: the audio generating part, needs to declare aout1, aout2, ...
        numsignals: the number of audio signals used in augiogen (generaly
            the result of analyzing the audiogen via `analyzeAudiogen`
        withEnvelope: do we generate envelope code?
        withPanning: do we generate panning code?
        withRouting: do we send the audio to outch?
        epilogue: any code needed **after** routing (things like turning off
            the event when silent)

    Returns:
        the presets body
    """
    # TODO: generate user pargs
    prologue = r'''
;5        6       7      8      9     0    1       2        3          4        
idataidx_,inumbps,ibplen,igain_,ichan,ipos,ifadein,ifadeout,ipchintrp_,ifadekind_ passign 5
idatalen_ = inumbps * ibplen
iArgs[] passign idataidx_, idataidx_ + idatalen_
ilastidx = idatalen_ - 1
iTimes[]     slicearray iArgs, 0, ilastidx, ibplen
iPitches[]   slicearray iArgs, 1, ilastidx, ibplen
iAmps[]      slicearray iArgs, 2, ilastidx, ibplen

k_time = (timeinstk() - 1) * ksmps/sr  ; use eventtime (csound 6.18)

if ipchintrp_ == 0 then      
    ; linear midi interpolation    
    kpitch, kamp bpf k_time, iTimes, iPitches, iAmps
    kfreq mtof kpitch
elseif (ipchintrp_ == 1) then  ; cos midi interpolation
    kidx bisect k_time, iTimes
    kpitch interp1d kidx, iPitches, "cos"
    kamp interp1d kidx, iAmps, "cos"
    kfreq mtof kpitch
elseif (ipchintrp_ == 2) then  ; linear freq interpolation
    iFreqs[] mtof iPitches
    kfreq, kamp bpf k_time, iTimes, iFreqs, iAmps
    kpitch ftom kfreq
elseif (ipchintrp_ == 3) then  ; cos freq interpolation
    kidx bisect k_time, iTimes
    kfreq interp1d kidx, iFreqs, "cos"
    kamp interp1d kidx, iAmps, "cos"
    kpitch ftom kfreq
endif

ifadein = max:i(ifadein, 1/kr)
ifadeout = max:i(ifadeout, 1/kr)
    '''
    envelope1 = r"""
    if (ifadekind_ == 0) then
        aenv_ linsegr 0, ifadein, igain_, ifadeout, 0
    elseif (ifadekind_ == 1) then
        aenv_ cosseg 0, ifadein, igain_, p3-ifadein-ifadeout, igain_, ifadeout, 0
        aenv_ *= linenr:a(1, 0, ifadeout, 0.01)
    elseif (ifadekind_ == 2) then
        aenv_ transeg 0, ifadein*.5, 2, igain_*0.5, ifadein*.5, -2, igain_, p3-ifadein-ifadeout, igain_, 1, ifadeout*.5, 2, igain_*0.5, ifadeout*.5, -2, 0 	
        aenv_ *= linenr:a(1, 0, ifadeout, 0.01)
    endif
    """
    parts = [prologue]
    if numsignals == 0:
        withEnvelope = 0
        withPanning = 0
        withRouting = 0

    if withEnvelope:
        parts.append(envelope1)
        audiovars = [f'aout{i}' for i in range(1, numsignals+1)]
        if withPanning and withRouting:
            # apply envelope at the end
            indentation = emlib.textlib.getIndentation(audiogen)
            prefix = ' ' * indentation
            envlines = [f'{prefix}{audiovar} *= aenv_' for audiovar in audiovars]
            audiogen = '\n'.join([audiogen] + envlines)
        else:
            audiogen = presetutils.embedEnvelope(audiogen, audiovars, envelope="aenv_")

    parts.append(audiogen)
    if withPanning:
        panning = ''
        if numsignals == 1:
            panning = r'aout1, aout2 pan2 aout1, ipos'
        elif numsignals == 2:
            panning = r'aout1, aout2 panstereo aout1, aout2, ipos'
        else:
            logger.error("Panning is only supported for 1 or 2 signals at the moment")
        parts.append(panning)

    if withRouting:
        if numsignals == 1:
            routing = r"""
            if (ipos <= 0) then
                outch ichan, aout1
            else
                aL_, aR_ pan2 aout1, ipos
                outch ichan, aL_, ichan+1, aR_
            endif
            """
        elif numsignals == 2:
            routing = r"""
            ipos = (ipos == -1) ? 0.5 : ipos
            aL_, aR_ panstereo aout1, aout2, ipos
            outch ichan, aL_, ichan+1, aR_
            """
        else:
            logger.error("Invalid preset. Audiogen:\n")
            logger.error(_textlib.reindent(audiogen, prefix="    "))
            raise ValueError(f"numsignals can be either 1 or 2 at the moment "
                             f"if automatic routing is enabled (got {numsignals})")
        parts.append(routing)
    if epilogue:
        parts.append(epilogue)
    parts = [_textlib.reindent(part) for part in parts]
    body = '\n'.join(parts)
    return body


class PresetDef:

    userPargsStart = 15

    """
    An instrument preset definition
    
    Normally a user does not create a PresetDef directly. A PresetDef is created
    when calling :func:`~maelzel.core.presetman.defPreset` .
    
    A Preset is aware the pitch and amplitude of a SynthEvent and generates all the
    interface code regarding play parameters like panning position, fadetime, 
    fade shape, gain, etc. The user only needs to define the audio generating code and
    any init code needed (global code needed by the instrument, like soundfiles which 
    need to be loaded, buffers which need to be allocated, etc). A Preset can define
    any number of extra parameters (transposition, filter cutoff frequency, etc.). 
    
    Attributes:
        body: the body of the instrument preset
        name: the name of the preset
        init: any init code (global code)
        includes: #include files
        audiogen: the audio generating code
        args: a dict(arg1: value1, arg2: value2, ...). Parameter names
            need to follow csound's naming: init-only parameters need to start with 'i',
            variable parameters need to start with 'k', string parameters start with 'S'. 
        description: a description of this instr definition
        envelope: If True, apply an envelope as determined by the fadein/fadeout
            play arguments. 
        routing: if True routing code is generated to output the audio
            to its corresponding channel. If False the audiogen code should
            be responsible for applying panning and sending the audio to 
            an output channel, bus, etc.
            
    """
    def __init__(self,
                 name: str,
                 audiogen: str = None,
                 init: str = None,
                 includes: list[str] = None,
                 epilogue: str = '',
                 args: Dict[str, float] = None,
                 numsignals: int = None,
                 numouts: int = None,
                 description: str = "",
                 builtin=False,
                 properties: dict[str, Any] = None,
                 envelope=True,
                 routing=True
                 ):
        assert isinstance(audiogen, str)

        audiogen = _textwrap.dedent(audiogen)
        audiogenInfo = analyzeAudiogen(audiogen)
        if audiogenInfo.numSignals == 0:
            envelope = False
            routing = False
        body = _makePresetBody(audiogen,
                               numsignals=audiogenInfo.numSignals,
                               withEnvelope=envelope,
                               withRouting=routing,
                               withPanning=routing,
                               epilogue=epilogue)

        self.name = name
        self.init = init
        self.includes = includes
        self.audiogen = audiogen.strip()
        self.epilogue = epilogue
        self.args = args
        self.userDefined = not builtin
        self.numsignals = numsignals if numsignals is not None else audiogenInfo.numSignals
        self.description = description
        self.numouts = numouts if numouts is not None else audiogenInfo.numOutputs
        self._consolidatedInit: str = ''
        self._instr: Optional[csoundengine.instr.Instr] = None
        self.body = body
        self.properties: dict[str, Any] = properties or {}
        self.hasRouting = routing

    def __repr__(self):
        lines = []
        descr = f"({self.description})" if self.description else ""
        lines.append(f"Preset: {self.name}  {descr}")
        info = [f"hasRouting={self.hasRouting}"]
        if self.properties:
            info.append(f"properties={self.properties}")
        lines.append(_textwrap.indent(', '.join(info), "    "))

        if self.includes:
            includesline = ", ".join(self.includes)
            lines.append(f"  includes: {includesline}")
        if self.init:
            lines.append(f"  init: {self.init.strip()}")
        if self.args:
            tabstr = ", ".join(f"{key}={value}" for key, value in self.args.items())
            lines.append(f"  |{tabstr}|")
        audiogen = _textwrap.indent(self.audiogen, _INSTR_INDENT)
        lines.append(audiogen)
        if self.epilogue:
            lines.append(f"  epilogue:")
            lines.append(_textwrap.indent(self.epilogue, "    "))
        return "\n".join(lines)

    def _sortOrder(self):
        return 1 - int(self.userDefined), 1-int(self.isSoundFont()), self.name

    def isSoundFont(self) -> bool:
        """
        Is this Preset based on a soundfont?

        Returns:
            True if this Preset is based on a soundfont
        """
        return re.search(r"\bsfplay(3m|m|3)?\b", self.body) is not None

    def _repr_html_(self, theme=None, showGeneratedCode=False):
        if self.description:
            descr = _util.htmlSpan(self.description, italic=True, color=':grey3')
        else:
            descr = ''
        ps = [f"Preset: <b>{self.name}</b> {descr}<br>"]
        info = [f"hasRouting={self.hasRouting}"]
        if self.properties:
            info.append(f"properties={self.properties}")
        infostr = "(" + ', '.join(info) + ")"
        fontsize = "92%"
        ps.append(f"<code>{_INSTR_INDENT}{_util.htmlSpan(infostr, color=':grey1', fontsize=fontsize)}</code>")

        if self.init:
            init = _textwrap.indent(self.init, _INSTR_INDENT)
            inithtml = csoundengine.csoundlib.highlightCsoundOrc(init, theme=theme)
            ps.append(rf"init: {inithtml}")
            ps.append("audiogen:")
        if self.args:
            argstr = ", ".join(f"{key}={value}" for key, value in self.args.items())
            argstr = f"{_INSTR_INDENT}|{argstr}|"
            arghtml = csoundengine.csoundlib.highlightCsoundOrc(argstr, theme=theme)
            arghtml = _util.htmlSpan(arghtml, fontsize=fontsize)
            ps.append(arghtml)
        body = self.audiogen if not showGeneratedCode else self.getInstr().body
        body = _textwrap.indent(body, _INSTR_INDENT)
        bodyhtml = csoundengine.csoundlib.highlightCsoundOrc(body, theme=theme)
        bodyhtml = _util.htmlSpan(bodyhtml, fontsize=fontsize)
        ps.append(bodyhtml)
        if self.epilogue:
            ps.append("epilogue:")
            epilogue = _textwrap.indent(self.epilogue, _INSTR_INDENT)
            ps.append(csoundengine.csoundlib.highlightCsoundOrc(epilogue, theme=theme))
        return "\n".join(ps)

    def instrName(self) -> str:
        """
        Returns the Instr name corresponding to this Preset
        """
        return _instrNameFromPresetName(self.name)

    def getInstr(self, namedArgsMethod: str = 'pargs') -> csoundengine.Instr:
        """
        Returns the csoundengine's Instr corresponding to this PresetDef

        This method is cached, the Instr is constructed only the first time

        Args:
            namedArgsMethod: one of 'table' or 'pargs'. None will fallback
                to the config (key: 'play.namedArgsMethod')

        Returns:
            the csoundengine.Instr corresponding to this PresetDef

        """
        if self._instr:
            return self._instr
        instrName = self.instrName()

        if namedArgsMethod == 'table':
            self._instr = csoundengine.Instr(name=instrName,
                                             body=self.body,
                                             init=self.globalCode(),
                                             tabargs=self.args,
                                             numchans=self.numouts)
        elif namedArgsMethod == 'pargs':
            self._instr = csoundengine.Instr(name=instrName,
                                             body=self.body,
                                             init=self.globalCode(),
                                             args=self.args,
                                             userPargsStart=PresetDef.userPargsStart,
                                             numchans=self.numouts)
        else:
            raise ValueError(f"namedArgsMethod expected 'table' or 'pargs', "
                             f"got {namedArgsMethod}")
        return self._instr

    def globalCode(self) -> str:
        if self._consolidatedInit:
            return self._consolidatedInit
        self._consolidatedInit = _consolidateInitCode(self.init, self.includes)
        return self._consolidatedInit

    def save(self) -> str:
        """
        Save this preset to disk

        All presets are saved to the presets path. Saved presets will be available
        in a future session

        .. seealso:: :func:`maelzel.core.workspace.presetsPath`
        """
        from . import presetman
        savedpath = presetman.presetManager.savePreset(self.name)
        return savedpath


def _consolidateInitCode(init: str, includes: list[str]) -> str:
    if includes:
        includesCode = _genIncludes(includes)
        init = _textlib.joinPreservingIndentation((includesCode, init))
    return init


def _genIncludes(includes: list[str]) -> str:
    return "\n".join(_makeIncludeLine(inc) for inc in includes)


def _makeIncludeLine(include: str) -> str:
    if include.startswith('"'):
        return f'#include {include}'
    else:
        return f'#include "{include}"'
