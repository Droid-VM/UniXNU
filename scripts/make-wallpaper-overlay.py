#!/usr/bin/env python3
"""Build the overlay for the default iPadOS 17 wallpaper's layered animation with PNG images in place of its HEIC ones.

  make-wallpaper-overlay.py SYSTEM_ROOT OUTPUT_DIR [ipados27|ipados27-floating]

SYSTEM_ROOT is the mounted OS volume of the IPSW (ipsw mount fs). The iPad7,1 variant of wallpaper 7570 (Flat, two image layers) is the
one PosterBoard picks on this device; its images are HEIC, which ImageIO cannot decode without a hardware HEVC decoder. OUTPUT_DIR gets
the animation's index.xml, assetManifest.caml and a main.caml that points at J120.png and J120-Dark.png next to it (converted with sips,
so this runs on macOS, and scaled to EDGE pixels). make-gpu-addon.py --wallpaper OUTPUT_DIR carries it to the kernel, which mounts it over the sealed animation.
With ipados27 the overlay is for iPadOS 27's variant of the same wallpaper (1032w-1376h, a background and a floating layer): its background
layer, whose two images (Background Light/Dark, 1620 x 1080) become Background-Light.png and Background-Dark.png at their own size.
With ipados27-floating it is the same variant's floating layer: its 13 images (blurred colour shapes with alpha) become PNG images, the
ones wider than 1000 pixels at a third of their size (the layers stretch their contents to their bounds; full size does not fit the boot
payload)."""
import subprocess
import sys
from pathlib import Path

PROFILES = {
    None: (('Library/Wallpaper/Collections/iPadOS_17~ipad.wallpaperCollection/Wallpapers/7570.iPadOS_17_Flat-1024w-1366h@2x~ipad.wallpaper/'
            '7570.iPadOS_17_Flat_Background-1024w-1366h@2x~ipad.ca'), {'J120': 'J120', 'J120-Dark': 'J120-Dark'}),
    'ipados27': (('Library/Wallpaper/Collections/iPadOS_17~ipad.wallpaperCollection/Wallpapers/7570.iPadOS_17-1032w-1376h@2x~ipad.wallpaper/'
                  '7570.iPadOS_17_Background-1032w-1376h@2x~ipad.ca'),
                 {'Background%20Light': 'Background-Light', 'Background%20Dark': 'Background-Dark'}),
    'ipados27-floating': (('Library/Wallpaper/Collections/iPadOS_17~ipad.wallpaperCollection/Wallpapers/7570.iPadOS_17-1032w-1376h@2x~ipad.wallpaper/'
                           '7570.iPadOS_17_Floating-1032w-1376h@2x~ipad.ca'),
                          {name.replace(' ', '%20'): name.replace(' ', '-') for name in (
                              'Aqua Dark', 'Aqua Light', 'Green Dark', 'Green Light', 'Orange Dark', 'Orange Light', 'Red Dark', 'Red Light',
                              'Red Landscape Patch', 'Red Yellow Edge Patch Dark', 'Red Yellow Edge Patch Light', 'Yellow Dark', 'Yellow Light')}),
}
# Longest edge of each profile's PNG images: None keeps the image's own size, a negative value divides images wider than its absolute
# value by three.
EDGES = {None: 2732,   # iPadOS 17's HEIC images are 3416 pixels square; PNG at that size does not fit the boot payload
         'ipados27': None, 'ipados27-floating': -1000}


def main():
    if len(sys.argv) not in (3, 4) or len(sys.argv) == 4 and sys.argv[3] not in PROFILES:
        sys.exit(__doc__)
    profile = sys.argv[3] if len(sys.argv) == 4 else None
    animation, images = PROFILES[profile]
    edge = EDGES[profile]
    source, output = Path(sys.argv[1]) / animation, Path(sys.argv[2])
    if not (source / 'main.caml').is_file():
        sys.exit('No wallpaper animation at ' + str(source))
    if output.exists():
        sys.exit('Refuse to overwrite ' + str(output))
    output.mkdir(parents=True)
    for name in ('index.xml', 'assetManifest.caml'):
        (output / name).write_bytes((source / name).read_bytes())
    main_caml = (source / 'main.caml').read_text()
    for image, png in images.items():
        main_caml = main_caml.replace(f'"/assets/{image}.HEIC"', f'"/{png}.png"').replace(f'"assets/{image}.HEIC"', f'"{png}.png"')
        heic = source / 'assets' / (image.replace('%20', ' ') + '.HEIC')
        size = []
        if edge and edge > 0:
            size = ['-Z', str(edge)]
        elif edge:
            width = int(subprocess.check_output(['sips', '-g', 'pixelWidth', str(heic)], text=True).split()[-1])
            if width > -edge:
                size = ['-Z', str(width // 3)]
        subprocess.run(['sips', *size, '-s', 'format', 'png', str(heic), '--out', str(output / f'{png}.png')],
                       check=True, stdout=subprocess.DEVNULL)
    if '.HEIC"/>' in main_caml:
        sys.exit('main.caml still points at a HEIC image')
    (output / 'main.caml').write_text(main_caml)
    for path in sorted(output.iterdir()):
        print(f'{path.stat().st_size:>10} {path.name}')


if __name__ == '__main__':
    main()
