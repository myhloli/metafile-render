# Native GDI+ fixtures

Generated on Windows with `tools/generate_emfplus_fixtures.ps1` using System.Drawing/GDI+.
Each scene includes an EMF+ Only file, an EMF+ Dual file, and PNGs rendered by GDI+.
The generator and fixtures are part of this MIT-licensed project; font files are not embedded.

Scenes cover geometry, graphics states/containers, transparent bitmaps, Unicode text,
gradient/hatch/texture degradation, and EMF drawing inside a GetDC interval.
PNG references use a 256×256 canvas at 96 DPI. Metafile frame rounding can produce a
255×255 parsed output; comparisons account for this one-pixel difference. Font and
antialiasing differences are expected across platforms. Gradient and texture differences
are intentional and must be accompanied by `partial` diagnostics.
