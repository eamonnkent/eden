/*
 *  Copyright (c) 2018-present, Facebook, Inc.
 *  All rights reserved.
 *
 *  This source code is licensed under the BSD-style license found in the
 *  LICENSE file in the root directory of this source tree. An additional grant
 *  of patent rights can be found in the PATENTS file in the same directory.
 *
 */
#include "eden/fs/inodes/InodeMetadata.h"

namespace facebook {
namespace eden {

void InodeMetadata::applyToStat(struct stat& st) const {
  st.st_mode = mode;
  st.st_uid = uid;
  st.st_gid = gid;
  timestamps.applyToStat(st);
}

} // namespace eden
} // namespace facebook