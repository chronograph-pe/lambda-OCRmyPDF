# © 2018 James R. Barlow: github.com/jbarlow83
#
# This file is part of OCRmyPDF.
#
# OCRmyPDF is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# OCRmyPDF is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with OCRmyPDF.  If not, see <http://www.gnu.org/licenses/>.

from functools import lru_cache
from subprocess import run
from tempfile import NamedTemporaryFile

from PIL import Image

from ..exceptions import MissingDependencyError
from . import get_version


@lru_cache(maxsize=1)
def version():
    return get_version('pngquant', regex=r'(\d+(\.\d+)*).*')


def available():
    try:
        version()
    except MissingDependencyError:
        return False
    return True


def quantize(input_file, output_file, quality_min, quality_max):
    if input_file.endswith('.jpg'):
        im = Image.open(input_file)
        with NamedTemporaryFile(suffix='.png') as tmp:
            im.save(tmp)
            args = [
                'pngquant',
                '--force',
                '--skip-if-larger',
                '--output',
                output_file,
                '--quality',
                f'{quality_min}-{quality_max}',
                '--',
                tmp.name,
            ]
            run(args)
    else:
        args = [
            'pngquant',
            '--force',
            '--skip-if-larger',
            '--output',
            output_file,
            '--quality',
            f'{quality_min}-{quality_max}',
            '--',
            input_file,
        ]
        run(args)
