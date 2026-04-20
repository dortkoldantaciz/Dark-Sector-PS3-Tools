#!/usr/bin/env python3
"""
Dark Sector PS3 Cache Tool
===========================
Extract and repack .cache files from Dark Sector (PS3).

.cache files are ZIP archives that use a custom compression method (method 64)
based on chunked LZFX (LZF variant). This tool handles both standard store
(method 0) and the custom Dark Sector LZFX compression (method 64).

Made by: dortkoldantaciz
Version: 1
"""

import struct
import os
import sys
import zlib
import traceback
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path

# ============================================================================
#  LZFX Compression / Decompression (Pure Python port of lzfx.c)
# ============================================================================

LZFX_MAX_LIT = 1 << 5   # 32
LZFX_MAX_OFF = 1 << 13  # 8192
LZFX_MAX_REF = (1 << 8) + (1 << 3)  # 264


def lzfx_decompress(data: bytes, expected_size: int) -> bytes:
    """Decompress LZFX compressed data."""
    ip = 0
    out = bytearray(expected_size)
    op = 0
    in_end = len(data)

    while ip < in_end:
        ctrl = data[ip]
        ip += 1

        if ctrl < 0x20:
            # Literal run: copy ctrl+1 bytes
            ctrl += 1
            if ip + ctrl > in_end:
                break
            if op + ctrl > expected_size:
                break
            out[op:op + ctrl] = data[ip:ip + ctrl]
            op += ctrl
            ip += ctrl
        else:
            # Back reference
            length = ctrl >> 5
            if length == 7:
                if ip >= in_end:
                    break
                length += data[ip]
                ip += 1
            length += 2

            if ip >= in_end:
                break

            ref = op - ((ctrl & 0x1F) << 8) - 1 - data[ip]
            ip += 1

            if ref < 0:
                break
            if op + length > expected_size:
                break

            # Copy byte-by-byte (overlapping allowed)
            for _ in range(length):
                out[op] = out[ref]
                op += 1
                ref += 1

    return bytes(out[:op])


def lzfx_compress(data: bytes) -> bytes:
    """Compress data using LZFX algorithm."""
    if len(data) == 0:
        return b''

    if len(data) < 4:
        # Too short to compress, store as literal
        out = bytearray()
        out.append(len(data) - 1)
        out.extend(data)
        return bytes(out)

    HTAB_BITS = 16
    HTAB_SIZE = 1 << HTAB_BITS
    htab = [0] * HTAB_SIZE

    ip = 0
    in_end = len(data)
    out = bytearray(len(data) * 2 + 16)  # worst case
    op = 0
    lit = 0
    op += 1  # reserve space for literal length

    def frst(p):
        return (data[p] << 8) | data[p + 1]

    def nxt(v, p):
        return ((v << 8) | data[p + 2]) & 0xFFFFFFFF

    def idx(h):
        return (((h >> (3 * 8 - HTAB_BITS)) - h) & (HTAB_SIZE - 1))

    hval = frst(ip)

    while ip + 2 < in_end:
        hval = nxt(hval, ip)
        hslot = idx(hval)
        ref = htab[hslot]
        htab[hslot] = ip

        off = ip - ref - 1

        if (ref < ip and
            off < LZFX_MAX_OFF and
            ip + 4 < in_end and
            ref > 0 and
            data[ref] == data[ip] and
            data[ref + 1] == data[ip + 1] and
            data[ref + 2] == data[ip + 2]):

            # Found a match
            length = 3
            maxlen = min(in_end - ip - 2, LZFX_MAX_REF)
            while length < maxlen and data[ref + length] == data[ip + length]:
                length += 1

            # Terminate literal run
            out[op - lit - 1] = lit - 1 if lit > 0 else 0
            if lit == 0:
                op -= 1

            length -= 2  # encode as length - 2

            if length < 7:
                out[op] = (off >> 8) + (length << 5)
                op += 1
                out[op] = off & 0xFF
                op += 1
            else:
                out[op] = (off >> 8) + (7 << 5)
                op += 1
                out[op] = length - 7
                op += 1
                out[op] = off & 0xFF
                op += 1

            lit = 0
            op += 1  # reserve literal length

            ip += length + 1

            if ip + 3 >= in_end:
                ip += 1
                break

            hval = frst(ip)
            hval = nxt(hval, ip)
            htab[idx(hval)] = ip
            ip += 1
        else:
            lit += 1
            out[op] = data[ip]
            op += 1
            ip += 1

            if lit == LZFX_MAX_LIT:
                out[op - lit - 1] = lit - 1
                lit = 0
                op += 1

    # Remaining bytes
    while ip < in_end:
        lit += 1
        out[op] = data[ip]
        op += 1
        ip += 1

        if lit == LZFX_MAX_LIT:
            out[op - lit - 1] = lit - 1
            lit = 0
            op += 1

    out[op - lit - 1] = lit - 1 if lit > 0 else 0
    if lit == 0:
        op -= 1

    return bytes(out[:op])


# ============================================================================
#  Dark Sector Chunk Compression (wraps LZFX with chunk headers)
# ============================================================================

CHUNK_SIZE = 0x4000  # 16384 bytes per chunk


def darksector_decompress(data: bytes, expected_size: int) -> bytes:
    """Decompress Dark Sector chunked LZFX data (ZIP method 64)."""
    result = bytearray()
    pos = 0

    while pos < len(data) and len(result) < expected_size:
        if pos + 8 > len(data):
            break

        # Chunk header: 4 bytes BE compressed size, 4 bytes BE uncompressed size
        chunk_comp_size = struct.unpack('>I', data[pos:pos + 4])[0]
        chunk_uncomp_size = struct.unpack('>I', data[pos + 4:pos + 8])[0]
        pos += 8

        if chunk_comp_size == 0:
            break

        chunk_data = data[pos:pos + chunk_comp_size]
        pos += chunk_comp_size

        decompressed = lzfx_decompress(chunk_data, chunk_uncomp_size)
        result.extend(decompressed)

    return bytes(result[:expected_size])


def darksector_compress(data: bytes) -> bytes:
    """Compress data using Dark Sector chunked LZFX format (ZIP method 64)."""
    result = bytearray()
    offset = 0

    while offset < len(data):
        chunk = data[offset:offset + CHUNK_SIZE]
        chunk_uncomp_size = len(chunk)
        offset += chunk_uncomp_size

        compressed = lzfx_compress(chunk)

        # If compressed is larger than original, store uncompressed
        # (shouldn't happen often, but safety check)
        if len(compressed) >= chunk_uncomp_size:
            # Store as a single literal run
            compressed = lzfx_compress(chunk)

        # Chunk header: 4 bytes BE compressed size, 4 bytes BE uncompressed size
        result.extend(struct.pack('>I', len(compressed)))
        result.extend(struct.pack('>I', chunk_uncomp_size))
        result.extend(compressed)

    return bytes(result)


# ============================================================================
#  ZIP Structure Parsing and Building
# ============================================================================

class ZipEntry:
    """Represents a single file entry in the ZIP/cache archive."""

    def __init__(self):
        self.name = ""
        self.method = 0         # 0 = store, 64 = darksector LZFX
        self.crc32 = 0
        self.comp_size = 0
        self.uncomp_size = 0
        self.offset = 0         # offset of local file header
        self.data_offset = 0    # offset of actual file data
        self.ver_made = 0x000C
        self.ver_need = 0
        self.flag = 0
        self.modtime = 0
        self.moddate = 0
        self.disk = 0
        self.int_attr = 0
        self.ext_attr = 0
        self.extra = b''
        self.local_extra = b''   # extra field from local file header
        self.comment = b''


def parse_cache(filepath: str) -> list:
    """Parse a .cache file and return list of ZipEntry objects."""
    with open(filepath, 'rb') as f:
        data = f.read()

    entries = []

    # Find End of Central Directory
    eocd_offset = data.rfind(b'PK\x05\x06')
    if eocd_offset < 0:
        raise ValueError("Invalid cache file: EOCD not found")

    (eocd_sig, disk_num, disk_start, central_entries_disk,
     central_entries, central_size, central_offset,
     comment_len) = struct.unpack_from('<IHHHHIIH', data, eocd_offset)

    # Parse Central Directory
    pos = central_offset
    for i in range(central_entries):
        if pos + 46 > len(data):
            break

        sig = struct.unpack_from('<I', data, pos)[0]
        if sig != 0x02014B50:
            break

        entry = ZipEntry()
        (_, entry.ver_made, entry.ver_need, entry.flag, entry.method,
         entry.modtime, entry.moddate, entry.crc32, entry.comp_size,
         entry.uncomp_size, name_len, extra_len, comment_len,
         entry.disk, entry.int_attr, entry.ext_attr,
         entry.offset) = struct.unpack_from('<IHHHHHHIIIHHHHHII', data, pos)

        name_start = pos + 46
        entry.name = data[name_start:name_start + name_len].decode('ascii', errors='replace')
        entry.extra = data[name_start + name_len:name_start + name_len + extra_len]
        entry.comment = data[name_start + name_len + extra_len:
                             name_start + name_len + extra_len + comment_len]

        # Find actual data offset from local file header and preserve local extra
        local_pos = entry.offset
        if local_pos + 30 <= len(data):
            local_name_len = struct.unpack_from('<H', data, local_pos + 26)[0]
            local_extra_len = struct.unpack_from('<H', data, local_pos + 28)[0]
            local_extra_start = local_pos + 30 + local_name_len
            entry.local_extra = data[local_extra_start:local_extra_start + local_extra_len]
            entry.data_offset = local_pos + 30 + local_name_len + local_extra_len

        entries.append(entry)
        pos = name_start + name_len + extra_len + comment_len

    return entries


def extract_cache(cache_path: str, output_dir: str, progress_callback=None):
    """Extract all files from a .cache archive."""
    with open(cache_path, 'rb') as f:
        cache_data = f.read()

    entries = parse_cache(cache_path)
    total = len(entries)
    extracted = 0
    errors = []

    for i, entry in enumerate(entries):
        if progress_callback:
            progress_callback(i + 1, total, entry.name)

        # Skip directories
        if entry.name.endswith('/') and entry.uncomp_size == 0:
            dir_path = os.path.join(output_dir, entry.name)
            os.makedirs(dir_path, exist_ok=True)
            continue

        # Get compressed data
        comp_data = cache_data[entry.data_offset:entry.data_offset + entry.comp_size]

        try:
            if entry.method == 0:
                # Store - no compression
                file_data = comp_data
            elif entry.method == 64:
                # Dark Sector LZFX
                file_data = darksector_decompress(comp_data, entry.uncomp_size)
            elif entry.method == 8:
                # Standard deflate
                file_data = zlib.decompress(comp_data, -15)
            else:
                errors.append(f"Unsupported method {entry.method}: {entry.name}")
                continue

            # Write file
            file_path = os.path.join(output_dir, entry.name.replace('/', os.sep))
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            with open(file_path, 'wb') as f:
                f.write(file_data)
            extracted += 1

        except Exception as e:
            errors.append(f"Error extracting {entry.name}: {str(e)}")

    return extracted, total, errors


def repack_cache(original_cache_path: str, input_dir: str, output_cache_path: str,
                 progress_callback=None):
    """Repack files into a .cache archive, preserving original compression methods.

    For files that haven't changed (identical size to original), the original
    compressed data is copied verbatim to avoid any LZFX compatibility issues.
    Only modified files are re-compressed. CRC32 values are preserved from the
    original (the game stores 0 for all entries).
    """
    # Read original cache data
    with open(original_cache_path, 'rb') as f:
        orig_cache_data = f.read()

    # Parse original cache to get the file list and compression methods
    original_entries = parse_cache(original_cache_path)
    total = len(original_entries)

    # Collect all files from input directory
    input_files = {}
    input_dir_path = Path(input_dir)
    for file_path in input_dir_path.rglob('*'):
        if file_path.is_file():
            rel_path = file_path.relative_to(input_dir_path).as_posix()
            input_files[rel_path] = str(file_path)

    # Build the ZIP file
    local_headers = bytearray()
    central_dir = bytearray()
    file_count = 0
    errors = []

    for i, orig_entry in enumerate(original_entries):
        if progress_callback:
            progress_callback(i + 1, total, orig_entry.name)

        # Skip empty directories
        if orig_entry.name.endswith('/') and orig_entry.uncomp_size == 0:
            continue

        # Find the file in input directory
        disk_file_exists = orig_entry.name in input_files

        try:
            # Read disk file if available
            if disk_file_exists:
                with open(input_files[orig_entry.name], 'rb') as f:
                    file_data = f.read()
                disk_size = len(file_data)
            else:
                file_data = None
                disk_size = -1

            method = orig_entry.method
            uncomp_size = orig_entry.uncomp_size

            # Determine if the disk file matches this specific entry.
            # For duplicate entries (same name, different versions), the disk
            # file will only match the LAST version written during extract.
            # Earlier versions MUST use original compressed data verbatim.
            disk_matches_entry = (disk_size == orig_entry.uncomp_size)

            if disk_matches_entry and method == 64:
                # Disk file matches this entry's size - use original compressed
                # data (safest: avoids LZFX compatibility issues with game engine)
                comp_data = orig_cache_data[orig_entry.data_offset:
                                            orig_entry.data_offset + orig_entry.comp_size]
                comp_size = orig_entry.comp_size
            elif disk_matches_entry and method == 0:
                # Store method, size matches - use disk file data
                comp_data = file_data
                comp_size = disk_size
                uncomp_size = disk_size
            elif not disk_matches_entry and disk_file_exists:
                # Disk file size differs from this entry's original size.
                # This can happen when:
                #   a) User modified the file (size changed) - last entry for this name
                #   b) This is an older duplicate version - NOT the last entry
                #
                # Check if this is the last entry for this name by looking ahead
                is_last_entry = True
                for future_entry in original_entries[i+1:]:
                    if future_entry.name == orig_entry.name:
                        is_last_entry = False
                        break

                if is_last_entry:
                    # User likely modified this file - pack as store (method 0)
                    # to avoid LZFX re-compression compatibility issues
                    comp_data = file_data
                    comp_size = disk_size
                    uncomp_size = disk_size
                    method = 0
                else:
                    # Older duplicate version - preserve original compressed data
                    comp_data = orig_cache_data[orig_entry.data_offset:
                                                orig_entry.data_offset + orig_entry.comp_size]
                    comp_size = orig_entry.comp_size
            elif not disk_file_exists:
                # File not on disk at all - copy original compressed data
                comp_data = orig_cache_data[orig_entry.data_offset:
                                            orig_entry.data_offset + orig_entry.comp_size]
                comp_size = orig_entry.comp_size
            else:
                # Fallback: copy original
                comp_data = orig_cache_data[orig_entry.data_offset:
                                            orig_entry.data_offset + orig_entry.comp_size]
                comp_size = orig_entry.comp_size

            # Preserve original CRC32 (game uses 0 for all entries)
            crc = orig_entry.crc32

            name_bytes = orig_entry.name.encode('ascii')
            offset = len(local_headers)

            # Local file header
            local_header = struct.pack('<IHHHHHI IIHH',
                0x04034B50,         # signature
                orig_entry.ver_need if orig_entry.ver_need else 0x000C,
                orig_entry.flag,    # flags
                method,             # compression method
                orig_entry.modtime, # mod time
                orig_entry.moddate, # mod date
                crc,                # crc32 (preserved from original)
                comp_size,          # compressed size
                uncomp_size,        # uncompressed size
                len(name_bytes),    # filename length
                len(orig_entry.local_extra)  # extra field length (preserved)
            )
            local_headers.extend(local_header)
            local_headers.extend(name_bytes)
            local_headers.extend(orig_entry.local_extra)
            local_headers.extend(comp_data)

            # Central directory entry
            cd_entry = struct.pack('<IHHHHHHIIIHHHHHII',
                0x02014B50,         # signature
                orig_entry.ver_made,  # version made by
                orig_entry.ver_need,  # version needed (preserved from original)
                orig_entry.flag,    # flags
                method,             # compression method
                orig_entry.modtime, # mod time
                orig_entry.moddate, # mod date
                crc,                # crc32 (preserved from original)
                comp_size,          # compressed size
                uncomp_size,        # uncompressed size
                len(name_bytes),    # filename length
                len(orig_entry.extra),  # extra field length
                len(orig_entry.comment),  # comment length
                0,                  # disk number start
                orig_entry.int_attr,  # internal file attributes
                orig_entry.ext_attr,  # external file attributes
                offset              # relative offset of local header
            )
            central_dir.extend(cd_entry)
            central_dir.extend(name_bytes)
            central_dir.extend(orig_entry.extra)
            central_dir.extend(orig_entry.comment)

            file_count += 1

        except Exception as e:
            errors.append(f"Error packing {orig_entry.name}: {str(e)}")

    # End of Central Directory Record
    central_dir_offset = len(local_headers)
    central_dir_size = len(central_dir)

    eocd = struct.pack('<IHHHHIIH',
        0x06054B50,         # signature
        0,                  # disk number
        0,                  # disk with central dir start
        file_count,         # entries on this disk
        file_count,         # total entries
        central_dir_size,   # central directory size
        central_dir_offset, # central directory offset
        0                   # comment length
    )

    # Write output file
    os.makedirs(os.path.dirname(os.path.abspath(output_cache_path)), exist_ok=True)
    with open(output_cache_path, 'wb') as f:
        f.write(local_headers)
        f.write(central_dir)
        f.write(eocd)

    return file_count, total, errors


# ============================================================================
#  Tkinter GUI
# ============================================================================

class DarkSectorCacheTool:
    """Main application window."""

    APP_TITLE = "Dark Sector PS3 Cache Tool"
    VERSION = "1"

    def __init__(self, root):
        self.root = root
        self.root.title(f"{self.APP_TITLE} v{self.VERSION}")
        self.root.geometry("600x520")
        self.root.minsize(550, 480)
        self.root.resizable(True, True)
        self.root.configure(bg='#f0f0f0')

        self._setup_styles()
        self._build_ui()

    def _setup_styles(self):
        """Configure ttk styles for clean white theme."""
        style = ttk.Style()
        style.theme_use('clam')

        style.configure('TFrame', background='#f0f0f0')
        style.configure('TLabel', background='#f0f0f0', foreground='#222222',
                        font=('Segoe UI', 9))
        style.configure('Title.TLabel', background='#f0f0f0', foreground='#222222',
                        font=('Segoe UI', 12, 'bold'))
        style.configure('Credit.TLabel', background='#f0f0f0', foreground='#888888',
                        font=('Segoe UI', 8))
        style.configure('Section.TLabel', background='#f0f0f0', foreground='#333333',
                        font=('Segoe UI', 9, 'bold'))
        style.configure('Status.TLabel', background='#f0f0f0', foreground='#228B22',
                        font=('Segoe UI', 9))
        style.configure('Error.TLabel', background='#f0f0f0', foreground='#cc0000',
                        font=('Segoe UI', 9))

        style.configure('TButton', font=('Segoe UI', 9), padding=(12, 4))
        style.configure('Browse.TButton', font=('Segoe UI', 8), padding=(8, 3))

        style.configure('TNotebook', background='#f0f0f0')
        style.configure('TNotebook.Tab', font=('Segoe UI', 9, 'bold'), padding=(16, 4))

        style.configure('Horizontal.TProgressbar', thickness=6)

    def _build_ui(self):
        """Build the main UI layout."""
        main = ttk.Frame(self.root, padding=12)
        main.pack(fill='both', expand=True)

        # Header
        header = ttk.Frame(main)
        header.pack(fill='x', pady=(0, 8))
        ttk.Label(header, text="Dark Sector PS3 Cache Tool",
                  style='Title.TLabel').pack(side='left')
        ttk.Label(header, text=f"v{self.VERSION}  —  Made by dortkoldantaciz",
                  style='Credit.TLabel').pack(side='right', pady=(4, 0))

        # Notebook (tabs)
        notebook = ttk.Notebook(main)
        notebook.pack(fill='both', expand=True, pady=(0, 6))

        extract_tab = ttk.Frame(notebook, padding=12)
        notebook.add(extract_tab, text='  Extract  ')
        self._build_extract_tab(extract_tab)

        repack_tab = ttk.Frame(notebook, padding=12)
        notebook.add(repack_tab, text='  Repack  ')
        self._build_repack_tab(repack_tab)

        # Status bar
        self.status_var = tk.StringVar(value="Ready")
        bottom = ttk.Frame(main)
        bottom.pack(fill='x', pady=(4, 0))
        self.status_label = ttk.Label(bottom, textvariable=self.status_var,
                                      style='Status.TLabel')
        self.status_label.pack(side='left')

        # Progress bar
        self.progress_var = tk.DoubleVar(value=0)
        self.progress = ttk.Progressbar(main, variable=self.progress_var, maximum=100)
        self.progress.pack(fill='x', pady=(4, 0))

    def _build_extract_tab(self, parent):
        """Build the Extract tab UI."""
        # Cache file input
        ttk.Label(parent, text="Cache File (.cache)", style='Section.TLabel').pack(anchor='w')
        row1 = ttk.Frame(parent)
        row1.pack(fill='x', pady=(2, 8))
        self.extract_cache_var = tk.StringVar()
        ttk.Entry(row1, textvariable=self.extract_cache_var,
                  font=('Segoe UI', 9)).pack(side='left', fill='x', expand=True, padx=(0, 6))
        ttk.Button(row1, text="Browse...", style='Browse.TButton',
                   command=self._browse_extract_cache).pack(side='right')

        # Output directory
        ttk.Label(parent, text="Output Directory", style='Section.TLabel').pack(anchor='w')
        row2 = ttk.Frame(parent)
        row2.pack(fill='x', pady=(2, 8))
        self.extract_output_var = tk.StringVar()
        ttk.Entry(row2, textvariable=self.extract_output_var,
                  font=('Segoe UI', 9)).pack(side='left', fill='x', expand=True, padx=(0, 6))
        ttk.Button(row2, text="Browse...", style='Browse.TButton',
                   command=self._browse_extract_output).pack(side='right')

        # Extract button
        ttk.Button(parent, text="Extract Files",
                   command=self._do_extract).pack(pady=(6, 0))

        # Log
        self.extract_log = tk.Text(parent, height=8,
                                    bg='#ffffff', fg='#222222',
                                    font=('Consolas', 9), relief='solid',
                                    bd=1, wrap='word')
        self.extract_log.pack(fill='both', expand=True, pady=(8, 0))

    def _build_repack_tab(self, parent):
        """Build the Repack tab UI."""
        # Original cache file
        ttk.Label(parent, text="Original Cache File (.cache)",
                  style='Section.TLabel').pack(anchor='w')
        row1 = ttk.Frame(parent)
        row1.pack(fill='x', pady=(2, 8))
        self.repack_orig_var = tk.StringVar()
        ttk.Entry(row1, textvariable=self.repack_orig_var,
                  font=('Segoe UI', 9)).pack(side='left', fill='x', expand=True, padx=(0, 6))
        ttk.Button(row1, text="Browse...", style='Browse.TButton',
                   command=self._browse_repack_orig).pack(side='right')

        # Input directory (modified files)
        ttk.Label(parent, text="Input Directory (extracted/modified files)",
                  style='Section.TLabel').pack(anchor='w')
        row2 = ttk.Frame(parent)
        row2.pack(fill='x', pady=(2, 8))
        self.repack_input_var = tk.StringVar()
        ttk.Entry(row2, textvariable=self.repack_input_var,
                  font=('Segoe UI', 9)).pack(side='left', fill='x', expand=True, padx=(0, 6))
        ttk.Button(row2, text="Browse...", style='Browse.TButton',
                   command=self._browse_repack_input).pack(side='right')

        # Output cache file
        ttk.Label(parent, text="Output Cache File (.cache)",
                  style='Section.TLabel').pack(anchor='w')
        row3 = ttk.Frame(parent)
        row3.pack(fill='x', pady=(2, 8))
        self.repack_output_var = tk.StringVar()
        ttk.Entry(row3, textvariable=self.repack_output_var,
                  font=('Segoe UI', 9)).pack(side='left', fill='x', expand=True, padx=(0, 6))
        ttk.Button(row3, text="Browse...", style='Browse.TButton',
                   command=self._browse_repack_output).pack(side='right')

        # Repack button
        ttk.Button(parent, text="Repack Cache",
                   command=self._do_repack).pack(pady=(6, 0))

        # Log
        self.repack_log = tk.Text(parent, height=8,
                                   bg='#ffffff', fg='#222222',
                                   font=('Consolas', 9), relief='solid',
                                   bd=1, wrap='word')
        self.repack_log.pack(fill='both', expand=True, pady=(8, 0))

    # --- Browse handlers ---

    def _browse_extract_cache(self):
        path = filedialog.askopenfilename(
            title="Select Cache File",
            filetypes=[("Cache files", "*.cache"), ("All files", "*.*")])
        if path:
            self.extract_cache_var.set(path)

    def _browse_extract_output(self):
        path = filedialog.askdirectory(title="Select Output Directory")
        if path:
            self.extract_output_var.set(path)

    def _browse_repack_orig(self):
        path = filedialog.askopenfilename(
            title="Select Original Cache File",
            filetypes=[("Cache files", "*.cache"), ("All files", "*.*")])
        if path:
            self.repack_orig_var.set(path)

    def _browse_repack_input(self):
        path = filedialog.askdirectory(title="Select Input Directory")
        if path:
            self.repack_input_var.set(path)

    def _browse_repack_output(self):
        path = filedialog.asksaveasfilename(
            title="Save Cache File As",
            defaultextension=".cache",
            filetypes=[("Cache files", "*.cache"), ("All files", "*.*")])
        if path:
            self.repack_output_var.set(path)

    # --- Log helpers ---

    def _log(self, widget, msg):
        widget.insert('end', msg + "\n")
        widget.see('end')
        widget.update_idletasks()

    def _set_status(self, msg, is_error=False):
        self.status_var.set(msg)
        self.status_label.configure(
            style='Error.TLabel' if is_error else 'Status.TLabel')

    # --- Extract ---

    def _do_extract(self):
        cache_path = self.extract_cache_var.get().strip()
        output_dir = self.extract_output_var.get().strip()

        if not cache_path or not os.path.isfile(cache_path):
            messagebox.showerror("Error", "Please select a valid cache file.")
            return
        if not output_dir:
            messagebox.showerror("Error", "Please select an output directory.")
            return

        self.extract_log.delete('1.0', 'end')
        self._set_status("Extracting...")
        self.progress_var.set(0)

        def run():
            try:
                def progress(current, total, name):
                    pct = (current / total) * 100
                    self.root.after(0, lambda: self.progress_var.set(pct))
                    self.root.after(0, lambda: self._set_status(
                        f"Extracting {current}/{total}: {name}"))
                    if current % 200 == 0 or current == total:
                        self.root.after(0, lambda: self._log(
                            self.extract_log, f"[{current}/{total}] {name}"))

                extracted, total, errors = extract_cache(cache_path, output_dir, progress)

                self.root.after(0, lambda: self.progress_var.set(100))
                self.root.after(0, lambda: self._log(
                    self.extract_log,
                    f"\n✅ Done! Extracted {extracted}/{total} files."))

                if errors:
                    for err in errors:
                        self.root.after(0, lambda e=err: self._log(
                            self.extract_log, f"⚠ {e}"))
                    self.root.after(0, lambda: self._set_status(
                        f"Extracted {extracted}/{total} files ({len(errors)} errors)",
                        is_error=True))
                else:
                    self.root.after(0, lambda: self._set_status(
                        f"Successfully extracted {extracted} files"))

            except Exception as e:
                self.root.after(0, lambda: self._log(
                    self.extract_log, f"\n❌ Error: {str(e)}"))
                self.root.after(0, lambda: self._set_status(str(e), is_error=True))
                self.root.after(0, lambda: self._log(
                    self.extract_log, traceback.format_exc()))

        threading.Thread(target=run, daemon=True).start()

    # --- Repack ---

    def _do_repack(self):
        orig_path = self.repack_orig_var.get().strip()
        input_dir = self.repack_input_var.get().strip()
        output_path = self.repack_output_var.get().strip()

        if not orig_path or not os.path.isfile(orig_path):
            messagebox.showerror("Error", "Please select a valid original cache file.")
            return
        if not input_dir or not os.path.isdir(input_dir):
            messagebox.showerror("Error", "Please select a valid input directory.")
            return
        if not output_path:
            messagebox.showerror("Error", "Please select an output file path.")
            return

        self.repack_log.delete('1.0', 'end')
        self._set_status("Repacking...")
        self.progress_var.set(0)

        def run():
            try:
                def progress(current, total, name):
                    pct = (current / total) * 100
                    self.root.after(0, lambda: self.progress_var.set(pct))
                    self.root.after(0, lambda: self._set_status(
                        f"Repacking {current}/{total}: {name}"))
                    if current % 200 == 0 or current == total:
                        self.root.after(0, lambda: self._log(
                            self.repack_log, f"[{current}/{total}] {name}"))

                packed, total, errors = repack_cache(
                    orig_path, input_dir, output_path, progress)

                self.root.after(0, lambda: self.progress_var.set(100))
                self.root.after(0, lambda: self._log(
                    self.repack_log,
                    f"\n✅ Done! Packed {packed}/{total} files."))

                output_size = os.path.getsize(output_path)
                self.root.after(0, lambda: self._log(
                    self.repack_log,
                    f"📦 Output size: {output_size:,} bytes"))

                if errors:
                    for err in errors:
                        self.root.after(0, lambda e=err: self._log(
                            self.repack_log, f"⚠ {e}"))
                    self.root.after(0, lambda: self._set_status(
                        f"Packed {packed}/{total} files ({len(errors)} errors)",
                        is_error=True))
                else:
                    self.root.after(0, lambda: self._set_status(
                        f"Successfully packed {packed} files"))

            except Exception as e:
                self.root.after(0, lambda: self._log(
                    self.repack_log, f"\n❌ Error: {str(e)}"))
                self.root.after(0, lambda: self._set_status(str(e), is_error=True))
                self.root.after(0, lambda: self._log(
                    self.repack_log, traceback.format_exc()))

        threading.Thread(target=run, daemon=True).start()


# ============================================================================
#  CLI Fallback
# ============================================================================

def cli_main():
    """Command-line interface."""
    if len(sys.argv) < 2:
        print(f"Dark Sector PS3 Cache Tool v1 - Made by dortkoldantaciz")
        print(f"Usage:")
        print(f"  {sys.argv[0]} extract <cache_file> <output_dir>")
        print(f"  {sys.argv[0]} repack  <original_cache> <input_dir> <output_cache>")
        print(f"  {sys.argv[0]} gui     (launch GUI)")
        sys.exit(1)

    cmd = sys.argv[1].lower()

    if cmd == 'extract':
        if len(sys.argv) < 4:
            print("Usage: extract <cache_file> <output_dir>")
            sys.exit(1)
        cache_path = sys.argv[2]
        output_dir = sys.argv[3]
        print(f"Extracting {cache_path} -> {output_dir}")

        def progress(current, total, name):
            if current % 500 == 0 or current == total:
                print(f"  [{current}/{total}] {name}")

        extracted, total, errors = extract_cache(cache_path, output_dir, progress)
        print(f"\nDone! Extracted {extracted}/{total} files.")
        for err in errors:
            print(f"  WARNING: {err}")

    elif cmd == 'repack':
        if len(sys.argv) < 5:
            print("Usage: repack <original_cache> <input_dir> <output_cache>")
            sys.exit(1)
        orig_cache = sys.argv[2]
        input_dir = sys.argv[3]
        output_cache = sys.argv[4]
        print(f"Repacking {input_dir} -> {output_cache}")
        print(f"  Using compression map from: {orig_cache}")

        def progress(current, total, name):
            if current % 500 == 0 or current == total:
                print(f"  [{current}/{total}] {name}")

        packed, total, errors = repack_cache(orig_cache, input_dir, output_cache, progress)
        print(f"\nDone! Packed {packed}/{total} files.")
        output_size = os.path.getsize(output_cache)
        print(f"Output size: {output_size:,} bytes")
        for err in errors:
            print(f"  WARNING: {err}")

    elif cmd == 'gui':
        launch_gui()
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


def launch_gui():
    """Launch the Tkinter GUI."""
    root = tk.Tk()
    app = DarkSectorCacheTool(root)
    root.mainloop()


# ============================================================================
#  Entry Point
# ============================================================================

if __name__ == '__main__':
    if len(sys.argv) > 1:
        cli_main()
    else:
        launch_gui()
