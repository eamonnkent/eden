/*
 *  Copyright (c) 2016-present, Facebook, Inc.
 *  All rights reserved.
 *
 *  This source code is licensed under the BSD-style license found in the
 *  LICENSE file in the root directory of this source tree. An additional grant
 *  of patent rights can be found in the PATENTS file in the same directory.
 *
 */
#include "LocalStore.h"

#include <folly/Format.h>
#include <folly/Optional.h>
#include <folly/String.h>
#include <folly/futures/Future.h>
#include <folly/io/Cursor.h>
#include <folly/io/IOBuf.h>
#include <folly/lang/Bits.h>
#include <folly/logging/xlog.h>
#include <array>

#include "eden/fs/model/Blob.h"
#include "eden/fs/model/Tree.h"
#include "eden/fs/model/git/GitBlob.h"
#include "eden/fs/model/git/GitTree.h"
#include "eden/fs/store/StoreResult.h"

using facebook::eden::Hash;
using folly::ByteRange;
using folly::IOBuf;
using folly::Optional;
using folly::StringPiece;
using folly::io::Cursor;
using std::string;
using std::unique_ptr;

namespace {
using namespace facebook::eden;
class SerializedBlobMetadata {
 public:
  explicit SerializedBlobMetadata(const BlobMetadata& metadata) {
    serialize(metadata.sha1, metadata.size);
  }
  SerializedBlobMetadata(const Hash& contentsHash, uint64_t blobSize) {
    serialize(contentsHash, blobSize);
  }

  ByteRange slice() const {
    return ByteRange{data_};
  }

  static BlobMetadata parse(Hash blobID, const StoreResult& result) {
    auto bytes = result.bytes();
    if (bytes.size() != SIZE) {
      throw std::invalid_argument(folly::sformat(
          "Blob metadata for {} had unexpected size {}. Could not deserialize.",
          blobID.toString(),
          bytes.size()));
    }

    uint64_t blobSizeBE;
    memcpy(&blobSizeBE, bytes.data(), sizeof(uint64_t));
    bytes.advance(sizeof(uint64_t));
    auto contentsHash = Hash{bytes};
    return BlobMetadata{contentsHash, folly::Endian::big(blobSizeBE)};
  }

 private:
  void serialize(const Hash& contentsHash, uint64_t blobSize) {
    uint64_t blobSizeBE = folly::Endian::big(blobSize);
    memcpy(data_.data(), &blobSizeBE, sizeof(uint64_t));
    memcpy(
        data_.data() + sizeof(uint64_t),
        contentsHash.getBytes().data(),
        Hash::RAW_SIZE);
  }

  static constexpr size_t SIZE = sizeof(uint64_t) + Hash::RAW_SIZE;

  /**
   * The serialized data is stored as stored as:
   * - size (8 bytes, big endian)
   * - hash (20 bytes)
   */
  std::array<uint8_t, SIZE> data_;
};

enum class Persistence : bool {
  Ephemeral = false,
  Persistent = true,
};

static constexpr struct KeySpaceRecord {
  LocalStore::KeySpace keySpace;
  Persistence persistence;
} kKeySpaceRecords[] = {
    {LocalStore::BlobFamily, Persistence::Ephemeral},
    {LocalStore::BlobMetaDataFamily, Persistence::Ephemeral},

    // If the trees were imported from a flatmanifest, we cannot delete them.
    // See test_contents_are_the_same_if_handle_is_held_open when running
    // against a flatmanifest repository.
    {LocalStore::TreeFamily, Persistence::Persistent},

    // Proxy hashes are required to fetch objects from hg from a hash.
    // Deleting them breaks re-importing after an inode is unloaded.
    {LocalStore::HgProxyHashFamily, Persistence::Persistent},

    {LocalStore::HgCommitToTreeFamily, Persistence::Ephemeral},
};
} // namespace

namespace facebook {
namespace eden {

void LocalStore::clearCachesAndCompactAll() {
  for (auto ks : kKeySpaceRecords) {
    if (ks.persistence == Persistence::Ephemeral) {
      clearKeySpace(ks.keySpace);
    }
    compactKeySpace(ks.keySpace);
  }
}

void LocalStore::clearCaches() {
  for (auto ks : kKeySpaceRecords) {
    if (ks.persistence == Persistence::Ephemeral) {
      clearKeySpace(ks.keySpace);
    }
  }
}

void LocalStore::compactStorage() {
  for (auto ks : kKeySpaceRecords) {
    compactKeySpace(ks.keySpace);
  }
}

StoreResult LocalStore::get(KeySpace keySpace, const Hash& id) const {
  return get(keySpace, id.getBytes());
}

// This is the fallback implementation for stores that don't have any
// internal support for asynchronous fetches.  This just performs the
// fetch and wraps it in a future
folly::Future<StoreResult> LocalStore::getFuture(
    KeySpace keySpace,
    folly::ByteRange key) const {
  return folly::makeFutureWith(
      [keySpace, key, this] { return get(keySpace, key); });
}

folly::Future<std::vector<StoreResult>> LocalStore::getBatch(
    KeySpace keySpace,
    const std::vector<folly::ByteRange>& keys) const {
  return folly::makeFutureWith([keySpace, keys, this] {
    std::vector<StoreResult> results;
    for (auto& key : keys) {
      results.emplace_back(get(keySpace, key));
    }
    return results;
  });
}

// TODO(mbolin): Currently, all objects in our RocksDB are Git objects. We
// probably want to namespace these by column family going forward, at which
// point we might want to have a GitLocalStore that delegates to an
// LocalStore so a vanilla LocalStore has no knowledge of deserializeGitTree()
// or deserializeGitBlob().

folly::Future<std::unique_ptr<Tree>> LocalStore::getTree(const Hash& id) const {
  return getFuture(KeySpace::TreeFamily, id.getBytes())
      .then([id](StoreResult&& data) {
        if (!data.isValid()) {
          return std::unique_ptr<Tree>(nullptr);
        }
        return deserializeGitTree(id, data.bytes());
      });
}

folly::Future<std::unique_ptr<Blob>> LocalStore::getBlob(const Hash& id) const {
  return getFuture(KeySpace::BlobFamily, id.getBytes())
      .then([id](StoreResult&& data) {
        if (!data.isValid()) {
          return std::unique_ptr<Blob>(nullptr);
        }
        auto buf = data.extractIOBuf();
        return deserializeGitBlob(id, &buf);
      });
}

folly::Future<Optional<BlobMetadata>> LocalStore::getBlobMetadata(
    const Hash& id) const {
  return getFuture(KeySpace::BlobMetaDataFamily, id.getBytes())
      .then([id](StoreResult&& data) -> Optional<BlobMetadata> {
        if (!data.isValid()) {
          return folly::none;
        } else {
          return SerializedBlobMetadata::parse(id, data);
        }
      });
}

std::pair<Hash, folly::IOBuf> LocalStore::serializeTree(const Tree* tree) {
  GitTreeSerializer serializer;
  for (auto& entry : tree->getTreeEntries()) {
    serializer.addEntry(std::move(entry));
  }
  IOBuf treeBuf = serializer.finalize();

  auto id = tree->getHash();
  if (id == Hash()) {
    id = Hash::sha1(&treeBuf);
  }
  return std::make_pair(id, treeBuf);
}

bool LocalStore::hasKey(KeySpace keySpace, const Hash& id) const {
  return hasKey(keySpace, id.getBytes());
}

Hash LocalStore::putTree(const Tree* tree) {
  auto serialized = LocalStore::serializeTree(tree);
  ByteRange treeData = serialized.second.coalesce();

  auto& id = serialized.first;
  put(KeySpace::TreeFamily, id, treeData);
  return id;
}

Hash LocalStore::WriteBatch::putTree(const Tree* tree) {
  auto serialized = LocalStore::serializeTree(tree);
  ByteRange treeData = serialized.second.coalesce();

  auto& id = serialized.first;
  put(KeySpace::TreeFamily, id.getBytes(), treeData);
  return id;
}

BlobMetadata LocalStore::putBlob(const Hash& id, const Blob* blob) {
  // Since blob serialization is moderately complex, just delegate
  // the immediate putBlob to the method on the WriteBatch.
  // Pre-allocate a buffer of approximately the right size; it
  // needs to hold the blob content plus have room for a couple of
  // hashes for the keys, plus some padding.
  auto batch = beginWrite(blob->getContents().computeChainDataLength() + 64);
  auto result = batch->putBlob(id, blob);
  batch->flush();
  return result;
}

void LocalStore::put(
    LocalStore::KeySpace keySpace,
    const Hash& id,
    folly::ByteRange value) {
  put(keySpace, id.getBytes(), value);
}

void LocalStore::WriteBatch::put(
    LocalStore::KeySpace keySpace,
    const Hash& id,
    folly::ByteRange value) {
  put(keySpace, id.getBytes(), value);
}

BlobMetadata LocalStore::WriteBatch::putBlob(const Hash& id, const Blob* blob) {
  const IOBuf& contents = blob->getContents();

  BlobMetadata metadata{Hash::sha1(&contents),
                        contents.computeChainDataLength()};

  SerializedBlobMetadata metadataBytes(metadata);

  auto hashSlice = id.getBytes();

  // Add a git-style blob prefix
  auto prefix = folly::to<string>("blob ", contents.computeChainDataLength());
  prefix.push_back('\0');
  std::vector<ByteRange> bodySlices;
  bodySlices.emplace_back(StringPiece(prefix));

  // Add all of the IOBuf chunks
  Cursor cursor(&contents);
  while (true) {
    auto bytes = cursor.peekBytes();
    if (bytes.empty()) {
      break;
    }
    bodySlices.push_back(bytes);
    cursor.skip(bytes.size());
  }

  put(LocalStore::KeySpace::BlobFamily, hashSlice, bodySlices);
  put(LocalStore::KeySpace::BlobMetaDataFamily,
      hashSlice,
      metadataBytes.slice());
  return metadata;
}

LocalStore::WriteBatch::~WriteBatch() {}
LocalStore::~LocalStore() {}

} // namespace eden
} // namespace facebook
