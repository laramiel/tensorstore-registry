
REG="file://$(PWD)"

for x in tests/*; do
(
  echo "$x"
  cd $x
  bazelisk test "--registry=$REG" --registry=https://bcr.bazel.build //:all
)
done
