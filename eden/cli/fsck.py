#!/usr/bin/env python3
#
# Copyright (c) 2004-present, Facebook, Inc.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree. An additional grant
# of patent rights can be found in the PATENTS file in the same directory.

import binascii
import contextlib
import enum
import os
import stat
import time
import types
from typing import ContextManager, Dict, List, NamedTuple, Optional, Tuple, Type

from . import overlay as overlay_mod


class InodeType(enum.Enum):
    FILE = enum.auto()
    DIR = enum.auto()
    ERROR = enum.auto()
    DIR_ERROR = enum.auto()


class ChildInfo(NamedTuple):
    inode_number: int
    name: str
    mode: int
    hash: Optional[bytes]


class InodeInfo:
    __slots__ = ["inode_number", "type", "parents", "children", "mtime", "error"]

    def __init__(
        self,
        inode_number: int,
        type: InodeType,
        children: List[ChildInfo],
        mtime: Optional[float],
        error: Optional[Exception],
    ) -> None:
        self.inode_number = inode_number
        self.type = type

        # The mtime is the modification time on the overlay file itself, not the
        # value for the logical file represented by this overlay entry.
        # This is mainly present for helping identify when a problem was introduced in
        # the overlay.
        self.mtime = mtime

        self.error = error

        # The other inode(s) that list this as a child
        self.parents: List[Tuple[InodeInfo, ChildInfo]] = []
        self.children = children

    def compute_path(self) -> str:
        if not self.parents:
            if self.inode_number == overlay_mod.Overlay.ROOT_INODE_NUMBER:
                return "/"
            return "[unlinked]"

        parent, child_entry = self.parents[0]
        if parent.inode_number == overlay_mod.Overlay.ROOT_INODE_NUMBER:
            return child_entry.name
        return parent.compute_path() + os.path.sep + child_entry.name


class ErrorLevel(enum.IntEnum):
    # WARNING is for issues that do not affect our ability to show file contents
    # correctly.
    WARNING = 1

    # ERROR issues are problems that prevent us from being able to read file or
    # directory contents.
    ERROR = 2

    @staticmethod
    def get_label(level: int) -> str:
        if level == ErrorLevel.WARNING:
            return "warning"
        return "error"


class Error:
    def __init__(self, level: ErrorLevel) -> None:
        self.level = level

    def detailed_description(self) -> Optional[str]:
        return None


class UnexpectedOverlayFile(Error):
    def __init__(self, path: str) -> None:
        super().__init__(ErrorLevel.WARNING)
        self.path = path
        self.mtime = None
        with contextlib.suppress(OSError):
            self.mtime = os.lstat(path).st_mtime

    def __str__(self) -> str:
        mtime_str = _get_mtime_str(self.mtime)
        return f"unexpected file present in overlay: {self.path}{mtime_str}"


class MissingMaterializedInode(Error):
    def __init__(self, inode: InodeInfo, child: ChildInfo) -> None:
        super().__init__(ErrorLevel.ERROR)
        self.inode = inode
        self.child = child

    def __str__(self) -> str:
        return (
            f"missing overlay file for materialized inode "
            f"{self.inode.compute_path()}/{self.child.name} with "
            f"file mode {self.child.mode:#o}"
        )


class InvalidMaterializedInode(Error):
    def __init__(self, inode: InodeInfo) -> None:
        super().__init__(ErrorLevel.ERROR)
        self.inode = inode
        self.expected_type = self._compute_expected_type()

    def _compute_expected_type(self) -> Optional[InodeType]:
        # Look at the parents to see if this looks like it should be a file or directory
        if self.inode.parents:
            _parent_inode, child_entry = self.inode.parents[0]
            if stat.S_ISDIR(child_entry.mode):
                self.expected_type = InodeType.DIR
            else:
                self.expected_type = InodeType.FILE
        elif self.inode.type == InodeType.DIR_ERROR:
            self.expected_type = InodeType.DIR
        return None

    def __str__(self) -> str:
        if self.expected_type is None:
            type_str = "inode"
        elif self.expected_type == InodeType.DIR:
            type_str = "directory inode"
        else:
            type_str = "file inode"

        mtime_str = _get_mtime_str(self.inode.mtime)
        return (
            f"invalid overlay file for materialized {type_str} "
            f"{self.inode.compute_path()}{mtime_str}: {self.inode.error}"
        )


class OrphanInode(Error):
    def __init__(self, inode: InodeInfo) -> None:
        super().__init__(ErrorLevel.WARNING)
        self.inode = inode

    def __str__(self) -> str:
        if self.inode.type == InodeType.DIR:
            type_str = "directory inode"
        elif self.inode.type == InodeType.FILE:
            type_str = "file inode"
        else:
            type_str = f"{self.inode.type} inode"
        mtime_str = _get_mtime_str(self.inode.mtime)
        return f"found orphan {type_str} {self.inode.inode_number}{mtime_str}"

    def detailed_description(self) -> Optional[str]:
        entries = []
        if self.inode.type == InodeType.DIR:
            for child in self.inode.children:
                mode_str = f"{child.mode:#o}"
                hash_str = binascii.hexlify(child.hash) if child.hash else "-"
                entries.append(
                    f"{child.name:<30} {mode_str} {child.inode_number} {hash_str}"
                )
        return "\n".join(entries)


class HardLinkedInode(Error):
    def __init__(self, inode: InodeInfo) -> None:
        super().__init__(ErrorLevel.WARNING)
        self.inode = inode


class FilesystemChecker:
    def __init__(self, overlay: overlay_mod.Overlay, verbose: bool = False) -> None:
        self.overlay = overlay
        self.errors: List[Error] = []
        self.verbose = verbose
        self._overlay_locked: Optional[bool] = None
        self._overlay_lock: Optional[ContextManager[bool]] = None

    def __enter__(self) -> "FilesystemChecker":
        self._overlay_lock = self.overlay.try_lock()
        self._overlay_locked = self._overlay_lock.__enter__()
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_value: Optional[BaseException],
        exc_traceback: Optional[types.TracebackType],
    ) -> Optional[bool]:
        assert self._overlay_lock is not None
        return self._overlay_lock.__exit__(exc_type, exc_value, exc_traceback)

    def _add_error(self, error: Error) -> None:
        print(f"{ErrorLevel.get_label(error.level)}: {error}")
        if self.verbose:
            details = error.detailed_description()
            if details:
                print("  " + "\n  ".join(details.splitlines()))
        self.errors.append(error)

    def scan_for_errors(self) -> None:
        print(f"Checking overlay storage at {self.overlay.path}.")
        print("This may take some time...")

        if not self._overlay_locked:
            print("Warning: unable to lock the overlay directory")
            print(
                "Will only scan for issues.  In order to fix problems you must "
                "first unmount the checkout."
            )

        print("Reading materialized inodes...")
        inodes = self._read_inodes()

        print(f"Found {len(inodes)} materialized inodes")
        print(f"Computing directory relationships...")
        self._link_inode_children(inodes)

        print(f"Scanning for inconsistencies...")
        self._scan_inodes_for_errors(inodes)

        # TODO: Check that the stored max inode number is valid

    def _read_inodes(self) -> Dict[int, InodeInfo]:
        inodes: Dict[int, InodeInfo] = {}

        for subdir_num in range(256):
            dir_name = "{:02x}".format(subdir_num)
            dir_path = os.path.join(self.overlay.path, dir_name)

            # TODO: Handle the error if os.listdir() fails
            for entry in os.listdir(dir_path):
                try:
                    inode_number = int(entry, 10)
                except ValueError as ex:
                    entry_path = os.path.join(dir_path, entry)
                    self._add_error(UnexpectedOverlayFile(entry_path))
                    continue

                # TODO: check if inode_number is actually in the correct subdirectory.
                # Handle the error if it is in the wrong directory, and if we found
                # multiple files with the same inode number in different subdirectories

                inode_info = self._load_inode_info(inode_number)
                inodes[inode_number] = inode_info

        return inodes

    def _link_inode_children(self, inodes: Dict[int, InodeInfo]) -> None:
        for inode in inodes.values():
            for child_info in inode.children:
                if child_info.inode_number == 0:
                    # Older versions of edenfs would leave the inode number set to 0
                    # if the child inode has never been loaded.  The child can't be
                    # present in the overlay if it doesn't have an inode number
                    # allocated for it yet.
                    #
                    # Newer versions of edenfs always allocate an inode number for all
                    # children, even if they haven't been loaded yet.
                    continue
                child_inode = inodes.get(child_info.inode_number, None)
                if child_inode is None:
                    if child_info.hash is None:
                        # This child is materialized (since it doesn't have a hash
                        # linking it to a source control object).  It's a problem if the
                        # materialized data isn't actually present in the overlay.
                        self._add_error(MissingMaterializedInode(inode, child_info))
                else:
                    child_inode.parents.append((inode, child_info))

    def _scan_inodes_for_errors(self, inodes: Dict[int, InodeInfo]) -> None:
        for inode in inodes.values():
            if inode.type in (InodeType.ERROR, InodeType.DIR_ERROR):
                self._add_error(InvalidMaterializedInode(inode))

            num_parents = len(inode.parents)
            if (
                num_parents == 0
                and inode.inode_number != overlay_mod.Overlay.ROOT_INODE_NUMBER
            ):
                self._add_error(OrphanInode(inode))
            elif num_parents > 1:
                self._add_error(HardLinkedInode(inode))

    def _load_inode_info(self, inode_number: int) -> InodeInfo:
        dir_data = None
        stat_info = None
        error = None
        try:
            with self.overlay.open_overlay_file(inode_number) as f:
                stat_info = os.fstat(f.fileno())
                header = self.overlay.read_header(f)
                if header.type == overlay_mod.OverlayHeader.TYPE_DIR:
                    dir_data = f.read()
                    type = InodeType.DIR
                elif header.type == overlay_mod.OverlayHeader.TYPE_FILE:
                    type = InodeType.FILE
                else:
                    type = InodeType.ERROR
        except Exception as ex:
            # If anything goes wrong trying to open or parse the overlay file
            # report this as an error, regardless of what type of error it is.
            type = InodeType.ERROR
            error = ex

        dir_entries = None
        children: List[ChildInfo] = []
        if dir_data is not None:
            try:
                parsed_data = self.overlay.parse_dir_inode_data(dir_data)
                dir_entries = parsed_data.entries
            except Exception as ex:
                type = InodeType.DIR_ERROR
                error = ex

        if dir_entries is not None:
            for name, entry in dir_entries.items():
                children.append(
                    ChildInfo(
                        inode_number=entry.inodeNumber or 0,
                        name=name,
                        mode=entry.mode,
                        hash=entry.hash,
                    )
                )

        mtime = None
        if stat_info is not None:
            mtime = stat_info.st_mtime
        return InodeInfo(inode_number, type, children, mtime, error)


def _get_mtime_str(mtime: Optional[float]) -> str:
    if mtime is None:
        return ""
    return f", with mtime {time.ctime(mtime)}"
