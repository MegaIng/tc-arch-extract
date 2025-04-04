from __future__ import annotations

import os
import platform
import sys
from argparse import BooleanOptionalAction
from pathlib import Path
import zipfile
import argparse
from pprint import pp
from textwrap import wrap


def get_save_path():
    match platform.system():
        case 'Linux':
            return Path("~/.local/share/godot/app_userdata/Turing Complete").expanduser()
        case 'Darwin':
            return Path("~/Library/Application Support/Godot/app_userdata/Turing Complete").expanduser()
        case 'Windows':
            return Path(os.path.expandvars(r"%AppData%\godot\app_userdata\Turing Complete"))
        case other:
            raise ValueError(f"Unrecognized platform: {other!r}")


SAVE_PATH = get_save_path()
verbosity: int = None


def read_varint(data: bytes, start=0) -> tuple[int, int]:
    i = start
    out = 0
    last = 0x80
    while last & 0x80 != 0:
        out <<= 7
        out |= data[i] & 0x7f
        last = data[i]
        i += 1
    return (out, i)


class SnappyDecompress:
    def __init__(self, compressed: bytes, base: int = 0):
        self.compressed_data = compressed
        self.uncompressed_data = bytearray()
        (self.uncompressed_length, self.index) = read_varint(compressed, base)

    def __getitem__(self, item) -> bytearray | int:
        if isinstance(item, int):
            self._uncompress_till(item)
        elif isinstance(item, slice):
            item = slice(*item.indices(self.uncompressed_length))
            self._uncompress_till(max(item.start, item.stop))
        return self.uncompressed_data[item]

    def _uncompress_literal(self):
        tag = length = self.compressed_data[self.index] >> 2
        start_index = self.index
        self.index += 1
        if length >= 60:
            bc = length - 59
            length = int.from_bytes(self.compressed_data[self.index:self.index + bc], 'little')
            self.index += bc
        length += 1
        self.uncompressed_data += self.compressed_data[self.index:self.index + length]
        self.index += length

    def _uncompress_copy(self):
        tag = self.compressed_data[self.index]
        start_index = self.index
        self.index += 1
        if tag & 0x3 == 1:
            length = ((tag & 0b00011100) >> 2) + 4
            offset = (tag & 0b11100000) << 3
            offset |= self.compressed_data[self.index]
            self.index += 1
        elif tag & 0x3 == 2:
            length = (tag >> 2) + 1
            offset = int.from_bytes(self.compressed_data[self.index:self.index + 2], 'little')
            self.index += 2
        elif tag & 0x3 == 3:
            length = (tag >> 2) + 1
            offset = int.from_bytes(self.compressed_data[self.index:self.index + 4], 'little')
            self.index += 4
        else:
            raise ValueError(bin(tag))
        k = len(self.uncompressed_data) - offset
        for i in range(length):
            self.uncompressed_data.append(self.uncompressed_data[k + i])

    def _uncompress_till(self, index: int):
        while self.index < len(self.compressed_data) and len(self.uncompressed_data) < index:
            command = self.compressed_data[self.index]
            if command & 0x3 == 0:
                self._uncompress_literal()
            else:
                self._uncompress_copy()


def get_sch_id(version: int, content: SnappyDecompress) -> int:
    assert version in {6, 7, 8, 9, 10}, version
    return int.from_bytes(content[0:8], 'little')


def extract_info(version: int, content: SnappyDecompress) -> tuple[int, tuple[int, int], list[int]]:
    assert version in {6, 7, 8, 9, 10}, version
    i = 0
    schematic_id = int.from_bytes(content[i:i + 8], 'little')
    i += 8
    i += 4  # u4 hub_id
    gate = int.from_bytes(content[i:i + 8], 'little')
    i += 8
    delay = int.from_bytes(content[i:i + 8], 'little')
    i += 8
    i += 1  # u1 menu_visible
    if version in {6}:
        i += 4  # u4 clock_speed
    else:
        i += 8  # u8 clock_speed
    dependency_count = int.from_bytes(content[i:i + 2], 'little')
    i += 2
    dependencies = [int.from_bytes(content[i + j * 8:i + (j + 1) * 8], 'little') for j in range(dependency_count)]
    i += dependency_count * 8
    return schematic_id, (gate, delay), dependencies


def scan_component_factory() -> dict[int, tuple[str, Path, (int, SnappyDecompress)]]:
    component_factory = (SAVE_PATH / "schematics" / "component_factory")
    out = {}
    for e in component_factory.rglob("circuit.data"):
        c = e.read_bytes()
        v = c[0]
        dc = SnappyDecompress(c, base=1)
        ccid = get_sch_id(v, dc)
        cc_name = str(e.relative_to(component_factory).parent).replace('\\', '/')
        out[ccid] = (cc_name, e, (v, dc))
    return out


def collect_files(arch_name: str) -> tuple[dict[str, Path], int, int]:
    main = SAVE_PATH / "schematics" / "architecture" / arch_name / "circuit.data"
    assert main.is_file(), main
    out = {arch_name: main}
    content = main.read_bytes()
    ccs = scan_component_factory()
    v = content[0]
    dc = SnappyDecompress(content, base=1)
    main_id, (gate, delay), dependencies = extract_info(v, dc)
    if verbosity >= 3:
        print(f"CC to name mapping:")
        for ccid, (name,_,_) in ccs.items():
            print(f"\t{ccid}: {name}")
    if verbosity >= 2:
        print(f"Main Schematic with ID {main_id} depends on {dependencies}")
    queue = list(dependencies)
    to_extract = set()
    while queue:
        ccid = queue.pop()
        if ccid not in to_extract:
            to_extract.add(ccid)
            if ccid not in ccs:
                raise ValueError(f"Missing component with id {ccid:016X}")
            _, _, args = ccs[ccid]
            _, _, dependencies = extract_info(*args)
            assert 0 not in dependencies, (ccid, ccs[ccid])
            if verbosity >= 2:
                print(f"CC Schematic {ccs[ccid][0]} (CCID={ccid}) depends on {dependencies}")
            queue.extend(dependencies)
    for ccid in to_extract:
        name, path, _ = ccs[ccid]
        out[name] = path
    return out, gate, delay


def parse_args(args: list[str] | None = None):
    parser = argparse.ArgumentParser()
    parser.add_argument("arch_name")
    parser.add_argument("-v", "--verbose", action="count", default=0)
    parser.add_argument("--scores", action=BooleanOptionalAction, default=False, help="Controls if levels.txt is included in the zip file")
    return parser.parse_args(args)


def main(args: list[str] | None = None):
    global verbosity
    nspace = parse_args(args)
    verbosity = nspace.verbose
    sch_files, gate, delay = collect_files(nspace.arch_name)
    zip_name = f"{Path(nspace.arch_name).name}_{gate}_{delay}.zip"
    with zipfile.ZipFile(zip_name, "w") as f:
        for name, path in sch_files.items():
            if "component_factory" in path.parts:
                local_path = f"schematics/component_factory/tc-archs/{nspace.arch_name}/{name}/circuit.data"
            else:
                local_path = f"schematics/architecture/tc-archs/{name}/circuit.data"
            if verbosity >= 1:
                print(f"Adding {local_path!r} (from {path})")
            f.write(path, local_path)
        if nspace.scores:
            if verbosity >= 1:
                print(f'Adding {'levels.txt'!r} (from {SAVE_PATH / "levels.txt"})')
            f.write(SAVE_PATH / "levels.txt", "levels.txt")
    print("Wrote", zip_name)


if __name__ == '__main__':
    main()
