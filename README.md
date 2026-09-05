# Metafile Render

Render Windows Metafile (WMF) and Enhanced Metafile (EMF) images to SVG, PNG,
JPEG, and WebP on Linux, macOS, and Windows.

Requires Python 3.10–3.14. Runtime dependencies are Pillow and pyclipper;
no Office installation, external conversion executable, or Windows GDI is required.

## Installation

```bash
pip install metafile-render
```

Python 3.10–3.13 requires Pillow >=11.0.0 and pyclipper >=1.3.0,<2.
Python 3.14 requires Pillow >=12.0.0 and pyclipper >=1.4.0,<2.
Existing compatible dependencies can be retained; these ranges do not pin a fresh installation to older releases.

## Python API

```python
from pathlib import Path
from metafile_render import MetafileError, render_metafile

try:
    result = render_metafile(
        Path("input.emf").read_bytes(),
        output_format="svg",
        dpi=144,
        size_hint=None,
    )
except MetafileError as error:
    print(error.code, str(error))
else:
    Path("output.svg").write_bytes(result.data)
    print(result.width, result.height, result.media_type, result.partial)
    for diagnostic in result.diagnostics:
        print(diagnostic.code, diagnostic.message)
```

`render_metafile(data: bytes, *, output_format="png", dpi=144, size_hint=None)`
returns a `MetafileRenderResult`. `dpi` must be an integer from 1 through 1200;
`size_hint`, when supplied, is a pair of positive integer pixel dimensions, useful
for standard WMF images without physical dimensions. Resource limits may reduce
the actual canvas size.

The result contains `data`, `output_format`, `media_type`, `width`, `height`,
`source_format`, `emfplus_mode`, `partial`, and a tuple of `diagnostics`.
Each diagnostic contains a code, level, message, and optional record location.

Supported output format strings are `svg`, `png`, `jpeg`, and `webp`.
PNG preserves transparency; JPEG uses a white background at quality 90.
WebP preserves transparency and uses lossy quality 90, method 4.
SVG is self-contained, with embedded images and a PNG fallback in metadata.
Some raster operations require a raster image wrapped in SVG.

Public exports are `render_metafile`, `MetafileOutputFormat`,
`MetafileRenderResult`, `MetafileDiagnostic`, `MetafileError`,
`MetafileMalformedError`, `MetafileResourceLimitError`, and `MetafileUnsupportedError`.
The parser, drawing models, and renderer internals are not a stable public API.
Invalid API arguments raise `TypeError` or `ValueError`; malformed, unsupported,
and over-budget images raise the corresponding `MetafileError` subclass.

## Command line

```bash
metafile-render input.emf -o output.svg
metafile-render input.wmf -o output.png --dpi 144 --size 800 600
metafile-render input.emf -o output.webp
python -m metafile_render input.emf -o output.jpg --force
metafile-render --version
```

The output extension selects the format: `.svg`, `.png`, `.jpg`, `.jpeg`, or `.webp`
(case-insensitive). The output directory must exist. Existing output files are
preserved unless `--force` is supplied; input and output must be different files.
Completed outputs are published atomically. Input reads are bounded.

Exit codes: `0` for a completed conversion (including partial rendering), `1` for
conversion or filesystem errors, and `2` for invalid arguments. Diagnostics go to
stderr. Partial rendering is explicitly reported. Use the Python API to inspect
individual diagnostic fields.

## Rendering and fonts

Placeable and standard WMF, common EMF drawing records, text, DIB images, paths,
transforms, clipping, and common raster operations are supported. EMF+ Dual uses
the EMF fallback; EMF+ Only is unsupported. Unhandled records may produce partial
results with diagnostics. This is not a complete implementation of every GDI record.

Font lookup uses installed system fonts and common aliases, then Pillow's default
font. Install the fonts used by the source document for closer text fidelity;
glyph coverage and measurements can vary across systems. Font files are not bundled.
WebP output requires a Pillow build with WebP encoding support, as provided by its
standard wheels; an unavailable encoder raises `MetafileUnsupportedError`.

SVG uses `data-metafile-render="wmf-emf"`, PNG metadata ID
`metafile-render-raster-fallback`, and local clip IDs `metafile-render-clip-N`.
The generated-image marker is not authentication of an arbitrary SVG. Consumers
accepting externally supplied SVG should validate its structure independently.

Fixed budgets bound input bytes, record and object counts, nesting, geometry,
embedded images, and rendering work. Input is limited to 128 MiB; generated SVG
is limited to 64 MiB; the canvas is limited to 8192 per side and 16 million pixels.

## Development

```bash
uv venv .venv
uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/python -m pytest
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
.venv/bin/python -m build
.venv/bin/python -m twine check dist/*
```

On Windows, the virtual environment interpreter is `.venv\Scripts\python.exe`.
CI tests Python 3.10–3.14 on Linux, macOS, and Windows, plus minimum dependency combinations.
Real EMF test images are read from a test-only presentation package dependency.

## Publishing

Releases use PyPI Trusted Publishing. Configure the PyPI pending publisher with
project `metafile-render`, owner `myhloli`, repository `metafile-render`, workflow
`publish.yml`, and environment `pypi`. Publishing a GitHub Release such as `v0.1.0`
runs tests, verifies that the tag matches the package version, builds the wheel
and source distribution, and uploads them through OIDC.

## License

MIT. Copyright (c) 2026 Xiaomeng Zhao (myhloli).
