# Dark Sector PS3 Cache Tool

A tool for extracting and repacking `.cache` files from the PS3 version of Dark Sector.

The game stores its assets in ZIP-based `.cache` archives that use a custom compression method (method 64) — a chunked LZFX variant specific to Dark Sector. Standard ZIP tools can't handle this format properly, so this tool was made to work with it.

## Usage

Run `DarkSectorCacheTool.exe` for the GUI, or use the command line:

```
DarkSectorCacheTool.exe extract <cache_file> <output_dir>
DarkSectorCacheTool.exe repack <original_cache> <input_dir> <output_cache>
```

When repacking, the tool needs the original `.cache` file as a reference to preserve compression methods and internal structure. Unmodified files are copied verbatim from the original archive. Modified files are stored uncompressed (method 0) to avoid compatibility issues with the game's decompressor.

## Building

Requires Python 3. No external dependencies — only standard library modules are used.

```
pip install pyinstaller
pyinstaller --onefile --windowed --name "DarkSectorCacheTool" darksector_cache_tool.py
```

## Notes

- Built and tested against the PS3 version (BLUS30116). It may also work with the Xbox 360 and PC versions since they share the same archive format, but these haven't been tested.
- Some files appear multiple times in the archive with different versions (e.g. `Dependancies.cs.1` has 10 entries). The tool handles these correctly during repack.
- The game doesn't use the CRC32 field in its ZIP headers — it's always zero. The tool preserves this behavior.
