#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
#  stackup.py
#  Data structures to represent technology's special cells.
#
#  See LICENSE for licence details.

from enum import Enum
from typing import List, NamedTuple, Tuple, Dict, Optional
from hammer_utils import reverse_dict
from decimal import Decimal

class CellType(Enum):
    TieCell = 1
    EndCap = 2
    IOFiller = 3
    StdFiller = 4

    @classmethod
    def __mapping(cls) -> Dict[str, "CellType"]:
        return {
            "tiecell": CellType.TieCell,
            "endcap": CellType.EndCap,
            "iofiller": CellType.IOFiller,
            "stdfiller": CellType.StdFiller,
        }

    @staticmethod
    def from_str(input_str: str) -> "CellType":
        # pylint: disable=missing-docstring
        try:
            return CellType.__mapping()[input_str]
        except KeyError:
            raise ValueError("Invalid CellType: " + str(input_str))

    def __str__(self) -> str:
        return reverse_dict(CellType.__mapping())[self]


class SpecialCell(NamedTuple('SpecialCell', [
        ('cell_type', CellType),
        ('name', str),
        ('size', Optional[Decimal])
])):
    """
    A SpecialCell is a technology cell used for non logic task (e.g. Tiecells,
    Endcap, filler, etc.

    """
    __slots__ = ()

    @staticmethod
    def from_setting(d: dict) -> "SpecialCell":
        size = d['size']
        if size is not None:
            size = Decimal(d['size'])
        # pylint: disable=missing-docstring
        return SpecialCell(
            cell_type=CellType.from_str(d["cell_type"]),
            name=str(d["name"]),
            size=size,
        )
