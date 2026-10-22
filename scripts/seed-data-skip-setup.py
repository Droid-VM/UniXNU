#!/usr/bin/env python3
"""Make a Data seed that boots to SpringBoard instead of Setup: the preference changes github.com/dipa-studio/ios-sweet
(tools/sweet-root-tweaks.py) established on hardware, applied to a copy of an existing packed Data seed.

  seed-data-skip-setup.py BASE.dbr OUTPUT.dbr

BASE is a DBRDISK1 packed APFS container (drivers/DarwinBoot/Test/packed_ramdisk.py format). It is expanded to a raw image, attached
read-write on this Mac, the preference files below are merged into /private/var/mobile/Library/Preferences, and the result is packed
again. The volume UUID is unchanged. Nothing is written to Git or to the OS inputs; BASE is not modified.
  com.apple.purplebuddy            SetupDone, SetupFinishedAllSteps, SetupVersion 11, daemonizeNeedsToRun false   (skips Setup)
  com.apple.purplebuddy.notbackedup  the mini-buddy flags false
  com.apple.managedconfiguration.notbackedup  LockdownActivationIndicatesCloudConfigurationAvailable false
  com.apple.springboard            BKNoWatchdogs, FBLaunchWatchdogExceptions{com.apple.purplebuddy: 100}, MCForceShowCloudConfigurationUI false
  com.apple.migration              LastSystemVersion (no data migration on every boot)
and the container directories under /private/var/containers that on a device live on the data volume.
"""
import hashlib, json, os, plistlib, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'drivers/DarwinBoot/Test'))
import packed_ramdisk

BUILD, VERSION = '21H461', '17.7.11'
MOBILE = 501

def merge_pref(var, name, values):
    path = var / 'mobile/Library/Preferences' / (name + '.plist')
    path.parent.mkdir(parents=True, exist_ok=True)
    data = plistlib.loads(path.read_bytes()) if path.exists() else {}
    data.update(values)
    path.write_bytes(plistlib.dumps(data, fmt=plistlib.FMT_BINARY))
    os.chmod(path, 0o600)
    print('pref', name, sorted(values))

def write_network(var):
    """Seed a SystemConfiguration Ethernet service on en0 set to DHCP, so configd/IPConfiguration brings the guest's
    virtio-net interface (UniXNU-droidvm package/DroidVMVirtioEthernet.cpp, an IOEthernetController) up and leases an address
    from the br-wifi network it is bridged to. configd brings an interface up only for a service in the current set, and none
    is created for a new Ethernet interface on its own. The NetworkInterfaces.plist entry, which InterfaceNamer matches by the
    MAC (the VM's --net mac), is what names the interface en0 for that service; with another MAC it would come up as en1, down."""
    import uuid
    scdir = var / 'preferences/SystemConfiguration'
    scdir.mkdir(parents=True, exist_ok=True)
    def load(name):
        f = scdir / name
        return (plistlib.loads(f.read_bytes()) if f.exists() else {}), f
    prefs, pfile = load('preferences.plist')
    prefs.setdefault('__VERSION__', 20191120)
    service, setid = str(uuid.uuid4()).upper(), str(uuid.uuid4()).upper()
    prefs['CurrentSet'] = '/Sets/' + setid
    prefs['NetworkServices'] = {service: {
        'DNS': {}, 'IPv4': {'ConfigMethod': 'DHCP'}, 'IPv6': {'ConfigMethod': 'Automatic'},
        'Interface': {'DeviceName': 'en0', 'Hardware': 'Ethernet', 'Type': 'Ethernet', 'UserDefinedName': 'Ethernet'},
        'Proxies': {'ExceptionsList': ['*.local', '169.254/16'], 'FTPPassive': 1},
        'UserDefinedName': 'Ethernet'}}
    prefs['Sets'] = {setid: {'Network': {
        'Global': {'IPv4': {'ServiceOrder': [service]}},
        'Service': {service: {'__LINK__': '/NetworkServices/' + service}}}, 'UserDefinedName': 'Automatic'}}
    pfile.write_bytes(plistlib.dumps(prefs, fmt=plistlib.FMT_BINARY)); os.chmod(pfile, 0o644)
    interfaces, ifile = load('NetworkInterfaces.plist')
    interfaces.setdefault('__VERSION__', 20191120)
    interfaces['Interfaces'] = [{
        'Active': True, 'BSD Name': 'en0', 'IOBuiltin': False, 'IOInterfaceNamePrefix': 'en',
        'IOInterfaceType': 6, 'IOInterfaceUnit': 0, 'IOMACAddress': bytes([0x02, 0x00, 0x00, 0xaa, 0xbb, 0x01]),
        'SCNetworkInterfaceInfo': {'UserDefinedName': 'Ethernet'}, 'SCNetworkInterfaceType': 'Ethernet'}]
    ifile.write_bytes(plistlib.dumps(interfaces, fmt=plistlib.FMT_BINARY)); os.chmod(ifile, 0o644)
    print('network service en0 DHCP (service %s)' % service)


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    base, output = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
    if output.exists():
        sys.exit('Refuse to overwrite ' + str(output))
    blob = base.read_bytes()
    logical, extents = packed_ramdisk.regions(blob)
    with tempfile.TemporaryDirectory(prefix='data-seed-', dir=str(ROOT / 'build')) as td:
        raw = Path(td) / 'seed.raw'
        with raw.open('wb') as f:
            f.truncate(logical)
            for offset, data in extents:
                f.seek(offset)
                f.write(data)
        mount = Path(td) / 'mount'
        mount.mkdir()
        attached = plistlib.loads(subprocess.check_output(
            ['hdiutil', 'attach', '-imagekey', 'diskimage-class=CRawDiskImage', '-nobrowse', '-owners', 'off',
             '-mountpoint', str(mount), '-plist', str(raw)]))
        device = attached['system-entities'][0]['dev-entry']
        try:
            if not (mount / 'mobile').is_dir():
                sys.exit('The Data seed has no /mobile; wrong base image?')
            merge_pref(mount, 'com.apple.migration', {
                'LastSystemVersion': BUILD,
                'DMUserDataDisposition': {'SyntheticDidUpgrade': False, 'buildVersion': BUILD}})
            merge_pref(mount, 'com.apple.springboard', {
                'BKNoWatchdogs': True,
                'FBLaunchWatchdogExceptions': {'com.apple.purplebuddy': 100.0},
                'MCForceShowCloudConfigurationUI': False})
            merge_pref(mount, 'com.apple.purplebuddy', {
                'SetupDone': True, 'SetupFinishedAllSteps': True, 'SetupVersion': 11, 'daemonizeNeedsToRun': False})
            merge_pref(mount, 'com.apple.purplebuddy.notbackedup', {
                k: False for k in ('ShouldRunDiagnosticsMiniBuddy', 'ShouldRunHSA2MiniBuddy', 'ShouldRunKeychainSyncMiniBuddy',
                                   'ShouldRunMesaMiniBuddy', 'ShouldRunPaymentMiniBuddy', 'ShouldRunStandardMiniBuddy',
                                   'SilentICDPUpgrade')})
            merge_pref(mount, 'com.apple.managedconfiguration.notbackedup', {
                'LockdownActivationIndicatesCloudConfigurationAvailable': False})
            write_network(mount)
            for sub in ('Bundle', 'Data/System', 'Shared/SystemGroup', 'Temp'):
                d = mount / 'containers' / sub
                if not d.is_dir():
                    d.mkdir(parents=True)
                    os.chmod(d, 0o755)
                    print('mkdir', d.relative_to(mount))
        finally:
            subprocess.run(['hdiutil', 'detach', device], check=True)
        if raw.read_bytes()[32:36] != b'NXSB':
            sys.exit('The edited image is no APFS container any more')
        metadata = packed_ramdisk.pack(raw, output)
    report = dict(base_sha256=packed_ramdisk.inspect(base)[0]['sha256'], output=metadata,
                  preferences=['com.apple.migration', 'com.apple.springboard', 'com.apple.purplebuddy',
                               'com.apple.purplebuddy.notbackedup', 'com.apple.managedconfiguration.notbackedup'], network='en0 DHCP',
                  build=BUILD, source='github.com/dipa-studio/ios-sweet tools/sweet-root-tweaks.py')
    output.with_suffix('.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))

if __name__ == '__main__':
    main()
