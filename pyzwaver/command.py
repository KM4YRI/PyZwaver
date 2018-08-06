#!/usr/bin/python3
# Copyright 2016 Robert Muth <robert@muth.org>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; version 3
# of the License.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA  02111-1307, USA.

"""
command.py contain code for parsing and assembling API_APPLICATION_COMMAND_requests.

It also contains some logic pertaining to the node state machine.
"""

import logging
import re

from pyzwaver import zwave as z


# ======================================================================
def EventTypeToString(t):
    if t < len(DOOR_LOG_EVENT_TYPE):
        return DOOR_LOG_EVENT_TYPE[t]
    return "@UNKNOWN_EVENT[%d]@" % t


# ======================================================================
def _GetSignedValue(data):
    value = 0
    negative = (data[0] & 0x80) != 0
    for d in data:
        value <<= 8
        if negative:
            value += ~d
        else:
            value += d

    if negative:
        value += 1
        return -value
    else:
        return value


# ======================================================================
def _GetReading(m, index, units_extra):
    c = m[index]
    size = c & 0x7
    units = (c & 0x18) >> 3 | units_extra
    exp = (c & 0xe0) >> 5
    mantissa = m[index + 1: index + 1 + size]
    value = _GetSignedValue(mantissa) / pow(10, exp)
    return index + 1 + size, units, mantissa, exp, value


def _GetTimeDelta(m, index):
    return index + 2, m[index] * 256 + m[index + 1]


def _ParseMeter(m, index):
    if index + 2 > len(m):
        logging.error("cannot parse value")
        return index, None
    c1 = m[index]
    unit_extra = (c1 & 0x80) >> 7
    type = c1 & 0x1f
    rate = (c1 & 0x60) >> 5
    c2 = m[index + 1]
    size = c2 & 0x7
    unit = (c2 & 0x18) >> 3 | unit_extra << 2
    exp = (c2 & 0xe0) >> 5
    index += 2
    out = {
        "type": type,
        "unit": unit,
        "exp": exp,
        "rate": rate,
    }
    if index + size >= len(m):
        logging.error("cannot parse value")
        return index, None
    mantissa = m[index: index + size]
    index += size
    value = _GetSignedValue(mantissa) / pow(10, exp)
    out["mantissa"], out["_value"] = mantissa, value
    if index + 2 <= len(m):
        # TODO: provide non-raw version of this
        index, out["dt"] = _GetTimeDelta(m, index)
    n = 2
    if index + size <= len(m):
        mantissa = m[index: index + size]
        value = _GetSignedValue(mantissa) / pow(10, out["exp"])
        out["mantissa%d" % n], out["_value%d" % n] = mantissa, value
        index += size
        n += 1
    return index, out


# ======================================================================
# all parsers return the amount of consumed bytes or a negative number to indicate
# success


def _ParseByte(m, index):
    if len(m) <= index:
        logging.error("cannot parse byte")
        return index, None
    return index + 1, m[index]


def _ParseWord(m, index):
    if len(m) <= index + 1:
        logging.error("cannot parse word")
        return index, None
    return index + 2, m[index] * 256 + m[index + 1]


_ENCODING_TO_DECODER = [
    "ascii",
    "latin1",  # "cp437" ,
    "utf-16-be",
]


def _ParseName(m, index):
    assert len(m) > index
    encoding = m[index] & 3
    m = m[index + 1:]
    decoded = bytes(m).decode(_ENCODING_TO_DECODER[encoding])
    return len(m), {"encoding": encoding, "text": m, "_decoded": decoded}


def _ParseStringWithLength(m, index):
    size = m[index]
    return 1 + size + index, bytes(m[index + 1: size])


def _ParseStringWithLengthAndEncoding(m, index):
    encoding = m[index] >> 5
    size = m[index] & 0x1f
    return 1 + size, {"encoding": encoding, "text": m[index + 1:index + 1 + size]}


def _ParseListRest(m, index):
    size = len(m) - index
    return index + size, m[index:index + size]


def _ExtractBitVector(data, offset):
    bits = set()
    for i in range(len(data)):
        b = data[i]
        for j in range(8):
            if b & (1 << j) != 0:
                bits.add(j + i * 8 + offset)
    return bits


def _ParseGroups(m, index):
    misc = m[index]
    count = misc & 0x3f
    if len(m) < index + 1 + count * 7:
        logging.error("malformed groups section: %d (%d)", len(m), count)
        return index, None
    groups = []
    index += 1
    for i in range(count):
        num = m[index + 0]
        profile = m[index + 2] * 256 + m[index + 3]
        event = m[index + 5] * 256 + m[index + 6]
        groups.append((num, profile, event))
        index += 7
    return index, groups


def _ParseBitVector(m, index):
    size = m[index]
    return index + 1 + size, _ExtractBitVector(m[index + 1:index + 1 + size], 0)


def _ParseBitVectorRest(m, index):
    # size = len(m) - index
    return len(m), _ExtractBitVector(m[index:], 0)


def _ParseNonce(m, index):
    size = 8
    if len(m) < index + size:
        logging.error("malformed nonce:")
        return index, None
    return index + size, m[index:index + size]


def _ParseDataRest(m, index):
    size = len(m) - index
    return index + size, m[index:index + size]


def _GetIntLittleEndian(m):
    x = 0
    shift = 0
    for i in m:
        x += i << shift
        shift += 8
    return x


def _GetIntBigEndian(m):
    x = 0
    for i in m:
        x <<= 8
        x += i
    return x


def _ParseRestLittleEndianInt(m, index):
    size = len(m) - index
    return index + size, {"size": size, "value": _GetIntLittleEndian(m[index:index + size])}


def _ParseSensor(m, index):
    # we need at least two bytes
    if len(m) < index + 2:
        logging.error("malformed sensor string")
        return index, None

    c = m[index]
    precision = (c >> 5) & 7
    scale = (c >> 3) & 3
    size = c & 7
    if len(m) < index + 1 + size:
        logging.error(
            "malformed sensor string %d %d %d", precision, scale, size)
        return index
    mantissa = m[index + 1: index + 1 + size]
    value = _GetSignedValue(mantissa) / pow(10, precision)
    return index + 1 + size, {"exp": precision, "scale": scale, "mantissa": mantissa,
                              "_value": value}


def _ParseValue(m, index):
    size = m[index] & 0x7
    start = index + 1
    return index + 1 + size, {"size": size, "value": _GetIntBigEndian(m[start:start + size])}


def _ParseDate(m, index):
    if len(m) < index + 7:
        logging.error("malformed time data")
        return len(m)

    year = m[index] * 256 + m[index + 1]
    month = m[index + 2]
    day = m[index + 3]
    hours = m[index + 4]
    mins = m[index + 5]
    secs = m[index + 6]
    return index + 7, [year, month, day, hours, mins, secs]


_PARSE_ACTIONS = {
    'A': _ParseStringWithLength,
    'F': _ParseStringWithLengthAndEncoding,
    'B': _ParseByte,
    'C': _ParseDate,
    'G': _ParseGroups,
    'N': _ParseName,
    'L': _ParseListRest,
    'R': _ParseRestLittleEndianInt,  # as integer
    "W": _ParseWord,
    "V": _ParseValue,
    "M": _ParseMeter,
    "O": _ParseNonce,
    "D": _ParseDataRest,  # as Uint8List
    "T": _ParseBitVector,
    "U": _ParseBitVectorRest,
    "X": _ParseSensor,
}


def _GetParameterDescriptors(m):
    if len(m) < 2:
        logging.error("malformed command %s", m)
        return None
    key = m[0] * 256 + m[1]
    return z.SUBCMD_TO_PARSE_TABLE[key]


def ParseCommand(m, prefix=""):
    """ParseCommand decodes an API_APPLICATION_COMMAND request into a map of values"""
    table = _GetParameterDescriptors(m)

    if table is None:
        logging.error("%s unknown command", prefix)
        return []

    out = {}
    index = 2
    for t in table:
        kind = t[0]
        name = t[2:-1]
        new_index, value = _PARSE_ACTIONS[kind](m, index)
        out[name] = value
        if value is None:
            logging.error("%s malformed message while parsing format %s %s", prefix, kind, table)
            return None
        index = new_index
    return out


# ======================================================================


def _MakeValue(conf, value):
    size = conf & 7
    assert size in (1, 2, 4)

    data = [conf]
    shift = (size - 1) * 8
    while shift >= 0:
        data.append(0xff & (value >> shift))
        shift -= 8
    return data


def _MakeDate(date):
    return [date[0] // 256, date[0] % 256, date[1], date[2], date[3], date[4], date[5]]


def _MakeSensor(args):
    m = args["mantissa"]
    c = args["exp"] << 5 | args["scale"] << 3 | len(m)
    return [c] + m


def _MakeMeter(args):
    c1 = (args["unit"] & 4) << 7 | args["rate"] << 5 | (args["type"] & 0x1f)
    c2 = args["exp"] << 5 | (args["unit"] & 3) << 3 | len(args["mantissa"])
    delta = []
    if "dt" in args:
        dt = args["dt"]
        delta = [dt >> 8, dt & 0xff]
    return [c1, c2] + args["mantissa"] + delta + args.get("mantissa2", [])


# raw_cmd: [class, subcommand, arg1, arg2, ....]
def AssembleCommand(cmd0, cmd1, args):
    table = z.SUBCMD_TO_PARSE_TABLE[cmd0 * 256 + cmd1]
    assert table is not None
    data = [
        cmd0,
        cmd1
    ]
    # logging.debug("${raw_cmd[0]} ${raw_cmd[1]}: table length:
    # ${table.length}")
    for t in table:
        kind = t[0]
        name = t[2:-1]
        v = args[name]
        if kind == 'B':
            data.append(v)
        elif kind == 'W':
            data.append((v >> 8) & 0xff)
            data.append(v & 0xff)
        elif kind == 'Y':
            if v is not None:
                data.append(v)
        elif kind == 'N':
            data.append(1)
            # for c in v:
            # out.append(ord(c))
        elif kind == 'K':
            if len(v) != 16:
                logging.error("bad key parameter: ${v}")
                assert False
            data += v
        elif kind == 'D':
            data += v
        elif kind == 'S':
            logging.info("unknown parameter: ${t[0]}")
            assert False, "unreachable"
            # for c in v:
            # out.append(ord(c))
        elif kind == 'L':
            data += v
        elif kind == 'C':
            data += _MakeDate(v)
        elif kind == 'O':
            if len(v) != 8:
                logging.error("bad nonce parameter: ${v}")
            data += v
        elif kind == 'V':
            size = v["size"]
            value = v["value"]
            data += [size]
            for i in reversed(range(size)):
                data += [(value >> 8 * i) & 0xff]
        elif kind == 'X':
            data += _MakeSensor(v)
        elif kind == 'M':
            data += _MakeMeter(v)
        elif kind == 'F':
            m = v["text"]
            c = (v["encoding"] << 5) | len(m)
            data += [c] + v["text"]
        elif kind == 'R':
            value = v["value"]
            for i in range(v["size"]):
                data += [value & 0xff]
                value >>= 8
        else:
            logging.error("unknown parameter: ${t[0]}")
            assert False, "unreachable"

    return data


