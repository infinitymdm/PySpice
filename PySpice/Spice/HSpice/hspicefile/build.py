import sys
from setuptools import Extension
from setuptools.command.build_ext import build_ext


class LazyBuildExt(build_ext):
    def finalize_options(self):
        super().finalize_options()
        import numpy as np

        for ext in self.extensions:
            ext.include_dirs.append(np.get_include())


macros = []
if sys.platform.startswith("linux"):
    macros.append(("LINUX", None))

ext_modules = [
    Extension(
        "PySpice.Spice.HSpice._hspice_read",
        ["PySpice/Spice/HSpice/hspicefile/hspice_read.c"],
        include_dirs=["PySpice/Spice/HSpice/hspicefile"],
        define_macros=macros,
    )
]

cmdclass = {"build_ext": LazyBuildExt}
