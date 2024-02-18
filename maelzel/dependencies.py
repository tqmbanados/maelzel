"""
Module to check dependencies of maelzel
"""
from __future__ import annotations
import shutil
from pathlib import Path
import os
import sys
import logging
from datetime import datetime

import csoundengine

from maelzel import _state
import functools


logger = logging.getLogger('maelzel')


def csoundLibVersion() -> int | None:
    """
    Query the version of the installed csound lib, or None if not found

    Returns:
        the version times 1000 (so 6.19 = 6190) or None if csound was not found
    """
    try:
        import ctcsound7
    except Exception as e:
        logger.error(f"Could not import ctcsound7: {e}")
        return None
    return ctcsound7.VERSION


def checkCsound(minversion="6.17", checkBinaryInPath=True) -> str:
    """
    Returns an empty string if csound is installed, otherwise an error message

    Args:
        minversion: min. csound version, as "{major}.{minor}"
        checkBinaryInPath: if True, check that the binary 'csound' is in the path

    Returns:
        an error message, or an empty str if csound is installed and the version
        is >= than the version given
    """
    logger.debug("Checking the csound installation")
    if not isinstance(minversion, str) or minversion.count('.') != 1:
        raise ValueError(f"minversion should be of the form <major>.<minor>, got {minversion}")
    version = csoundLibVersion()
    if version is None:
        logger.error("Could not find csound library (csoundLibVersion returned None)")
        return "Could not find csound library"
    major, minor = map(int, minversion.split("."))
    versionid = major * 1000 + minor * 10
    if version < versionid:
        return f"Csound is too old (detected version: {version/1000}, " \
               f"min. version: {minversion})"

    if checkBinaryInPath and shutil.which("csound") is None:
        return "Could not find csound in the path (checked via shutil.which)"

    return ""


def vampPluginsInstalled(cached=True) -> bool:
    """
    Are the needed VAMP plugins installed?

    Args:
        cached: if True, results are cached

    Returns:
        True if the needed VAMP plugins are installed

    """
    from maelzel.snd import vamptools
    return 'pyin:pyin' in vamptools.listPlugins(cached=cached)


def checkVampPlugins(fix=True) -> str:
    """
    Check if the needed VAMP plugins are installed

    Args:
        fix: if True, VAMP plugins are installed if they are not present

    Returns:
        an error string or an empty string if everything is ok

    """
    logger.debug("Checking vamp plugins")
    if vampPluginsInstalled():
        return ""
    if not fix:
        return ("Vamp plugin 'pyin' not found. Install it from "
                "https://code.soundsoftware.ac.uk/projects/pyin.")
    try:
        installVampPlugins()
    except RuntimeError as err:
        logger.error(f"Error while trying to install vamp plugins: {err}")
        return str(err)


def checkLilypond() -> str:
    """
    Check if lilypond is installed

    Returns:
        an error string if something went wrong, an empty string if lilypond is
        installed
    """
    logger.debug("Checking lilypond")
    from maelzel.music import lilytools
    lilybin = lilytools.findLilypond()
    if not lilybin:
        return "Could not find lilypond"
    logger.debug(f"lilypond ok. lilypond binary: {lilybin}")
    return ""


def _copyFiles(files: list[str], dest: str, verbose=False) -> None:
    assert os.path.isdir(dest)
    for f in files:
        if verbose:
            print(f"Copying file {f} to {dest}")
        shutil.copy(f, dest)


def maelzelRootFolder() -> Path:
    """
    Returns the root folder of the maelzel installation
    """
    return Path(os.path.split(__file__)[0])


def checkCsoundPlugins(fix=True) -> str:
    """
    Checks that the needed csound plugins are installed

    Returns:
        an error string if failed, an empty string if OK
    """
    logger.debug("Checking dependencies for csound plugins")
    import risset
    logger.debug("Reading risset's main index")
    rissetindex = risset.MainIndex(update=True)
    neededopcodes = {
        'dict_get': 'klib',
        'presetinterp': 'else',
        'weightedsum': 'else',
        'poly': 'poly'
    }
    errors = []
    installedopcodes = {opcode.name for opcode in rissetindex.defined_opcodes()
                        if opcode.installed}
    for opcodename, pluginname in neededopcodes.items():
        if opcodename not in installedopcodes:
            if fix:
                print(f"Opcode {opcodename} (from plugin {pluginname}) not found, I will "
                      f"try to install it now...")
                plugin = rissetindex.plugins[pluginname]
                errmsg = rissetindex.install_plugin(plugin)
                if errmsg:
                    logger.error(f"Could not install plugin {pluginname}, error: {errmsg}")
                    errors.append(errmsg)
                else:
                    print(f"... Installed {pluginname} OK")
            else:
                logger.error(f"Opcode {opcodename} (from plugin {pluginname}) not found! "
                             f"Called with fix=False, so the issue will not be fixed")
                errors.append(f"Opcode {opcodename} (plugin: {pluginname}) not found, fix=False")
    if errors:
        return "\n".join(errors)
    return ''


def dataPath() -> Path:
    return maelzelRootFolder() / 'data'


def vampPluginsDataFolder() -> Path:
    subfolder = {
        'darwin': 'macos',
        'windows': 'windows',
        'linux': 'linux'
    }.get(sys.platform, None)
    return dataPath() / 'vamp' / subfolder


def checkDataFiles() -> bool:
    data = dataPath()
    if not data.exists():
        print(f"Data path not found: {data}")
        return False
    vampdir = vampPluginsDataFolder()
    if not vampdir.exists():
        print(f"Data folder with vamp plugins not found: {vampdir}")
        return False

    knownfiles = ['pyin.cat', 'pyin.n3']
    for knownfile in knownfiles:
        path = vampdir / knownfile
        if not path.exists():
            print(f"Vamp plugin component {path} not found")
            return False
    return True


def installVampPlugins() -> None:
    """
    Install needed vamp plugins in the user folder

    Raises RuntimeError if there was an error during the installation

    To ease the installation maelzel is actually shipped with the needed
    plugins at the moment. Installation is thus reduced to copying the
    plugins to the correct folder for the platform

    Raises RuntimeError if there is an installation problem
    """
    print("Installing vamp plugins")
    from maelzel.snd import vamptools
    osname, arch = getPlatform()
    if osname == 'darwin':
        subfolder = f'macos-{arch}'
    else:
        subfolder = f'{osname}-{arch}'
    pluginspath = dataPath() / 'vamp' / subfolder
    if not pluginspath.exists():
        raise RuntimeError(f"Could not find vamp plugins for {subfolder}. Folder not found: {pluginspath}")
    components = list(pluginspath.glob("*"))
    if not components:
        raise RuntimeError(f"Plugins not found in our distribution. "
                           f"Plugins folder: {pluginspath}")
    pluginsDest = vamptools.vampFolder()
    os.makedirs(pluginsDest, exist_ok=True)
    print(f"Installing vamp plugins from {pluginspath} to {pluginsDest}")
    logger.info(f"Plugins found: {list(pluginspath.glob('*.n3'))}")
    _copyFiles([component.as_posix() for component in components], pluginsDest, verbose=True)
    # This step will fail since vampyhost cached the pluginloader. We need
    # to reload the module, which we cannot do here
    vampplugins = vamptools.listPlugins(cached=False)
    if 'pyin:pyin' in vampplugins:
        print(f"VAMP plugins installed OK. Detected plugins: {vampplugins}")
    else:
        print(f"VAMP plugins were installed to the user folder: {pluginsDest}, "
              f"but they are not being detected by the built-in vamp host. "
              f"Plugins detected: {vampplugins}")
        print("Components installed: ")
        for comp in components:
            print(f"    {comp.name}")
        msg = ("You might need to restart the python session in order for the installed "
               "plugins to be available to maelzel")
        from maelzel import tui
        tui.panel(msg, margin=(1, 1), padding=(1, 2), bordercolor='red', title="VAMP plugins",
                  width=80)


def installAssets():
    """
    Install necessary assets (csound plugins, vamp plugins, ...)
    """
    checkCsoundPlugins(fix=True)
    checkVampPlugins(fix=True)


def checkDependencies(abortIfErrors=False, fix=True, verbose=False
                      ) -> list[str]:
    """
    Checks the dependencies of all maelzel subpackages

    Returns a list of errors as strings

    Args:
        abortIfErrors: if True, abort the check if any errors are found
            Otherwise all checks are performed even if some checks return
            errors
        fix: if True, try to fix errors along the way, if possible. Some
            fixes might require to restart the current python session
        verbose: if True, display information during the checking process

    Returns:
        a list of errors (or an empty list if no errors where found)
    """
    def _csoundengineDependencies(fix=True):
        ok = csoundengine.checkDependencies(fix=fix)
        return '' if ok else 'csoundengine: dependencies not fullfilled or error during check'

    steps = [
        ('Checking csound', checkCsound),
        ('Checking lilypond', checkLilypond),
        ('Checking external csound plugins', lambda: checkCsoundPlugins(fix=fix)),
        ('Checking vamp plugins', lambda: checkVampPlugins(fix=fix)),
        ('Checking csoundengine dependencies', lambda: _csoundengineDependencies(fix=fix))
    ]

    logger.info("Maelzel - checking dependencies")
    errors = []
    echo = print if verbose else logger.debug
    for msg, step in steps:
        echo(msg)
        err = step()
        if err:
            errors.append(err)
            logger.error(f"  ERROR: {err}")
            if abortIfErrors:
                return errors
        else:
            echo("  -- ok")
    if not errors:
        logger.info("Check Dependencies: everything OK!")
    else:
        logger.error("Errors while checking dependencies:")
        for err in errors:
            logger.error(f"    {err}")

    if not errors:
        _state.state['last_dependency_check'] = datetime.now().isoformat()

    return errors


def printReport(echo=print, updaterisset=False):
    import risset
    import vamp
    from maelzel.music import lilytools
    csoundbin = csoundengine.csoundlib.findCsound()

    echo("Dependencies report")
    echo("-------------------")
    echo(f"Python version: {sys.version}")
    echo(f"Csound library version: {csoundLibVersion()}")
    if csoundbin:
        echo(f"Csound binary: {csoundbin}")
        echo(f"Csound binary version: {csoundengine.csoundlib.getVersion(useApi=False)}")
    else:
        echo("WARNING: csound binary not found")
    echo(f"Lilypond binary: {lilytools.findLilypond()}")
    echo(f"Risset version: {risset.__version__}")
    rissetidx = risset.MainIndex(update=updaterisset)
    rissetidx.list_plugins(installed=True)
    echo(f"Vamp plugins: {vamp.list_plugins()}")


def checkDependenciesIfNeeded(daysSinceLastCheck=0) -> bool:
    """
    Checks dependencies if needed

    A check is needed at the first run and after a given interval

    Args:
        daysSinceLastCheck: only check after so many days since last check

    Returns:
        True if dependencies are installed
    """
    if daysSinceLastCheck == 0 and not _state.isFirstSession():
        logger.info('Skipping dependency check - not first run')
        return True

    lastcheck = datetime.fromisoformat(_state.state['last_dependency_check'])
    if (datetime.now() - lastcheck).days < daysSinceLastCheck:
        logger.debug("Dependency check not needed")
        return True

    errors = checkDependencies(abortIfErrors=False, fix=True)
    if not errors:
        return True
    logger.error(f"Error while checking dependencies: ")
    for err in errors:
        logger.error(f"    {err}")
    return False


@functools.cache
def getPlatform(normalize=True) -> tuple[str, str]:
    """
    Return a string with current platform (system and machine architecture).

    This attempts to improve upon `sysconfig.get_platform` by fixing some
    issues when running a Python interpreter with a different architecture than
    that of the system (e.g. 32bit on 64bit system, or a multiarch build),
    which should return the machine architecture of the currently running
    interpreter rather than that of the system (which didn't seem to work
    properly). The reported machine architectures follow platform-specific
    naming conventions (e.g. "x86_64" on Linux, but "x64" on Windows).
    Use normalize=True to reduce those labels (returns one of 'x86_64', 'arm64', 'x86')
    Example output for common platforms (normalized):

        ('darwin', 'arm64')
        ('darwin', 'x86_64')
        ('linux', 'x86_64')
        ('windows', 'x86_64')
        ...

    Normalizations:

    * aarch64 -> arm64
    * x64 -> x86_64
    * amd64 -> x86_64


    """
    import platform
    import sysconfig

    system = platform.system().lower()
    machine = sysconfig.get_platform().split("-")[-1].lower()
    is_64bit = sys.maxsize > 2 ** 32

    # Normalize system name
    if system == 'win32':
        system = 'windows'

    if system == "darwin":
        # get machine architecture of multiarch binaries
        if any([x in machine for x in ("fat", "intel", "universal")]):
            machine = platform.machine().lower()
        if machine not in ('x86_64', 'arm64'):
            raise RuntimeError(f"Unknown macos architecture '{machine}'")

    elif system == "linux":  # fix running 32bit interpreter on 64bit system
        if not is_64bit and machine == "x86_64":
            machine = "i686"
        elif not is_64bit and machine == "aarch64":
            machine = "armv7l"
        if machine not in ('x86', 'x86_64', 'arm64', 'armv7l', 'i686', 'i386'):
            raise RuntimeError(f"Unknown linux arch '{machine}'")
    elif system == "windows":
        # return more precise machine architecture names
        if machine == "amd64":
            machine = "x86_64"
        elif machine == "win32":
            if is_64bit:
                machine = platform.machine().lower()
            else:
                machine = "x86"
        if machine not in ('x86', 'x86_64', 'arm64', 'i386'):
            raise RuntimeError(f"Unknown windows architecture '{machine}'")
    else:
        raise RuntimeError(f"System '{system}' unknown")

    # some more fixes based on examples in https://en.wikipedia.org/wiki/Uname
    if not is_64bit and machine in ("x86_64", "amd64"):
        if any([x in system for x in ("cygwin", "mingw", "msys")]):
            machine = "i686"
        else:
            machine = "i386"

    if normalize:
        machine = {
            'x64': 'x86_64',
            'aarch64': 'arm64',
            'amd64': 'x86_64'
        }.get(machine, machine)

    return system, machine
