# XnuPrelink

Start with the [adaptation procedure](/porting/platform) for board inputs, source changes, build integration and verification order.

Kernel prelink emitter and ABI tooling.

[Source repository](https://github.com/UniXNU/XnuPrelink).

XnuPrelink binds the built XNU kernel to selected original kexts and writes the prelink plan. The OS profile supplies the kernelcache pin, roots, slide, and ABI settings.

For arm64e fileset kernel collections, `emit_prelink.py` selects the fileset
emitter and generates a function/data map. `--fileset-map FILE` supplies an
existing map with matching input hashes and matcher version. The selected roots
and their dependency closure are retained.
Code comparison accounts for complete compiler-inserted PAC failure checks;
the emitted native code retains them. Both emitter entry points validate the
matching code signature before reusing a map.
`--fileset-reference FILE` supplies an additional kernel with symbols for code
comparison. It transfers unambiguous names from unique code matches and resolves
those names in the actual native kernel. The map records the reference hash.

The emitter places native segments within the collection's code, constant and
data ranges, applies native split-segment records, rebuilds chained fixups and
function starts, and recomputes corecrypto's code HMAC. The collection root uses
the native link address. The ordinary prelink `--slide` value does not apply to
filesets. The native input executable is unchanged.

Missing mappings stop emission by default. `--allow-missing` produces an
experimental image and the JSON report lists every unresolved target by kext.
An image that passes structural checks still requires complete mappings and
hardware validation before boot compatibility can be established.

`unixnu kernel --skip-prelink` stages the native executable as `kernel.macho`.
`unixnu prelink --skip-prelink` stages an existing build. This mode does not add
the original kexts and records `native-only` beside the output.

## Integration

The catalog records implementation scope, hardware and OS requirements, board dependencies, revision, build stage, outputs, and runtime identifiers. The selected profile orders build commands by dependency.

```sh
./unixnu build prelink -D ../UniXNU-sheng
```

[Component catalog](/reference/components)
