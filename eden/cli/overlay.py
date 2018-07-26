#!/usr/bin/env python3
#
# Copyright (c) 2004-present, Facebook, Inc.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree. An additional grant
# of patent rights can be found in the PATENTS file in the same directory.

import contextlib
import errno
import fcntl
import os
import stat
import struct
from typing import BinaryIO, Iterator, Optional, Tuple

from facebook.eden.overlay.ttypes import OverlayDir


class InvalidOverlayFile(Exception):
    pass


class NoSuchOverlayFile(Exception):
    def __init__(self, inode_number: int) -> None:
        super().__init__(f"inode {inode_number} is not materialized in the overlay")
        self.inode_number = inode_number


class InodeLookupError(Exception):
    def __init__(self, msg: str, errnum: int) -> None:
        super().__init__(msg)
        self.errno = errnum


class OverlayHeader:
    LENGTH = 64
    VERSION_1 = 1

    TYPE_DIR = b"OVDR"
    TYPE_FILE = b"OVFL"

    STRUCT_FORMAT = ">4sIQQQQQQ8s"

    @classmethod
    def parse(cls, data: bytes, type: Optional[bytes] = None) -> "OverlayHeader":
        # A 0-length file is somewhat common on unclean reboot,
        # so use a separate exception message for this case.
        if len(data) == 0:
            raise InvalidOverlayFile("zero-sized overlay file")
        if len(data) < cls.LENGTH:
            raise InvalidOverlayFile(
                "overlay file is too short to contain a header: length={len(data)}"
            )

        (
            header_id,
            version,
            atime_sec,
            atime_nsec,
            ctime_sec,
            ctime_nsec,
            mtime_sec,
            mtime_nsec,
            padding,
        ) = struct.unpack(cls.STRUCT_FORMAT, data)
        if header_id not in (cls.TYPE_DIR, cls.TYPE_FILE):
            raise InvalidOverlayFile(
                "overlay file is too short to contain a header: length={len(data)}"
            )
        if version != cls.VERSION_1:
            raise InvalidOverlayFile(f"unsupported overlay file version {version}")

        return OverlayHeader(
            header_id,
            version,
            atime_sec,
            atime_nsec,
            ctime_sec,
            ctime_nsec,
            mtime_sec,
            mtime_nsec,
        )

    def __init__(
        self,
        type: bytes,
        version: int,
        atime_sec: int = 0,
        atime_nsec: int = 0,
        ctime_sec: int = 0,
        ctime_nsec: int = 0,
        mtime_sec: int = 0,
        mtime_nsec: int = 0,
        padding: bytes = b"\0\0\0\0\0\0\0\0",
    ) -> None:
        self.type = type
        self.version = version
        self.atime_sec = atime_sec
        self.atime_nsec = atime_nsec
        self.ctime_sec = ctime_sec
        self.ctime_nsec = ctime_nsec
        self.mtime_sec = mtime_sec
        self.mtime_nsec = mtime_nsec
        self.padding = padding

    @property
    def atime(self) -> float:
        return self.atime_sec + (self.atime_nsec / 1000000000.0)

    @atime.setter
    def atime(self, value: float) -> None:
        self.atime_sec = int(value)
        self.atime_nsec = int((value - self.atime_sec) * 1000000000)

    @property
    def ctime(self) -> float:
        return self.ctime_sec + (self.ctime_nsec / 1000000000.0)

    @ctime.setter
    def ctime(self, value: float) -> None:
        self.ctime_sec = int(value)
        self.ctime_nsec = int((value - self.ctime_sec) * 1000000000)

    @property
    def mtime(self) -> float:
        return self.mtime_sec + (self.mtime_nsec / 1000000000.0)

    @mtime.setter
    def mtime(self, value: float) -> None:
        self.mtime_sec = int(value)
        self.mtime_nsec = int((value - self.mtime_sec) * 1000000000)

    def serialize(self) -> bytes:
        return struct.pack(
            self.STRUCT_FORMAT,
            self.type,
            self.version,
            self.atime_sec,
            self.atime_nsec,
            self.ctime_sec,
            self.ctime_nsec,
            self.mtime_sec,
            self.mtime_nsec,
            self.padding,
        )


class Overlay:
    ROOT_INODE_NUMBER = 1

    def __init__(self, path: str) -> None:
        self.path = path

    @contextlib.contextmanager
    def try_lock(self) -> Iterator[bool]:
        info_path = os.path.join(self.path, "info")
        try:
            lock_file = open(info_path, "rb")
        except OSError:
            yield False
            return

        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield True
        except OSError:
            yield False
        finally:
            # Release the lock once the yield returns
            lock_file.close()

    def get_path(self, inode_number: int) -> str:
        dir_name = "{:02x}".format(inode_number % 256)
        return os.path.join(self.path, dir_name, str(inode_number))

    def open_overlay_file(self, inode_number: int) -> BinaryIO:
        try:
            return open(self.get_path(inode_number), "rb")
        except OSError as ex:
            if ex.errno == errno.ENOENT:
                raise NoSuchOverlayFile(inode_number)
            raise

    def read_header(self, f: BinaryIO) -> OverlayHeader:
        data = f.read(OverlayHeader.LENGTH)
        return OverlayHeader.parse(data)

    def check_header(
        self, f: BinaryIO, inode_number: int, expected_type: bytes
    ) -> OverlayHeader:
        data = f.read(OverlayHeader.LENGTH)
        header = OverlayHeader.parse(data)
        if header.type != expected_type:
            raise InvalidOverlayFile(
                "unexpected type for inode {inode_number} in overlay: "
                "expected {expected_type!r} but found {header.type!r}"
            )
        return header

    def read_dir_inode(self, inode_number: int) -> OverlayDir:
        return self.read_dir_inode_tuple(inode_number)[1]

    def read_dir_inode_tuple(
        self, inode_number: int
    ) -> Tuple[OverlayHeader, OverlayDir]:
        with self.open_overlay_file(inode_number) as f:
            header = self.check_header(f, inode_number, OverlayHeader.TYPE_DIR)
            data = f.read()

        return (header, self.parse_dir_inode_data(data))

    def parse_dir_inode_data(self, data: bytes) -> OverlayDir:
        from thrift.util import Serializer
        from thrift.protocol import TCompactProtocol

        tree_data = OverlayDir()
        protocol_factory = TCompactProtocol.TCompactProtocolFactory()
        Serializer.deserialize(protocol_factory, data, tree_data)
        return tree_data

    def open_file_inode(self, inode_number: int) -> BinaryIO:
        return self.open_file_inode_tuple(inode_number)[1]

    def open_file_inode_tuple(
        self, inode_number: int
    ) -> Tuple[OverlayHeader, BinaryIO]:
        """Open the overlay file for the specified inode number.

        Returns the header information and a file object opened to the start of the
        file inode contents.
        """
        f = self.open_overlay_file(inode_number)
        try:
            header = self.check_header(f, inode_number, OverlayHeader.TYPE_FILE)
        except Exception:
            f.close()
            raise
        return (header, f)

    def lookup_path(self, path: str) -> Optional[int]:
        """
        Lookup a path in the overlay.

        Returns the inode number corresponding to the path, if the path is materialized.

        - If an inode number is found for this path, returns the inode number.
        - If one of the parent directories is not materialized, returns None.
          Without checking the source control data we cannot tell if this logical path
          exists or not.
        - If this path or one of its parent directories does not exist throws an
          InodeLookupError

        May throw other exceptions on error.
        """
        assert path
        assert not os.path.isabs(path)

        parent_inode_number = self.ROOT_INODE_NUMBER
        path_parts = path.split(os.sep)

        index = 0
        while True:
            parent_dir = self.read_dir_inode(parent_inode_number)
            desired = path_parts[index]
            index += 1

            entries = [] if parent_dir.entries is None else parent_dir.entries.items()
            for name, entry in entries:  # noqa: ignore=B007
                if name == desired:
                    break
            else:
                raise InodeLookupError(f"{path} does not exist", errno.ENOENT)

            if index >= len(path_parts):
                return entry.inodeNumber

            if entry.mode is None or stat.S_IFMT(entry.mode) != stat.S_IFDIR:
                non_dir_path = os.path.sep.join(path_parts[:index])
                raise InodeLookupError(
                    f"error looking up {path}: {non_dir_path} is not a directory",
                    errno.ENOTDIR,
                )
            if entry.hash:
                # This directory along the chain is not materialized
                return None

            parent_inode_number = entry.inodeNumber