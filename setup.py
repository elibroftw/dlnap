# @author: elibroftw

import io
import os
from setuptools import setup

from dlnap import __version__

NAME = 'dlnap'
FOLDER = 'dlnap'
DESCRIPTION = 'A UPnP/DLNA client, support local file and online resource cast to screen.'
URL = 'https://github.com/elibroftw/dlnap'
EMAIL = 'elijahllopezz@gmail.com'
AUTHOR = 'elibroftw'
REQUIRES_PYTHON = '>=3.6.0'

here = os.path.abspath(os.path.dirname(__file__))

try:
    with io.open(os.path.join(here, 'README.md'), encoding='utf-8') as f:
        long_description = '\n' + f.read()
except FileNotFoundError:
    long_description = DESCRIPTION


setup(
    name=NAME,
    version=__version__,
    description=DESCRIPTION,
    long_description=long_description,
    long_description_content_type='text/markdown',
    author=AUTHOR,
    author_email=EMAIL,
    url=URL,
    packages=['dlnap'],
    include_package_data=True,
    license='MIT',
    entry_points={
        'console_scripts': ['dlnap = dlnap.client:run']
    },
    classifiers=[
        'License :: OSI Approved :: MIT License',
        'Programming Language :: Python :: 3.6',
        'Programming Language :: Python :: 3.7',
        'Programming Language :: Python :: 3.8',
        'Programming Language :: Python :: Implementation :: CPython',
    ],
    keywords=[
        'dlnap'
    ],
)
