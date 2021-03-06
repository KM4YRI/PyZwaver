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

"""

import logging
import struct
import threading
import time
import queue

from Crypto.Cipher import AES

from pyzwaver import zmessage
from pyzwaver import command
from pyzwaver import z

_DEFAULT_NETWORK_KEY = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]

_TEMP_NETWORK_KEY = [0] * 16

_CRYPT_SECRET = [0xaa] * 16

_AUTH_SECRET = [0x55] * 16

_NONCE_TIMEOUT_SEC = 5.0

_NONCE_GET_RAW = [z.Security, z.Security_NonceGet]


def Crypt(key, data, iv_orig):
    assert len(iv_orig) == 16
    iv = bytes(iv_orig)
    cipher = AES.new(bytes(key))
    out = [0] * len(data)
    for i in range(len(data)):
        if i % 16 == 0:
            iv = cipher.encrypt(iv)
        out[i] = data[i] ^ iv[i % 16]
    return out


def ComputeMAC(key, data, iv, sub_command, src_node, dst_node):
    assert len(iv) == 16
    padding = [0] * 16
    header = [sub_command, src_node, dst_node, len(data)]
    buf = header + data + padding
    cipher = AES.new(bytes(key))

    auth = list(cipher.encrypt(bytes(iv)))
    for x in range(0, len(header) + len(data), 16):
        for i in range(16):
            auth[i] = auth[i] ^ buf[x + i]
        auth = list(cipher.encrypt(bytes(auth)))

    return auth[0:8]


class Crypter:

    def __init__(self, key):
        assert len(key) == 16
        cipher = AES.new(bytes(key))
        self._auth_key = cipher.encrypt(bytes(_AUTH_SECRET))
        self._crypt_key = cipher.encrypt(bytes(_CRYPT_SECRET))

    def Wrap(self, payload, nonce, random, sub_command, src_node, dst_node):
        assert len(random) == 8
        assert len(nonce) == 8
        iv = random + nonce
        plain = [0] + payload
        enc = Crypt(self._crypt_key, plain, iv)
        mac = ComputeMAC(self._auth_key, enc, iv, sub_command, src_node, dst_node)
        return iv[0:8] + enc + [nonce[0]] + mac

    def Unwrap(self, wrapped, nonce, sub_command, src_node, dst_node):
        enc_size = len(wrapped) - 8 - 8 - 1
        iv = wrapped[0:8] + nonce
        enc = wrapped[8:8 + enc_size]
        mac_expected = wrapped[-8:]
        if wrapped[-9] != nonce[0]:
            logging.error("nonce in wrapped message is off %02x vs %02x",
                          wrapped[-9], nonce[0])
            return None
        mac_actual = ComputeMAC(self._auth_key, enc, iv, sub_command, src_node, dst_node)
        for a, b in zip(mac_actual, mac_expected):
            if a != b:
                logging.error("mac mismatch %s vs %s", mac_expected, mac_actual)
                return None
        plain = Crypt(self._crypt_key, enc, iv)
        return plain[1:]


class Nonce:
    def __init(self, value, now):
        self.value = value
        self.expiration = now + _NONCE_EXPIRATION_SEC

    def IsExpired(self, now):
        return now > self.expiration


class SecureQueue:
    """ SecurityQueue handles per node security

    It manages a queue of outbound message that need to encrypted and
    makes sure that nonces are available to both nodes involved in the communication.
    An in-bound nonce is provide by *this* node so that the *other* node can send it
    encrypted messages.
    An out-bound nonce is provide by the *other* node so that *this* node can send it
    encrypted messages.
    """

    def EnqueueMessage(self, message):
        self._queue.append(message)

    def SetKey(self, key):
        logging.warning("about to change key to %s", repr(key));
        self._crypter = _Crypter(key)

    def GetRandomList(self, n):
        # DEBUGGING HACK FIXME
        return _CRYPT_SECRET[:n]

    def __init(self, node, controller_node, random):
        self, _node = node
        self._controller_node = controller_node
        self.SetKey(_DEFAULT_KEY)
        self._queue = []

        # for message send form controller to the node
        self, _nonce_outbound = Nonce(None, -_NONCE_EXPIRATION_SEC)
        self, _nonce_outbound_requested = False

    def ProcessReceivedNonce(self, nonce):
        # we assume the nonce is not expired
        self._nonce_outbound = nonce
        if len(self._queue) != 0:
            return None
        cmd = self._queue.pop(0)
        raw = command.AssembleCommand(cmd)
        wrapped = self._security.Wrap(raw,
                                      nonce.value,
                                      self.GetRandomList(8),
                                      z.Security_MessageEncap,
                                      self._controller,
                                      self._node)
        if cmd[0] == z.Security and cmd[1] == z.Security_NetworkKeySet:
            self.SetKey(cmd[2])

        return [z.Security, z.Security_MessageEncap, wrapped];
