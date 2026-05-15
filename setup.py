import io
from os import path

from setuptools import find_packages, setup

pwd = path.abspath(path.dirname(__file__))
with io.open(path.join(pwd, "README.md"), encoding="utf-8") as readme:
    desc = readme.read()

setup(
    name="winrm",
    version=__import__("winrm").__version__,
    description="WinRM client for Linux",
    long_description=desc,
    long_description_content_type="text/markdown",
    author="sdushantha",
    license="MIT",
    url="https://github.com/sdushantha/winrm",
    packages=find_packages(),
    install_requires=[
        "pypsrp[kerberos]==0.8.1",
        "prompt_toolkit==3.0.52",
        "tqdm==4.67.3",
    ],
    python_requires=">=3.13",
    entry_points={
        "console_scripts": [
            "winrm = winrm.winrm:main",
        ]
    },
)
