/*
 *  Copyright (c) 2016-present, Facebook, Inc.
 *  All rights reserved.
 *
 *  This source code is licensed under the BSD-style license found in the
 *  LICENSE file in the root directory of this source tree. An additional grant
 *  of patent rights can be found in the PATENTS file in the same directory.
 *
 */
#pragma once
#include "eden/fs/fuse/Dispatcher.h"
#include "eden/fs/inodes/InodePtr.h"

namespace facebook {
namespace eden {

class EdenMount;
class FileInode;
class InodeBase;
class InodeMap;
class TreeInode;

/**
 * A FUSE request dispatcher for eden mount points.
 */
class EdenDispatcher : public Dispatcher {
 public:
  /*
   * Create an EdenDispatcher.
   * setRootInode() must be called before using this dispatcher.
   */
  explicit EdenDispatcher(EdenMount* mount);

  folly::Future<Attr> getattr(InodeNumber ino) override;
  folly::Future<Attr> setattr(InodeNumber ino, const fuse_setattr_in& attr)
      override;
  folly::Future<std::shared_ptr<DirHandle>> opendir(InodeNumber ino, int flags)
      override;
  folly::Future<fuse_entry_out> lookup(
      InodeNumber parent,
      PathComponentPiece name) override;

  void forget(InodeNumber ino, unsigned long nlookup) override;
  folly::Future<std::shared_ptr<FileHandle>> open(InodeNumber ino, int flags)
      override;
  folly::Future<std::string> readlink(InodeNumber ino) override;
  folly::Future<fuse_entry_out> mknod(
      InodeNumber parent,
      PathComponentPiece name,
      mode_t mode,
      dev_t rdev) override;
  folly::Future<fuse_entry_out>
  mkdir(InodeNumber parent, PathComponentPiece name, mode_t mode) override;
  folly::Future<folly::Unit> unlink(InodeNumber parent, PathComponentPiece name)
      override;
  folly::Future<folly::Unit> rmdir(InodeNumber parent, PathComponentPiece name)
      override;
  folly::Future<fuse_entry_out> symlink(
      InodeNumber parent,
      PathComponentPiece name,
      folly::StringPiece link) override;
  folly::Future<folly::Unit> rename(
      InodeNumber parent,
      PathComponentPiece name,
      InodeNumber newparent,
      PathComponentPiece newname) override;

  folly::Future<fuse_entry_out> link(
      InodeNumber ino,
      InodeNumber newparent,
      PathComponentPiece newname) override;

  folly::Future<Dispatcher::Create> create(
      InodeNumber parent,
      PathComponentPiece name,
      mode_t mode,
      int flags) override;
  folly::Future<std::string> getxattr(InodeNumber ino, folly::StringPiece name)
      override;
  folly::Future<std::vector<std::string>> listxattr(InodeNumber ino) override;

 private:
  // The EdenMount that owns this EdenDispatcher.
  EdenMount* const mount_;
  // The EdenMount's InodeMap.
  // We store this pointer purely for convenience.  We need it on pretty much
  // every FUSE request, and having it locally avoids  having to dereference
  // mount_ first.
  InodeMap* const inodeMap_;
};
} // namespace eden
} // namespace facebook
