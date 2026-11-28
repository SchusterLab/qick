"""Schuster Lab fork: release notes and firmware manifest shipped with the library.

Standard library only, so it works on a PC without pynq and on a board.

    import qick.lab_release as lr
    print(lr.version())          # e.g. '0.2.450.1'
    print(lr.notes())            # LAB_RELEASE.md
    lr.firmware()                # list of lab firmware builds
    lr.identify('/path/to/qick.bit')   # the matching build, or None
"""
import hashlib
import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))


def version():
    """Library version: upstream number plus the lab release number."""
    from . import __version__
    return __version__


def notes():
    """Text of the lab release notes."""
    with open(os.path.join(_HERE, 'LAB_RELEASE.md')) as f:
        return f.read()


def firmware():
    """Lab firmware builds, newest last: list of dicts (name, release, sha256s, summary, status)."""
    with open(os.path.join(_HERE, 'lab_firmware.json')) as f:
        return json.load(f)['builds']


def _sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def identify(path):
    """Return the manifest entry whose bitstream (.bit) or hardware description (.hwh) matches the file, or None.

    A .bit path is also matched through its .hwh, if one sits next to it."""
    digest = _sha256(path)
    for build in firmware():
        if digest in (build.get('bit_sha256'), build.get('hwh_sha256')):
            return build
    root, ext = os.path.splitext(path)
    if ext == '.bit' and os.path.exists(root + '.hwh'):
        digest = _sha256(root + '.hwh')
        for build in firmware():
            if digest == build.get('hwh_sha256'):
                return build
    return None
