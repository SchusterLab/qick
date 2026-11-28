"""Lab version number, release notes and firmware manifest ship with the library."""
import json, os, re, sys
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'qick_lib'))
import qick.lab_release as lr

def test_version_is_upstream_plus_lab():
    up = open(os.path.join(ROOT, 'qick_lib/qick/VERSION')).read().strip()
    lab = open(os.path.join(ROOT, 'qick_lib/qick/LAB_VERSION')).read().strip()
    assert re.fullmatch(r'\d+', lab)
    variant = os.path.join(ROOT, 'qick_lib/qick/LAB_VARIANT')
    suffix = ''
    if os.path.exists(variant) and open(variant).read().strip():
        name = open(variant).read().strip()
        assert re.fullmatch(r'[a-z0-9]+([.][a-z0-9]+)*', name), 'variant name must be a PEP 440 local label'
        suffix = '+' + name
    assert lr.version() == up + '.' + lab + suffix

def test_notes_name_this_version():
    v = lr.version()
    if not v.endswith('.0'):
        assert '## ' + v + ' ' in lr.notes()

def test_manifest_and_identify(tmp_path):
    builds = lr.firmware()
    assert builds and all(re.fullmatch(r'[0-9a-f]{64}', b['bit_sha256']) for b in builds)
    f = tmp_path / 'x.bit'; f.write_bytes(b'not firmware')
    assert lr.identify(str(f)) is None
    bit = os.path.join(ROOT, 'build', builds[0]['name'], 'qick_216_slab.bit')
    if os.path.exists(bit):
        assert lr.identify(bit)['name'] == builds[0]['name']
