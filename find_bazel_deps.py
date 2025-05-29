"""Recursively collects all the bazel dependencies for a set of MODULE.bazel files."""

import argparse
import ast
import functools
import io
import os
import pathlib
import re
from typing import NamedTuple, Tuple
import urllib
import requests


_PATTERN = re.compile(
    r"^([a-zA-Z0-9.]+)(?:-([a-zA-Z0-9.-]+))?(?:\+[a-zA-Z0-9.-]+)?$"
)

IGNORE_DEV_DEPENDENCY = False


# Translated from:
# https://github.com/bazelbuild/bazel/src/main/java/com/google/devtools/build/lib/bazel/bzlmod/Version.java#L58
@functools.total_ordering
class Version:

  @functools.total_ordering
  class Identifier:

    def __init__(self, s: str):
      if not s:
        raise ValueError("identifier is empty")
      self.val: int | str = int(s) if s.isnumeric() else s

    def __eq__(self, other):
      if (isinstance(self.val, int) and isinstance(other.val, int)) or (
          isinstance(self.val, str) and isinstance(other.val, str)
      ):
        return self.val == other.val
      return False

    def __lt__(self, other):
      if (isinstance(self.val, int) and isinstance(other.val, int)) or (
          isinstance(self.val, str) and isinstance(other.val, str)
      ):
        return self.val < other.val
      return isinstance(self.val, str)

    def __hash__(self):
      return hash(self.val)

    def __str__(self):
      return str(self.val)

  @staticmethod
  def convert_to_identifiers(s: str) -> tuple[Identifier, ...] | None:
    if not s:
      return None
    return tuple([Version.Identifier(i) for i in s.split(".")])

  def __init__(self, version_str: str):
    self.release = tuple()
    self.prerelease = None
    if version_str:
      m = _PATTERN.match(version_str)
      if m:
        self.release = Version.convert_to_identifiers(m.groups()[0])
        self.prerelease = Version.convert_to_identifiers(m.groups()[1])

  def __eq__(self, other):
    return (self.release, self.prerelease) == (other.release, other.prerelease)

  def __hash__(self):
    return hash((self.release, self.prerelease))

  def __lt__(self, other):
    if self.release != other.release:
      return self.release < other.release
    if self.prerelease == None:
      return False
    if other.prerelease == None:
      return True
    return self.prerelease < other.prerelease

  def __str__(self):
    a = ".".join([str(x) for x in self.release])
    if not self.prerelease:
      return a
    b = ".".join([str(x) for x in self.prerelease])
    return f"{a}-{b}"


class BazelDep(NamedTuple):
  package_name: str
  version: Version


def _ast_node_to_value(node):
  if isinstance(node, ast.Constant):
    return node.value
  elif isinstance(node, ast.List):
    return [_ast_node_to_value(x) for x in node.elts]
  else:
    return None


def process_module_bzl(
    module_file: str, content: str
) -> Tuple[BazelDep, list[BazelDep]]:
  """Extracts info.args and cmake_data from a workspace.bzl for a package."""

  module = None
  bazel_deps = []

  tree = ast.parse(content, filename=module_file)
  for node in tree.body:  # Module.body
    if (
        not isinstance(node, ast.Expr)
        or not isinstance(node.value, ast.Call)
        or not isinstance(node.value.func, ast.Name)
    ):
      continue
    if node.value.func.id not in ("module", "bazel_dep"):
      continue
    n, v, dev_dependency = (None, None, False)  # Default to 0.0.0
    for x in node.value.keywords:
      if x.arg == "name":
        n = _ast_node_to_value(x.value)
      elif x.arg == "version":
        v = _ast_node_to_value(x.value)
      elif x.arg == "dev_dependency":
        dev_dependency = _ast_node_to_value(x.value)

    if not n:
      continue
    v = Version(v) if v else Version("")

    if node.value.func.id == "module":
      module = BazelDep(n, v)
    elif node.value.func.id == "bazel_dep" and n:
      if dev_dependency and IGNORE_DEV_DEPENDENCY:
        continue
      bazel_deps.append(BazelDep(n, v))

  if not module:
    print(ast.dump(tree))
  return module, bazel_deps


################################################################################


def _make_session() -> requests.Session:
  """Creates a requests session with retries."""
  s = requests.Session()
  retry = requests.packages.urllib3.util.retry.Retry(
      connect=10, read=10, backoff_factor=0.2
  )
  adapter = requests.adapters.HTTPAdapter(max_retries=retry)
  s.mount("http://", adapter)
  s.mount("https://", adapter)
  return s


class Registry:

  def __init__(self, *, registries):
    p = lambda x: x.removesuffix("/") if x.endswith("/") else x
    self.registries = [p(x) for x in registries]
    self.session = None
    self.bazel_deps: dict[BazelDep, set[BazelDep]] = {}
    self.module_path: dict[BazelDep, str] = {}
    self.not_found: set[BazelDep] = set()

  def _get_all_bazel_deps(self) -> set[BazelDep]:
    """Returns all modules in the registry."""
    all_modules: set[BazelDep] = set()
    for x in self.bazel_deps.values():
      all_modules.update(x)
    return all_modules

  def load_module_from_file(self, module_file: str) -> bool:
    """Visits a module file and adds its deps to the registry."""
    filename = pathlib.Path(module_file)
    try:
      content = filename.read_text(encoding="utf-8")
    except FileNotFoundError:
      return False

    module_file = filename.as_posix()
    module, bazel_deps = process_module_bzl(module_file, content)
    if not module:
      return False
    if module not in self.bazel_deps:
      self.bazel_deps[module] = set(bazel_deps)
      self.module_path[module] = "file://" + module_file
    else:
      self.bazel_deps[module] = self.bazel_deps[module] | set(bazel_deps)
    return True

  def load_module_from_network(self, url: str) -> bool:
    """Loads a module from the network and adds its deps to the registry."""
    try:
      if self.session is None:
        self.session = _make_session()
      response = self.session.get(url)
      if response.status_code != 200:
        return False
      content = response.content
    except Exception as e:
      print(e)
      return False

    module, bazel_deps = process_module_bzl(url, content)
    if not module:
      return False
    if module not in self.bazel_deps:
      self.bazel_deps[module] = set(bazel_deps)
      self.module_path[module] = url
    else:
      self.bazel_deps[module] = self.bazel_deps[module] | set(bazel_deps)
    return True

  def load_module(self, module: str) -> bool:
    parsed = urllib.parse.urlparse(module)
    if not parsed.scheme:
      return self.load_module_from_file(module)
    if parsed.scheme == "file":
      if parsed.path:
        return self.load_module_from_file(parsed.path)
      else:
        return False
    else:
      return self.load_module_from_network(module)

  def collect_missing_bazel_deps(self):
    """Collects all modules and adds their deps to the registry."""
    while True:
      to_visit: set[BazelDep] = self._get_all_bazel_deps()
      to_visit = to_visit - set(self.bazel_deps.keys())
      to_visit = to_visit - self.not_found
      if not to_visit:
        return
      for r in self.registries:
        for x in to_visit:
          # Construct a registry url from the package name and version
          self.load_module(
              f"{r}/modules/{x.package_name}/{x.version}/MODULE.bazel"
          )
        to_visit = to_visit - set(self.bazel_deps.keys())

      # Mark any not-found repositories as unavailable.
      self.not_found = self.not_found | to_visit

  def _build_report(
      self,
      report_latest: bool,
      forward_deps: dict[str, dict[Version, set[BazelDep]]],
      reverse_deps: dict[str, dict[Version, set[BazelDep]]],
      latest_version: dict[str, Version],
  ):
    """Builds the forward and reverse dependency graphs."""

    def _maybe_add_name(x, v):
      nonlocal forward_deps, reverse_deps, latest_version
      if x not in latest_version or latest_version[x] < v:
        latest_version[x] = v
      if x not in forward_deps:
        forward_deps[x] = {}
      if x not in reverse_deps:
        reverse_deps[x] = {}

    for x in sorted(self.bazel_deps):
      v = x.version
      _maybe_add_name(x.package_name, v)
      if v not in forward_deps[x.package_name]:
        forward_deps[x.package_name][v] = set()
      forward_deps[x.package_name][v] = (
          forward_deps[x.package_name][v] | self.bazel_deps[x]
      )
      for y in self.bazel_deps[x]:
        v = y.version
        _maybe_add_name(y.package_name, v)
        if v not in reverse_deps[y.package_name]:
          reverse_deps[y.package_name][v] = set()
        reverse_deps[y.package_name][v].add(x)

    # Maybe filter to latest version.
    if report_latest:
      for x in forward_deps:
        for v in set(forward_deps[x]):
          if v != latest_version[x]:
            del forward_deps[x][v]
      for x in reverse_deps:
        for v in set(reverse_deps[x]):
          if v != latest_version[x]:
            del reverse_deps[x][v]

      return forward_deps, reverse_deps, latest_version

  def report(self, report_latest: bool = False) -> str:

    forward_deps: dict[str, dict[Version, set[BazelDep]]] = {}
    reverse_deps: dict[str, dict[Version, set[BazelDep]]] = {}
    latest_version: dict[str, Version] = {}

    # Build forward and reverse deps
    self._build_report(
        report_latest, forward_deps, reverse_deps, latest_version
    )

    out = io.StringIO()
    out.write("Dependency graph:\n")
    for package_name in sorted(latest_version.keys()):
      out.write(f"{package_name}\n")
      versions: set[Version] = set()
      versions = versions | set(forward_deps[package_name].keys())
      versions = versions | set(reverse_deps[package_name].keys())

      for v in sorted(versions):
        path = self.module_path.get(BazelDep(package_name, v), "")
        if v == latest_version.get(package_name, None):
          out.write("* ")
        else:
          out.write("  ")
        out.write(f"{package_name}@{v}: {path}\n")
        if v in forward_deps[package_name] and forward_deps[package_name][v]:
          out.write("    forward:\n")
          for y in forward_deps[package_name][v]:
            out.write(f"      {y.package_name}@{y.version}\n")
        if v in reverse_deps[package_name] and reverse_deps[package_name][v]:
          out.write("    reverse:\n")
          for y in reverse_deps[package_name][v]:
            out.write(f"      {y.package_name}@{y.version}\n")
      out.write("\n")

    if not report_latest:
      out.write("\n\nAll module versions and their locations\n")
      for x in sorted(self.module_path.items()):
        out.write(f"{x[0].package_name}@{x[0].version} {x[1]}\n")

      out.write("\n\nMissing module versions\n")
      for x in self.not_found:
        if x.version is not None:
          out.write(f"{x.package_name}@{x.version}\n")
    return out.getvalue()


def main():
  parser = argparse.ArgumentParser(
      description="Recursively finds all bazel dependencies."
  )
  parser.add_argument(
      "modules", nargs="+", help="MODULE.bazel files to process."
  )
  parser.add_argument(
      "--registry",
      type=str,
      action="append",
      help="Bazel registry urls.",
      required=True,
  )
  parser.add_argument(
      "--ignore_dev_dependency",
      action="store_true",
      default=False,
      help="Ignore bazel_dep()s with dev_dependency=True.",
  )
  parser.add_argument(
      "--report-latest",
      action="store_true",
      default=False,
      help=(
          "Only show the latest version of each package; this should be similar"
          " to the bazel lockfile."
      ),
  )

  args = parser.parse_args()
  if args.ignore_dev_dependency:
    global IGNORE_DEV_DEPENDENCY
    IGNORE_DEV_DEPENDENCY = True

  registries = args.registry
  if not registries:
    registries.append("https://bcr.bazel.build")

  registry = Registry(registries=registries)

  for module in args.modules:
    if not registry.load_module(module):
      print(f"Failed to load module {module}")

  registry.collect_missing_bazel_deps()
  print(registry.report(args.report_latest))


if __name__ == "__main__":
  if "BUILD_WORKSPACE_DIRECTORY" in os.environ:
    os.chdir(os.environ["BUILD_WORKSPACE_DIRECTORY"])
  main()
