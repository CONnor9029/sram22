#!/usr/bin/env python3
"""Extract timing data from Liberate MX .lib files and write
timingdata/{num_words}m{mux_ratio}w{write_size}.json.

Usage:
    python3 timingdata/extract_lib2json.py <num_words> <mux_ratio> <write_size>

Example:
    python3 timingdata/extract_lib2json.py 64 4 8
    python3 timingdata/extract_lib2json.py 128 8 8
    python3 timingdata/extract_lib2json.py 256 4 8
"""

import json
import re
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD_DIR = os.path.join(REPO_ROOT, "build")

CORNER_SUFFIX = {
    "tt": "tt_025C_1v80",
    "ss": "ss_100C_1v60",
    "ff": "ff_n40C_1v95",
}

DATA_WIDTHS = [8, 16, 32, 48, 64, 96, 128]

if len(sys.argv) != 4:
    print(__doc__, file=sys.stderr)
    sys.exit(1)
NUM_WORDS  = int(sys.argv[1])
MUX_RATIO  = int(sys.argv[2])
WRITE_SIZE = int(sys.argv[3])


def join_continuations(text):
    """Join backslash-continued lines."""
    return re.sub(r'\\\n\s*', ' ', text)


def parse_7x7(block):
    """Extract a 7x7 float table from a Liberty values block string."""
    m = re.search(r'values\s*\(\s*(.*?)\s*\)', block, re.DOTALL)
    if not m:
        return None
    rows = []
    for quoted in re.findall(r'"([^"]+)"', m.group(1)):
        nums = [float(x.strip()) for x in quoted.split(',')]
        rows.append(nums)
    if len(rows) != 7 or any(len(r) != 7 for r in rows):
        return None
    return rows


def parse_1x7(block):
    """Extract a 1x7 float table from a Liberty values block string."""
    m = re.search(r'values\s*\(\s*(.*?)\s*\)', block, re.DOTALL)
    if not m:
        return None
    for quoted in re.findall(r'"([^"]+)"', m.group(1)):
        nums = [float(x.strip()) for x in quoted.split(',')]
        if len(nums) == 7:
            return nums
    return None


def extract_table_block(text, start):
    """Return the {...} block starting at 'start' index (after the opening '{')."""
    depth = 0
    i = start
    while i < len(text):
        if text[i] == '{':
            depth += 1
        elif text[i] == '}':
            depth -= 1
            if depth == 0:
                return text[start:i+1]
        i += 1
    return text[start:]


def max_elementwise(a, b):
    """Element-wise max of two lists (or lists of lists)."""
    if isinstance(a[0], list):
        return [[max(a[i][j], b[i][j]) for j in range(len(a[i]))] for i in range(len(a))]
    return [max(a[i], b[i]) for i in range(len(a))]


def parse_lib(lib_path):
    """Parse a single .lib file and return a dict of timing tables."""
    with open(lib_path) as f:
        raw = f.read()
    text = join_continuations(raw)

    result = {}

    # ── capacitances ─────────────────────────────────────────────────────────
    # Find capacitance of the first occurrence for each pin type.
    # clk pin is a scalar pin; addr/din/ce/we/rstb/wmask are bus or scalar pins.
    def get_cap(pin_name):
        # Search for the pin block and extract 'capacitance : VALUE'
        m = re.search(r'pin\s*\(\s*' + re.escape(pin_name) + r'\s*\)', text)
        if not m:
            return 0.0
        after = text[m.end():]
        brace = after.find('{')
        if brace == -1:
            return 0.0
        block = extract_table_block(after, brace)
        cm = re.search(r'capacitance\s*:\s*([0-9.e+\-]+)', block)
        return float(cm.group(1)) if cm else 0.0

    # For bus pins, take max across all bits
    def get_bus_cap(pin_prefix):
        caps = []
        for m in re.finditer(r'pin\s*\(\s*' + re.escape(pin_prefix) + r'\[\d+\]\s*\)', text):
            after = text[m.end():]
            brace = after.find('{')
            if brace == -1:
                continue
            block = extract_table_block(after, brace)
            cm = re.search(r'capacitance\s*:\s*([0-9.e+\-]+)', block)
            if cm:
                caps.append(float(cm.group(1)))
        return max(caps) if caps else 0.0

    result['clk_cap']   = get_cap('clk')
    result['addr_cap']  = get_bus_cap('addr')
    result['din_cap']   = get_bus_cap('din')
    result['ce_cap']    = get_cap('ce')
    result['we_cap']    = get_cap('we')
    result['rstb_cap']  = get_cap('rstb')
    result['wmask_cap'] = get_cap('wmask')

    # ── timing tables ─────────────────────────────────────────────────────────
    # Strategy: iterate over all timing () blocks; use surrounding context to
    # determine pin type and timing type.

    # Split into timing blocks by finding all occurrences of `timing () {`
    # We need to track which pin/bus context we're in.

    # Build a list of (pos, context_pin) by finding pin/bus declarations.
    pin_contexts = []
    for m in re.finditer(r'(?:bus|pin)\s*\(\s*(\w+)(?:\[\d+\])?\s*\)', text):
        name = m.group(1)
        # Map to canonical pin type
        ptype = name  # clk, ce, we, rstb, wmask, addr, din, dout
        pin_contexts.append((m.start(), ptype))

    def pin_type_at(pos):
        """Return the nearest preceding pin type for a given position."""
        best = None
        for (p, t) in pin_contexts:
            if p <= pos:
                best = t
        return best

    # Find all timing () blocks
    tables = {
        'cell_rise': None, 'cell_fall': None,
        'rise_transition': None, 'fall_transition': None,
        'addr_hold_rise': None, 'addr_hold_fall': None,
        'din_hold_rise': None,  'din_hold_fall': None,
        'ce_hold_rise': None,   'ce_hold_fall': None,
        'we_hold_rise': None,   'we_hold_fall': None,
        'rstb_hold_rise': None, 'rstb_hold_fall': None,
        'addr_setup_rise': None, 'addr_setup_fall': None,
        'din_setup_rise': None,  'din_setup_fall': None,
        'ce_setup_rise': None,   'ce_setup_fall': None,
        'we_setup_rise': None,   'we_setup_fall': None,
        'rstb_setup_rise': None, 'rstb_setup_fall': None,
        'min_period': None, 'mpw_rise': None, 'mpw_fall': None,
    }

    for m in re.finditer(r'timing\s*\(\s*\)\s*\{', text):
        block_start = m.end() - 1  # position of '{'
        block = extract_table_block(text, block_start)

        tm = re.search(r'timing_type\s*:\s*(\w+)', block)
        if not tm:
            continue
        ttype = tm.group(1)

        ptype = pin_type_at(m.start())

        if ttype == 'rising_edge' and ptype == 'dout':
            for tbl in ['cell_rise', 'cell_fall', 'rise_transition', 'fall_transition']:
                m2 = re.search(re.escape(tbl) + r'\s*\([^)]*\)\s*\{(.*?)\}', block, re.DOTALL)
                if m2:
                    val = parse_7x7('{' + m2.group(1) + '}')
                    if val is not None:
                        if tables[tbl] is None:
                            tables[tbl] = val
                        else:
                            tables[tbl] = max_elementwise(tables[tbl], val)

        elif ttype in ('hold_rising', 'setup_rising'):
            arc = 'hold' if ttype == 'hold_rising' else 'setup'
            if ptype in ('addr', 'din', 'ce', 'we', 'rstb', 'wmask'):
                for (constraint, suffix) in [('rise_constraint', f'_{arc}_rise'), ('fall_constraint', f'_{arc}_fall')]:
                    key = ptype + suffix
                    if key not in tables:
                        continue
                    m2 = re.search(re.escape(constraint) + r'\s*\([^)]*\)\s*\{(.*?)\}', block, re.DOTALL)
                    if m2:
                        val = parse_7x7('{' + m2.group(1) + '}')
                        if val is not None:
                            if tables[key] is None:
                                tables[key] = val
                            else:
                                tables[key] = max_elementwise(tables[key], val)

        elif ttype == 'min_pulse_width' and ptype == 'clk':
            m2 = re.search(r'rise_constraint\s*\([^)]*\)\s*\{(.*?)\}', block, re.DOTALL)
            if m2:
                val = parse_1x7('{' + m2.group(1) + '}')
                if val is not None:
                    if tables['mpw_rise'] is None:
                        tables['mpw_rise'] = val
                    else:
                        tables['mpw_rise'] = max_elementwise(tables['mpw_rise'], val)
            m2 = re.search(r'fall_constraint\s*\([^)]*\)\s*\{(.*?)\}', block, re.DOTALL)
            if m2:
                val = parse_1x7('{' + m2.group(1) + '}')
                if val is not None:
                    if tables['mpw_fall'] is None:
                        tables['mpw_fall'] = val
                    else:
                        tables['mpw_fall'] = max_elementwise(tables['mpw_fall'], val)

        elif ttype == 'minimum_period' and ptype == 'clk':
            m2 = re.search(r'rise_constraint\s*\([^)]*\)\s*\{(.*?)\}', block, re.DOTALL)
            if m2:
                val = parse_1x7('{' + m2.group(1) + '}')
                if val is not None:
                    if tables['min_period'] is None:
                        tables['min_period'] = val
                    else:
                        tables['min_period'] = max_elementwise(tables['min_period'], val)

    result.update(tables)
    result['num_words']  = NUM_WORDS
    result['mux_ratio']  = MUX_RATIO
    result['write_size'] = WRITE_SIZE
    return result


def main():
    out = {}
    for corner, suffix in CORNER_SUFFIX.items():
        out[corner] = {}
        for dw in DATA_WIDTHS:
            name = f"sram22_{NUM_WORDS}x{dw}m{MUX_RATIO}w{WRITE_SIZE}"
            lib_path = os.path.join(BUILD_DIR, name, f"{name}_{suffix}.lib")
            if not os.path.exists(lib_path):
                print(f"  MISSING: {lib_path}", file=sys.stderr)
                continue
            print(f"  Parsing {corner} dw={dw}...")
            data = parse_lib(lib_path)
            # Check for None tables
            for k, v in data.items():
                if v is None and k not in ('num_words', 'mux_ratio', 'write_size'):
                    print(f"    WARNING: {k} is None for dw={dw} {corner}", file=sys.stderr)
            out[corner][str(dw)] = data

    out_name = f"{NUM_WORDS}m{MUX_RATIO}w{WRITE_SIZE}.json"
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), out_name)
    with open(out_path, 'w') as f:
        json.dump(out, f, indent=2)
    print(f"\nWrote {out_path}")

    # Sanity check: print a few values
    print("\nSanity check (tt dw=8 cell_rise[0][0]):", out['tt']['8']['cell_rise'][0][0])
    print("Sanity check (tt dw=128 min_period[0]):", out['tt']['128']['min_period'][0])
    print("Sanity check (ss dw=8 cell_rise[0][0]):", out['ss']['8']['cell_rise'][0][0])


if __name__ == '__main__':
    main()
