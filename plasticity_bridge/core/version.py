"""Single source of truth for the plugin version.

Read by plasticity_c4d.pyp (printed on load) and by
scripts/build_release.py (zip name, CHANGELOG check, tag check).
Keep it a plain string literal: the build script parses it without
importing the package.
"""

__version__ = "1.2.0"
