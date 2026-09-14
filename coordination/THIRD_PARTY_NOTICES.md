# Third-party notices

No third-party source is vendored in the core coordination package.

- Beads (`gastownhall/beads`) v1.2.2 is an external MIT-licensed upstream
  executable and remains the authoritative project/work store. It is not
  copied into the Hub database or this source tree.
- `vncdotool` v1.4.2 is an optional MIT-licensed dependency used only by the
  retained `fleet_desktop` integration. Its resolved dependency versions are
  pinned in `requirements-optional-fleet.lock`; none are installed or imported
  by the default Hub/bootstrap flow. Each installed dependency remains under
  its own package license.

The native Hub/bootstrap core has no third-party Python package dependency.
The BORG MIT license is included as `LICENSE`.
