# Provenance

`PROVENANCE.json` records all 45 supplied native source files with both their
upstream SHA-256 and packaged SHA-256. Files are marked `byte_exact` or
`portable_patch`; no source module was silently omitted. The private absolute
origin paths are deliberately excluded.

The input was an owner-supplied native Inbox source snapshot verified by the
integration owner before this public-source preparation. Its private work and
release identifiers are deliberately excluded. The portable patches are restricted to product
naming, installation-local policy/config discovery, explicit enrollment grant
parameters, removal of fixed deployment source paths, and default-off optional
integrations. `comms.hub.store` remains the native coordination core.

`tools/build_provenance.py` reproducibly rebuilds the public manifest from an
authorized private handoff manifest without copying any origin path.

## Attribution gap

The supplied source release did not include a component-level SPDX header,
component copyright notice, or canonical source commit. This package therefore
retains file-level hashes and the verified source class but does not invent
a commit or author attribution. The BORG MIT license is included in this
package; the release owner should resolve component-level attribution before
claiming a more specific native-source origin.
