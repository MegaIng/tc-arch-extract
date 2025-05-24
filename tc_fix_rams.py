from __future__ import annotations

import shutil
from dataclasses import dataclass, fields, field
import argparse
from typing import Self


def read_varint(data: bytes, start=0) -> tuple[int, int]:
    i = start
    out = 0
    j = 0
    last = 0x80
    while last & 0x80 != 0:
        out |= (data[i] & 0x7f) << j
        j += 7
        last = data[i]
        i += 1
    return (out, i)


def make_varint(n: int) -> bytes:
    out = bytearray()
    while n > 0x7f:
        out.append(n & 0x7f | 0x80)
        n >>= 7
    out.append(n)
    return out


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


def snappy_encode(data: bytes) -> bytes:
    if not data:
        return b''
    return make_varint(len(data)) + bytes([63 << 2]) + int.to_bytes(len(data) - 1, 4, 'little') + data


def get_unparser(parser):
    if hasattr(parser, 'unparse'):
        return parser.unparse
    return parser.__self__.unparse


def U(n):
    def inner(data, i):
        return (int.from_bytes(data[i:i + n], 'little', signed=False), i + n)

    inner.unparse = lambda x: x.to_bytes(n, 'little', signed=False)

    inner.__qualname__ = inner.__name__ = f"U({n})"
    return inner


def S(n):
    def inner(data, i):
        return (int.from_bytes(data[i:i + n], 'little', signed=True), i + n)

    inner.unparse = lambda x: x.to_bytes(n, 'little', signed=True)
    inner.__qualname__ = inner.__name__ = f"S({n})"
    return inner


def seq(f, l=U(2)):
    def inner(data, i):
        out = []
        length, i = with_note(l, data, i, note=f"While parsing length at {i:X}")
        assert length < len(data), (inner, hex(length))
        for j in range(length):
            e, i = with_note(f, data, i, note=f"While parsing element {j} at {i:X}")
            out.append(e)
        return out, i

    def unparse(x):
        f_un = get_unparser(f)
        l_un = get_unparser(l)
        return l_un(len(x)) + b''.join(f_un(e) for e in x)

    inner.unparse = unparse

    inner.__qualname__ = inner.__name__ = f"seq({f.__name__}, {l.__qualname__})"
    return inner


def pair(f, g):
    def inner(data, i):
        a, i = with_note(f, data, i, note=f'Left side at {i:X}')
        b, i = with_note(g, data, i, note=f'Right side at {i:X}')
        return (a, b), i

    inner.unparse = lambda x: get_unparser(f)(x[0]) + get_unparser(g)(x[1])

    return inner


def string(l=U(2)):
    def inner(data, i):
        length, i = l(data, i)
        return data[i:i + length].decode('utf-8'), i + length

    def unparse(x):
        data = x.encode('utf-8')
        return get_unparser(l)(len(data)) + data

    inner.unparse = unparse
    return inner


def remainder():
    def inner(data, i):
        return data[i:], len(data)

    inner.unparse = lambda x: x
    return inner


def with_note(f, *args, note):
    try:
        a, i = f(*args)
        assert isinstance(i, int), (f, args)
        return a, i
    except Exception as e:
        e.add_note(note)
        raise e


@dataclass
class Parser:
    raw: bytes = field(repr=False)

    @classmethod
    def parse(cls, data: bytes, i: int) -> tuple[Self, int]:
        if i >= len(data):
            raise ValueError
        start = i
        kwargs = {}
        for field in fields(cls):
            if field.name == 'raw':
                continue
            if 'if' in field.metadata:
                if not field.metadata['if'](**kwargs):
                    continue
            parser = field.metadata['parser']
            kwargs[field.name], i = with_note(parser, data, i, note=f"While parsing field {field.name} at {i:X}")
        kwargs['raw'] = data[start:i]
        return cls(**kwargs), i

    def unparse(self):
        data = bytearray()
        for field in fields(self):
            if field.name == 'raw':
                continue
            if 'if' in field.metadata:
                if not field.metadata['if'](**vars(self)):
                    continue
            parser = field.metadata['parser']
            data.extend(get_unparser(parser)(getattr(self, field.name)))
        return data


@dataclass
class Point(Parser):
    x: int = field(metadata={'parser': S(2)})
    y: int = field(metadata={'parser': S(2)})


@dataclass
class LinkedComponent(Parser):
    permanent_id: int = field(metadata={'parser': S(8)})
    inner_id: int = field(metadata={'parser': S(8)})
    name: str = field(metadata={'parser': string()})
    offset: int = field(metadata={'parser': S(8)})


@dataclass
class CustomCompData(Parser):
    custom_id: int = field(metadata={'parser': S(8)})
    static_states: list[tuple[int, int]] = field(metadata={'parser': seq(pair(S(8), S(8)))})


@dataclass
class Component(Parser):
    component_kind: int = field(metadata={'parser': U(2)})
    position: Point = field(metadata={'parser': Point.parse})
    rotation: int = field(metadata={'parser': U(1)})
    permanent_id: int = field(metadata={'parser': U(8)})
    custom_string: str = field(metadata={'parser': string()})
    settings: list[int] = field(metadata={'parser': seq(U(8))})
    buffer_size: int = field(metadata={'parser': S(8)})
    ui_order: int = field(metadata={'parser': S(2)})
    word_size: int = field(metadata={'parser': S(8)})
    linked_components: list[LinkedComponent] = field(metadata={'parser': seq(LinkedComponent.parse)})
    selected_programs: list[tuple[str, str]] = field(metadata={'parser': seq(pair(string(), string()))})
    custom_data: CustomCompData | None = field(metadata={'parser': CustomCompData.parse,
                                                         'if': lambda component_kind, **_: component_kind == 78},
                                               default=None)


@dataclass
class Schematic(Parser):
    custom_id: bytes = field(metadata={'parser': S(8)})
    hub_id: bytes = field(metadata={'parser': U(4)})
    gate: int = field(metadata={'parser': S(8)})
    delay: int = field(metadata={'parser': S(8)})
    menu_visible: int = field(metadata={'parser': U(1)})
    clock_speed: int = field(metadata={'parser': U(8)})
    dependencies: list[int] = field(metadata={'parser': seq(S(8))})
    description: str = field(metadata={'parser': string()})
    camera_position: Point = field(metadata={'parser': Point.parse})
    synced: int = field(metadata={'parser': U(1)})
    _dummy: int = field(metadata={'parser': U(2)})
    player_data: list[int] = field(metadata={'parser': seq(U(8))})
    hub_description: str = field(metadata={'parser': string()})
    components: list[Component] = field(metadata={'parser': seq(Component.parse, S(8))})
    wires: bytes = field(metadata={'parser': remainder()})


def parse_args(args: list[str] | None = None):
    parser = argparse.ArgumentParser()
    parser.add_argument("circuit_file", help="circuit.data file to fix")
    return parser.parse_args(args)


def main(args: list[str] | None = None):
    nspace = parse_args(args)

    with open(nspace.circuit_file, "rb") as f:
        content = f.read()
    if content[0] != 10:
        raise ValueError("Only version 10 circuit.data files are supported;"
                         " Make an edit to the schematic to make the game resave it.")
    dc = SnappyDecompress(content, 1)[:]

    res, _ = Schematic.parse(dc, 0)

    ram_components = [comp for comp in res.components if comp.component_kind == 118]
    if len(ram_components) != 2:
        raise ValueError("Expected exactly two RAM components")
    ram_components.sort(key=lambda x: x.buffer_size)
    reg_file, program = ram_components
    if reg_file.buffer_size != 256:
        raise ValueError(f"Expected the smaller RAM (the register file) to have exactly 256 bytes of storage. "
                         f"Has {reg_file.buffer_size} bytes")
    if program.buffer_size != 65536:
        raise ValueError(f"Expected the larger RAM (the program) to have exactly 65536 bytes of storage. "
                         f"Has {program.buffer_size} bytes")

    reg_file.word_size = 16
    program.word_size = 32

    res.components.remove(reg_file)
    res.components.remove(program)
    res.components.append(reg_file)
    res.components.append(program)

    shutil.move(nspace.circuit_file, nspace.circuit_file + ".bak")
    with open(nspace.circuit_file, "wb") as f:
        f.write(b'\x0A')
        f.write(snappy_encode(res.unparse()))


if __name__ == '__main__':
    main()
