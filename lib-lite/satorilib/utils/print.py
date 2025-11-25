from typing import Union
from enum import Enum


class Color(Enum):
    darkgrey = "\033[2m"
    black = "\033[8m"
    red = "\033[91m"
    green = "\033[92m"
    yellow = "\033[93m"
    blue = "\033[94m"
    magenta = "\033[95m"
    teal = "\033[96m"
    white = "\033[97m"
    grey = "\033[90m"

    def __init__(self, code):
        self.code = code
        self.ansiEsacpeCodes = code


_ansiEsacpeCodesStyle = {
    'bold': "\033[1m",
    'italic': "\033[3m",
    'underlined': "\033[4m",
    'outlined': "\033[7m",
    'strikethru': "\033[9m"}

_ansiEsacpeCodesColor = {
    'darkgrey': "\033[2m",
    'black': "\033[8m",
    'red': "\033[91m",
    'green': "\033[92m",
    'yellow': "\033[93m",
    'blue': "\033[94m",
    'magenta': "\033[95m",
    'teal': "\033[96m",
    'white': "\033[97m",
    'grey': "\033[90m"}


def colors():
    return list(_ansiEsacpeCodesColor.keys())


def styles():
    return list(_ansiEsacpeCodesStyle.keys())


def colored(
    msg: str,
    color: Union[str, None] = None,
    style: Union[str, None] = None
):
    msg = str(msg)
    reset = "\033[0m"
    if color in _ansiEsacpeCodesColor.keys():
        msg = msg + reset
        if style in _ansiEsacpeCodesStyle.keys():
            msg = _ansiEsacpeCodesStyle[style] + msg
        return _ansiEsacpeCodesColor[color] + msg
    return msg
