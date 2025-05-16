
REG="file://$(PWD)"

function test_repo() {
(
  # --registry=https://bcr.bazel.build 
  echo "$1"
  cd $1
  bazelisk test "--registry=${REG:-file:///Users/lar/github/tensorstore-registry}" --registry=https://bcr.bazel.build  --explain=test.txt --verbose_failures //:all
)
}

for x in tests/*; do test_repo $x; done

# --registry=https://bcr.bazel.build 