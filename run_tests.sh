
REG="file://$(PWD)"
RC="$(PWD)/test_bazelrc"

function test_repo() {
(
  BZL=$(pwd)
  # --registry=https://bcr.bazel.build 
  echo "$1"
  cd $1
  bazelisk "--bazelrc=$RC" test "--registry=${REG:-file:///Users/lar/github/tensorstore-registry}" --registry=https://bcr.bazel.build --verbose_failures //:all
)
}

for x in tests/*; do test_repo $x; done
