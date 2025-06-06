"""The core class for freezing scripts into executables."""

from __future__ import annotations

import marshal
import os
import shutil
import stat
import struct
import sys
import sysconfig
import time
from abc import abstractmethod
from contextlib import suppress
from functools import cached_property
from importlib.util import MAGIC_NUMBER
from pathlib import Path
from pkgutil import resolve_name
from typing import TYPE_CHECKING, Any
from zipfile import ZIP_DEFLATED, ZIP_STORED, PyZipFile, ZipFile, ZipInfo

from setuptools import Distribution

from cx_Freeze._compat import (
    ABI_THREAD,
    BUILD_EXE_DIR,
    IS_CONDA,
    IS_MACOS,
    IS_MINGW,
    IS_WINDOWS,
    PYTHON_VERSION,
)
from cx_Freeze.common import process_path_specs, resource_path
from cx_Freeze.dep_parser import ELFParser, Parser, PEParser
from cx_Freeze.exception import FileError, OptionError
from cx_Freeze.executable import Executable
from cx_Freeze.finder import ModuleFinder
from cx_Freeze.module import ConstantsModule, DistributionCache, Module

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from cx_Freeze._typing import IncludesList, InternalIncludesList

if IS_WINDOWS or IS_MINGW:
    with suppress(ImportError):
        from .util import AddIcon, GetSystemDir, GetWindowsDir, UpdateCheckSum
elif IS_MACOS:
    from .darwintools import DarwinFile, DarwinFileTracker, MachOReference

__all__ = ["ConstantsModule", "Executable", "Freezer"]

WARNING_PIP_CX_FREEZE_IN_CONDA_PYTHON = """WARNING:

    It is not recommended to use cx_Freeze installed from pip with conda \
python.
    To install cx_Freeze as conda package (cx_freeze):
        pip uninstall cx_Freeze
        conda install conda-forge::cx_freeze

    To fix this issue, refer to the documentation:
        \
https://cx-freeze.readthedocs.io/en/stable/installation.html#conda-forge

    On macOS, you should get an error shortly.
"""

WARNING_USING_PYTHON_FROM_MS_STORE = """WARNING:

    Because of restrictions on Microsoft Store apps, Python scripts may not \
have full write access to built executable.
    You will need to install the full installer.
"""


class Freezer:
    """Freezer base class."""

    def __new__(
        cls, *args, **kwargs
    ) -> WinFreezer | DarwinFreezer | LinuxFreezer:
        # create instance of appropriate sub-class, depending on the platform.
        if IS_WINDOWS or IS_MINGW:
            return super().__new__(WinFreezer)
        if IS_MACOS:
            return super().__new__(DarwinFreezer)
        # assume any other platform would be handled by LinuxFreezer
        return super().__new__(LinuxFreezer)

    def __init__(
        self,
        executables: Sequence[Executable, Mapping[str, str], str],
        constants_module: ConstantsModule | None = None,
        includes: list[str] | None = None,
        excludes: list[str] | None = None,
        packages: list[str] | None = None,
        replace_paths: list[str] | None = None,
        compress: bool | None = True,
        optimize: int = 0,
        path: list[str | Path] | None = None,
        target_dir: str | Path | None = None,
        bin_includes: list[str] | None = None,
        bin_excludes: list[str] | None = None,
        bin_path_includes: list[str] | None = None,
        bin_path_excludes: list[str] | None = None,
        include_files: IncludesList | None = None,
        zip_includes: IncludesList | None = None,
        silent: bool | int = 0,
        metadata: Any = None,
        include_msvcr: bool = False,
        include_msvcr_version: str | None = None,
        zip_include_packages: Sequence[str] | None = None,
        zip_exclude_packages: Sequence[str] | None = None,
        zip_filename: Path | str | None = None,
    ) -> None:
        executables = self._validate_executables(executables)
        self.executables: list[Executable] = executables
        if constants_module is None:
            constants_module = ConstantsModule(
                metadata.version if metadata else None,
                executables[0].copyright,
            )
        self.constants_module: ConstantsModule = constants_module
        self.includes: list[str] = list(includes or [])
        self.excludes: list[str] = list(excludes or [])
        self.packages: set[str] = set(packages or [])
        self.replace_paths: list[str] = list(replace_paths or [])
        self.compress: bool = True if compress is None else compress
        self.optimize: int = int(optimize or 0)
        self.path: list[str] | None = self._validate_path(path)
        # include-msvcr is used on Windows, but not in MingW
        self.include_msvcr: bool = IS_WINDOWS and bool(include_msvcr)
        self.include_msvcr_version: str | None = include_msvcr_version
        self.target_dir = target_dir
        self.default_bin_includes: list[str] = self._default_bin_includes()
        self.default_bin_excludes: list[str] = self._default_bin_excludes()
        self.default_bin_path_includes: list[str] = (
            self._default_bin_path_includes()
        )
        self.default_bin_path_excludes: list[str] = (
            self._default_bin_path_excludes()
        )
        self.bin_includes: list[str] = self._validate_bin_file(bin_includes)
        self.bin_excludes: list[str] = self._validate_bin_file(bin_excludes)
        self.bin_path_includes: list[str] = self._validate_bin_path(
            bin_path_includes
        )
        self.bin_path_excludes: list[str] = self._validate_bin_path(
            bin_path_excludes
        )
        self.include_files: InternalIncludesList = process_path_specs(
            include_files
        )
        self.zip_includes: InternalIncludesList = process_path_specs(
            zip_includes
        )
        self.silent = int(silent or 0)
        self.metadata: Any = metadata

        self.zip_exclude_packages: set[str] = {"*"}
        self.zip_include_packages: set[str] = set()
        self.zip_include_all_packages: bool = False
        self._populate_zip_options(zip_include_packages, zip_exclude_packages)

        self.zip_filename: Path | None = None
        if zip_filename is None and self.compress:
            zip_filename = "library"
        if zip_filename:
            zip_filename = Path(zip_filename).with_suffix(".zip").name
            self.zip_filename = self.target_dir / "lib" / zip_filename

        self._symlinks: set[tuple[Path, Path, bool]] = set()
        self.files_copied: set[Path] = set()
        self.modules_copied: list[Module] = []
        self.finder: ModuleFinder = self._get_module_finder()
        self._check_installation()

    @property
    def target_dir(self) -> Path:
        """Directory for built executables and dependent files."""
        return self._targetdir

    @target_dir.setter
    def target_dir(self, path: str | Path | None) -> None:
        if path is None:
            path = BUILD_EXE_DIR
        path = Path(os.path.abspath(path)).resolve()
        if os.fspath(path) in self.path:
            msg = "the build_exe directory cannot be used as search path"
            raise OptionError(msg)
        if path.is_dir():
            # starts in a clean directory
            try:
                shutil.rmtree(path)
            except OSError:
                msg = "the build_exe directory cannot be cleaned"
                raise OptionError(msg) from None
        self._targetdir: Path = path

    def _add_resources(self, exe: Executable) -> None:
        """Add resources for an executable, platform dependent."""
        # Copy icon into application. (Overridden on Windows)
        if exe.icon is None:
            return
        if not exe.icon.exists():
            if self.silent < 3:
                print(f"WARNING: Icon file not found: {exe.icon}")
            return
        target_icon = self.target_dir / exe.icon.name
        self._copy_file(exe.icon, target_icon, copy_dependent_files=False)

    def _check_installation(self) -> None:
        if IS_CONDA:
            dist = DistributionCache(self.finder.cache_path, "cx_Freeze")
            if dist.installer == "pip":
                print(WARNING_PIP_CX_FREEZE_IN_CONDA_PYTHON, file=sys.stderr)

    def _copy_file(
        self,
        source: Path,
        target: Path,
        copy_dependent_files: bool,
        include_mode: bool = False,
    ) -> None:
        if not self._should_copy_file(source):
            return

        # handle pre-copy tasks, normally on the target path
        source, target = self._pre_copy_hook(source, target)

        if target in self.files_copied:
            return
        if source == target:
            return
        self._create_directory(target.parent)
        if self.silent < 1:
            print(f"copying {source} -> {target}")
        shutil.copyfile(source, target)
        if include_mode:
            shutil.copymode(source, target)
            shutil.copystat(source, target)
        else:
            try:
                shutil.copystat(source, target)
            except OSError:
                if self.silent < 3:
                    print("WARNING: unable to copy file metadata:", target)
        self.files_copied.add(target)

        # handle post-copy tasks, including copying dependencies
        self._post_copy_hook(source, target, copy_dependent_files)

    def _copy_package_data(self, module: Module, target_dir: Path) -> None:
        """Copy any non-Python files to the target directory."""
        ignore_patterns = ("__pycache__", "*.py", "*.pyc", "*.pyo")

        def copy_tree(
            source_dir: Path, target_dir: Path, excludes: set[str]
        ) -> None:
            self._create_directory(target_dir)
            for source in source_dir.iterdir():
                if any(filter(source.match, ignore_patterns)):
                    continue
                source_name = source.name
                if source_name in excludes:
                    continue
                target = target_dir / source_name
                if source.is_dir():
                    source_subdir = source_dir / source_name
                    excludes_subdir = {
                        m.split(f"{source_name}.", 1)[1]
                        for m in excludes
                        if m.startswith(f"{source_name}.")
                    }
                    copy_tree(source_subdir, target, excludes_subdir)
                else:
                    self._copy_file(source, target, copy_dependent_files=True)

        source_dir = module.file.parent
        module_name = module.name
        if self.silent < 1:
            print(f"copying data from package {module_name}...")
        # do not copy the subfolders which belong to excluded modules
        excludes = {
            m.split(f"{module_name}.", 1)[1]
            for m in self.finder.excludes
            if m.startswith(f"{module_name}.")
        }
        copy_tree(source_dir, target_dir, excludes)

    def _pre_copy_hook(self, source: Path, target: Path) -> tuple[Path, Path]:
        """Prepare the source and target paths. In addition, it ensures that
        the source of a symbolic link is copied by deferring the creation of
        the link.
        """
        real_source = source.resolve()
        if source.is_symlink():
            symlink = source.readlink()
            real_target = target.with_name(symlink.name)
            if self.silent < 1:
                print(f"[delay] linking {target} -> {symlink}")
            self._symlinks.add((target, symlink, real_source.is_dir()))
            # return the real source to be copied
            return real_source, real_target
        return real_source, target

    @abstractmethod
    def _post_copy_hook(
        self,
        source: Path,
        target: Path,
        copy_dependent_files: bool,
    ) -> None:
        """Post-copy task."""

    def _create_directory(self, path: str | Path) -> None:
        path = Path(path)
        if not path.is_dir():
            if self.silent < 1:
                print(f"creating directory {path}")
            path.mkdir(parents=True, exist_ok=True)

    def _freeze_executable(self, exe: Executable) -> None:
        finder: ModuleFinder = self.finder
        finder.include_file_as_module(exe.main_script, exe.main_module_name)
        finder.include_file_as_module(exe.init_script, exe.init_module_name)
        finder.include_file_as_module(
            resource_path("initscripts/__startup__.py")
        )

        # copy the executable and its dependencies
        target_path = self.target_dir / exe.target_name
        self._get_top_dependencies(exe.base)
        self._copy_file(
            exe.base, target_path, copy_dependent_files=True, include_mode=True
        )
        if not os.access(target_path, os.W_OK):
            mode = target_path.stat().st_mode
            target_path.chmod(mode | stat.S_IWUSR)

        # copy a file with a the cx_freeze license into frozen application
        respath = resource_path("initscripts/frozen_application_license.txt")
        if respath is None:
            msg = "Unable to find license for frozen application."
            raise FileError(msg)
        self._copy_file(
            respath.absolute(),
            self.target_dir / "frozen_application_license.txt",
            copy_dependent_files=False,
            include_mode=False,
        )

        # Add resources like version metadata and icon
        self._add_resources(exe)

    @abstractmethod
    def _get_top_dependencies(self, source: Path, target: Path) -> None:
        """Called to get the dependencies of an executable."""

    @abstractmethod
    def _default_bin_excludes(self) -> list[str]:
        """Return the file names of libraries that need not be included because
        they would normally be expected to be found on the target system or
        because they are part of a package which requires independent
        installation anyway.
        """

    def _default_bin_includes(self) -> list[str]:
        """Return the file names of libraries which must be included for the
        frozen executable to work.
        (Overridden on Windows and macOS).
        """
        python_shared_libs: list[Path] = []
        name = sysconfig.get_config_var("INSTSONAME")
        if name:
            if name.endswith(".a"):
                # conda-forge returns a static library
                name = name.replace(".a", ".so")
            name = self._remove_version_numbers(name)
            for bin_path in self._default_bin_path_includes():
                fullname = Path(bin_path, name).resolve()
                if fullname.is_file():
                    python_shared_libs.append(fullname)
                    break
        return self._validate_bin_file(python_shared_libs)

    @abstractmethod
    def _default_bin_path_excludes(self) -> list[str]:
        """Return the paths of directories which contain files that should not
        be included, generally because they contain standard system
        libraries.
        """

    def _default_bin_path_includes(self) -> list[str]:
        """Return the paths of directories which contain files that should
        be included.
        (Overridden on Windows, inherited in macOS).
        """
        bin_path = [
            sysconfig.get_config_var("LIBDIR"),
            sysconfig.get_config_var("DESTLIB"),
            Path(sys.base_prefix, sys.platlibdir),  # uv python
        ]
        return self._validate_bin_path(bin_path)

    def _get_module_finder(self) -> ModuleFinder:
        finder = ModuleFinder(
            self.constants_module,
            self.excludes,
            self.include_files,
            self.path,
            self.replace_paths,
            self.zip_exclude_packages,
            self.zip_include_packages,
            self.zip_include_all_packages,
            self.zip_includes,
        )
        finder.optimize = self.optimize
        for name in self.includes:
            finder.include_module(name)
        for name in self.packages:
            finder.include_package(name)
        finder.add_base_modules()
        return finder

    def _post_freeze_hook(self) -> None:
        """Post-Freeze work (can be overridden)."""
        for target, symlink, symlink_is_directory in self._symlinks:
            if self.silent < 1:
                print(f"linking {target} -> {symlink}")
            if not target.exists():
                target.symlink_to(symlink, symlink_is_directory)

    @staticmethod
    def _remove_version_numbers(filename: str) -> str:
        tweaked = False
        parts = filename.split(".")
        while parts:
            if not parts[-1].isdigit():
                break
            parts.pop(-1)
            tweaked = True
        if tweaked:
            return ".".join(parts)
        return filename

    def _should_copy_file(self, path: Path) -> bool:  # noqa: PLR0911
        """Return true if the file should be copied to the target machine.

        This is done by checking the bin_includes and bin_excludes
        configuration variables using first the full file name, then just the
        base file name, then the file name without any version numbers.
        Then, bin_path_includes and bin_path_excludes are checked.
        Finally, check the default variables.

        Files are included unless specifically excluded but inclusions take
        precedence over exclusions.
        """
        dirname = path.parent
        filename = Path(os.path.normcase(path.name))
        filename_noversion = Path(self._remove_version_numbers(filename.name))
        # check the full path
        # check the file name by itself (with any included version numbers)
        # check the file name by itself (version numbers removed)
        for binfile in self.bin_includes:
            if (
                path.match(binfile)
                or filename.match(binfile)
                or filename_noversion.match(binfile)
            ):
                return True
        for binfile in self.bin_excludes:
            if (
                path.match(binfile)
                or filename.match(binfile)
                or filename_noversion.match(binfile)
            ):
                return False

        # check the path for inclusion/exclusion
        for binpath in self.bin_path_includes:
            if dirname.is_relative_to(binpath):
                return True
        for binpath in self.bin_path_excludes:
            if dirname.is_relative_to(binpath):
                return False

        # check the default variables
        for binfile in self.default_bin_includes:
            if path.match(binfile):
                return True
        for binfile in self.default_bin_excludes:
            if filename_noversion.match(binfile):
                return False
        for binpath in self.default_bin_path_includes:
            if dirname.is_relative_to(binpath):
                return True
        for binpath in self.default_bin_path_excludes:
            if dirname.is_relative_to(binpath):
                return False

        return True

    def _validate_executables(
        self, executables: Sequence[Executable, Mapping[str, str], str]
    ) -> list[Executable]:
        """Returns valid Executable list."""
        dist = Distribution(attrs={"executables": executables})
        return dist.executables

    @staticmethod
    def _validate_path(path: list[str | Path] | None = None) -> list[str]:
        """Returns valid search path for modules, and fix the path for built-in
        modules when it differs from the running python built-in modules.
        """
        path = list(map(os.path.normpath, path or sys.path))
        dynload = resource_path("bases/lib-dynload")
        if dynload and dynload.is_dir():
            # add bases/lib-dynload to the finder path, if has modules
            ext_suffix = sysconfig.get_config_var("EXT_SUFFIX")
            if len(list(dynload.glob(f"*{ext_suffix}"))) > 0:
                index = 0
                dest_shared = sysconfig.get_config_var("DESTSHARED")
                if dest_shared:
                    with suppress(ValueError, IndexError):
                        index = path.index(dest_shared)
                        path.pop(index)
                path.insert(index, os.path.normpath(dynload))
        return path

    @staticmethod
    def _validate_bin_file(
        filenames: Sequence[str | Path] | None,
    ) -> list[str]:
        """Returns valid filenames for bin_includes and bin_excludes."""
        if filenames is None:
            return []
        return [os.path.normcase(filename) for filename in filenames]

    @staticmethod
    def _validate_bin_path(bin_path: Sequence[str | Path] | None) -> list[str]:
        """Returns valid search path for bin_path_includes and
        bin_path_excludes.
        """
        if bin_path is None:
            return []
        return [
            os.fspath(path.resolve())
            for path in map(Path, bin_path)
            if path.is_dir()
        ]

    def _populate_zip_options(
        self,
        zip_include_packages: Sequence[str] | None,
        zip_exclude_packages: Sequence[str] | None,
    ) -> None:
        """Verify, normalize and populate zip_*_packages options.
        Raises OptionError on failure.
        """
        if zip_include_packages is None and zip_exclude_packages is None:
            zip_include_packages = []
            zip_exclude_packages = ["*"]
        else:
            zip_include_packages = list(zip_include_packages or [])
            zip_exclude_packages = list(zip_exclude_packages or [])
        zip_include_all_packages = "*" in zip_include_packages
        zip_exclude_all_packages = "*" in zip_exclude_packages
        # check the '*' option
        if zip_exclude_all_packages and zip_include_all_packages:
            msg = (
                "all packages cannot be included and excluded "
                "from the zip file at the same time"
            )
            raise OptionError(msg)
        # normalize namespace packages - syntax suggar
        zip_include_packages = {
            name.partition(".")[0] for name in zip_include_packages
        }
        zip_exclude_packages = {
            name.partition(".")[0] for name in zip_exclude_packages
        }
        # check invalid usage
        invalid = sorted(zip_include_packages & zip_exclude_packages)
        if invalid:
            msg = (
                f"package{'s' if len(invalid) > 1 else ''} "
                f"{', '.join(invalid)!r} "
                "cannot be both included and excluded from zip file"
            )
            raise OptionError(msg)
        # populate
        self.zip_include_packages = zip_include_packages
        self.zip_exclude_packages = zip_exclude_packages
        self.zip_include_all_packages = zip_include_all_packages

    def _write_modules(self) -> None:
        filename: Path = self.target_dir / "lib" / "library.zip"
        finder: ModuleFinder = self.finder
        cache_path = finder.cache_path

        modules: list[Module] = [
            m for m in finder.modules if m.name not in finder.excludes
        ]
        for module in finder.namespaces:
            # if namespace package should be written to zip file, convert it
            # to regular package, since then zipimport doesn't support PEP420
            if module.in_file_system == 0:
                module.code = compile(
                    "", "__init__.py", "exec", dont_inherit=True
                )
                modules.append(module)
        modules.append(
            finder.include_file_as_module(
                self.constants_module.create(cache_path, modules)
            )
        )
        modules.sort(key=lambda m: m.name)

        target_lib_dir = filename.parent
        self._create_directory(target_lib_dir)

        # Prepare zip file
        compress_type = ZIP_DEFLATED if self.compress else ZIP_STORED
        with PyZipFile(filename, "w", compress_type) as outfile:
            files_to_copy: list[tuple[Module, Path]] = []

            for module in modules:
                # determine if the module should be written to the file system;
                # a number of packages make the assumption that files that they
                # require will be found in a location relative to where they
                # are located on disk; these packages will fail with strange
                # errors when they are written to a zip file instead
                include_in_file_system = module.in_file_system
                mod_name = module.name
                mod_name_parts = mod_name.split(".")

                # if the module refers to a package, check to see if this
                # package should be written to the file system
                if (
                    include_in_file_system >= 1
                    and module.path is not None
                    and module.file is not None
                ):
                    parts = mod_name_parts
                    target_package_dir = target_lib_dir.joinpath(*parts)
                    if include_in_file_system == 2:
                        # a few packages are optimized on the hooks,
                        # so for now create the directory for this package
                        self._create_directory(target_package_dir)

                    elif not target_package_dir.exists():
                        # whether the package and its data will be written to
                        # the file system, any non-Python files are copied at
                        # this point if the target directory does not already
                        # exist
                        self._copy_package_data(module, target_package_dir)

                # if an extension module is found in a package that is to be
                # included in a zip file, copy the actual file to the build
                # directory because shared libraries cannot be loaded from a
                # zip file
                if (
                    module.code is None
                    and module.file is not None
                    and include_in_file_system == 0
                ):
                    parts = mod_name_parts.copy()
                    if module.file.name.startswith("__init__."):
                        # if a module init is distributed as compiled like
                        # __init__.pyd, it should be copied with a new name
                        # ending with the name of the parent module, and it
                        # should be imported in the fake code.
                        last = parts[-1]
                        source = f"from {module.name}.{last} import *"
                        module.code = compile(
                            source, "__init__.py", "exec", dont_inherit=True
                        )
                        parts.append(
                            module.file.name.replace("__init__.", f"{last}.")
                        )
                    else:
                        parts.pop()
                        parts.append(module.file.name)
                    target = target_lib_dir / ".".join(parts)
                    files_to_copy.append((module, target))

                # starting with Python 3.3 the pyc file format contains the
                # source size; it is not actually used for anything except
                # determining if the file is up to date so we can safely set
                # this value to zero
                if module.code is not None:
                    if module.file is not None and module.file.exists():
                        file_stat = module.file.stat()
                        mtime = int(file_stat.st_mtime) & 0xFFFF_FFFF
                        size = file_stat.st_size & 0xFFFFFFFF
                    else:
                        mtime = int(time.time()) & 0xFFFF_FFFF
                        size = 0
                    header = MAGIC_NUMBER + struct.pack("<iLL", 0, mtime, size)
                    data = header + marshal.dumps(module.code)

                # if the module should be written to the file system, do so
                if include_in_file_system >= 1 and module.file is not None:
                    parts = mod_name_parts.copy()
                    if module.code is None:
                        # if a module init is distributed as compiled like
                        # __init__.pyd, its name should be preserved.
                        if not module.file.name.startswith("__init__."):
                            parts.pop()
                        parts.append(module.file.name)
                        target_name = target_lib_dir.joinpath(*parts)
                        self._copy_file(
                            module.file, target_name, copy_dependent_files=True
                        )
                    else:
                        if module.path is not None:
                            parts.append("__init__")
                        target_name = target_lib_dir.joinpath(*parts)
                        target_name = target_name.with_suffix(".pyc")
                        self._create_directory(target_name.parent)
                        target_name.write_bytes(data)

                # otherwise, write to the zip file
                elif module.code is not None:
                    zip_time = time.localtime(mtime)[:6]
                    target_name = "/".join(mod_name_parts)
                    if module.path:
                        target_name += "/__init__"
                    zinfo = ZipInfo(target_name + ".pyc", zip_time)
                    zinfo.compress_type = compress_type
                    outfile.writestr(zinfo, data)

            # put the distribution files metadata in the zip file
            pos = len(cache_path.as_posix()) + 1
            for name in cache_path.rglob("*.dist-info/*"):
                if name.is_dir():
                    continue
                outfile.write(name, name.as_posix()[pos:])

            # write any files to the zip file that were requested specially
            for source_path, target_path in finder.zip_includes:
                if source_path.is_dir():
                    for source_filename in source_path.rglob("*"):
                        if source_filename.is_dir():
                            continue
                        target = target_path.joinpath(
                            source_filename.relative_to(source_path)
                        )
                        outfile.write(source_filename, target.as_posix())
                else:
                    outfile.write(source_path, target_path.as_posix())

        # Copy Python extension modules from the list built above.
        orig_path = os.environ["PATH"]
        for module, target in files_to_copy:
            try:
                if module.parent is not None:
                    path = os.pathsep.join(
                        [orig_path, *list(map(os.fspath, module.parent.path))]
                    )
                    os.environ["PATH"] = path
                self._copy_file(module.file, target, copy_dependent_files=True)
            finally:
                os.environ["PATH"] = orig_path

        # put all files in file system or keep in zip file
        if self.zip_filename is None:
            with ZipFile(filename) as outfile:
                outfile.extractall(target_lib_dir)
            filename.unlink()
        else:
            if self.zip_filename.name != filename.name:
                # zip_filename differs from default
                filename.rename(self.zip_filename)
            library_data = self.target_dir / "lib" / "library.dat"
            library_data.write_bytes(self.zip_filename.name.encode())

        # to report
        self.modules_copied = modules

    def freeze(self) -> None:
        """Do the freeze."""
        finder: ModuleFinder = self.finder

        # Add the executables to target
        executables = []
        for executable in self.executables:
            self._freeze_executable(executable)
            executables.append(executable.target_name)
        finder.add_constant("__EXECUTABLES__", os.pathsep.join(executables))

        # Write the modules
        self._write_modules()

        # Include user-defined files and hooks-defined files
        target_dir = self.target_dir
        excluded_dependent_files = finder.excluded_dependent_files
        for source_path, target_path in finder.included_files:
            copy_dependent_files = source_path not in excluded_dependent_files
            if source_path.is_dir():
                # Copy directories by recursing into them.
                # Can't use shutil.copytree because we may need dependencies
                target_base = target_dir / target_path
                for name in source_path.rglob("*"):
                    if name.is_dir():
                        continue
                    if any(
                        parent
                        for parent in name.parents
                        if parent.name in (".git", ".svn", "CVS")
                    ):
                        continue
                    fulltarget = target_base / name.relative_to(source_path)
                    self._create_directory(fulltarget.parent)
                    self._copy_file(name, fulltarget, copy_dependent_files)
            else:
                # Copy regular files.
                fulltarget = target_dir / target_path
                self._copy_file(source_path, fulltarget, copy_dependent_files)

        # do any platform-specific post-Freeze work
        self._post_freeze_hook()
        self.finder.cleanup()

    def print_report(self) -> None:
        """Display report:
        - list of modules and packages;
        - list of modules  that weren't found;
        - list of dependencies that weren't found.
        """
        if self.silent < 1:
            if self.zip_filename:
                print(f"writing zip file {self.zip_filename}\n")
            print(f"  {'Name':<25} File")
            print(f"  {'----':<25} ----")
            for module in self.modules_copied:
                if module.path:
                    print("P", end="")
                else:
                    print("m", end="")
                print(f" {module.name:<25} {module.file or ''}")
        if self.silent < 2:
            self.finder.report_missing_modules()
        if self.silent < 3:
            # Display a list of dependencies that weren't found
            names = {
                name for name, value in self.linker_warnings.items() if value
            }
            copied = {file.name for file in self.files_copied}
            missing = names.difference(copied)
            if missing:
                print("Missing dependencies:")
                for name in sorted(missing):
                    print(f"? {name}")
                print("This is not necessarily a problem - the ", end="")
                print("dependencies may not be needed on this platform.\n")


class WinFreezer(Freezer, PEParser):
    """Freezer base class for Windows OS."""

    def __init__(self, *args, **kwargs) -> None:
        Freezer.__init__(self, *args, **kwargs)
        PEParser.__init__(
            self,
            self.path,
            self.default_bin_path_includes + self.bin_path_includes,
            self.silent,
        )

    def _add_resources(self, exe: Executable) -> None:
        target_path: Path = self.target_dir / exe.target_name

        # Add version resource
        if self.metadata is not None:
            warning_msg = "WARNING: unable to create version resource:"
            if not self.metadata.version:
                if self.silent < 3:
                    print(warning_msg, "version must be specified")
            else:
                version = resolve_name("cx_Freeze.winversioninfo:VersionInfo")(
                    self.metadata.version,
                    comments=self.metadata.long_description,
                    description=self.metadata.description,
                    company=self.metadata.author,
                    product=self.metadata.name,
                    copyright=exe.copyright,
                    trademarks=exe.trademarks,
                    verbose=bool(self.silent < 1),
                )
                try:
                    version.stamp(target_path)
                except (FileNotFoundError, RuntimeError) as exc:
                    if self.silent < 3:
                        print(warning_msg, exc)

        # Add icon
        if exe.icon is not None:
            if not exe.icon.exists():
                if self.silent < 3:
                    print(f"WARNING: Icon file not found: {exe.icon}")
            else:
                try:
                    AddIcon(target_path, exe.icon)
                except MemoryError:
                    if self.silent < 3:
                        print("WARNING: MemoryError")
                except RuntimeError as exc:
                    if self.silent < 3:
                        print("WARNING:", exc)
                except OSError as exc:
                    if "\\WindowsApps\\" in sys.base_prefix:
                        if self.silent < 3:
                            print("WARNING:", exc)
                            print(WARNING_USING_PYTHON_FROM_MS_STORE)
                    else:
                        raise

        # Change the manifest
        manifest: str | None = exe.manifest
        if manifest is not None or exe.uac_admin or exe.uac_uiaccess:
            if self.silent < 1:
                print(f"writing manifest -> {target_path}")
            try:
                if exe.uac_admin:
                    if self.silent < 1:
                        print("manifest requires elevation")
                    manifest = manifest or self.read_manifest(target_path)
                    manifest = manifest.replace(
                        "asInvoker", "requireAdministrator"
                    )
                if exe.uac_uiaccess:
                    if self.silent < 1:
                        print("manifest allow ui access")
                    manifest = manifest or self.read_manifest(target_path)
                    manifest = manifest.replace(
                        'uiAccess="false"', 'uiAccess="true"'
                    )
                    manifest = manifest.replace(
                        "uiAccess='false'", "uiAccess='true'"
                    )
                self.write_manifest(target_path, manifest)
            except FileNotFoundError as exc:
                if self.silent < 3:
                    print("WARNING:", exc)
            except RuntimeError as exc:
                if self.silent < 3:
                    print(f"WARNING: error parsing {target_path}:", exc)

        # Update the PE checksum (or fix it in case it is zero)
        try:
            UpdateCheckSum(target_path)
        except MemoryError:
            if self.silent < 3:
                print("WARNING: MemoryError")
        except (RuntimeError, OSError) as exc:
            if self.silent < 3:
                print("WARNING:", exc)

    def _post_copy_hook(
        self,
        source: Path,
        target: Path,
        copy_dependent_files: bool,
    ) -> None:
        if not (
            copy_dependent_files
            and source not in self.finder.excluded_dependent_files
        ):
            return
        library_dir = self.target_dir / "lib"
        source_dir = source.parent
        target_dir = target.parent
        lib_files = self.finder.lib_files
        platform_bin_path = self._platform_bin_path
        for dependent in self.get_dependent_files(source):
            if not self._should_copy_file(dependent):
                continue
            dependent_source = dependent.resolve()
            dependent_name = dependent_source.name
            lib_file = lib_files.get(dependent_source)
            if lib_file:
                dependent_target = self.target_dir / lib_file
            else:
                # put the dependency (relatively) in the target_dir subtree
                # this is possible with most packages installed by pip
                try:
                    relative = dependent_source.relative_to(source_dir)
                    # dependency located with source or in a subdirectory
                    dependent_target = target_dir / relative
                except ValueError:
                    # check if dependency is on default binaries path
                    dependent_srcdir = dependent_source.parent
                    if dependent_srcdir in platform_bin_path:
                        dependent_target = library_dir / dependent_name
                    else:
                        # check if dependency is located in a upper level
                        try:
                            relative = source_dir.relative_to(dependent_srcdir)
                            # fix the target_dir - go to the previous level
                            parts = target_dir.parts[: -len(relative.parts)]
                            dependent_target = Path(*parts) / dependent_name
                        except ValueError:
                            dependent_target = target_dir / dependent_name
                # make sure the dependency is in the correct directory because
                # it cannot be outside the library_dir directory subtree
                if not dependent_target.is_relative_to(library_dir):
                    dependent_target = library_dir / dependent_name
                if 0:  # else:
                    _, dependent_target = self._pre_copy_hook(
                        dependent_source, dependent_target
                    )
                    if dependent_target not in self.files_copied:
                        for file in self.files_copied:
                            if file.match(dependent_name):
                                dependent_target = file
                                break
            self._copy_file(
                dependent_source, dependent_target, copy_dependent_files
            )

    def _post_freeze_hook(self) -> None:
        if self.include_msvcr:
            from cx_Freeze.winmsvcr_repack import get_msvcr_files

            # remove MSVC runtime from default excludes
            excludes = set(self.default_bin_excludes)
            runtime = self._runtime_files()
            self.default_bin_excludes = list(excludes.difference(runtime))

            # copy the MSVC runtime files
            target_dir = self.target_dir
            license_dir = target_dir / "share/licenses/vc_redist"
            for source in get_msvcr_files(self.include_msvcr_version):
                if source.stem == "LICENSE":
                    target = license_dir / source.name
                else:
                    target = target_dir / source.name
                self._copy_file(source, target, copy_dependent_files=False)

    def _runtime_files(self) -> set[str]:
        """Deal with C-runtime files."""
        from cx_Freeze.winmsvcr import MSVC_FILES, UCRT_FILES

        return [*MSVC_FILES, *UCRT_FILES]

    def _default_bin_excludes(self) -> list[str]:
        # MSVC runtime files are also on the exclusion list, but can still be
        # added on demand in _post_freeze_hook
        return ["comctl32.dll", "oci.dll", *self._runtime_files()]

    def _default_bin_includes(self) -> list[str]:
        name = sysconfig.get_config_var("INSTSONAME")
        if name:
            # MSYS2 python returns a static library.
            names = [name.replace(".dll.a", ".dll")]
        else:
            py_version = f"{PYTHON_VERSION}{ABI_THREAD}"
            names = [
                f"python{sys.version_info[0]}.dll",
                f"python{py_version.replace('.', '')}.dll",
            ]
        python_shared_libs: list[Path] = []
        for name in names:
            for bin_path in self._default_bin_path_includes():
                fullname = Path(bin_path, name).resolve()
                if fullname.is_file():
                    python_shared_libs.append(fullname)
                    break
        return self._validate_bin_file(python_shared_libs)

    def _default_bin_path_excludes(self) -> list[str]:
        system_dir = GetSystemDir()
        windows_dir = GetWindowsDir()
        return [windows_dir, system_dir, os.path.join(windows_dir, "WinSxS")]

    def _default_bin_path_includes(self) -> list[str]:
        return self._validate_bin_path(sys.path + self._platform_bin_path)

    def _get_top_dependencies(self, source: Path) -> None:
        # executable dependencies go into build root directory on windows
        # msys2 libpython depends on libgcc_s_seh and libwinpthread dlls
        # conda-forge python3x.dll depends on zlib.dll
        lib_files = self.finder.lib_files
        for filename in self.get_dependent_files(source):
            path = filename.resolve()
            if path not in lib_files and self._should_copy_file(path):
                lib_files.setdefault(path, path.name)
                self._get_top_dependencies(path)
        if not IS_MINGW:
            # abi3 packages requires python3.dll (windows/conda windows)
            for path in map(Path, self.default_bin_includes):
                lib_files.setdefault(path, path.name)

    @cached_property
    def _platform_bin_path(self) -> list[Path]:
        # try to find the paths (windows, conda-forge, msys2/mingw)
        paths = set()
        dest_shared = sysconfig.get_config_var("DESTSHARED")  # msys2
        dest_relative = None
        if dest_shared:
            dest_shared = Path(dest_shared)
            paths.add(dest_shared)
            with suppress(ValueError):
                dest_relative = dest_shared.relative_to(sys.prefix)
        prefixes = [
            sys.base_exec_prefix,
            sys.base_prefix,
            sys.exec_prefix,
            sys.prefix,
        ]
        for prefix in map(Path, prefixes):
            paths.add(prefix / "bin")
            paths.add(prefix / "DLLs")
            paths.add(prefix / "Library/bin")
            if dest_relative:
                paths.add(prefix / dest_relative)
        # return only valid paths
        return [path.resolve() for path in paths if path.is_dir()]


class DarwinFreezer(Freezer, Parser):
    """Freezer base class for macOS."""

    def __init__(self, *args, **kwargs) -> None:
        Freezer.__init__(self, *args, **kwargs)
        Parser.__init__(
            self,
            self.path,
            self.default_bin_path_includes + self.bin_path_includes,
            self.silent,
        )
        self.darwin_tracker: DarwinFileTracker = DarwinFileTracker()

    def _default_bin_excludes(self) -> list[str]:
        return []

    def _default_bin_includes(self) -> list[str]:
        python_shared_libs: list[Path] = []
        # Check for distributed "cx_Freeze/bases/lib/Python"
        name = f"Python{ABI_THREAD.upper()}"
        for bin_path in self._default_bin_path_includes():
            fullname = Path(bin_path, name).resolve()
            if fullname.is_file():
                python_shared_libs.append(fullname)
                break
        if not python_shared_libs:
            # INSTSONAME returns a relative path like
            # "Python.framework/Versions/3.12/Python" if PYTHONFRAMEWORK is
            # set, or can return a .dylib that does not exist.
            # On the other hand, in conda-forge it returns a static library .a
            # instead of a .dylib.
            name = sysconfig.get_config_var("INSTSONAME")
            if name.endswith(".a"):
                name = name.replace(".a", ".dylib")  # fix for conda-forge
            for bin_path in self._default_bin_path_includes():
                fullname = Path(bin_path, name).resolve()
                if fullname.is_file():
                    python_shared_libs.append(fullname)
                    break
        return self._validate_bin_file(python_shared_libs)

    def _default_bin_path_excludes(self) -> list[str]:
        return ["/lib", "/usr/lib", "/System/Library/Frameworks"]

    def _default_bin_path_includes(self) -> list[str]:
        # use macpython distributed files if available
        bases_lib = resource_path("bases/lib")
        if bases_lib:
            return self._validate_bin_path([bases_lib])
        # use default
        return super()._default_bin_path_includes()

    def _get_top_dependencies(self, source: Path) -> None:
        # this recovers the cached MachOReference pointers to the files
        # found by the get_dependent_files calls made previously (if any).
        # If one is found, pass into _copy_file.
        # We need to do this so the file knows what file referenced it,
        # and can therefore calculate the appropriate rpath.
        # (We only cache one reference.)
        # =cached_reference = self.darwin_tracker.getCachedReferenceTo(source)
        # =darwin_file = DarwinFile(source, cached_reference)
        dependent_files = self.get_dependent_files(source)  # darwin_file
        dependent_files.update(set(map(Path, self.default_bin_includes)))
        for dep_source in dependent_files:
            dep_target = self.target_dir / "lib" / dep_source.name
            reference = self.darwin_tracker.getCachedReferenceTo(dep_source)
            self._copy_file_recursion(
                dep_source,
                dep_target,
                copy_dependent_files=True,
                include_mode=True,
                reference=reference,
            )

    def _post_freeze_hook(self) -> None:
        self.darwin_tracker.finalizeReferences()
        # Make all references to libraries relative
        self.darwin_tracker.set_relative_reference_paths(
            self.target_dir, self.target_dir
        )
        super()._post_freeze_hook()

    def _post_copy_hook(
        self,
        source: Path,
        target: Path,
        copy_dependent_files: bool,
        reference: MachOReference | None = None,
    ) -> None:
        # The file was not previously copied, so need to create a
        # DarwinFile file object to represent the file being copied.
        if reference is not None:
            referencing_file = reference.source_file
        else:
            referencing_file = None
        darwin_file = DarwinFile(source, referencing_file)
        darwin_file.setBuildPath(target)
        if reference is not None:
            reference.setTargetFile(darwin_file)

        self.darwin_tracker.recordCopiedFile(target, darwin_file)
        if (
            copy_dependent_files
            and source not in self.finder.excluded_dependent_files
        ):
            # copy dependent files on "lib" directory and set relative
            # reference
            target_lib = self.target_dir / "lib"
            for dependent in self.get_dependent_files(source, darwin_file):
                target = target_lib / dependent.name
                reference = darwin_file.getMachOReferenceForPath(dependent)
                self._copy_file_recursion(
                    dependent,
                    target,
                    copy_dependent_files=True,
                    reference=reference,
                )

    def _copy_file_recursion(
        self,
        source: Path,
        target: Path,
        copy_dependent_files: bool,
        include_mode: bool = False,
        reference: MachOReference | None = None,
    ) -> None:
        """Essentially the same as Freezer._copy_file, except that it also
        takes a reference parameter. Used when recursing to dependencies
        of a file on Darwin.
        """
        if not self._should_copy_file(source):
            return

        # handle pre-copy tasks, normally on the target path
        source, target = self._pre_copy_hook(source, target)

        if target in self.files_copied:
            if reference is not None:
                # If file was already copied, and we are following a reference
                # from a DarwinFile, then we need to tell the reference where
                # the file was copied to (the reference can later be updated).
                reference.setTargetFile(
                    self.darwin_tracker.getDarwinFile(source, target)
                )
            return
        if source == target:
            return
        self._create_directory(target.parent)
        if self.silent < 1:
            print(f"copying {source} -> {target}")
        shutil.copyfile(source, target)
        shutil.copystat(source, target)
        if include_mode:
            shutil.copymode(source, target)
        self.files_copied.add(target)

        # handle post-copy tasks, including copying dependencies
        self._post_copy_hook(
            source,
            target,
            copy_dependent_files=copy_dependent_files,
            reference=reference,
        )

    def get_dependent_files(
        self, filename: Path, darwinFile: DarwinFile | None = None
    ) -> set[Path]:
        with suppress(KeyError):
            return self.dependent_files[filename]

        # if darwinFile is None (which means that get_dependent_files is
        # being called outside of _copy_file -- e.g., one of the
        # preliminary calls in _freeze_executable), create a temporary
        # DarwinFile object for the filename, just so we can read its
        # dependencies
        if darwinFile is None:
            darwinFile = DarwinFile(filename)
        dependent_files = darwinFile.getDependentFilePaths()

        # cache the MachOReferences to the dependencies, so they can be
        # called up later in _copy_file if copying a dependency without
        # an explicit reference provided (to assist in resolving @rpaths)
        for reference in darwinFile.getMachOReferenceList():
            if reference.isResolved():
                self.darwin_tracker.cacheReferenceTo(
                    reference.resolved_path, reference
                )
        self.dependent_files[filename] = dependent_files
        return dependent_files

    _get_dependent_files = None


class LinuxFreezer(Freezer, ELFParser):
    """Freezer base class for Linux and Posix OSes."""

    def __init__(self, *args, **kwargs) -> None:
        Freezer.__init__(self, *args, **kwargs)
        ELFParser.__init__(
            self,
            self.path,
            self.default_bin_path_includes + self.bin_path_includes,
            self.silent,
        )

    def _default_bin_excludes(self) -> list[str]:
        # https://github.com/AppImageCommunity/pkg2appimage/blob/master/excludelist
        return [
            "ldd",  # old alpine uses ldd instead of ld.so
            "ld.so",
            "ld64.so",
            "ld-linux.so",
            "ld-linux-*.so",  # ld-linux-x86-64.so.2, ld-linux-aarch64.so.2
            "libc.so",
            "libdl.so",
            "libm.so",
            "libpthread.so",
            "libutil.so",
            # musllinux
            "ld-musl-*.so",
        ]

    def _default_bin_path_excludes(self) -> list[str]:
        return [
            "/lib",
            "/lib32",
            "/lib64",
            "/usr/lib",
            "/usr/lib32",
            "/usr/lib64",
        ]

    def _get_top_dependencies(self, source: Path) -> None:  # noqa: ARG002
        lib_files = self.finder.lib_files
        for path in map(Path, self.default_bin_includes):
            lib_files.setdefault(path, f"lib/{path.name}")

    def _post_copy_hook(
        self,
        source: Path,
        target: Path,
        copy_dependent_files: bool,
    ) -> None:
        if not (
            copy_dependent_files
            and source not in self.finder.excluded_dependent_files
        ):
            return
        source_dir = source.parent
        target_dir = target.parent
        lib_files = self.finder.lib_files
        fix_rpath = set()
        fix_needed = {}
        for dependent in self.get_dependent_files(source):
            if not self._should_copy_file(dependent):
                continue
            dependent_source = dependent.resolve()
            dependent_name = dependent_source.name
            lib_file = lib_files.get(dependent_source)
            if lib_file:
                dependent_target = self.target_dir / lib_file
                try:
                    relative = dependent_target.relative_to(target_dir)
                except ValueError:
                    relative = Path(
                        os.path.relpath(dependent_target, target_dir)
                    )
            else:
                # put the dependency (relatively) in the target_dir subtree
                # this is possible with most packages installed by pip
                try:
                    relative = dependent_source.relative_to(source_dir)
                    # dependency located with source or in a subdirectory
                    dependent_target = target_dir / relative
                except ValueError:
                    # put the dependency in target_dir along with the binary
                    # file being copied, unless the dependency has already been
                    # copied to another location and is relative to the source
                    dependent_target = target_dir / dependent_name
                    relative = Path(dependent_name)
                    for file in self.files_copied:
                        if file.name == dependent_name:
                            try:
                                relative = file.relative_to(target_dir)
                                dependent_target = file
                            except ValueError:
                                relative = Path(
                                    os.path.relpath(file, target_dir)
                                )
                            break
            fix_rpath.add(f"$ORIGIN/{relative.parent.as_posix()}")
            self._copy_file(
                dependent_source, dependent_target, copy_dependent_files
            )
            if dependent.name != dependent_name:
                fix_needed.setdefault(dependent.name, dependent_name)
        if fix_rpath:
            self.set_rpath(target, ":".join(fix_rpath))
        for needed_old, needed_new in fix_needed.items():
            self.replace_needed(target, needed_old, needed_new)
