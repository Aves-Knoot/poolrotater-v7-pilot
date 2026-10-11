"""Explicit module allowlist: offline wheels cannot include live transports."""
from setuptools import setup
from setuptools.command.build_py import build_py

OFFLINE_BOT = {'__init__', 'pool', 'mania_pool', 'standard_pool', 'ratings', 'jump_catalog', 'composition'}


class OfflineBuild(build_py):
    def find_package_modules(self, package, package_dir):
        modules = super().find_package_modules(package, package_dir)
        return [entry for entry in modules if package != 'bot' or entry[1] in OFFLINE_BOT]


setup(cmdclass={'build_py': OfflineBuild})
