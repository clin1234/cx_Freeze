"""A collection of functions which are triggered automatically by finder when
scipy package is included.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cx_Freeze._compat import IS_LINUX, IS_MINGW, IS_WINDOWS

if TYPE_CHECKING:
    from cx_Freeze.finder import ModuleFinder
    from cx_Freeze.module import Module


def load_scipy(finder: ModuleFinder, module: Module) -> None:
    """The scipy package.

    Supported pypi and conda-forge versions (lasted tested version is 1.15.2).
    """
    # Exclude unnecessary modules
    distribution = module.distribution
    if distribution:
        files = distribution.original.files or []
        for file in files:
            if file.parent.match("**/tests"):
                mod = file.parent.as_posix().replace("/", ".")
                finder.exclude_module(mod)
    finder.exclude_module("scipy.conftest")

    finder.include_package("scipy.integrate")
    finder.include_package("scipy._lib")
    finder.include_package("scipy.misc")
    finder.include_package("scipy.optimize")


def load_scipy__distributor_init(finder: ModuleFinder, module: Module) -> None:
    """Fix the location of dependent files in Windows and macOS."""
    if IS_LINUX or IS_MINGW:
        return  # it is detected correctly.

    # patch the code when necessary
    if module.in_file_system == 0:
        module.code = compile(
            module.file.read_bytes().replace(
                b"__file__", b"__file__.replace('library.zip', '.')"
            ),
            module.file.as_posix(),
            "exec",
            dont_inherit=True,
            optimize=finder.optimize,
        )


def load_scipy_interpolate(
    finder: ModuleFinder,
    module: Module,  # noqa: ARG001
) -> None:
    """The scipy.interpolate must be loaded as a package."""
    finder.exclude_module("scipy.interpolate.tests")
    finder.include_package("scipy.interpolate")


def load_scipy_linalg(finder: ModuleFinder, module: Module) -> None:
    """The scipy.linalg module loads items within itself in a way that causes
    problems without the entire package being present.
    """
    module.global_names.add("norm")
    finder.include_package("scipy.linalg")


def load_scipy_linalg_interface_gen(_, module: Module) -> None:
    """The scipy.linalg.interface_gen module optionally imports the pre module;
    ignore the error if this module cannot be found.
    """
    module.ignore_names.add("pre")


def load_scipy_ndimage(
    finder: ModuleFinder,
    module: Module,  # noqa: ARG001
) -> None:
    """The scipy.ndimage must be loaded as a package."""
    finder.exclude_module("scipy.ndimage.tests")
    finder.include_package("scipy.ndimage")


def load_scipy_sparse(
    finder: ModuleFinder,
    module: Module,  # noqa: ARG001
) -> None:
    """The scipy.sparse must be loaded as a package."""
    finder.exclude_module("scipy.sparse.tests")
    finder.include_package("scipy.sparse")


def load_scipy_sparse_csgraph(
    finder: ModuleFinder,
    module: Module,  # noqa: ARG001
) -> None:
    """The scipy.sparse.csgraph must be loaded as a package."""
    finder.exclude_module("scipy.sparse.csgraph.tests")
    finder.include_package("scipy.sparse.csgraph")


def load_scipy_sparse_linalg__dsolve_linsolve(
    finder: ModuleFinder,  # noqa: ARG001
    module: Module,
) -> None:
    """The scipy.sparse.linalg._dsolve.linsolve optionally loads
    scikits.umfpack.
    """
    module.ignore_names.add("scikits.umfpack")


def load_scipy_spatial(
    finder: ModuleFinder,
    module: Module,  # noqa: ARG001
) -> None:
    """The scipy.spatial must be loaded as a package."""
    finder.include_package("scipy.spatial")
    finder.exclude_module("scipy.spatial.tests")
    if IS_WINDOWS or IS_MINGW:
        finder.exclude_module("scipy.spatial.cKDTree")


def load_scipy_spatial_transform(
    finder: ModuleFinder,
    module: Module,  # noqa: ARG001
) -> None:
    """The scipy.spatial.transform must be loaded as a package."""
    finder.include_package("scipy.spatial.transform")
    finder.exclude_module("scipy.spatial.transform.tests")


def load_scipy_special(
    finder: ModuleFinder,
    module: Module,  # noqa: ARG001
) -> None:
    """The scipy.special must be loaded as a package."""
    finder.exclude_module("scipy.special.tests")
    finder.include_package("scipy.special")
    finder.include_package("scipy.special._precompute")


def load_scipy_special__cephes(
    finder: ModuleFinder,  # noqa: ARG001
    module: Module,
) -> None:
    """The scipy.special._cephes is an extension module and the scipy module
    imports * from it in places; advertise the global names that are used
    in order to avoid spurious errors about missing modules.
    """
    module.global_names.add("gammaln")


def load_scipy_stats(
    finder: ModuleFinder,
    module: Module,  # noqa: ARG001
) -> None:
    """The scipy.stats must be loaded as a package."""
    finder.exclude_module("scipy.stats.tests")
    finder.include_package("scipy.stats")
